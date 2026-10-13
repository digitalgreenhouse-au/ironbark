/* Ironbark for the web — the playback engine.

   Two <audio> elements take turns: while one plays, the next song loads in the other, so the
   change of song is as short as the browser allows (a browser can't promise gapless playback).
   Both feed one Web Audio graph:

     element ─▶ source ─▶ tap ─┬─▶ splitter ─▶ analyser L / R   (the meters: the file's level)
                               └─▶ volume ─▶ speakers

   The meters read the signal before the volume, so they show the music's own level. The
   elements' own volume stays at 1: the volume is one gain stage, and 100% leaves the samples
   untouched.

   Events (EventTarget): "state" (playing / paused), "time", "ended", "error", "loaded". */
(function (root) {
  "use strict";

  class Player extends EventTarget {
    constructor() {
      super();
      this.els = [new Audio(), new Audio()];
      for (const el of this.els) {
        el.preload = "auto";
        el.crossOrigin = null;
        el.addEventListener("playing", () => this._emit("state"));
        el.addEventListener("pause", () => this._emit("state"));
        el.addEventListener("timeupdate", () => { if (el === this.el) this._emit("time"); });
        el.addEventListener("durationchange", () => { if (el === this.el) this._emit("time"); });
        el.addEventListener("loadedmetadata", () => { if (el === this.el) this._emit("loaded"); });
        el.addEventListener("ended", () => { if (el === this.el) this._emit("ended"); });
        el.addEventListener("error", () => {
          if (el === this.el) this._emit("error", { code: el.error ? el.error.code : 0, message: el.error ? el.error.message : "" });
          else if (el === this.spareEl) this.spareUrl = null;     // the next song won't play: load it again later
        });
      }
      this.cur = 0;
      this.url = null;
      this.spareUrl = null;
      this.ctx = null;
      this._volume = 1;
      this._muted = false;
    }

    get el() { return this.els[this.cur]; }
    get spareEl() { return this.els[1 - this.cur]; }

    _emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }

    // The audio graph needs a click or key first (the browser's autoplay rule).
    ensureContext() {
      if (this.ctx) {
        if (this.ctx.state === "suspended") this.ctx.resume().catch(() => {});
        return this.ctx;
      }
      const Ctx = root.AudioContext || root.webkitAudioContext;
      if (!Ctx) return null;
      try {
        this.ctx = new Ctx({ latencyHint: "playback" });
      } catch (e) {
        this.ctx = new Ctx();
      }
      const ctx = this.ctx;
      this.tap = ctx.createGain();
      this.gain = ctx.createGain();
      this.splitter = ctx.createChannelSplitter(2);
      this.analysers = [ctx.createAnalyser(), ctx.createAnalyser()];
      for (const a of this.analysers) { a.fftSize = 8192; a.smoothingTimeConstant = 0.6; }
      for (const el of this.els) {
        el.volume = 1;
        try {
          ctx.createMediaElementSource(el).connect(this.tap);
        } catch (e) {
          this.graphError = String(e && e.message || e);
        }
      }
      this.tap.connect(this.splitter);
      this.splitter.connect(this.analysers[0], 0);
      this.splitter.connect(this.analysers[1], 1);
      this.tap.connect(this.gain);
      this.gain.connect(ctx.destination);
      this._applyVolume();
      return ctx;
    }

    get outputRate() { return this.ctx ? this.ctx.sampleRate : null; }

    // -- songs ------------------------------------------------------------------------------
    // Plays url now. If it's the one already loaded in the spare element, swaps to it.
    async load(url, { autoplay = true, at = 0 } = {}) {
      this.ensureContext();
      const old = this.el;
      if (url && url === this.spareUrl) {
        this.cur = 1 - this.cur;
        this.spareUrl = this.url;
      } else {
        this.el.src = url;
      }
      this.url = url;
      if (old !== this.el) { old.pause(); try { old.currentTime = 0; } catch (e) { /* not loaded */ } }
      if (at) {
        const seekWhenReady = () => { try { this.el.currentTime = at; } catch (e) { /* ignore */ } };
        if (this.el.readyState >= 1) seekWhenReady(); else this.el.addEventListener("loadedmetadata", seekWhenReady, { once: true });
      } else if (this.el.currentTime) {
        try { this.el.currentTime = 0; } catch (e) { /* ignore */ }
      }
      this._emit("loaded");
      if (autoplay) return this.play();
      this._emit("state");
      return true;
    }

    // Loads the next song into the spare element, ready to start the moment this one ends.
    preload(url) {
      if (!url || url === this.spareUrl || url === this.url) return;
      this.spareUrl = url;
      this.spareEl.src = url;
      this.spareEl.load();
    }

    forgetSpare() {
      this.spareUrl = null;
      this.spareEl.removeAttribute("src");
      this.spareEl.load();
    }

    stop() {
      for (const el of this.els) { el.pause(); el.removeAttribute("src"); el.load(); }
      this.url = this.spareUrl = null;
      this._emit("state");
      this._emit("time");
    }

    async play() {
      this.ensureContext();
      if (!this.el.src) return false;
      try {
        await this.el.play();
        return true;
      } catch (e) {
        // AbortError: another song was loaded first. NotSupportedError: the element's own
        // "error" event reports it (with the reason), so it isn't reported twice.
        if (e && e.name !== "AbortError" && e.name !== "NotSupportedError") this._emit("error", { code: -1, message: e.message || String(e) });
        return false;
      }
    }

    pause() { this.el.pause(); }

    toggle() { if (this.playing) this.pause(); else return this.play(); }

    get playing() { return !!this.el.src && !this.el.paused && !this.el.ended; }
    get loaded() { return !!this.url; }
    get position() { return this.el.currentTime || 0; }
    get duration() { const d = this.el.duration; return Number.isFinite(d) ? d : 0; }

    seek(t) {
      if (!this.loaded) return;
      const d = this.duration;
      t = Math.max(0, d ? Math.min(t, d - 0.05) : t);
      try { this.el.currentTime = t; } catch (e) { /* not seekable yet */ }
      this._emit("time");
    }

    // -- volume ------------------------------------------------------------------------------
    // 0–1 on the slider; the gain follows a square law so the slider feels even to the ear.
    // 1 = the samples untouched.
    get volume() { return this._volume; }
    set volume(v) { this._volume = Math.max(0, Math.min(1, v)); this._applyVolume(); }
    get muted() { return this._muted; }
    set muted(m) { this._muted = !!m; this._applyVolume(); }
    get gainValue() { return this._muted ? 0 : this._volume * this._volume; }

    _applyVolume() {
      const g = this.gainValue;
      if (this.gain) {
        this.gain.gain.setTargetAtTime(g, this.ctx.currentTime, 0.015);
      } else {
        for (const el of this.els) el.volume = Math.min(1, g);   // no audio graph in this browser
      }
      this._emit("volume");
    }
  }

  root.IronbarkPlayer = Player;
})(typeof globalThis !== "undefined" ? globalThis : this);

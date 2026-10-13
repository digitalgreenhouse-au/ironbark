/* Ironbark for the web — the meters: level (peak and RMS per channel, in dBFS: decibels
   relative to full scale) and a spectrum, drawn on a canvas from the player's analysers.

   Peaks hold for 1.5 s. The 0 dBFS lamp lights when a sample reaches full scale. When the
   browser resamples (the file's rate isn't the output's), the meters see the resampled signal,
   so a peak can read a little above the file's own. */
(function (root) {
  "use strict";

  const FLOOR = -60;           // the bottom of the level scale, dBFS
  const HOLD_MS = 1500;

  const dB = (x) => (x > 0 ? 20 * Math.log10(x) : -Infinity);

  // Level of one block of samples: { peak, rms } as amplitudes (1 = full scale).
  function levels(samples) {
    let peak = 0, sum = 0;
    for (let i = 0; i < samples.length; i++) {
      const v = samples[i];
      const a = v < 0 ? -v : v;
      if (a > peak) peak = a;
      sum += v * v;
    }
    return { peak, rms: samples.length ? Math.sqrt(sum / samples.length) : 0 };
  }

  class Meters {
    constructor(canvas, player) {
      this.canvas = canvas;
      this.player = player;
      this.mode = "levels";
      this.mono = false;
      this.hold = [{ v: 0, t: 0 }, { v: 0, t: 0 }];
      this.over = [0, 0];
      this.bufs = null;
      this.freq = null;
      this.running = false;
      this.colours = {};
      this.readColours();
    }

    readColours() {
      const cs = getComputedStyle(this.canvas);
      const v = (n) => cs.getPropertyValue(n).trim();
      this.colours = {
        bg: v("--surface"), track: v("--border"), text: v("--muted"), level: v("--accent"),
        rms: v("--meter-level"), warn: v("--warn"), danger: v("--danger"), hold: v("--text"),
        spec: v("--accent"), grid: v("--border"),
      };
    }

    start() {
      if (this.running) return;
      this.running = true;
      const tick = () => {
        if (!this.running) return;
        this.draw();
        this.raf = requestAnimationFrame(tick);
      };
      this.raf = requestAnimationFrame(tick);
    }

    stop() {
      this.running = false;
      if (this.raf) cancelAnimationFrame(this.raf);
    }

    resetPeaks() {
      this.hold = [{ v: 0, t: 0 }, { v: 0, t: 0 }];
      this.over = [0, 0];
    }

    _size() {
      const c = this.canvas, dpr = root.devicePixelRatio || 1;
      const w = Math.max(1, Math.round(c.clientWidth * dpr)), h = Math.max(1, Math.round(c.clientHeight * dpr));
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
      return { w, h, dpr };
    }

    draw() {
      const { w, h, dpr } = this._size();
      const g = this.canvas.getContext("2d");
      g.clearRect(0, 0, w, h);
      const p = this.player;
      if (!p.analysers) { this._label(g, w, h, dpr, "Play a song to see its level"); return; }
      if (this.mode === "spectrum") this._spectrum(g, w, h, dpr);
      else this._levels(g, w, h, dpr);
    }

    _label(g, w, h, dpr, text) {
      g.fillStyle = this.colours.text;
      g.font = `${12 * dpr}px system-ui, sans-serif`;
      g.textAlign = "center";
      g.textBaseline = "middle";
      g.fillText(text, w / 2, h / 2);
    }

    _levels(g, w, h, dpr) {
      const an = this.player.analysers;
      if (!this.bufs || this.bufs[0].length !== an[0].fftSize) this.bufs = an.map((a) => new Float32Array(a.fftSize));
      const now = performance.now();
      const C = this.colours;
      const left = 18 * dpr, right = w - 8 * dpr;
      const barH = Math.max(6 * dpr, (h - 26 * dpr) / 2 - 3 * dpr);
      const span = right - left;
      const xOf = (db) => left + span * Math.max(0, Math.min(1, (db - FLOOR) / -FLOOR));
      g.font = `${10 * dpr}px system-ui, sans-serif`;
      g.textBaseline = "middle";
      for (let ch = 0; ch < 2; ch++) {
        const src = this.mono ? 0 : ch;
        an[src].getFloatTimeDomainData(this.bufs[src]);
        const { peak, rms } = levels(this.bufs[src]);
        if (peak >= this.hold[ch].v || now - this.hold[ch].t > HOLD_MS) this.hold[ch] = { v: peak, t: now };
        if (peak >= 0.99999) this.over[ch] = now;
        const y = 4 * dpr + ch * (barH + 6 * dpr);
        g.fillStyle = C.track;
        g.fillRect(left, y, span, barH);
        // RMS (body) and peak (thin top bar)
        g.fillStyle = C.rms;
        g.fillRect(left, y, xOf(dB(rms)) - left, barH);
        g.fillStyle = C.level;
        g.fillRect(left, y, xOf(dB(peak)) - left, Math.max(2 * dpr, barH * 0.35));
        const hx = xOf(dB(this.hold[ch].v));
        if (this.hold[ch].v > 0) {
          g.fillStyle = this.hold[ch].v >= 0.99999 ? C.danger : dB(this.hold[ch].v) > -1 ? C.warn : C.hold;
          g.fillRect(Math.min(hx, right - 2 * dpr), y, 2 * dpr, barH);
        }
        g.fillStyle = now - this.over[ch] < HOLD_MS ? C.danger : C.text;
        g.textAlign = "left";
        g.fillText(ch ? "R" : "L", 4 * dpr, y + barH / 2);
      }
      // scale
      const sy = h - 10 * dpr;
      g.textAlign = "center";
      g.fillStyle = C.text;
      for (const db of [-60, -40, -30, -20, -12, -6, -3, 0]) {
        const x = xOf(db);
        if (db === -60) { g.textAlign = "left"; g.fillText(String(db), x, sy); g.textAlign = "center"; continue; }
        if (db === 0) { g.textAlign = "right"; g.fillText("0 dBFS", right, sy); continue; }
        if (span < 260 * dpr && (db === -30 || db === -3)) continue;
        g.fillText(String(db), x, sy);
      }
    }

    _spectrum(g, w, h, dpr) {
      const an = this.player.analysers;
      const n = an[0].frequencyBinCount;
      if (!this.freq || this.freq[0].length !== n) this.freq = [new Float32Array(n), new Float32Array(n)];
      an[0].getFloatFrequencyData(this.freq[0]);
      an[1].getFloatFrequencyData(this.freq[1]);
      const C = this.colours;
      const rate = this.player.outputRate || 48000, nyq = rate / 2;
      const left = 4 * dpr, right = w - 4 * dpr, top = 4 * dpr, bottom = h - 16 * dpr;
      const fmin = 20, fmax = Math.min(nyq, 24000);
      const lx = (f) => left + (right - left) * (Math.log(f / fmin) / Math.log(fmax / fmin));
      const yOf = (db) => bottom - (bottom - top) * Math.max(0, Math.min(1, (db + 110) / 110));
      g.strokeStyle = C.grid;
      g.lineWidth = 1;
      g.fillStyle = C.text;
      g.font = `${10 * dpr}px system-ui, sans-serif`;
      g.textBaseline = "alphabetic";
      g.textAlign = "center";
      for (const f of [50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]) {
        if (f > fmax) continue;
        const x = lx(f);
        g.beginPath(); g.moveTo(x, top); g.lineTo(x, bottom); g.stroke();
        g.fillText(f >= 1000 ? f / 1000 + "k" : String(f), x, h - 3 * dpr);
      }
      // bars: about one per 3 px, each the loudest bin it covers
      const bars = Math.max(16, Math.floor((right - left) / (3 * dpr)));
      g.fillStyle = C.spec;
      const binHz = nyq / n;
      for (let i = 0; i < bars; i++) {
        const f0 = fmin * Math.pow(fmax / fmin, i / bars), f1 = fmin * Math.pow(fmax / fmin, (i + 1) / bars);
        let b0 = Math.floor(f0 / binHz), b1 = Math.max(b0 + 1, Math.ceil(f1 / binHz));
        let m = -Infinity;
        for (let b = b0; b < b1 && b < n; b++) {
          const v = this.mono ? this.freq[0][b] : Math.max(this.freq[0][b], this.freq[1][b]);
          if (v > m) m = v;
        }
        const x0 = lx(f0), x1 = lx(f1);
        const y = yOf(m);
        if (y < bottom) g.fillRect(x0, y, Math.max(1, x1 - x0 - dpr), bottom - y);
      }
    }
  }

  root.IronbarkMeters = { Meters, levels, dB };
  if (typeof module !== "undefined" && module.exports) module.exports = { levels, dB };
})(typeof globalThis !== "undefined" ? globalThis : this);

/* Ironbark for the web — proving a FLAC file decodes exactly.

   A FLAC encoder stores the MD5 of the original audio (the samples, interleaved, little-endian,
   in whole bytes) in the file's STREAMINFO block. verifyFlac() has the browser decode the whole
   file at its own sample rate, turns the decoded samples back into whole numbers and works out
   the same MD5. A match proves every sample came out as it went in.

   The MD5 is written here (RFC 1321): browsers' Web Crypto has no MD5, and Ironbark loads no
   outside code. Works in a browser and in Node 18+. */
(function (root) {
  "use strict";

  const MAX_DECODE = 1.2 * 1073741824;   // bytes of decoded audio a check may hold at once

  // -- MD5, streaming ------------------------------------------------------------------------
  const S = [7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21];
  const K = new Int32Array(64);
  for (let i = 0; i < 64; i++) K[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296) | 0;

  class MD5 {
    constructor() {
      this.h = new Int32Array([0x67452301, 0xefcdab89 | 0, 0x98badcfe | 0, 0x10325476]);
      this.buf = new Uint8Array(64);
      this.bufLen = 0;
      this.total = 0;
      this.w = new Int32Array(16);
    }

    _block(b, o) {
      const w = this.w;
      for (let i = 0; i < 16; i++) {
        const j = o + i * 4;
        w[i] = b[j] | (b[j + 1] << 8) | (b[j + 2] << 16) | (b[j + 3] << 24);
      }
      let a = this.h[0], bb = this.h[1], c = this.h[2], d = this.h[3];
      for (let i = 0; i < 64; i++) {
        let f, g;
        if (i < 16) { f = (bb & c) | (~bb & d); g = i; }
        else if (i < 32) { f = (d & bb) | (~d & c); g = (5 * i + 1) & 15; }
        else if (i < 48) { f = bb ^ c ^ d; g = (3 * i + 5) & 15; }
        else { f = c ^ (bb | ~d); g = (7 * i) & 15; }
        const t = d;
        d = c;
        c = bb;
        const x = (a + f + K[i] + w[g]) | 0;
        bb = (bb + ((x << S[i]) | (x >>> (32 - S[i])))) | 0;
        a = t;
      }
      this.h[0] = (this.h[0] + a) | 0;
      this.h[1] = (this.h[1] + bb) | 0;
      this.h[2] = (this.h[2] + c) | 0;
      this.h[3] = (this.h[3] + d) | 0;
    }

    update(bytes) {
      let o = 0;
      const n = bytes.length;
      this.total += n;
      if (this.bufLen) {
        const take = Math.min(64 - this.bufLen, n);
        this.buf.set(bytes.subarray(0, take), this.bufLen);
        this.bufLen += take; o = take;
        if (this.bufLen < 64) return this;
        this._block(this.buf, 0);
        this.bufLen = 0;
      }
      for (; o + 64 <= n; o += 64) this._block(bytes, o);
      if (o < n) { this.buf.set(bytes.subarray(o), 0); this.bufLen = n - o; }
      return this;
    }

    hex() {
      const bits = this.total * 8;
      const pad = new Uint8Array(((this.bufLen < 56 ? 56 : 120) - this.bufLen) + 8);
      pad[0] = 0x80;
      const lo = bits >>> 0, hi = Math.floor(bits / 4294967296) >>> 0;
      const p = pad.length - 8;
      for (let i = 0; i < 4; i++) { pad[p + i] = (lo >>> (8 * i)) & 0xff; pad[p + 4 + i] = (hi >>> (8 * i)) & 0xff; }
      const keep = this.total;
      this.update(pad);
      this.total = keep;
      let s = "";
      for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) s += ((this.h[i] >>> (8 * j)) & 0xff).toString(16).padStart(2, "0");
      return s;
    }
  }

  function md5Hex(bytes) {
    return new MD5().update(bytes).hex();
  }

  // -- decoded audio -> the bytes FLAC's MD5 covers ------------------------------------------
  // channels: Float32Array per channel, as the browser decodes (whole numbers / 2^(bits-1)).
  // Returns the MD5 hex and how many samples were not whole numbers at that bit depth
  // (any at all means the decoder changed the audio: resampled, dithered or lossy).
  function pcmMd5(channels, bits, progress) {
    const md5 = new MD5();
    const nch = channels.length, frames = channels[0].length;
    const bytesPer = Math.ceil(bits / 8), scale = Math.pow(2, bits - 1);
    const max = scale - 1, min = -scale;
    const chunkFrames = 16384;
    const out = new Uint8Array(chunkFrames * nch * bytesPer);
    let offGrid = 0;
    for (let f0 = 0; f0 < frames; f0 += chunkFrames) {
      const f1 = Math.min(frames, f0 + chunkFrames);
      let o = 0;
      for (let f = f0; f < f1; f++) {
        for (let c = 0; c < nch; c++) {
          const x = channels[c][f] * scale;
          let v = Math.round(x);
          if (v !== x) offGrid++;
          if (v > max) v = max; else if (v < min) v = min;
          out[o++] = v & 0xff;
          if (bytesPer > 1) out[o++] = (v >> 8) & 0xff;
          if (bytesPer > 2) out[o++] = (v >> 16) & 0xff;
          if (bytesPer > 3) out[o++] = (v >> 24) & 0xff;
        }
      }
      md5.update(out.subarray(0, o));
      if (progress) progress(f1 / frames);
    }
    return { md5: md5.hex(), offGrid };
  }

  // Browser only: decode `file` at its own rate and compare with the stored MD5.
  // Resolves to { ok, verdict, detail, md5, stored }.
  async function verifyFlac(file, tags, progress) {
    if (!tags || !tags.md5) {
      return { ok: null, verdict: "No fingerprint to check", detail: "This file's encoder stored no MD5, so there is nothing to compare against." };
    }
    // the browser decodes the whole song into memory first (4 bytes a sample)
    const need = (tags.duration || 0) * (tags.sampleRate || 0) * (tags.channels || 2) * 4;
    if (need > MAX_DECODE) {
      return { ok: null, verdict: "Too long to check in a browser", detail: "Checking it would take about " + (need / 1073741824).toFixed(1) + " GB of memory. The desktop Ironbark checks files of any length." };
    }
    const Ctx = root.OfflineAudioContext || root.webkitOfflineAudioContext;
    if (!Ctx) return { ok: null, verdict: "Can't check in this browser", detail: "It has no offline audio decoder." };
    let ctx;
    try {
      ctx = new Ctx(tags.channels || 2, 1, tags.sampleRate);
    } catch (e) {
      return { ok: null, verdict: "Can't check in this browser", detail: "It can't decode at " + (tags.sampleRate / 1000) + " kHz." };
    }
    if (progress) progress(0, "Decoding…");
    let buffer;
    try {
      buffer = await ctx.decodeAudioData(await file.arrayBuffer());
    } catch (e) {
      return { ok: false, verdict: "The browser couldn't decode it", detail: String(e && e.message || e) };
    }
    if (buffer.sampleRate !== tags.sampleRate) {
      return { ok: null, verdict: "Can't check in this browser", detail: "It decoded the file at " + buffer.sampleRate + " Hz, not the file's own " + tags.sampleRate + " Hz." };
    }
    const channels = [];
    for (let c = 0; c < buffer.numberOfChannels; c++) channels.push(buffer.getChannelData(c));
    // let the page repaint before the long sum
    await new Promise((res) => setTimeout(res, 0));
    const got = pcmMd5(channels, tags.bitsPerSample, (p) => progress && progress(p, "Fingerprinting…"));
    const ok = got.md5 === tags.md5;
    const seconds = (buffer.length / buffer.sampleRate);
    return {
      ok,
      md5: got.md5,
      stored: tags.md5,
      verdict: ok ? "Every sample checks out" : "The audio doesn't match its fingerprint",
      detail: ok
        ? "The decoded audio (" + buffer.length.toLocaleString() + " samples per channel, " + Math.round(seconds) + " s) has the MD5 the encoder stored: " + got.md5 + "."
        : "Stored MD5 " + tags.md5 + ", decoded MD5 " + got.md5 + (got.offGrid ? " (" + got.offGrid.toLocaleString() + " samples weren't whole " + tags.bitsPerSample + "-bit values)" : "") + ". The file may be damaged.",
    };
  }

  const api = { MD5, md5Hex, pcmMd5, verifyFlac };
  root.IronbarkVerify = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

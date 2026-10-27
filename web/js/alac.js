/* Ironbark for the web — Apple Lossless (ALAC) .m4a files, for browsers that can't play them.

   Safari plays ALAC itself; Chrome, Edge and Firefox don't (AAC .m4a files they play). So for
   those, this decodes the file here, on the listener's computer, into a WAV file in memory, and
   the player plays that like any other song: seeking, the meters and the volume work as ever.
   Nothing leaves the page. A song is decoded in full before it starts — about a second for a CD
   track — and the next song is decoded while this one plays, so the change of song stays short.

   Three parts:
   1. The MP4's index (ISO/IEC 14496-12 boxes, as iTunes, Apple Music and FFmpeg write them):
      moov → trak (handler "soun") → mdia → mdhd (time scale), minf → stbl → stsd (the "alac"
      sample entry and its 24-byte setup block, the "magic cookie"), stts (packet durations), stsz
      (packet sizes), stsc (packets per chunk), stco / co64 (where each chunk starts). The same
      reading as the desktop player's ironbark/m4a.py.
   2. The ALAC decoder, written from Apple's ALAC format as Apple published it with its open-source
      ALAC encoder and decoder (Apache-2.0): each packet is a run of elements (a single channel or
      a channel pair, ended by an END tag); each channel is adaptive-Golomb coded residuals, an
      adaptive linear predictor, and for a pair a mid/side-style mix undone at the end. Up to two
      channels here (multichannel ALAC is rare); 16-, 20-, 24- and 32-bit.
   3. The WAV: plain PCM, little-endian, in whole bytes (20-bit in a 24-bit container,
      left-justified, as WAV stores it).

   Works as a page script (IronbarkAlac), as a Web Worker (new Worker("js/alac.js"): post it
   { file }, it posts { progress } and then { blob } or { error }), and in Node 18+ for the
   tests. */
(function (root) {
  "use strict";

  const MAX_WAV = 1.2 * 1073741824;      // bytes of decoded audio a song may take in memory
  const MAX_MOOV = 64 * 1048576;         // an index bigger than this is damaged
  const MAX_FRAME_LENGTH = 1 << 16;      // samples per packet: Apple's encoder uses 4096
  const MAX_PACKETS = 1 << 24;           // packets in a song (4096-sample packets: over 400 hours at 44.1 kHz)
  const WINDOW = 8 * 1048576;            // packets are read from the file this many bytes at a time

  class NotAlac extends Error {
    constructor(message, codec) { super(message); this.name = "NotAlac"; this.codec = codec || ""; }
  }
  const damaged = (what) => new NotAlac("it's damaged (" + what + ")");

  const be16 = (b, o) => (b[o] << 8) | b[o + 1];
  const be32 = (b, o) => ((b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]) >>> 0;
  const be64 = (b, o) => be32(b, o) * 4294967296 + be32(b, o + 4);
  const ascii = (b, o, n) => String.fromCharCode.apply(null, b.subarray(o, o + n));

  async function bytes(file, start, length) {
    return new Uint8Array(await file.slice(start, start + length).arrayBuffer());
  }

  // -- 1. the MP4's index ------------------------------------------------------------------
  function* boxes(b, start, end) {
    let o = start;
    while (o + 8 <= end) {
      let size = be32(b, o), head = 8;
      if (size === 1) { size = be64(b, o + 8); head = 16; }
      else if (size === 0) size = end - o;
      if (size < head || o + size > end) return;
      yield { type: ascii(b, o + 4, 4), start: o + head, end: o + size };
      o += size;
    }
  }
  const find = (b, box, type) => { for (const c of boxes(b, box.start, box.end)) if (c.type === type) return c; return null; };

  // A table's entry count must fit in its box: a damaged count would otherwise ask for
  // gigabytes (or read past the box).
  function checkCount(count, box, header, entry, what) {
    if (count * entry > box.end - box.start - header) throw damaged("the " + what + " table is cut short");
  }

  // The ALAC track's layout: { rate, channels, bits, config, sizes, offsets, frames }. Throws
  // NotAlac for anything else (AAC, usually), or a damaged index.
  async function readIndex(file) {
    const head = await bytes(file, 0, 8);
    if (head.length < 8 || ascii(head, 4, 4) !== "ftyp") throw new NotAlac("not an MP4 file");
    let o = 0, moov = null;
    while (o + 8 <= file.size) {
      const h = await bytes(file, o, 16);
      let size = be32(h, 0), headLen = 8;
      if (size === 1) { size = be64(h, 8); headLen = 16; }
      else if (size === 0) size = file.size - o;
      if (size < headLen) break;
      if (ascii(h, 4, 4) === "moov") {
        if (size > MAX_MOOV) throw damaged("its index is far too big");
        moov = await bytes(file, o, size);
        break;
      }
      o += size;
    }
    if (!moov) throw new NotAlac("no index (moov) in this MP4 file");
    for (const trak of boxes(moov, 8, moov.length)) {
      if (trak.type !== "trak") continue;
      const mdia = find(moov, trak, "mdia");
      const hdlr = mdia && find(moov, mdia, "hdlr");
      if (!hdlr || ascii(moov, hdlr.start + 8, 4) !== "soun") continue;
      return readTrack(moov, mdia, file.size);
    }
    throw new NotAlac("no sound track in this MP4 file");
  }

  function readTrack(b, mdia, length) {
    const a = { codec: "", rate: 0, channels: 0, bits: 0, timescale: 0, frames: 0, config: null, sizes: null, offsets: null };
    const mdhd = find(b, mdia, "mdhd");
    if (mdhd) a.timescale = be32(b, mdhd.start + (b[mdhd.start] === 1 ? 20 : 12));
    const minf = find(b, mdia, "minf"), stbl = minf && find(b, minf, "stbl");
    if (!stbl) throw new NotAlac("no sample table in this MP4 file");
    const stsd = find(b, stbl, "stsd");
    const entry = stsd && boxes(b, stsd.start + 8, stsd.end).next().value;   // after version/flags + entry count
    if (!entry) throw new NotAlac("no sample description in this MP4 file");
    a.codec = entry.type.trim();
    if (entry.type !== "alac") throw new NotAlac("this .m4a holds " + (entry.type === "mp4a" ? "AAC" : a.codec.toUpperCase()) + ", not Apple Lossless", a.codec);
    // Sample entry: 6 reserved + 2 data reference index, then the sound description (version 0:
    // 20 bytes; QuickTime version 1 adds 16, version 2 adds 36), then child boxes.
    const version = be16(b, entry.start + 8);
    const children = entry.start + 28 + (version === 1 ? 16 : version === 2 ? 36 : 0);
    const cookie = find(b, { start: children, end: entry.end }, "alac");
    if (!cookie || cookie.end - cookie.start < 4 + 24) throw new NotAlac("Apple Lossless setup block missing");
    const k = cookie.start + 4;   // after the box's version/flags
    // ALACSpecificConfig: frameLength u32, compatibleVersion u8, bitDepth u8, pb u8, mb u8, kb u8,
    // numChannels u8, maxRun u16, maxFrameBytes u32, avgBitRate u32, sampleRate u32
    a.config = { frameLength: be32(b, k), bitDepth: b[k + 5], pb: b[k + 6], mb: b[k + 7], kb: b[k + 8], channels: b[k + 9], maxRun: be16(b, k + 10) };
    a.bits = a.config.bitDepth; a.channels = a.config.channels; a.rate = be32(b, k + 20);
    if (!(a.config.frameLength > 0 && a.config.frameLength <= MAX_FRAME_LENGTH) || !a.channels || !a.rate) throw damaged("its setup block");
    if (![16, 20, 24, 32].includes(a.bits)) throw new NotAlac("unusual Apple Lossless bit depth (" + a.bits + ")");

    const stsz = find(b, stbl, "stsz"), stsc = find(b, stbl, "stsc"), stts = find(b, stbl, "stts");
    const stco = find(b, stbl, "stco") || find(b, stbl, "co64");
    if (!stsz || !stsc || !stco || !stts) throw new NotAlac("incomplete sample table in this MP4 file");
    const fixed = be32(b, stsz.start + 4), count = be32(b, stsz.start + 8);
    if (count > MAX_PACKETS) throw damaged("its packet count");   // (a fixed size of 1 byte would otherwise allow one per byte)
    if (fixed) { if (count * fixed > length) throw damaged("more audio than the file holds"); }
    else checkCount(count, stsz, 12, 4, "packet size");
    a.sizes = new Uint32Array(count);
    for (let i = 0; i < count; i++) a.sizes[i] = fixed || be32(b, stsz.start + 12 + 4 * i);
    const wide = stco.type === "co64", chunks = be32(b, stco.start + 4);
    checkCount(chunks, stco, 8, wide ? 8 : 4, "chunk offset");
    const starts = new Float64Array(chunks);
    for (let i = 0; i < chunks; i++) starts[i] = wide ? be64(b, stco.start + 8 + 8 * i) : be32(b, stco.start + 8 + 4 * i);
    const runs = be32(b, stsc.start + 4);
    checkCount(runs, stsc, 8, 12, "packets-per-chunk");
    a.offsets = new Float64Array(count);
    let packet = 0, prevFirst = 0;
    for (let r = 0; r < runs && packet < count; r++) {
      const first = be32(b, stsc.start + 8 + 12 * r), perChunk = be32(b, stsc.start + 12 + 12 * r);
      const last = r + 1 < runs ? be32(b, stsc.start + 8 + 12 * (r + 1)) - 1 : chunks;
      // runs must move forward through the chunks: one going back could make the walk endless
      if (first <= prevFirst || last > chunks) throw damaged("its chunk table");
      prevFirst = first;
      for (let chunk = first; chunk <= last; chunk++) {
        let at = starts[chunk - 1];
        for (let i = 0; i < perChunk && packet < count; i++) {
          a.offsets[packet] = at;
          at += a.sizes[packet++];
        }
      }
    }
    if (packet !== count) throw damaged("its sample table doesn't add up");
    const entries = be32(b, stts.start + 4);
    checkCount(entries, stts, 8, 8, "packet duration");
    let duration = 0;
    for (let i = 0; i < entries; i++) duration += be32(b, stts.start + 8 + 8 * i) * be32(b, stts.start + 12 + 8 * i);
    const scale = a.timescale || a.rate;
    a.frames = Math.min(Math.floor(duration * a.rate / scale), count * a.config.frameLength);
    return a;
  }

  // -- 2. the ALAC decoder -----------------------------------------------------------------
  // Element tags (3 bits each)
  const ID_SCE = 0, ID_CPE = 1, ID_CCE = 2, ID_LFE = 3, ID_DSE = 4, ID_PCE = 5, ID_FIL = 6, ID_END = 7;
  // The adaptive Golomb coder's constants
  const QBSHIFT = 9, QB = 1 << QBSHIFT, MMULSHIFT = 2, MDENSHIFT = QBSHIFT - MMULSHIFT - 1, MOFF = 1 << (MDENSHIFT - 2);
  const BITOFF = 24, N_MAX_MEAN_CLAMP = 0xffff, N_MEAN_CLAMP_VAL = 0xffff, MAX_PREFIX = 9, MAX_DATATYPE_BITS_16 = 16;

  const sign = (x) => (x > 0) - (x < 0);

  class Decoder {
    constructor(config) {
      this.c = config;
      const n = config.frameLength;
      this.predictor = new Int32Array(n);
      this.mixU = new Int32Array(n);
      this.mixV = new Int32Array(n);
      this.shiftBuf = new Uint16Array(n * 2);
      this.out = Array.from({ length: config.channels }, () => new Int32Array(n));   // samples at the file's own bit depth
      this.coefsU = new Int16Array(32);
      this.coefsV = new Int16Array(32);
    }

    // -- the bits: b is the bytes, pos the bit position, end the packet's last bit + 1
    peek32(p) {
      const b = this.b, i = p >>> 3, s = p & 7;
      const w = ((b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]) >>> 0;   // (past the end: 0s)
      return s ? ((w << s) | (b[i + 4] >>> (8 - s))) >>> 0 : w;
    }
    bitsAt(p, n) { return n ? this.peek32(p) >>> (32 - n) : 0; }
    read(n) { const v = this.bitsAt(this.pos, n); this.pos += n; return v; }

    // Decodes the packet b[start, start + length): returns its frames, the samples in this.out.
    decode(b, start, length) {
      this.b = b; this.pos = start * 8; this.end = (start + length) * 8;
      const channels = this.c.channels;
      let ch = 0, frames = this.c.frameLength;
      elements: for (;;) {
        if (this.pos >= this.end) throw damaged("a packet ends early");
        const tag = this.read(3);
        switch (tag) {
          case ID_SCE: case ID_LFE:
            if (ch + 1 > channels) break elements;
            frames = this.single(ch, frames);
            ch += 1;
            break;
          case ID_CPE:
            if (ch + 2 > channels) break elements;
            frames = this.pair(ch, frames);
            ch += 2;
            break;
          case ID_DSE: {   // data stream element: skipped
            this.read(4);
            const align = this.read(1);
            let count = this.read(8);
            if (count === 255) count += this.read(8);
            if (align) this.pos = (this.pos + 7) & ~7;
            this.pos += count * 8;
            break;
          }
          case ID_FIL: {   // fill element: skipped
            let count = this.read(4);
            if (count === 15) count += this.read(8) - 1;
            this.pos += count * 8;
            break;
          }
          case ID_END:
            break elements;
          case ID_CCE: case ID_PCE: default:
            throw new NotAlac("this Apple Lossless file uses a feature the web player can't decode (element " + tag + ")");
        }
      }
      for (; ch < channels; ch++) this.out[ch].fill(0, 0, frames);   // channels the packet didn't carry: silence
      return frames;
    }

    // The header every element starts with (after its 3-bit tag and 4-bit instance tag)
    header(frames) {
      this.read(4);
      if (this.read(12) !== 0) throw damaged("an element's header");
      const h = this.read(4);
      const partial = h >> 3, shifted = (h >> 1) & 3, escape = h & 1;
      if (shifted === 3) throw damaged("an element's header");
      if (partial) frames = this.read(32);
      if (frames > this.c.frameLength) throw damaged("a packet longer than the setup block allows");
      return { frames, shifted, escape };
    }

    // One channel's predictor settings: mode, denominator shift, rice "pb" factor, coefficients
    params(coefs) {
      let h = this.read(8);
      const mode = h >> 4, denShift = h & 15;
      h = this.read(8);
      const pbFactor = h >> 5, num = h & 31;
      for (let i = 0; i < num; i++) coefs[i] = this.read(16);   // (Int16Array: signed)
      return { mode, denShift, pbFactor, num };
    }

    // The residuals (dynDecomp), then the predictor run over them (unpcBlock), into `out`
    channel(p, coefs, frames, chanBits, out) {
      this.dynDecomp((this.c.pb * p.pbFactor) >> 2, frames, chanBits);
      if (p.mode !== 0) unpcBlock(this.predictor, this.predictor, frames, null, 31, chanBits, 0);
      unpcBlock(this.predictor, out, frames, coefs, p.num, chanBits, p.denShift);
    }

    // Uncompressed ("escape") samples, chanBits each, sign-extended
    raw(chanBits, frames, outs) {
      const shift = 32 - chanBits;
      for (let i = 0; i < frames; i++) for (const out of outs) out[i] = (this.read(chanBits) << shift) >> shift;
    }

    single(ch, frames) {
      const h = this.header(frames);
      frames = h.frames;
      let shifted = h.shifted;
      const chanBits = this.c.bitDepth - shifted * 8, mix = this.mixU;
      let shiftAt = 0;
      if (!h.escape) {
        this.read(16);   // mixBits, mixRes: no mixing for one channel
        const p = this.params(this.coefsU);
        if (shifted) { shiftAt = this.pos; this.pos += shifted * 8 * frames; }
        this.channel(p, this.coefsU, frames, chanBits, mix);
      } else {
        this.raw(this.c.bitDepth, frames, [mix]);
        shifted = 0;
      }
      if (shifted) {
        const n = shifted * 8;
        for (let i = 0; i < frames; i++) this.shiftBuf[i] = this.bitsAt(shiftAt + i * n, n);
      }
      const out = this.out[ch];
      for (let i = 0; i < frames; i++) out[i] = this.sample(mix[i], shifted, shifted ? this.shiftBuf[i] : 0);
      return frames;
    }

    pair(ch, frames) {
      const h = this.header(frames);
      frames = h.frames;
      let shifted = h.shifted, mixBits = 0, mixRes = 0, shiftAt = 0;
      const u = this.mixU, v = this.mixV;
      if (!h.escape) {
        const chanBits = this.c.bitDepth - shifted * 8 + 1;   // (one bit more: the side channel's range)
        if (chanBits > 32) throw damaged("a channel pair's bit depth");
        mixBits = this.read(8);
        mixRes = (this.read(8) << 24) >> 24;
        const pU = this.params(this.coefsU), pV = this.params(this.coefsV);
        if (shifted) { shiftAt = this.pos; this.pos += shifted * 8 * 2 * frames; }
        this.channel(pU, this.coefsU, frames, chanBits, u);
        this.channel(pV, this.coefsV, frames, chanBits, v);
      } else {
        this.raw(this.c.bitDepth, frames, [u, v]);
        shifted = 0;
      }
      if (shifted) {
        const n = shifted * 8;
        for (let i = 0; i < frames * 2; i++) this.shiftBuf[i] = this.bitsAt(shiftAt + i * n, n);
      }
      const left = this.out[ch], right = this.out[ch + 1], sb = this.shiftBuf;
      for (let i = 0; i < frames; i++) {
        let l = u[i], r = v[i];
        if (mixRes !== 0) {   // matrixed stereo
          l = (u[i] + v[i] - (Math.imul(mixRes, v[i]) >> mixBits)) | 0;
          r = (l - v[i]) | 0;
        }
        left[i] = this.sample(l, shifted, shifted ? sb[2 * i] : 0);
        right[i] = this.sample(r, shifted, shifted ? sb[2 * i + 1] : 0);
      }
      return frames;
    }

    // A decoded value at the file's bit depth, its shifted-off low bytes put back
    sample(x, shifted, low) {
      switch (this.c.bitDepth) {
        case 16: return (x << 16) >> 16;
        case 20: return (x << 12) >> 12;
        case 24: return shifted ? (((x << (shifted * 8)) | low) << 8) >> 8 : (x << 8) >> 8;
        default: return shifted ? (x << (shifted * 8)) | low : x;
      }
    }

    // The adaptive Golomb decoder: `frames` residuals into this.predictor
    dynDecomp(pb, frames, maxSize) {
      const out = this.predictor, kb = this.c.kb, wb = (1 << kb) - 1;
      let mb = this.c.mb, zmode = 0, c = 0;
      while (c < frames) {
        if (this.pos >= this.end) throw damaged("a packet ends early");
        let k = 31 - Math.clz32((mb >>> QBSHIFT) + 3);
        if (k > kb) k = kb;
        const m = (1 << k) - 1;
        const n = this.get32(m, k, maxSize);
        const nd = (n + zmode) >>> 0;
        const half = ((nd + 1) >>> 1);   // (the least significant bit is the sign)
        out[c++] = nd & 1 ? -half | 0 : half | 0;
        mb = (Math.imul(pb, nd) + mb - (Math.imul(pb, mb) >>> QBSHIFT)) >>> 0;
        if (n > N_MAX_MEAN_CLAMP) mb = N_MEAN_CLAMP_VAL;
        zmode = 0;
        if (((mb << MMULSHIFT) >>> 0) < QB && c < frames) {   // a run of zeros
          zmode = 1;
          const kz = Math.clz32(mb) - BITOFF + ((mb + MOFF) >> MDENSHIFT);
          const run = this.get16(((1 << kz) - 1) & wb, kz);
          if (c + run > frames) throw damaged("a run of silence longer than its packet");
          out.fill(0, c, c + run);
          c += run;
          if (run >= 65535) zmode = 0;
          mb = 0;
        }
      }
    }

    // One residual: a unary prefix, then k bits (or, after MAX_PREFIX ones, maxBits bits as they are)
    get32(m, k, maxBits) {
      let w = this.peek32(this.pos);
      const pre = Math.clz32(~w);
      if (pre >= MAX_PREFIX) {
        const v = this.bitsAt(this.pos + MAX_PREFIX, maxBits);
        this.pos += MAX_PREFIX + maxBits;
        return v;
      }
      this.pos += pre + 1;
      if (k === 1) return pre;
      w = (w << (pre + 1)) >>> 0;
      const v = w >>> (32 - k);
      this.pos += k - 1;
      let result = pre * m;
      if (v >= 2) { result += v - 1; this.pos += 1; }
      return result;
    }

    // A run length (of zeros): the same code, with a 16-bit escape
    get16(m, k) {
      let w = this.peek32(this.pos);
      const pre = Math.clz32(~w);
      if (pre >= MAX_PREFIX) {
        const v = this.bitsAt(this.pos + MAX_PREFIX, MAX_DATATYPE_BITS_16);
        this.pos += MAX_PREFIX + MAX_DATATYPE_BITS_16;
        return v;
      }
      this.pos += pre + 1;
      w = (w << (pre + 1)) >>> 0;
      const v = w >>> (32 - k);
      this.pos += k;
      if (v < 2) { this.pos -= 1; return pre * m; }
      return pre * m + v - 1;
    }
  }

  // The adaptive linear predictor: `pc` (residuals) to `out` (samples, chanBits wide). numActive
  // 31 is the special "first difference" mode; 0 copies. The coefficients adapt as it goes
  // (int16, as Apple's are). In and out may be the same array for numActive 31.
  function unpcBlock(pc, out, num, coefs, numActive, chanBits, denShift) {
    const chanShift = 32 - chanBits;
    const denHalf = denShift ? 1 << (denShift - 1) : 0;
    out[0] = pc[0];
    if (numActive === 0) {
      if (pc !== out) out.set(pc.subarray(0, num));
      return;
    }
    if (numActive === 31) {
      let prev = out[0];
      for (let j = 1; j < num; j++) {
        prev = (((pc[j] + prev) | 0) << chanShift) >> chanShift;
        out[j] = prev;
      }
      return;
    }
    for (let j = 1; j <= numActive && j < num; j++) out[j] = (((pc[j] + out[j - 1]) | 0) << chanShift) >> chanShift;
    const lim = numActive + 1;
    for (let j = lim; j < num; j++) {
      const top = out[j - lim];
      let sum = 0;
      for (let k = 0; k < numActive; k++) sum = (sum + Math.imul(coefs[k], (out[j - 1 - k] - top) | 0)) | 0;
      let del = pc[j], del0 = del;
      const sg = sign(del);
      del = (del + top + ((sum + denHalf) >> denShift)) | 0;
      out[j] = (del << chanShift) >> chanShift;
      if (sg > 0) {
        for (let k = numActive - 1; k >= 0; k--) {
          const dd = (top - out[j - 1 - k]) | 0, sgn = sign(dd);
          coefs[k] -= sgn;
          del0 = (del0 - Math.imul(numActive - k, Math.imul(sgn, dd) >> denShift)) | 0;
          if (del0 <= 0) break;
        }
      } else if (sg < 0) {
        for (let k = numActive - 1; k >= 0; k--) {
          const dd = (top - out[j - 1 - k]) | 0, sgn = sign(dd);
          coefs[k] += sgn;
          del0 = (del0 - Math.imul(numActive - k, Math.imul(-sgn, dd) >> denShift)) | 0;
          if (del0 >= 0) break;
        }
      }
    }
  }

  // -- 3. the WAV ----------------------------------------------------------------------------
  const containerBytes = (bits) => (bits === 16 ? 2 : bits === 32 ? 4 : 3);

  function writeWavHeader(wav, frames, channels, bits, rate) {
    const bytesPer = containerBytes(bits), block = bytesPer * channels, data = frames * block;
    const v = new DataView(wav.buffer, wav.byteOffset, 44);
    const tag = (o, s) => { for (let i = 0; i < 4; i++) wav[o + i] = s.charCodeAt(i); };
    tag(0, "RIFF"); v.setUint32(4, 36 + data + (data & 1), true); tag(8, "WAVE");   // (an odd-sized chunk has a pad byte)
    tag(12, "fmt "); v.setUint32(16, 16, true);
    v.setUint16(20, 1, true);                   // PCM
    v.setUint16(22, channels, true);
    v.setUint32(24, rate, true);
    v.setUint32(28, rate * block, true);
    v.setUint16(32, block, true);
    v.setUint16(34, bytesPer * 8, true);
    tag(36, "data"); v.setUint32(40, data, true);
  }

  // Interleaves `frames` frames of `outs` (one Int32Array per channel) into wav at `at`
  function writeFrames(wav, at, outs, frames, bits) {
    const channels = outs.length, up = bits === 20 ? 4 : 0;   // (20-bit: left-justified in 24)
    for (let i = 0; i < frames; i++) {
      for (let c = 0; c < channels; c++) {
        const x = outs[c][i] << up;
        wav[at++] = x & 255; wav[at++] = (x >> 8) & 255;
        if (bits !== 16) wav[at++] = (x >> 16) & 255;
        if (bits === 32) wav[at++] = (x >> 24) & 255;
      }
    }
    return at;
  }

  // The whole song as a WAV Blob. onProgress(fraction) now and then.
  async function decodeToWav(file, { onProgress, index } = {}) {
    const a = index || await readIndex(file);
    if (a.channels > 2) throw new NotAlac("Apple Lossless with " + a.channels + " channels: the web player decodes mono and stereo only");
    const need = 44 + a.frames * a.channels * containerBytes(a.bits) + 1;   // (+1: room for a pad byte)
    if (need > MAX_WAV) throw new NotAlac("too long to decode in a browser (it would take about " + (need / 1073741824).toFixed(1) + " GB of memory)");
    const wav = new Uint8Array(need);
    const decoder = new Decoder(a.config);
    let frame = 0, at = 44, win = null, winStart = 0, winEnd = 0, shown = 0;
    for (let p = 0; p < a.sizes.length && frame < a.frames; p++) {
      const off = a.offsets[p], len = a.sizes[p];
      if (off + len > file.size) throw damaged("the file ends before its music does");
      if (!win || off < winStart || off + len > winEnd) {
        winStart = off;
        winEnd = Math.min(file.size, Math.max(off + len, off + WINDOW));
        win = await bytes(file, winStart, winEnd - winStart);
      }
      const n = Math.min(decoder.decode(win, off - winStart, len), a.frames - frame);
      at = writeFrames(wav, at, decoder.out, n, a.bits);
      frame += n;
      if (onProgress && frame - shown >= a.frames / 50) { shown = frame; onProgress(frame / a.frames); }
    }
    writeWavHeader(wav, frame, a.channels, a.bits, a.rate);   // (the frames really decoded)
    if (onProgress) onProgress(1);
    return new Blob([wav.subarray(0, at + ((at - 44) & 1))], { type: "audio/wav" });
  }

  const api = { readIndex, decodeToWav, Decoder, NotAlac, MAX_WAV, _internal: { unpcBlock, writeWavHeader, writeFrames } };
  root.IronbarkAlac = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  // -- as a Web Worker ----------------------------------------------------------------------
  if (typeof WorkerGlobalScope !== "undefined" && root instanceof WorkerGlobalScope) {
    root.onmessage = async (ev) => {
      try {
        const blob = await decodeToWav(ev.data.file, { onProgress: (progress) => root.postMessage({ progress }) });
        root.postMessage({ blob });
      } catch (e) {
        root.postMessage({ error: String(e && e.message || e), notAlac: !!(e && e.name === "NotAlac") });
      }
    };
  }
})(typeof globalThis !== "undefined" ? globalThis : this);

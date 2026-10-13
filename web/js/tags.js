/* Ironbark for the web — reading a music file's tags and stream facts in the browser.

   readTags(file) reads only the parts of the file it needs (File.slice), never the whole song,
   and returns one plain object whatever the format:

     { format, codec, title, artist, album, albumArtist, track, trackTotal, disc, discTotal,
       date, genre, sampleRate, bitsPerSample, channels, duration, bitrate, md5, picture }

   Anything the file doesn't say is null. `picture` is { mime, data: Uint8Array } or null;
   `hasPicture` says whether the file has one (also when it wasn't read: options.pictures).
   `md5` is the hex MD5 of the decoded audio that a FLAC encoder stores (null if absent).
   Formats: FLAC (also in Ogg), MP3 (ID3v1/v2, Xing/Info/VBRI), Ogg Vorbis, Opus, WAV (RIFF
   INFO, id3 chunk), AIFF/AIFC, MP4/M4A (AAC, ALAC). Works in a browser and in Node 18+. */
(function (root) {
  "use strict";

  const MAX_BLOCK = 32 * 1024 * 1024;   // never read more than this for one tag block

  // -- reading bytes -------------------------------------------------------------------------
  // Small reads are served from one 128 KB chunk read ahead (the tags of most files sit in their
  // first few kilobytes), so a file costs one or two reads from disk, not one per tag block.
  const CHUNK = 128 * 1024;
  function reader(blob) {
    const chunks = [];   // [{ start, bytes }], at most two: the head and the latest elsewhere
    async function slice(start, end) {
      return new Uint8Array(await blob.slice(start, end).arrayBuffer());
    }
    return {
      size: blob.size,
      async read(offset, length) {
        if (offset < 0) { length += offset; offset = 0; }
        const end = Math.min(blob.size, offset + Math.max(0, length));
        if (end <= offset) return new Uint8Array(0);
        for (const c of chunks) {
          if (offset >= c.start && end <= c.start + c.bytes.length) return c.bytes.subarray(offset - c.start, end - c.start);
        }
        if (end - offset > CHUNK / 2) return slice(offset, end);
        const start = Math.max(0, Math.min(offset, blob.size - CHUNK));
        const c = { start, bytes: await slice(start, Math.min(blob.size, start + CHUNK)) };
        if (chunks.length > 1) chunks.pop();
        chunks.push(c);
        return c.bytes.subarray(offset - c.start, end - c.start);
      },
    };
  }

  const latin1 = new TextDecoder("latin1");
  const utf8 = new TextDecoder("utf-8");
  const utf16le = new TextDecoder("utf-16le");
  const utf16be = new TextDecoder("utf-16be");

  function ascii(b, start, len) {
    let s = "";
    for (let i = start; i < start + len && i < b.length; i++) s += String.fromCharCode(b[i]);
    return s;
  }
  const be16 = (b, o) => (b[o] << 8) | b[o + 1];
  const be24 = (b, o) => (b[o] << 16) | (b[o + 1] << 8) | b[o + 2];
  const be32 = (b, o) => ((b[o] << 24) >>> 0) + ((b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]);
  const le16 = (b, o) => b[o] | (b[o + 1] << 8);
  const le32 = (b, o) => (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16)) + ((b[o + 3] << 24) >>> 0);
  const be64 = (b, o) => be32(b, o) * 4294967296 + be32(b, o + 4);
  const le64 = (b, o) => le32(b, o + 4) * 4294967296 + le32(b, o);
  const syncsafe = (b, o) => ((b[o] & 0x7f) << 21) | ((b[o + 1] & 0x7f) << 14) | ((b[o + 2] & 0x7f) << 7) | (b[o + 3] & 0x7f);
  const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

  function blank() {
    return {
      format: null, codec: null, title: null, artist: null, album: null, albumArtist: null,
      track: null, trackTotal: null, disc: null, discTotal: null, date: null, genre: null,
      sampleRate: null, bitsPerSample: null, channels: null, duration: null, bitrate: null,
      md5: null, picture: null,
    };
  }

  // "3/12" -> [3, 12]; "03" -> [3, null]; junk -> [null, null]
  function numberPair(text) {
    if (text == null) return [null, null];
    const m = /^\s*(\d+)\s*(?:\/\s*(\d+))?/.exec(String(text));
    if (!m) return [null, null];
    return [parseInt(m[1], 10), m[2] ? parseInt(m[2], 10) : null];
  }

  function setText(info, key, value) {
    if (value == null) return;
    value = String(value).replace(/\0+$/g, "").trim();
    if (value && info[key] == null) info[key] = value;
  }

  // -- Vorbis comments (FLAC, Ogg Vorbis, Opus) ----------------------------------------------
  function vorbisComments(b, start, info) {
    let o = start;
    if (o + 4 > b.length) return;
    o += 4 + le32(b, o);                    // vendor string
    if (o + 4 > b.length) return;
    const count = le32(b, o); o += 4;
    const tags = {};
    for (let i = 0; i < count && o + 4 <= b.length; i++) {
      const len = le32(b, o); o += 4;
      if (o + len > b.length) break;
      const text = utf8.decode(b.subarray(o, o + len)); o += len;
      const eq = text.indexOf("=");
      if (eq < 1) continue;
      const k = text.slice(0, eq).toUpperCase();
      (tags[k] = tags[k] || []).push(text.slice(eq + 1));
    }
    const first = (...keys) => { for (const k of keys) if (tags[k] && tags[k][0]) return tags[k][0]; return null; };
    setText(info, "title", first("TITLE"));
    setText(info, "artist", tags.ARTIST ? tags.ARTIST.join(", ") : null);
    setText(info, "album", first("ALBUM"));
    setText(info, "albumArtist", first("ALBUMARTIST", "ALBUM ARTIST", "ALBUM_ARTIST"));
    setText(info, "date", first("DATE", "YEAR", "ORIGINALDATE"));
    setText(info, "genre", tags.GENRE ? tags.GENRE.join(", ") : null);
    const [t, tt] = numberPair(first("TRACKNUMBER"));
    const [d, dt] = numberPair(first("DISCNUMBER"));
    if (info.track == null) info.track = t;
    if (info.trackTotal == null) info.trackTotal = tt ?? numberPair(first("TRACKTOTAL", "TOTALTRACKS"))[0];
    if (info.disc == null) info.disc = d;
    if (info.discTotal == null) info.discTotal = dt ?? numberPair(first("DISCTOTAL", "TOTALDISCS"))[0];
    if (!info.picture && tags.METADATA_BLOCK_PICTURE) {
      for (const b64 of tags.METADATA_BLOCK_PICTURE) {
        const pic = flacPicture(base64(b64), 0);
        if (pic && (!info.picture || pic.type === 3)) info.picture = pic;
        if (pic && pic.type === 3) break;
      }
    }
  }

  function base64(text) {
    try {
      const bin = atob(text.replace(/\s+/g, ""));
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out;
    } catch (e) {
      return new Uint8Array(0);
    }
  }

  // A FLAC PICTURE block body (also the payload of METADATA_BLOCK_PICTURE).
  function flacPicture(b, o) {
    if (o + 32 > b.length) return null;
    const type = be32(b, o); o += 4;
    const mlen = be32(b, o); o += 4;
    if (o + mlen > b.length) return null;
    const mime = latin1.decode(b.subarray(o, o + mlen)).toLowerCase(); o += mlen;
    const dlen = be32(b, o); o += 4 + dlen;
    o += 16;                                // width, height, depth, colours
    if (o + 4 > b.length) return null;
    const len = be32(b, o); o += 4;
    if (o + len > b.length || len === 0 || mime === "-->") return null;
    return { type, mime: mime || sniffImage(b.subarray(o, o + 4)), data: b.slice(o, o + len) };
  }

  function sniffImage(b) {
    if (b[0] === 0xff && b[1] === 0xd8) return "image/jpeg";
    if (b[0] === 0x89 && b[1] === 0x50) return "image/png";
    if (b[0] === 0x47 && b[1] === 0x49) return "image/gif";
    if (b[0] === 0x52 && b[1] === 0x49) return "image/webp";
    return "image/jpeg";
  }

  // FLAC STREAMINFO body (34 bytes).
  function streamInfo(b, o, info) {
    info.sampleRate = (b[o + 10] << 12) | (b[o + 11] << 4) | (b[o + 12] >> 4);
    info.channels = ((b[o + 12] >> 1) & 0x07) + 1;
    info.bitsPerSample = (((b[o + 12] & 0x01) << 4) | (b[o + 13] >> 4)) + 1;
    const total = (b[o + 13] & 0x0f) * 4294967296 + be32(b, o + 14);
    if (total > 0 && info.sampleRate > 0) info.duration = total / info.sampleRate;
    const md5 = b.subarray(o + 18, o + 34);
    if (md5.some((x) => x !== 0)) info.md5 = hex(md5);
    info.totalSamples = total || null;
  }

  // -- FLAC ----------------------------------------------------------------------------------
  async function readFlac(r, start, info) {
    info.format = "FLAC"; info.codec = "FLAC";
    let o = start + 4;
    for (let n = 0; n < 128 && o + 4 <= r.size; n++) {
      const h = await r.read(o, 4);
      if (h.length < 4) break;
      const last = (h[0] & 0x80) !== 0, type = h[0] & 0x7f, len = be24(h, 1);
      o += 4;
      if (type === 6 && info._skipPictures) {
        info.hasPicture = true;                               // read when the song plays
      } else if (type === 0 || type === 4 || (type === 6 && !info.picture)) {
        const body = await r.read(o, Math.min(len, MAX_BLOCK));
        if (type === 0) streamInfo(body, 0, info);
        else if (type === 4) vorbisComments(body, 0, info);
        else {
          const pic = flacPicture(body, 0);
          if (pic) info.picture = pic;
        }
      } else if (type === 6 && info.picture && info.picture.type !== 3) {
        const body = await r.read(o, Math.min(len, MAX_BLOCK));
        const pic = flacPicture(body, 0);
        if (pic && pic.type === 3) info.picture = pic;
      }
      o += len;
      if (last) break;
    }
    if (info.duration) info.bitrate = Math.round(((r.size - o) * 8) / info.duration / 1000);
  }

  // -- ID3v2 ---------------------------------------------------------------------------------
  function unsync(b) {
    const out = new Uint8Array(b.length);
    let n = 0;
    for (let i = 0; i < b.length; i++) {
      out[n++] = b[i];
      if (b[i] === 0xff && b[i + 1] === 0x00) i++;
    }
    return out.subarray(0, n);
  }

  function id3Text(b) {
    if (!b.length) return "";
    const enc = b[0], body = b.subarray(1);
    let s;
    if (enc === 1) {
      if (body[0] === 0xfe && body[1] === 0xff) s = utf16be.decode(body.subarray(2));
      else if (body[0] === 0xff && body[1] === 0xfe) s = utf16le.decode(body.subarray(2));
      else s = utf16le.decode(body);
    } else if (enc === 2) s = utf16be.decode(body);
    else if (enc === 3) s = utf8.decode(body);
    else s = latin1.decode(body);
    return s.split("\0").filter(Boolean).join(", ");   // ID3v2.4 lists values split by NUL
  }

  // where a NUL-terminated string in the given encoding ends: [end, next]
  function idNul(b, o, enc) {
    if (enc === 1 || enc === 2) {
      for (let i = o; i + 1 < b.length; i += 2) if (b[i] === 0 && b[i + 1] === 0) return [i, i + 2];
    } else {
      for (let i = o; i < b.length; i++) if (b[i] === 0) return [i, i + 1];
    }
    return [b.length, b.length];
  }

  function id3Genre(text) {
    // "(17)", "(17)Rock" or a bare "17": the old ID3v1 genre numbers (keep any words after the
    // brackets, else the number's name). "80s" or "2 Tone" are names, left as they are.
    const m = /^\((\d+)\)(.*)$/.exec(text) || /^(\d+)()$/.exec(text);
    if (!m) return text;
    if (m[2].trim()) return m[2].trim();
    return ID3_GENRES[parseInt(m[1], 10)] || text;
  }

  const ID3_GENRES = ["Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge",
    "Hip-Hop", "Jazz", "Metal", "New Age", "Oldies", "Other", "Pop", "R&B", "Rap", "Reggae",
    "Rock", "Techno", "Industrial", "Alternative", "Ska", "Death Metal", "Pranks", "Soundtrack",
    "Euro-Techno", "Ambient", "Trip-Hop", "Vocal", "Jazz+Funk", "Fusion", "Trance", "Classical",
    "Instrumental", "Acid", "House", "Game", "Sound Clip", "Gospel", "Noise", "Alternative Rock",
    "Bass", "Soul", "Punk", "Space", "Meditative", "Instrumental Pop", "Instrumental Rock",
    "Ethnic", "Gothic", "Darkwave", "Techno-Industrial", "Electronic", "Pop-Folk", "Eurodance",
    "Dream", "Southern Rock", "Comedy", "Cult", "Gangsta", "Top 40", "Christian Rap", "Pop/Funk",
    "Jungle", "Native American", "Cabaret", "New Wave", "Psychedelic", "Rave", "Showtunes",
    "Trailer", "Lo-Fi", "Tribal", "Acid Punk", "Acid Jazz", "Polka", "Retro", "Musical",
    "Rock & Roll", "Hard Rock"];

  // Parse an ID3v2 tag that starts at b[0] ("ID3"); returns the tag's full size.
  function id3v2(b, info) {
    const major = b[3], flags = b[5];
    const size = syncsafe(b, 6) + 10 + ((flags & 0x10) ? 10 : 0);
    let body = b.subarray(10, Math.min(b.length, syncsafe(b, 6) + 10));
    if (major < 4 && (flags & 0x80)) body = unsync(body);
    let o = 0;
    if (flags & 0x40) o += major === 3 ? be32(body, 0) + 4 : syncsafe(body, 0);   // extended header
    const idLen = major === 2 ? 3 : 4, hdrLen = major === 2 ? 6 : 10;
    const MAP2 = { TT2: "TIT2", TP1: "TPE1", TP2: "TPE2", TAL: "TALB", TRK: "TRCK", TPA: "TPOS",
      TYE: "TYER", TCO: "TCON", PIC: "APIC", TDA: "TDAT" };
    while (o + hdrLen <= body.length) {
      let id = ascii(body, o, idLen);
      if (!/^[A-Z0-9]+$/.test(id)) break;                    // padding
      let len = major === 2 ? be24(body, o + 3) : major === 4 ? syncsafe(body, o + 4) : be32(body, o + 4);
      const fflags = major === 2 ? 0 : be16(body, o + 8);
      o += hdrLen;
      if (len < 0 || o + len > body.length) break;
      if (len === 0) continue;                               // an empty frame: taggers do write them
      let data = body.subarray(o, o + len);
      o += len;
      if (major === 2) id = MAP2[id] || id;
      if (major === 4) {
        if (fflags & 0x0001) data = data.subarray(4);        // data length indicator
        if (fflags & 0x0002) data = unsync(data);
        if (fflags & 0x000c) continue;                       // compressed / encrypted: skip
      } else if (major === 3 && (fflags & 0x00c0)) continue;
      switch (id) {
        case "TIT2": setText(info, "title", id3Text(data)); break;
        case "TPE1": setText(info, "artist", id3Text(data)); break;
        case "TPE2": setText(info, "albumArtist", id3Text(data)); break;
        case "TALB": setText(info, "album", id3Text(data)); break;
        case "TCON": setText(info, "genre", id3Genre(id3Text(data))); break;
        case "TDRC": case "TYER": case "TDOR": setText(info, "date", id3Text(data)); break;
        case "TRCK": { const [t, tt] = numberPair(id3Text(data)); info.track ??= t; info.trackTotal ??= tt; break; }
        case "TPOS": { const [d, dt] = numberPair(id3Text(data)); info.disc ??= d; info.discTotal ??= dt; break; }
        case "APIC": {
          const pic = apic(data, major);
          if (pic && (!info.picture || (pic.type === 3 && info.picture.type !== 3))) info.picture = pic;
          break;
        }
      }
    }
    return size;
  }

  function apic(b, major) {
    if (b.length < 4) return null;
    const enc = b[0];
    let o = 1, mime;
    if (major === 2) {
      const fmt = ascii(b, 1, 3).toUpperCase();
      mime = fmt === "PNG" ? "image/png" : "image/jpeg"; o = 4;
    } else {
      const [end, next] = idNul(b, 1, 0);
      mime = latin1.decode(b.subarray(1, end)).toLowerCase(); o = next;
    }
    const type = b[o]; o += 1;
    o = idNul(b, o, enc)[1];                                  // description
    const data = b.slice(o);
    if (!data.length) return null;
    if (!mime || mime.indexOf("/") < 0) mime = sniffImage(data);
    return { type, mime, data };
  }

  function id3v1(b, info) {   // the 128 bytes at the end of a file
    if (b.length < 128 || ascii(b, 0, 3) !== "TAG") return;
    const s = (o, n) => latin1.decode(b.subarray(o, o + n)).replace(/\0.*$/s, "").trim();
    setText(info, "title", s(3, 30));
    setText(info, "artist", s(33, 30));
    setText(info, "album", s(63, 30));
    setText(info, "date", s(93, 4));
    if (b[125] === 0 && b[126] !== 0 && info.track == null) info.track = b[126];
    if (info.genre == null && ID3_GENRES[b[127]]) info.genre = ID3_GENRES[b[127]];
  }

  // -- MP3 -----------------------------------------------------------------------------------
  const MP3_RATES = { 3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000] };
  const MP3_BITRATES = {
    "1-1": [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    "1-2": [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    "1-3": [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    "2-1": [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    "2-2": [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
  };

  function mp3Header(b, o) {
    if (b[o] !== 0xff || (b[o + 1] & 0xe0) !== 0xe0) return null;
    const ver = (b[o + 1] >> 3) & 3, layerBits = (b[o + 1] >> 1) & 3;
    const brIdx = b[o + 2] >> 4, srIdx = (b[o + 2] >> 2) & 3;
    if (ver === 1 || layerBits === 0 || brIdx === 0 || brIdx === 15 || srIdx === 3) return null;
    const layer = 4 - layerBits;
    const key = (ver === 3 ? "1-" : "2-") + (ver === 3 ? layer : layer === 1 ? 1 : 2);
    const bitrate = MP3_BITRATES[key][brIdx];
    const rate = MP3_RATES[ver][srIdx];
    const pad = (b[o + 2] >> 1) & 1;
    const mono = (b[o + 3] >> 6) === 3;
    const spf = layer === 1 ? 384 : layer === 2 || ver === 3 ? 1152 : 576;
    const size = layer === 1 ? Math.floor((12 * bitrate * 1000) / rate + pad) * 4
      : Math.floor(((spf / 8) * bitrate * 1000) / rate) + pad;
    return { ver, layer, bitrate, rate, mono, spf, size };
  }

  async function readMp3(r, start, info) {
    const b = await r.read(start, 64 * 1024);
    let o = 0, h = null;
    for (; o + 4 < b.length; o++) {
      h = mp3Header(b, o);
      if (h && (o + h.size + 2 > b.length || mp3Header(b, o + h.size))) break;   // two frames in a row
      h = null;
    }
    if (!h) return;                                           // not MP3 after all: format stays unknown
    info.format = "MP3";
    info.codec = "MPEG-" + (h.ver === 3 ? "1" : h.ver === 2 ? "2" : "2.5") + " Layer " + ["", "I", "II", "III"][h.layer];
    if (h.layer !== 3) info.format = "MP" + h.layer;
    info.sampleRate = h.rate;
    info.channels = h.mono ? 1 : 2;
    const side = h.ver === 3 ? (h.mono ? 17 : 32) : (h.mono ? 9 : 17);
    const x = o + 4 + side, tag = ascii(b, x, 4);
    let frames = null;
    if (tag === "Xing" || tag === "Info") {
      if (b[x + 7] & 1) frames = be32(b, x + 8);
    } else if (ascii(b, o + 36, 4) === "VBRI") {
      frames = be32(b, o + 36 + 14);
    }
    const tail = await r.read(r.size - 128, 128);
    const audioBytes = r.size - start - o - (ascii(tail, 0, 3) === "TAG" ? 128 : 0);
    if (frames) {
      info.duration = (frames * h.spf) / h.rate;
      info.bitrate = Math.round((audioBytes * 8) / info.duration / 1000);
    } else {
      info.bitrate = h.bitrate;
      info.duration = (audioBytes * 8) / (h.bitrate * 1000);
    }
  }

  // -- Ogg -----------------------------------------------------------------------------------
  // Reads the first packets of the first logical stream (enough for the id and comment headers).
  async function oggPackets(r, want) {
    const packets = [];
    let o = 0, cur = [], serial = null;
    while (packets.length < want && o + 27 <= r.size) {
      const h = await r.read(o, 27 + 255);
      if (ascii(h, 0, 4) !== "OggS") break;
      const nseg = h[26];
      const lacing = h.subarray(27, 27 + nseg);
      const bodyLen = lacing.reduce((a, x) => a + x, 0);
      const pageSerial = le32(h, 14);
      const body = await r.read(o + 27 + nseg, bodyLen);
      o += 27 + nseg + bodyLen;
      if (serial === null) serial = pageSerial;
      if (pageSerial !== serial) continue;
      let p = 0;
      for (const len of lacing) {
        cur.push(body.subarray(p, p + len)); p += len;
        if (len < 255) {
          const total = cur.reduce((a, x) => a + x.length, 0);
          const pkt = new Uint8Array(total);
          let q = 0;
          for (const part of cur) { pkt.set(part, q); q += part.length; }
          packets.push(pkt); cur = [];
          if (packets.length >= want) break;
        }
      }
      if (cur.reduce((a, x) => a + x.length, 0) > MAX_BLOCK) break;
    }
    return { packets, serial };
  }

  async function lastGranule(r, serial) {
    const n = Math.min(r.size, 256 * 1024);
    const b = await r.read(r.size - n, n);
    for (let i = b.length - 27; i >= 0; i--) {
      if (b[i] === 0x4f && b[i + 1] === 0x67 && b[i + 2] === 0x67 && b[i + 3] === 0x53 && le32(b, i + 14) === serial) {
        return le64(b, i + 6);
      }
    }
    return null;
  }

  async function readOgg(r, info) {
    const { packets, serial } = await oggPackets(r, 2);
    const id = packets[0];
    if (!id) { info.format = "Ogg"; return; }
    let rate = null, preSkip = 0;
    if (ascii(id, 0, 8) === "OpusHead") {
      info.format = "Opus"; info.codec = "Opus";
      info.channels = id[9]; preSkip = le16(id, 10);
      info.sampleRate = le32(id, 12) || 48000;
      rate = 48000;                                           // Opus granules always count 48 kHz
      if (packets[1] && ascii(packets[1], 0, 8) === "OpusTags") vorbisComments(packets[1], 8, info);
    } else if (id[0] === 1 && ascii(id, 1, 6) === "vorbis") {
      info.format = "Ogg Vorbis"; info.codec = "Vorbis";
      info.channels = id[11]; rate = info.sampleRate = le32(id, 12);
      const nominal = le32(id, 20);
      if (nominal > 0 && nominal < 0x7fffffff) info.bitrate = Math.round(nominal / 1000);
      if (packets[1] && packets[1][0] === 3 && ascii(packets[1], 1, 6) === "vorbis") vorbisComments(packets[1], 7, info);
    } else if (id[0] === 0x7f && ascii(id, 1, 4) === "FLAC") {
      info.format = "FLAC (Ogg)"; info.codec = "FLAC";
      if (ascii(id, 9, 4) === "fLaC") streamInfo(id, 17, info);
      rate = info.sampleRate;
      if (packets[1] && (packets[1][0] & 0x7f) === 4) vorbisComments(packets[1], 4, info);
    } else {
      info.format = "Ogg";
    }
    if (rate) {
      const g = await lastGranule(r, serial);
      if (g != null && g > preSkip) info.duration = (g - preSkip) / rate;
      if (info.duration && info.bitrate == null) info.bitrate = Math.round((r.size * 8) / info.duration / 1000);
    }
  }

  // -- WAV / AIFF ----------------------------------------------------------------------------
  // RIFF INFO text: UTF-8 when it's valid UTF-8, else the Windows code page most rippers write.
  const strictUtf8 = new TextDecoder("utf-8", { fatal: true });
  const cp1252 = new TextDecoder("windows-1252");
  function infoText(b) {
    try { return strictUtf8.decode(b); } catch (e) { return cp1252.decode(b); }
  }

  async function readRiff(r, info) {
    info.format = "WAV";
    let o = 12, blockAlign = 0, dataLen = null, fmtTag = 1;
    while (o + 8 <= r.size) {
      const h = await r.read(o, 8);
      const id = ascii(h, 0, 4);
      let len = le32(h, 4);
      const body = o + 8;
      if (id === "fmt ") {
        const f = await r.read(body, Math.min(len, 64));
        fmtTag = le16(f, 0);
        info.channels = le16(f, 2); info.sampleRate = le32(f, 4);
        blockAlign = le16(f, 12); info.bitsPerSample = le16(f, 14);
        if (fmtTag === 0xfffe && f.length >= 26) fmtTag = le16(f, 24);   // WAVE_FORMAT_EXTENSIBLE
      } else if (id === "data") {
        dataLen = Math.min(len, r.size - body);
        if (len === 0xffffffff || len === 0) dataLen = r.size - body;
      } else if (id === "LIST" && len <= MAX_BLOCK) {
        const l = await r.read(body, len);
        if (ascii(l, 0, 4) === "INFO") {
          let p = 4;
          while (p + 8 <= l.length) {
            const sid = ascii(l, p, 4), slen = le32(l, p + 4);
            const v = infoText(l.subarray(p + 8, p + 8 + slen));
            if (sid === "INAM") setText(info, "title", v);
            else if (sid === "IART") setText(info, "artist", v);
            else if (sid === "IPRD") setText(info, "album", v);
            else if (sid === "ICRD") setText(info, "date", v);
            else if (sid === "IGNR") setText(info, "genre", v);
            else if (sid === "ITRK" || sid === "IPRT") { const [t] = numberPair(v); info.track ??= t; }
            p += 8 + slen + (slen & 1);
          }
        }
      } else if ((id === "id3 " || id === "ID3 ") && len <= MAX_BLOCK) {
        const t = await r.read(body, len);
        if (ascii(t, 0, 3) === "ID3") id3v2(t, info);
      }
      if (dataLen != null && id === "data" && (len === 0xffffffff || len === 0)) break;
      o = body + len + (len & 1);
    }
    info.codec = fmtTag === 3 ? "PCM float" : fmtTag === 1 ? "PCM" : "format 0x" + fmtTag.toString(16);
    if (dataLen != null && blockAlign && info.sampleRate) {
      info.duration = dataLen / blockAlign / info.sampleRate;
      info.bitrate = Math.round((info.sampleRate * blockAlign * 8) / 1000);
    }
  }

  function extended80(b, o) {  // IEEE 754 80-bit extended, as AIFF stores its sample rate
    const exp = ((b[o] & 0x7f) << 8) | b[o + 1];
    const mant = be32(b, o + 2) * 4294967296 + be32(b, o + 6);
    if (exp === 0 && mant === 0) return 0;
    return (b[o] & 0x80 ? -1 : 1) * mant * Math.pow(2, exp - 16383 - 63);
  }

  async function readAiff(r, info, kind) {
    info.format = kind === "AIFC" ? "AIFF-C" : "AIFF";
    info.codec = "PCM";
    let o = 12;
    while (o + 8 <= r.size) {
      const h = await r.read(o, 8);
      const id = ascii(h, 0, 4), len = be32(h, 4), body = o + 8;
      if (id === "COMM") {
        const c = await r.read(body, Math.min(len, 64));
        info.channels = be16(c, 0);
        const frames = be32(c, 2);
        info.bitsPerSample = be16(c, 6);
        info.sampleRate = Math.round(extended80(c, 8));
        if (kind === "AIFC" && c.length >= 22) {
          const comp = ascii(c, 18, 4);
          if (comp === "fl32" || comp === "FL32") info.codec = "PCM float";
          else if (comp !== "NONE" && comp !== "sowt" && comp !== "twos") info.codec = comp.trim();
        }
        if (info.sampleRate) info.duration = frames / info.sampleRate;
        info.bitrate = Math.round((info.sampleRate * info.channels * info.bitsPerSample) / 1000);
      } else if ((id === "NAME" || id === "AUTH" || id === "ANNO") && len < 4096) {
        const v = latin1.decode(await r.read(body, len));
        if (id === "NAME") setText(info, "title", v);
        else if (id === "AUTH") setText(info, "artist", v);
      } else if ((id === "ID3 " || id === "id3 ") && len <= MAX_BLOCK) {
        const t = await r.read(body, len);
        if (ascii(t, 0, 3) === "ID3") id3v2(t, info);
      }
      o = body + len + (len & 1);
    }
  }

  // -- MP4 / M4A -----------------------------------------------------------------------------
  function* boxes(b, start, end) {
    let o = start;
    while (o + 8 <= end) {
      let size = be32(b, o);
      const type = ascii(b, o + 4, 4);
      let head = 8;
      if (size === 1) { size = be64(b, o + 8); head = 16; }
      else if (size === 0) size = end - o;
      if (size < head || o + size > end) return;
      yield { type, start: o + head, end: o + size };
      o += size;
    }
  }
  const child = (b, box, type) => { for (const c of boxes(b, box.start, box.end)) if (c.type === type) return c; return null; };

  function ilst(b, box, info) {
    for (const item of boxes(b, box.start, box.end)) {
      let name = item.type;
      let data = null, dataType = 0;
      for (const c of boxes(b, item.start, item.end)) {
        if (c.type === "data") { dataType = be32(b, c.start) & 0xffffff; data = b.subarray(c.start + 8, c.end); break; }
        if (c.type === "name") name = "----:" + utf8.decode(b.subarray(c.start + 4, c.end));
      }
      if (!data) continue;
      const text = () => utf8.decode(data);
      switch (name) {
        case "©nam": setText(info, "title", text()); break;
        case "©ART": setText(info, "artist", text()); break;
        case "aART": setText(info, "albumArtist", text()); break;
        case "©alb": setText(info, "album", text()); break;
        case "©day": setText(info, "date", text()); break;
        case "©gen": setText(info, "genre", text()); break;
        case "gnre": if (data.length >= 2) setText(info, "genre", ID3_GENRES[be16(data, 0) - 1]); break;
        case "trkn": if (data.length >= 6) { info.track ??= be16(data, 2) || null; info.trackTotal ??= be16(data, 4) || null; } break;
        case "disk": if (data.length >= 6) { info.disc ??= be16(data, 2) || null; info.discTotal ??= be16(data, 4) || null; } break;
        case "covr":
          if (!info.picture && data.length) {
            info.picture = { type: 3, mime: dataType === 14 ? "image/png" : dataType === 13 ? "image/jpeg" : sniffImage(data), data: data.slice() };
          }
          break;
        case "----:ALBUMARTIST": case "----:ALBUM ARTIST": setText(info, "albumArtist", text()); break;
      }
    }
  }

  function moov(b, box, info) {
    let audioFound = false;
    for (const c of boxes(b, box.start, box.end)) {
      if (c.type === "mvhd" && info.duration == null) {
        const v = b[c.start];
        const scale = v === 1 ? be32(b, c.start + 20) : be32(b, c.start + 12);
        const dur = v === 1 ? be64(b, c.start + 24) : be32(b, c.start + 16);
        if (scale) info.duration = dur / scale;
      } else if (c.type === "trak" && !audioFound) {
        const mdia = child(b, c, "mdia");
        const hdlr = mdia && child(b, mdia, "hdlr");
        if (!hdlr || ascii(b, hdlr.start + 8, 4) !== "soun") continue;
        audioFound = true;
        const mdhd = child(b, mdia, "mdhd");
        if (mdhd) {
          const v = b[mdhd.start];
          const scale = v === 1 ? be32(b, mdhd.start + 20) : be32(b, mdhd.start + 12);
          const dur = v === 1 ? be64(b, mdhd.start + 24) : be32(b, mdhd.start + 16);
          if (scale && dur) info.duration = dur / scale;
        }
        const minf = child(b, mdia, "minf"), stbl = minf && child(b, minf, "stbl"), stsd = stbl && child(b, stbl, "stsd");
        if (stsd) {
          const entry = boxes(b, stsd.start + 8, stsd.end).next().value;
          if (entry) {
            const e = entry.start;
            info.channels = be16(b, e + 16);
            info.bitsPerSample = be16(b, e + 18);
            info.sampleRate = be16(b, e + 24);
            const codecs = { mp4a: "AAC", alac: "ALAC", fLaC: "FLAC", Opus: "Opus", "ac-3": "AC-3", "ec-3": "E-AC-3" };
            info.codec = codecs[entry.type] || entry.type;
            if (entry.type === "alac") {
              const cfg = child(b, { start: e + 28, end: entry.end }, "alac");
              if (cfg && cfg.end - cfg.start >= 28) {
                const k = cfg.start + 4;
                info.bitsPerSample = b[k + 5];
                info.channels = b[k + 9];
                info.sampleRate = be32(b, k + 20);
              }
            } else if (entry.type === "mp4a") {
              info.bitsPerSample = null;                      // AAC has no bit depth
            }
          }
        }
      } else if (c.type === "udta") {
        const meta = child(b, c, "meta");
        if (meta) {
          // ISO 'meta' is a full box (4 bytes of version + flags); QuickTime's is not
          const start = ascii(b, meta.start + 4, 4) === "hdlr" ? meta.start : meta.start + 4;
          const list = child(b, { start, end: meta.end }, "ilst");
          if (list) ilst(b, list, info);
        }
      }
    }
  }

  async function readMp4(r, info) {
    info.format = "MP4";
    let o = 0;
    while (o + 8 <= r.size) {
      const h = await r.read(o, 16);
      let size = be32(h, 0), head = 8;
      const type = ascii(h, 4, 4);
      if (size === 1) { size = be64(h, 8); head = 16; }
      else if (size === 0) size = r.size - o;
      if (size < head) break;
      if (type === "ftyp") {
        const brand = ascii(await r.read(o + 8, 4), 0, 4);
        if (/^M4[AB]/.test(brand)) info.format = "M4A";
      } else if (type === "moov") {
        const len = Math.min(size, MAX_BLOCK);
        const b = await r.read(o, len);
        moov(b, { start: head, end: b.length }, info);
      }
      o += size;
    }
    if (info.codec && info.format === "MP4") info.format = "M4A";
    if (info.codec === "ALAC") info.format = "ALAC";
    else if (info.codec === "AAC") info.format = "AAC";
    if (info.duration) info.bitrate = Math.round((r.size * 8) / info.duration / 1000);
  }

  // -- the front door ------------------------------------------------------------------------
  const EXT = {
    flac: "flac", fla: "flac", mp3: "mp3", mp2: "mp3", ogg: "ogg", oga: "ogg", opus: "ogg",
    wav: "wav", wave: "wav", aif: "aiff", aiff: "aiff", aifc: "aiff", m4a: "mp4", m4b: "mp4",
    mp4: "mp4", alac: "mp4", aac: "aac",
  };

  function extension(name) {
    const m = /\.([^.\\/]+)$/.exec(name || "");
    return m ? m[1].toLowerCase() : "";
  }

  function isMusicName(name) {
    return Object.prototype.hasOwnProperty.call(EXT, extension(name));
  }

  // options.pictures = false: don't read FLAC cover pictures (say only whether there is one, as
  // `hasPicture`) — for reading a whole library quickly.
  async function readTags(file, options) {
    const r = reader(file);
    const info = blank();
    info._skipPictures = !!(options && options.pictures === false);
    const head = await r.read(0, 16);
    let start = 0;
    if (ascii(head, 0, 3) === "ID3") {
      const h10 = await r.read(0, 10);
      const size = syncsafe(h10, 6) + 10;
      const tag = await r.read(0, Math.min(size, MAX_BLOCK));
      start = id3v2(tag, info) || size;
    }
    const at = start ? await r.read(start, 16) : head;
    const magic = ascii(at, 0, 4);
    if (magic === "fLaC") await readFlac(r, start, info);
    else if (magic === "OggS") await readOgg(r, info);
    else if (magic === "RIFF" && ascii(at, 8, 4) === "WAVE") await readRiff(r, info);
    else if (magic === "RF64" && ascii(at, 8, 4) === "WAVE") await readRiff(r, info);
    else if (magic === "FORM" && /^AIF[FC]$/.test(ascii(at, 8, 4))) await readAiff(r, info, ascii(at, 8, 4));
    else if (ascii(at, 4, 4) === "ftyp") await readMp4(r, info);
    else if (at[0] === 0xff && (at[1] & 0xf6) === 0xf0) { info.format = "AAC"; info.codec = "AAC (ADTS)"; }
    else await readMp3(r, start, info);
    if (info.format === "MP3" || info.format === "MP2" || info.format === "MP1") {
      id3v1(await r.read(r.size - 128, 128), info);
    }
    delete info.totalSamples;
    delete info._skipPictures;
    info.hasPicture = !!(info.picture || info.hasPicture);
    if (info.picture) delete info.picture.type;
    return info;
  }

  const api = { readTags, isMusicName, extension, numberPair, _internal: { flacPicture, mp3Header, extended80 } };
  root.IronbarkTags = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

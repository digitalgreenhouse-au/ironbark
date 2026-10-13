// The web player's tag reader, against files built here byte by byte.
// Run: node --test web/tests
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Tags = require("../js/tags.js");

// -- builders -------------------------------------------------------------------------------
// strings are written one byte per character (all ASCII here, except MP4's "©nam")
const enc = { encode: (s) => Uint8Array.from(s, (c) => c.charCodeAt(0)) };
const cat = (...parts) => {
  const arrs = parts.map((p) => (typeof p === "string" ? enc.encode(p) : p instanceof Uint8Array ? p : new Uint8Array(p)));
  const out = new Uint8Array(arrs.reduce((a, x) => a + x.length, 0));
  let o = 0;
  for (const a of arrs) { out.set(a, o); o += a.length; }
  return out;
};
const u32be = (n) => [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255];
const u24be = (n) => [(n >>> 16) & 255, (n >>> 8) & 255, n & 255];
const u16be = (n) => [(n >>> 8) & 255, n & 255];
const u32le = (n) => [n & 255, (n >>> 8) & 255, (n >>> 16) & 255, (n >>> 24) & 255];
const u16le = (n) => [n & 255, (n >>> 8) & 255];
const u64le = (n) => [...u32le(n % 4294967296), ...u32le(Math.floor(n / 4294967296))];
const syncsafe = (n) => [(n >> 21) & 127, (n >> 14) & 127, (n >> 7) & 127, n & 127];
const file = (bytes, name) => new File([bytes], name);
const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 4, 5, 0xff, 0xd9]);

function streamInfo({ rate, channels, bits, samples, md5 }) {
  const b = new Uint8Array(34);
  b.set(u16be(4096), 0); b.set(u16be(4096), 2);
  b[10] = (rate >> 12) & 255; b[11] = (rate >> 4) & 255;
  b[12] = ((rate & 15) << 4) | ((channels - 1) << 1) | (((bits - 1) >> 4) & 1);
  b[13] = (((bits - 1) & 15) << 4) | (Math.floor(samples / 4294967296) & 15);
  b.set(u32be(samples % 4294967296), 14);
  if (md5) b.set(md5, 18);
  return b;
}
function vorbisBlock(pairs) {
  const items = pairs.map(([k, v]) => enc.encode(k + "=" + v));
  return cat(u32le(6), "ironbk", u32le(items.length), ...items.flatMap((x) => [new Uint8Array(u32le(x.length)), x]));
}
function pictureBlock(type, mime, data) {
  return cat(u32be(type), u32be(mime.length), mime, u32be(0), u32be(1), u32be(1), u32be(24), u32be(0), u32be(data.length), data);
}
function flacBlock(type, body, last) { return cat([(last ? 0x80 : 0) | type, ...u24be(body.length)], body); }

// -- FLAC -----------------------------------------------------------------------------------
test("FLAC: stream facts, Vorbis comments, front cover, MD5", async () => {
  const md5 = new Uint8Array(16).map((_, i) => i + 1);
  const bytes = cat("fLaC",
    flacBlock(0, streamInfo({ rate: 96000, channels: 2, bits: 24, samples: 96000 * 10, md5 })),
    flacBlock(4, vorbisBlock([["TITLE", "Blue Train"], ["ARTIST", "John Coltrane"], ["ALBUM", "Blue Train"],
      ["ALBUMARTIST", "John Coltrane"], ["TRACKNUMBER", "1/5"], ["DISCNUMBER", "1"], ["DISCTOTAL", "2"], ["DATE", "1957"], ["genre", "Jazz"]])),
    flacBlock(6, pictureBlock(4, "image/png", new Uint8Array([0x89, 0x50, 1]))),
    flacBlock(6, pictureBlock(3, "image/jpeg", JPEG), true),
    new Uint8Array(1000));
  const t = await Tags.readTags(file(bytes, "01.flac"));
  assert.equal(t.format, "FLAC");
  assert.equal(t.sampleRate, 96000);
  assert.equal(t.channels, 2);
  assert.equal(t.bitsPerSample, 24);
  assert.equal(t.duration, 10);
  assert.equal(t.md5, "0102030405060708090a0b0c0d0e0f10");
  assert.equal(t.title, "Blue Train");
  assert.equal(t.artist, "John Coltrane");
  assert.equal(t.albumArtist, "John Coltrane");
  assert.equal(t.track, 1);
  assert.equal(t.trackTotal, 5);
  assert.equal(t.disc, 1);
  assert.equal(t.discTotal, 2);
  assert.equal(t.date, "1957");
  assert.equal(t.genre, "Jazz");
  assert.equal(t.picture.mime, "image/jpeg");          // the front cover wins over the other picture
  assert.deepEqual(Array.from(t.picture.data), Array.from(JPEG));
});

test("FLAC: 44.1 kHz 16-bit mono, no MD5, ID3 tag in front", async () => {
  const id3 = cat("ID3", [3, 0, 0], syncsafe(0));
  const bytes = cat(id3, "fLaC", flacBlock(0, streamInfo({ rate: 44100, channels: 1, bits: 16, samples: 44100 * 3 }), true));
  const t = await Tags.readTags(file(bytes, "x.flac"));
  assert.equal(t.sampleRate, 44100);
  assert.equal(t.channels, 1);
  assert.equal(t.bitsPerSample, 16);
  assert.equal(t.duration, 3);
  assert.equal(t.md5, null);
  assert.equal(t.title, null);
});

// -- MP3 ------------------------------------------------------------------------------------
function id3Frame(id, body) { return cat(id, u32be(body.length), [0, 0], body); }
function mp3Frames(n, xingFrames) {
  // MPEG-1 Layer III, 128 kbps, 44.1 kHz, joint stereo: 417 bytes a frame
  const frames = [];
  for (let i = 0; i < n; i++) {
    const f = new Uint8Array(417);
    f.set([0xff, 0xfb, 0x90, 0x64]);
    if (i === 0 && xingFrames) f.set(cat("Xing", u32be(1), u32be(xingFrames)), 4 + 32);
    frames.push(f);
  }
  return cat(...frames);
}

test("MP3: ID3v2.3 text, UTF-16 title, APIC, Xing frame count", async () => {
  const utf16 = cat([1, 0xff, 0xfe], new Uint8Array(new Uint16Array(Array.from("Café", (c) => c.charCodeAt(0))).buffer));
  const body = cat(
    id3Frame("TIT2", utf16),
    id3Frame("TPE1", cat([0], "Miles Davis")),
    id3Frame("TALB", cat([3], "Kind of Blue")),
    id3Frame("TRCK", cat([0], "2/5")),
    id3Frame("TCON", cat([0], "(8)")),
    id3Frame("APIC", cat([0], "image/jpeg", [0], [3], "cover", [0], JPEG)),
    new Uint8Array(20));
  const bytes = cat("ID3", [3, 0, 0], syncsafe(body.length), body, mp3Frames(3, 100));
  const t = await Tags.readTags(file(bytes, "a.mp3"));
  assert.equal(t.format, "MP3");
  assert.equal(t.codec, "MPEG-1 Layer III");
  assert.equal(t.title, "Café");
  assert.equal(t.artist, "Miles Davis");
  assert.equal(t.album, "Kind of Blue");
  assert.equal(t.track, 2);
  assert.equal(t.trackTotal, 5);
  assert.equal(t.genre, "Jazz");
  assert.equal(t.sampleRate, 44100);
  assert.equal(t.channels, 2);
  assert.ok(Math.abs(t.duration - (100 * 1152) / 44100) < 1e-9);
  assert.equal(t.picture.mime, "image/jpeg");
});

test("MP3: no Xing header, constant bit rate, ID3v1 at the end", async () => {
  const v1 = new Uint8Array(128);
  v1.set(enc.encode("TAG"));
  v1.set(enc.encode("Old Song"), 3);
  v1.set(enc.encode("Old Band"), 33);
  v1[126] = 7;
  const bytes = cat(mp3Frames(10), v1);
  const t = await Tags.readTags(file(bytes, "b.mp3"));
  assert.equal(t.title, "Old Song");
  assert.equal(t.artist, "Old Band");
  assert.equal(t.track, 7);
  assert.equal(t.bitrate, 128);
  assert.ok(Math.abs(t.duration - (4170 * 8) / 128000) < 1e-9);
});

test("ID3v2.4: sync-safe frame sizes and several values", async () => {
  const frame = (id, b) => cat(id, syncsafe(b.length), [0, 0], b);
  const body = cat(frame("TIT2", cat([3], "One")), frame("TPE1", cat([3], "A\0B")), frame("TDRC", cat([3], "2001-05-02")));
  const bytes = cat("ID3", [4, 0, 0], syncsafe(body.length), body, mp3Frames(3));
  const t = await Tags.readTags(file(bytes, "c.mp3"));
  assert.equal(t.title, "One");
  assert.equal(t.artist, "A, B");
  assert.equal(t.date, "2001-05-02");
});

// -- Ogg ------------------------------------------------------------------------------------
function oggPage(serial, seq, granule, packet, flags = 0) {
  const lacing = [];
  let n = packet.length;
  while (n >= 255) { lacing.push(255); n -= 255; }
  lacing.push(n);
  return cat("OggS", [0, flags], u64le(granule), u32le(serial), u32le(seq), u32le(0), [lacing.length], lacing, packet);
}

test("Opus: head, tags, length from the last page less the pre-skip", async () => {
  const head = cat("OpusHead", [1, 2], u16le(312), u32le(44100), [0, 0, 0]);
  const tags = cat("OpusTags", vorbisBlock([["TITLE", "Opus song"], ["ARTIST", "Band"], ["TRACKNUMBER", "4"]]));
  const bytes = cat(oggPage(7, 0, 0, head, 2), oggPage(7, 1, 0, tags), oggPage(7, 2, 48000 * 5 + 312, new Uint8Array(300), 4));
  const t = await Tags.readTags(file(bytes, "d.opus"));
  assert.equal(t.format, "Opus");
  assert.equal(t.channels, 2);
  assert.equal(t.sampleRate, 44100);
  assert.equal(t.title, "Opus song");
  assert.equal(t.track, 4);
  assert.equal(t.duration, 5);
});

test("Ogg Vorbis: a comment packet spanning two pages, with a cover", async () => {
  const id = cat([1], "vorbis", u32le(0), [2], u32le(48000), u32le(0), u32le(192000), u32le(0), [0xb8, 1]);
  const pic = Buffer.from(pictureBlock(3, "image/jpeg", JPEG)).toString("base64");
  const comments = cat([3], "vorbis", vorbisBlock([["TITLE", "Long"], ["COMMENT", "x".repeat(400)], ["METADATA_BLOCK_PICTURE", pic]]), [1]);
  // split the comment packet over two pages: the first carries 255-byte segments only
  const part1 = comments.subarray(0, 255 * 2), part2 = comments.subarray(255 * 2);
  const page2 = cat("OggS", [0, 0], u64le(0), u32le(9), u32le(1), u32le(0), [2], [255, 255], part1);
  const page3 = oggPage(9, 2, 0, part2, 1);
  const bytes = cat(oggPage(9, 0, 0, id, 2), page2, page3, oggPage(9, 3, 48000 * 2, new Uint8Array(100), 4));
  const t = await Tags.readTags(file(bytes, "e.ogg"));
  assert.equal(t.format, "Ogg Vorbis");
  assert.equal(t.sampleRate, 48000);
  assert.equal(t.bitrate, 192);
  assert.equal(t.title, "Long");
  assert.equal(t.duration, 2);
  assert.equal(t.picture.mime, "image/jpeg");
});

// -- WAV / AIFF -----------------------------------------------------------------------------
test("WAV: fmt, LIST INFO and length", async () => {
  const fmt = cat(u16le(1), u16le(2), u32le(44100), u32le(44100 * 4), u16le(4), u16le(16));
  const info = cat("INFO", "INAM", u32le(6), "Hello\0", "IART", u32le(5), "Them\0", [0]);
  const data = new Uint8Array(44100 * 4 * 2);
  const body = cat("WAVE", "fmt ", u32le(fmt.length), fmt, "LIST", u32le(info.length), info, "data", u32le(data.length), data);
  const t = await Tags.readTags(file(cat("RIFF", u32le(body.length), body), "f.wav"));
  assert.equal(t.format, "WAV");
  assert.equal(t.codec, "PCM");
  assert.equal(t.sampleRate, 44100);
  assert.equal(t.bitsPerSample, 16);
  assert.equal(t.duration, 2);
  assert.equal(t.title, "Hello");
  assert.equal(t.artist, "Them");
});

test("AIFF: COMM chunk with its 80-bit sample rate", async () => {
  const rate = [0x40, 0x0e, 0xac, 0x44, 0, 0, 0, 0, 0, 0];   // 44100
  const comm = cat(u16be(2), u32be(44100 * 3), u16be(24), rate);
  const body = cat("AIFF", "COMM", u32be(comm.length), comm, "NAME", u32be(4), "Song", "SSND", u32be(8), new Uint8Array(8));
  const t = await Tags.readTags(file(cat("FORM", u32be(body.length), body), "g.aiff"));
  assert.equal(t.format, "AIFF");
  assert.equal(t.sampleRate, 44100);
  assert.equal(t.bitsPerSample, 24);
  assert.equal(t.duration, 3);
  assert.equal(t.title, "Song");
});

// -- MP4 ------------------------------------------------------------------------------------
const box = (type, ...kids) => { const body = cat(...kids); return cat(u32be(body.length + 8), type, body); };
const full = (type, ...kids) => box(type, [0, 0, 0, 0], ...kids);

test("M4A (ALAC): the alac box's rate and depth, ilst text, numbers and cover", async () => {
  const alacCfg = full("alac", u32be(4096), [0, 24, 40, 10, 14, 2], u16be(255), u32be(0), u32be(0), u32be(96000));
  const entry = box("alac", new Uint8Array(6), u16be(1), new Uint8Array(8), u16be(2), u16be(16), u16be(0), u16be(0), u32be(44100 << 16 >>> 0), alacCfg);
  const stsd = full("stsd", u32be(1), entry);
  const mdhd = full("mdhd", u32be(0), u32be(0), u32be(96000), u32be(96000 * 7), u16be(0), u16be(0));
  const hdlr = full("hdlr", u32be(0), "soun", new Uint8Array(12), [0]);
  const trak = box("trak", box("mdia", mdhd, hdlr, box("minf", box("stbl", stsd))));
  const data = (type, payload) => box("data", u32be(type), u32be(0), payload);
  const ilst = box("ilst",
    box("©nam", data(1, new TextEncoder().encode("Apple song"))),
    box("trkn", data(0, new Uint8Array([0, 0, 0, 3, 0, 9, 0, 0]))),
    box("covr", data(13, JPEG)));
  const meta = full("meta", box("hdlr", new Uint8Array(25)), ilst);
  const moov = box("moov", full("mvhd", u32be(0), u32be(0), u32be(1000), u32be(7000)), trak, box("udta", meta));
  const bytes = cat(box("ftyp", "M4A ", u32be(0), "M4A "), moov, box("mdat", new Uint8Array(100)));
  const t = await Tags.readTags(file(bytes, "h.m4a"));
  assert.equal(t.format, "ALAC");
  assert.equal(t.codec, "ALAC");
  assert.equal(t.sampleRate, 96000);
  assert.equal(t.bitsPerSample, 24);
  assert.equal(t.channels, 2);
  assert.equal(t.duration, 7);
  assert.equal(t.title, "Apple song");
  assert.equal(t.track, 3);
  assert.equal(t.trackTotal, 9);
  assert.equal(t.picture.mime, "image/jpeg");
});

test("M4A (AAC): no bit depth, rate from the sample entry", async () => {
  const entry = box("mp4a", new Uint8Array(6), u16be(1), new Uint8Array(8), u16be(2), u16be(16), u16be(0), u16be(0), u32be((48000 << 16) >>> 0));
  const hdlr = full("hdlr", u32be(0), "soun", new Uint8Array(12), [0]);
  const mdhd = full("mdhd", u32be(0), u32be(0), u32be(48000), u32be(48000 * 4), u16be(0), u16be(0));
  const trak = box("trak", box("mdia", mdhd, hdlr, box("minf", box("stbl", full("stsd", u32be(1), entry)))));
  const bytes = cat(box("ftyp", "M4A ", u32be(0)), box("mdat", new Uint8Array(64)), box("moov", trak));   // moov after the audio
  const t = await Tags.readTags(file(bytes, "i.m4a"));
  assert.equal(t.format, "AAC");
  assert.equal(t.sampleRate, 48000);
  assert.equal(t.bitsPerSample, null);
  assert.equal(t.duration, 4);
});

test("names: which files count as music", () => {
  assert.ok(Tags.isMusicName("a/b/Song.FLAC"));
  assert.ok(Tags.isMusicName("x.opus"));
  assert.ok(!Tags.isMusicName("cover.jpg"));
  assert.ok(!Tags.isMusicName("notes.txt"));
  assert.deepEqual(Tags.numberPair(" 3 / 12"), [3, 12]);
  assert.deepEqual(Tags.numberPair("A1"), [null, null]);
});

test("damaged and empty files give an answer, not an error", async () => {
  for (const bytes of [new Uint8Array(0), cat("fLaC"), cat("fLaC", [0, 0, 0, 34], [1, 2]), cat("OggS", [0]), cat("RIFF", u32le(4), "WAVE"), new Uint8Array(500)]) {
    const t = await Tags.readTags(file(bytes, "bad.flac"));
    assert.equal(typeof t, "object");
  }
});

test("big blocks past the read-ahead chunk, and skipping pictures for a quick library read", async () => {
  const big = new Uint8Array(300 * 1024).map((_, i) => i & 255);
  big.set([0xff, 0xd8], 0);
  const bytes = cat("fLaC",
    flacBlock(0, streamInfo({ rate: 48000, channels: 2, bits: 16, samples: 48000 })),
    flacBlock(6, pictureBlock(3, "image/jpeg", big)),
    flacBlock(4, vorbisBlock([["TITLE", "After the picture"]]), true),
    new Uint8Array(200 * 1024));
  const full = await Tags.readTags(file(bytes, "big.flac"));
  assert.equal(full.title, "After the picture");
  assert.equal(full.picture.data.length, big.length);
  assert.equal(full.picture.data[1000], 1000 & 255);
  assert.equal(full.hasPicture, true);
  const quick = await Tags.readTags(file(bytes, "big.flac"), { pictures: false });
  assert.equal(quick.title, "After the picture");
  assert.equal(quick.picture, null);
  assert.equal(quick.hasPicture, true);
  assert.equal(quick.sampleRate, 48000);
});

test("ID3: an empty frame doesn't end the tag; genre names that start with digits stay whole", async () => {
  const body = cat(
    id3Frame("TIT2", cat([0], "Title")), id3Frame("TIT3", new Uint8Array(0)),
    id3Frame("TALB", cat([0], "Album")), id3Frame("TPE1", cat([0], "Artist")), id3Frame("TCON", cat([0], "80s")));
  const t = await Tags.readTags(file(cat("ID3", [3, 0, 0], syncsafe(body.length), body, mp3Frames(3)), "e.mp3"));
  assert.equal(t.title, "Title");
  assert.equal(t.album, "Album");
  assert.equal(t.artist, "Artist");
  assert.equal(t.genre, "80s");
  for (const [raw, want] of [["2 Tone", "2 Tone"], ["(17)", "Rock"], ["(17)Rock & Roll", "Rock & Roll"], ["17", "Rock"]]) {
    const b = id3Frame("TCON", cat([0], raw));
    const g = await Tags.readTags(file(cat("ID3", [3, 0, 0], syncsafe(b.length), b, mp3Frames(3)), "g.mp3"));
    assert.equal(g.genre, want, raw);
  }
});

test("WAV: INFO text in the Windows code page as well as UTF-8", async () => {
  const fmt = cat(u16le(1), u16le(2), u32le(44100), u32le(44100 * 4), u16le(4), u16le(16));
  const cafe1252 = new Uint8Array([0x43, 0x61, 0x66, 0xe9, 0]);            // "Café" in cp1252
  const utf = new TextEncoder().encode("Ünïcode\0");
  const info = cat("INFO", "INAM", u32le(cafe1252.length), cafe1252, [0], "IART", u32le(utf.length), utf);
  const body = cat("WAVE", "fmt ", u32le(fmt.length), fmt, "LIST", u32le(info.length), info, "data", u32le(4), new Uint8Array(4));
  const t = await Tags.readTags(file(cat("RIFF", u32le(body.length), body), "c.wav"));
  assert.equal(t.title, "Café");
  assert.equal(t.artist, "Ünïcode");
});

test("a file that isn't music has no format, rather than a guessed one", async () => {
  const t = await Tags.readTags(file(new Uint8Array(5000).fill(7), "junk.flac"));
  assert.equal(t.format, null);
});

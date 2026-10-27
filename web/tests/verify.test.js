// The web player's MD5 and the FLAC fingerprint of decoded samples.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { MD5, md5Hex, pcmMd5 } = require("../js/verify.js");

const nodeMd5 = (b) => crypto.createHash("md5").update(b).digest("hex");

test("MD5: the RFC 1321 test values", () => {
  const e = (s) => md5Hex(new TextEncoder().encode(s));
  assert.equal(e(""), "d41d8cd98f00b204e9800998ecf8427e");
  assert.equal(e("a"), "0cc175b9c0f1b6a831c399e269772661");
  assert.equal(e("abc"), "900150983cd24fb0d6963f7d28e17f72");
  assert.equal(e("message digest"), "f96b697d7cb7938d525a2f31aaf161d0");
  assert.equal(e("abcdefghijklmnopqrstuvwxyz"), "c3fcd3d76192e4007dfb496cca67e13b");
  assert.equal(e("12345678901234567890123456789012345678901234567890123456789012345678901234567890"), "57edf4a22be3c955ac49da2e2107b67a");
});

test("MD5: fed in odd-sized pieces, every length around the 64-byte blocks", () => {
  const data = crypto.randomBytes(1000);
  for (const n of [0, 1, 55, 56, 57, 63, 64, 65, 119, 120, 121, 128, 999, 1000]) {
    const bytes = new Uint8Array(data.subarray(0, n));
    const m = new MD5();
    for (let o = 0; o < n; o += 7) m.update(bytes.subarray(o, Math.min(n, o + 7)));
    assert.equal(m.hex(), nodeMd5(bytes), "length " + n);
  }
});

function samples(bits, frames, channels) {
  // whole-number samples, full scale both ends included, as the browser hands them over (÷ 2^(bits-1))
  const scale = 2 ** (bits - 1);
  const ints = Array.from({ length: channels }, (_, c) => Array.from({ length: frames }, (_, i) =>
    i === 0 ? scale - 1 : i === 1 ? -scale : Math.round(Math.sin(i * 0.1 + c) * (scale - 1) * 0.7)));
  const floats = ints.map((ch) => Float32Array.from(ch, (v) => v / scale));
  return { ints, floats };
}

function packed(ints, bits) {
  const bytesPer = Math.ceil(bits / 8), frames = ints[0].length;
  const out = Buffer.alloc(frames * ints.length * bytesPer);
  let o = 0;
  for (let f = 0; f < frames; f++) for (const ch of ints) { out.writeIntLE(ch[f], o, bytesPer); o += bytesPer; }
  return out;
}

for (const bits of [8, 16, 24]) {
  test(`decoded ${bits}-bit samples give the encoder's MD5`, () => {
    const { ints, floats } = samples(bits, 40000, 2);
    const got = pcmMd5(floats, bits);
    assert.equal(got.offGrid, 0);
    assert.equal(got.md5, nodeMd5(packed(ints, bits)));
  });
}

test("a changed sample changes the MD5, and off-grid samples are counted", () => {
  const { ints, floats } = samples(16, 5000, 1);
  const good = nodeMd5(packed(ints, 16));
  floats[0][100] += 0.3 / 32768;
  const got = pcmMd5(floats, 16);
  assert.equal(got.offGrid, 1);
  assert.equal(got.md5, good);              // rounded back: same sample, but counted as off-grid
  floats[0][100] += 1 / 32768;
  assert.notEqual(pcmMd5(floats, 16).md5, good);
});

// (16-bit only: that's where Chromium does it; it decodes 24-bit FLAC exactly, and a float32 couldn't
// carry a 24-bit sample through that division anyway)
for (const bits of [8, 16]) {
  test(`${bits}-bit: a decoder that divides the positive half by 2^(bits-1) - 1, as Chromium's does, still gives the encoder's MD5`, () => {
    const { ints } = samples(bits, 40000, 2);
    const scale = 2 ** (bits - 1);
    // loud: full scale at both ends, so plain rounding at 2^(bits-1) would land a whole step off
    const floats = ints.map((ch) => Float32Array.from(ch, (v) => (v > 0 ? v / (scale - 1) : v / scale)));
    const got = pcmMd5(floats, bits);
    assert.equal(got.md5, nodeMd5(packed(ints, bits)));
    assert.equal(got.offGrid, 0);              // nothing was changed, so nothing is blamed on the decoder
  });
}

test("a decoder that really changed the audio is still caught, whichever scale is tried", () => {
  const { ints, floats } = samples(16, 5000, 1);
  const good = nodeMd5(packed(ints, 16));
  const changed = floats.map((ch) => Float32Array.from(ch, (v, i) => v + (i % 3 ? 0.37 : -0.41) / 32768));
  const got = pcmMd5(changed, 16);
  assert.ok(got.offGrid > 4000);
  assert.equal(got.md5, good);              // rounded back, but every changed sample is counted
});

test("a file too long to decode in memory is refused politely, before any decoding", async () => {
  const { verifyFlac } = require("../js/verify.js");
  const tags = { md5: "00".repeat(16), duration: 3600, sampleRate: 192000, channels: 2, bitsPerSample: 24 };
  const r = await verifyFlac(new File([new Uint8Array(4)], "long.flac"), tags);
  assert.equal(r.ok, null);
  assert.match(r.verdict, /Too long/);
  const none = await verifyFlac(new File([new Uint8Array(4)], "x.flac"), { md5: null });
  assert.equal(none.ok, null);
  assert.match(none.verdict, /No fingerprint/);
});

test("a file of unknown length is judged by its size, so a huge one is still refused before decoding", async () => {
  const { verifyFlac } = require("../js/verify.js");
  // a stand-in for a 200 MB file: only its size is read before the refusal
  const big = { size: 200 * 1048576, arrayBuffer: () => { throw new Error("read the whole file"); } };
  const r = await verifyFlac(big, { md5: "00".repeat(16), sampleRate: 44100, channels: 2, bitsPerSample: 16 });
  assert.equal(r.ok, null);
  assert.match(r.verdict, /Too long/);
});

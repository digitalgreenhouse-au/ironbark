// The web player's Apple Lossless decoder: the WAV it writes, and what it refuses. (Decoding
// real ALAC files is checked from tests/test_web_alac.py, which can make them.)
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Alac = require("../js/alac.js");
const { writeWavHeader, writeFrames, unpcBlock } = Alac._internal;

const ascii = (b, o, n) => String.fromCharCode(...b.subarray(o, o + n));

test("WAV header: plain PCM, little-endian, 24-bit samples in 3 bytes, a pad byte counted", () => {
  const wav = new Uint8Array(44 + 3 * 3 + 1);
  writeWavHeader(wav, 3, 1, 24, 96000);
  const v = new DataView(wav.buffer);
  assert.equal(ascii(wav, 0, 4), "RIFF");
  assert.equal(v.getUint32(4, true), 36 + 9 + 1);   // the odd-sized data chunk's pad byte
  assert.equal(ascii(wav, 8, 8), "WAVEfmt ");
  assert.deepEqual([v.getUint16(20, true), v.getUint16(22, true), v.getUint32(24, true)], [1, 1, 96000]);
  assert.deepEqual([v.getUint32(28, true), v.getUint16(32, true), v.getUint16(34, true)], [96000 * 3, 3, 24]);
  assert.equal(ascii(wav, 36, 4), "data");
  assert.equal(v.getUint32(40, true), 9);
});

test("WAV samples: interleaved, 16-bit in 2 bytes, 20-bit left-justified in 3", () => {
  const out = new Uint8Array(8);
  writeFrames(out, 0, [Int32Array.of(1, -32768), Int32Array.of(-1, 32767)], 2, 16);
  assert.deepEqual([...out], [1, 0, 255, 255, 0, 128, 255, 127]);
  const out20 = new Uint8Array(6);
  writeFrames(out20, 0, [Int32Array.of(0x7ffff, -0x80000)], 2, 20);
  assert.deepEqual([...out20], [0xf0, 0xff, 0x7f, 0x00, 0x00, 0x80]);
});

test("the predictor's first-difference mode (31) runs in place", () => {
  const pc = Int32Array.of(5, 1, 1, -3, 0);
  unpcBlock(pc, pc, 5, null, 31, 16, 0);
  assert.deepEqual([...pc], [5, 6, 7, 4, 4]);
});

test("not an MP4 file, and an MP4 without an index: refused, as NotAlac", async () => {
  await assert.rejects(Alac.decodeToWav(new Blob([new Uint8Array(64)])), { name: "NotAlac", message: "not an MP4 file" });
  const ftyp = Uint8Array.of(0, 0, 0, 16, ...Buffer.from("ftypM4A "), 0, 0, 0, 0);
  await assert.rejects(Alac.readIndex(new Blob([ftyp])), { name: "NotAlac", message: "no index (moov) in this MP4 file" });
});

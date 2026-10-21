"""FLAC at bit depths the decoder doesn't take (12, 20 and 32-bit are valid FLAC; libsndfile opens
only 8, 16 and 24): listed as "can't be played", not offered for playing, not checked as damaged —
already-stored songs included. And FLAC's MD5 bytes for any depth (RFC 9639 §8.2: each sample's
value sign-extended to whole bytes).

The FLAC files here are written by hand (one frame of verbatim samples), so what the decoder makes
of each depth is measured, not assumed."""
from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from ironbark import analysis, library, library_db
from ironbark.library import Track
from ironbark.library_db import LibraryDB

RATE = 44100
SAMPLES = 16


def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class _Bits:
    def __init__(self):
        self.value, self.count = 0, 0

    def put(self, value: int, width: int):
        self.value = (self.value << width) | (value & ((1 << width) - 1))
        self.count += width

    def bytes(self) -> bytes:
        pad = -self.count % 8
        return (self.value << pad).to_bytes((self.count + pad) // 8, "big")


def flac_bytes(bits: int, samples: list[int]) -> bytes:
    """A valid mono FLAC: STREAMINFO (with the MD5 of the samples) and one frame of verbatim samples."""
    md5 = hashlib.md5(b"".join(int(s).to_bytes((bits + 7) // 8, "little", signed=True) for s in samples)).digest()
    info = _Bits()
    for value, width in ((len(samples), 16), (len(samples), 16), (0, 24), (0, 24), (RATE, 20), (0, 3),
                         (bits - 1, 5), (len(samples), 36)):
        info.put(value, width)
    streaminfo = info.bytes() + md5
    header = _Bits()
    for value, width in ((0b11111111111110, 14), (0, 1), (0, 1),   # sync, reserved, fixed block size
                         (0b0110, 4), (0b0000, 4),                 # block size: 8 bits at the end; rate: STREAMINFO's
                         (0b0000, 4), (0b000, 3), (0, 1),          # mono; depth: STREAMINFO's; reserved
                         (0, 8), (len(samples) - 1, 8)):           # frame 0 (UTF-8 coded); block size - 1
        header.put(value, width)
    head = header.bytes()
    body = _Bits()
    body.put(0, 1)
    body.put(0b000001, 6)   # verbatim
    body.put(0, 1)          # no wasted bits
    for sample in samples:
        body.put(sample, bits)
    frame = head + bytes([_crc8(head)]) + body.bytes()
    frame += struct.pack(">H", _crc16(frame))
    return b"fLaC" + bytes([0x80, 0, 0, 34]) + streaminfo + frame


def samples_for(bits: int) -> list[int]:
    top = (1 << (bits - 1)) - 1
    return [0, 1, -1, top, -top - 1, top // 3, -(top // 5), 7, -8, 2, -2, 100, -100, top // 2, 5, -5][:SAMPLES]


@pytest.fixture
def flac(tmp_path):
    def make(bits: int) -> Path:
        path = tmp_path / f"{bits}-bit.flac"
        path.write_bytes(flac_bytes(bits, samples_for(bits)))
        return path
    return make


@pytest.mark.parametrize("bits", [8, 16, 24])
def test_the_depths_the_decoder_takes_play_and_are_listed_as_usual(flac, bits):
    path = flac(bits)
    with sf.SoundFile(str(path)) as f:   # (decodes: so the hand-made files are real FLAC)
        decoded = f.read(dtype="int32")
    assert [int(v) >> (32 - bits) for v in decoded] == samples_for(bits)
    track = library.read_track(path)
    assert track.codec == "FLAC" and track.bits == bits and not track.unsupported


@pytest.mark.parametrize("bits", [12, 20, 32])
def test_other_depths_are_valid_flac_the_decoder_refuses_and_are_listed_as_unplayable(flac, bits):
    path = flac(bits)
    with pytest.raises(RuntimeError, match="unimplemented format"):
        sf.SoundFile(str(path))   # (if the decoder ever takes them, widen library.FLAC_DECODER_BITS)
    track = library.read_track(path)
    assert track.codec == "FLAC" and track.bits == bits and not track.unreadable
    assert track.unsupported.startswith(f"{bits}-bit FLAC can't be played")
    assert "can't be played" in track.format_line
    assert dict(track.details)["Warning"].startswith("can't be played")


def test_songs_stored_before_are_marked_when_the_library_opens(tmp_path):
    root = Path("C:/Music") if os.name == "nt" else Path("/music")
    folder = root / "Hi-res"
    tracks = [Track(path=folder / f"0{n} {name}.flac", title=name, track_no=n, codec="FLAC", sample_rate=96000,
                    bits=bits) for n, (name, bits) in enumerate((("Twenty", 20), ("Sixteen", 16)), start=1)]
    listing = {t.path.name: (1000 + i, 5000 + i, 1.0e9) for i, t in enumerate(tracks)}
    db = LibraryDB.open(tmp_path / "library.db", [root])
    db.apply(library_db.folder_result(root, folder, listing, {t.path.name: [t] for t in tracks}))
    db._con().execute("UPDATE tracks SET unsupported = '', details = NULL")   # (as an older version stored them)
    db.close()

    db = LibraryDB.open(tmp_path / "library.db", [root])
    try:
        rows = {row[0]: row[1:] for row in db._con().execute("SELECT title, unsupported, details FROM tracks")}
        assert rows["Twenty"][0].startswith("20-bit FLAC can't be played") and "Warning" in rows["Twenty"][1]
        assert rows["Sixteen"] == ("", None)
        checked = {os.path.basename(str(row[0])): row[6] for row in db.check_list()}
        assert checked["01 Twenty.flac"] and not checked["02 Sixteen.flac"]   # (the check leaves it out)
    finally:
        db.close()


@pytest.mark.parametrize("bits", [8, 12, 16, 20, 24, 32])
def test_the_md5_bytes_are_each_value_sign_extended_to_whole_bytes(bits):
    values = samples_for(bits)
    left_justified = np.array([[v << (32 - bits)] for v in values], dtype=np.int64).astype(np.int32)
    expected = b"".join(v.to_bytes((bits + 7) // 8, "little", signed=True) for v in values)
    assert analysis.pcm_bytes(left_justified, bits) == expected


def test_the_md5_of_a_file_the_decoder_takes_still_matches_its_own(flac):
    for bits in (16, 24):
        path = flac(bits)
        with sf.SoundFile(str(path)) as f:
            block = f.read(dtype="int32", always_2d=True)
        stored = library.read_track(path).md5
        assert int(hashlib.md5(analysis.pcm_bytes(block, bits)).hexdigest(), 16) == stored

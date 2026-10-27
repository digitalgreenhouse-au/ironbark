"""MP4 (.m4a) layouts that are legal but less common: a moov whose size is written in 64 bits (a
16-byte header), a moov whose size is 0 (it runs to the end of the file), a QuickTime version-2
sound entry, and AAC above 65,535 Hz (the entry's 16.16 rate field can't hold it: 0)."""

from __future__ import annotations

import struct

from ironbark.tagio.mp4 import MP4File, box


def _full(kind: bytes, body: bytes) -> bytes:
    return box(kind, bytes(4) + body)


def _ilst(title: str) -> bytes:
    return box(b"ilst", box(b"\xa9nam", box(b"data", struct.pack(">II", 1, 0) + title.encode())))


def _udta(title: str) -> bytes:
    hdlr = _full(b"hdlr", bytes(4) + b"mdir" + b"appl" + bytes(9))
    return box(b"udta", _full(b"meta", hdlr + _ilst(title)))


def _descriptor(tag: int, body: bytes) -> bytes:
    return bytes([tag, len(body)]) + body


def _esds(config: bytes) -> bytes:
    decoder = bytes([0x40, 0x15, 0, 0, 0]) + struct.pack(">II", 0, 256000) + _descriptor(5, config)
    return _full(b"esds", _descriptor(3, struct.pack(">HB", 1, 0) + _descriptor(4, decoder)))


def _entry(version: int, channels: int, bits: int, rate_field: int, extra: bytes = b"") -> bytes:
    body = bytes(6) + struct.pack(">H", 1) + struct.pack(">HH", version, 0) + bytes(4)
    body += struct.pack(">HHHHI", channels, bits, 0, 0, rate_field)
    if version == 2:   # sizeOfStructOnly, rate (float64), channels, 0x7F000000, bits, flags, bytes/packet, frames/packet
        body += struct.pack(">IdIIIIII", 72, 96000.0, 6, 0x7F000000, 0, 0, 0, 1024)
    return box(b"mp4a", body + extra)


def _trak(entry: bytes) -> bytes:
    hdlr = _full(b"hdlr", bytes(4) + b"soun" + bytes(13))
    mdhd = _full(b"mdhd", struct.pack(">IIII", 0, 0, 44100, 441000) + bytes(4))
    stbl = box(b"stbl", _full(b"stsd", struct.pack(">I", 1) + entry))
    return box(b"trak", box(b"mdia", mdhd + hdlr + box(b"minf", stbl)))


FTYP = box(b"ftyp", b"M4A \x00\x00\x00\x00M4A mp42isom")
PLAIN = _trak(_entry(0, 2, 16, 44100 << 16))


def test_a_moov_with_a_64_bit_size_has_its_tags_and_audio_facts_read_and_saved(tmp_path):
    inner = PLAIN + _udta("Original")
    path = tmp_path / "big.m4a"
    path.write_bytes(FTYP + struct.pack(">I4sQ", 1, b"moov", 16 + len(inner)) + inner + box(b"mdat", bytes(64)))
    song = MP4File(path)
    assert song.tags["\xa9nam"] == ["Original"]
    assert song.info.length == 10.0 and song.info.sample_rate == 44100 and song.info.channels == 2
    song["\xa9alb"] = ["New album"]
    song.save()
    again = MP4File(path)
    assert again.tags["\xa9nam"] == ["Original"] and again.tags["\xa9alb"] == ["New album"]
    raw = path.read_bytes()
    assert raw.count(b"udta") == 1 and raw.count(b"ilst") == 1   # (the tag it had, not a second one)


def test_a_moov_running_to_the_end_of_the_file_keeps_doing_so_when_its_tags_grow(tmp_path):
    path = tmp_path / "zero.m4a"
    path.write_bytes(FTYP + box(b"mdat", bytes(64)) + struct.pack(">I4s", 0, b"moov") + PLAIN + _udta("Short"))
    song = MP4File(path)
    assert song.tags["\xa9nam"] == ["Short"]
    song["\xa9nam"] = ["A much longer title than before, long enough to make the tags grow"]
    song.save()
    again = MP4File(path)
    assert again.tags["\xa9nam"] == ["A much longer title than before, long enough to make the tags grow"]
    assert struct.unpack_from(">I", path.read_bytes(), again.moov_start)[0] == 0
    assert again.moov_end == path.stat().st_size


def test_a_quicktime_version_2_entry_gives_its_real_rate_and_channels(tmp_path):
    path = tmp_path / "v2.m4a"
    path.write_bytes(FTYP + box(b"moov", _trak(_entry(2, 3, 16, 0x00010000))) + box(b"mdat", bytes(64)))
    info = MP4File(path).info
    assert info.sample_rate == 96000 and info.channels == 6


def test_aac_above_65535_hz_takes_its_rate_from_the_aac_config(tmp_path):
    lc_96k = bytes([0x10, 0x10])            # AAC LC, 96 kHz, stereo
    path = tmp_path / "aac96.m4a"
    path.write_bytes(FTYP + box(b"moov", _trak(_entry(0, 2, 16, 0, _esds(lc_96k)))) + box(b"mdat", bytes(64)))
    info = MP4File(path).info
    assert info.sample_rate == 96000 and info.codec_description == "AAC LC"


def test_he_aac_with_no_rate_in_its_entry_gives_the_rate_it_plays_at(tmp_path):
    he_48k = bytes([0x2B, 0x11, 0x80])     # HE-AAC: a 24 kHz core, played at 48 kHz
    path = tmp_path / "he.m4a"
    path.write_bytes(FTYP + box(b"moov", _trak(_entry(0, 2, 16, 0, _esds(he_48k)))) + box(b"mdat", bytes(64)))
    assert MP4File(path).info.sample_rate == 48000


def test_an_ordinary_entry_keeps_its_own_rate(tmp_path):
    path = tmp_path / "plain.m4a"
    lc_48k = bytes([0x11, 0x90])            # (the config says 48 kHz; the entry's 44.1 kHz stands)
    path.write_bytes(FTYP + box(b"moov", _trak(_entry(0, 2, 16, 44100 << 16, _esds(lc_48k)))) + box(b"mdat", bytes(64)))
    assert MP4File(path).info.sample_rate == 44100

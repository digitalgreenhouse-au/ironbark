"""A tag added to a WAV or AIFF goes in a new chunk at the end of the file — only when the file's
chunks lead there. One that claims more than the file holds (cut short, a WAV still being recorded
with its size left at 0xFFFFFFFF) or bytes after the chunks (an old ID3v1 "TAG" block) would leave the
new chunk inside the sound: the save is refused and the file left exactly as it was."""

from __future__ import annotations

import struct

import pytest
import soundfile as sf

from ironbark.tagio import id3
from ironbark.tagio._files import TagError
from ironbark.tagio.riff import AiffFile, WaveFile


def _wav(declared: int | None = None, frames: int = 1000, after: bytes = b"") -> bytes:
    pcm = b"\x00\x00" * frames                    # 16-bit mono silence
    fmt = struct.pack("<HHIIHH", 1, 1, 44100, 88200, 2, 16)
    data_len = len(pcm) if declared is None else declared
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", data_len) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body + after


def _titled(path, cls=WaveFile):
    f = cls(path)
    f.add_tags().add(id3.TextFrame("TIT2", ["My Title"]))
    f.save(3)
    return cls(path)


@pytest.mark.parametrize("declared", [1_000_000, 0xFFFFFFFF], ids=["cut-short", "still-recording"])
def test_a_wav_whose_sound_runs_past_its_end_isnt_given_a_tag_inside_the_sound(tmp_path, declared):
    path = tmp_path / "t.wav"
    path.write_bytes(_wav(declared))
    before = path.read_bytes()
    with pytest.raises(TagError, match="don't lead to the end"):
        _titled(path)
    assert path.read_bytes() == before


def test_a_wav_with_an_id3v1_block_after_its_chunks_isnt_changed(tmp_path):
    path = tmp_path / "v1.wav"
    path.write_bytes(_wav(after=(b"TAG" + b"Song".ljust(30, b"\0") + bytes(95))[:128]))
    before = path.read_bytes()
    with pytest.raises(TagError):
        _titled(path)
    assert path.read_bytes() == before


def test_a_whole_wav_still_takes_a_tag_and_keeps_its_sound(tmp_path):
    path = tmp_path / "ok.wav"
    path.write_bytes(_wav())
    after = _titled(path)
    assert after.tags is not None and after.tags.frames[0].text == ["My Title"]
    assert sf.info(str(path)).frames == 1000


def test_a_wav_missing_only_its_last_pad_byte_still_takes_a_tag(tmp_path):
    path = tmp_path / "odd.wav"
    pcm = b"\x01" * 1001                          # 8-bit mono, odd length: the pad byte is left off
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 1, 8)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    after = _titled(path)
    assert after.tags is not None
    assert sf.info(str(path)).frames == 1001


def test_an_aiff_cut_short_isnt_changed(tmp_path):
    comm = struct.pack(">hIh", 1, 1000, 16) + bytes.fromhex("400EAC44000000000000")   # 44.1 kHz, 80-bit
    ssnd = struct.pack(">II", 0, 0) + b"\x00\x00" * 1000
    body = (b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm
            + b"SSND" + struct.pack(">I", 1_000_000) + ssnd)
    path = tmp_path / "t.aiff"
    path.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)
    before = path.read_bytes()
    with pytest.raises(TagError):
        _titled(path, AiffFile)
    assert path.read_bytes() == before

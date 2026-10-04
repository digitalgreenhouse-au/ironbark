"""Map an MP3's frames to byte positions, for a live bitrate readout on VBR files.

Every MP3 frame starts with a 4-byte header that gives its bitrate, sample rate and
padding, and so its exact length — so we can walk the file frame to frame. Only
Layer III (MP3) is handled. A candidate frame is accepted only if the next frame
follows exactly where the header says it should, so stray sync bytes are ignored.
"""

from __future__ import annotations

import mmap
from pathlib import Path

import numpy as np

from .flacframes import FrameIndex

# kbps by bitrate index, Layer III
_KBPS_MPEG1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_KBPS_MPEG2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
# sample rates by the 2-bit version field: 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5
_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _frame(data, pos: int, size: int):
    """(sample rate, samples in frame, frame length) if a Layer III frame header is at pos."""
    if pos + 4 > size or data[pos] != 0xFF or data[pos + 1] & 0xE0 != 0xE0:
        return None
    header = int.from_bytes(data[pos:pos + 4], "big")
    version = (header >> 19) & 3
    layer = (header >> 17) & 3
    bitrate_index = (header >> 12) & 0xF
    rate_index = (header >> 10) & 3
    padding = (header >> 9) & 1
    if version == 1 or layer != 1 or bitrate_index in (0, 15) or rate_index == 3:
        return None  # reserved values, or not Layer III
    rate = _RATES[version][rate_index]
    kbps = (_KBPS_MPEG1 if version == 3 else _KBPS_MPEG2)[bitrate_index]
    samples = 1152 if version == 3 else 576
    length = (144 if version == 3 else 72) * kbps * 1000 // rate + padding
    return rate, samples, length


def _skip_id3v2(data) -> int:
    if data[:3] != b"ID3":
        return 0
    size = 0
    for b in data[6:10]:
        size = (size << 7) | (b & 0x7F)  # "synchsafe" integer: 7 bits per byte
    footer = 10 if data[5] & 0x10 else 0
    return 10 + size + footer


def build_index(path: str | Path) -> FrameIndex:
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        size = len(data)
        pos = _skip_id3v2(data)
        samples: list[int] = []
        offsets: list[int] = []
        total = 0
        sample_rate = 0
        while pos + 4 <= size:
            frame = _frame(data, pos, size)
            if frame is not None:
                rate, frame_samples, length = frame
                following = _frame(data, pos + length, size)
                # Accept when the next frame lines up, or this one ends exactly at the file end/tag.
                at_end = pos + length >= size - 128
                if (following is not None and following[0] == rate) or (at_end and samples):
                    if not sample_rate:
                        sample_rate = rate
                    if rate == sample_rate:
                        samples.append(total)
                        offsets.append(pos)
                        total += frame_samples
                        pos += length
                        continue
            next_sync = data.find(b"\xff", pos + 1)
            if next_sync < 0:
                break
            pos = next_sync
        samples.append(total)
        offsets.append(pos if offsets else size)
    return FrameIndex(np.array(samples, dtype=np.int64), np.array(offsets, dtype=np.int64), sample_rate or 44100)

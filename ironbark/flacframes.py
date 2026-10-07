"""Map a FLAC file's audio frames to byte positions, for a live bitrate readout.

FLAC compresses each frame (a few thousand samples) to a different size: quiet
passages shrink, dense ones don't. Knowing where every frame starts lets us say
how many kbps the music *right now* takes, like foobar2000's live bitrate.

A frame header starts with the sync code 0xFFF8/0xFFF9 and ends with a CRC-8, which
we check so stray 0xFFF8 bytes inside compressed audio aren't mistaken for frames.
"""

from __future__ import annotations

import mmap
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_CRC8 = []
for _byte in range(256):
    _crc = _byte
    for _ in range(8):
        _crc = ((_crc << 1) ^ 0x07) & 0xFF if _crc & 0x80 else (_crc << 1) & 0xFF
    _CRC8.append(_crc)


def crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC8[crc ^ b]
    return crc


@dataclass
class FrameIndex:
    samples: np.ndarray  # first sample of each frame, plus a final end-of-stream entry
    offsets: np.ndarray  # byte offset of each frame, plus end of audio data
    sample_rate: int

    def bitrate_at(self, seconds: float, window: float = 1.0) -> int | None:
        """Bits per second of the compressed audio around `seconds`."""
        if len(self.samples) < 2:
            return None
        centre = seconds * self.sample_rate
        half = window * self.sample_rate / 2
        lo = max(0, int(np.searchsorted(self.samples, centre - half, side="right")) - 1)
        hi = min(len(self.samples) - 1, int(np.searchsorted(self.samples, centre + half, side="left")))
        if hi <= lo:
            hi = min(lo + 1, len(self.samples) - 1)
        span = self.samples[hi] - self.samples[lo]
        if span <= 0:
            return None
        return int((self.offsets[hi] - self.offsets[lo]) * 8 * self.sample_rate / span)


def _audio_start(data) -> tuple[int, int, int]:
    """(offset of first frame, sample rate, total samples) from the fLaC metadata blocks."""
    pos = data.find(b"fLaC")
    if pos < 0:
        raise ValueError("not a FLAC file")
    pos += 4
    sample_rate = total = 0
    while True:
        header = data[pos]
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        if header & 0x7F == 0:  # STREAMINFO
            info = data[pos + 4:pos + 4 + 34]
            packed = int.from_bytes(info[10:18], "big")
            sample_rate = packed >> 44
            total = packed & ((1 << 36) - 1)
        pos += 4 + length
        if header & 0x80:  # last metadata block
            return pos, sample_rate, total


def _parse_header(data, pos: int, size: int):
    """Return (coded number, block size, header length) if a valid frame header is at pos."""
    if pos + 6 > size:
        return None
    b2, b3 = data[pos + 2], data[pos + 3]
    bs_code, sr_code = b2 >> 4, b2 & 0x0F
    if bs_code == 0 or sr_code == 0x0F or (b3 >> 4) > 10 or ((b3 >> 1) & 0x07) == 3 or b3 & 1:
        return None
    # UTF-8-style coded frame/sample number
    i = pos + 4
    first = data[i]
    if first < 0x80:
        n_extra, value = 0, first
    else:
        n_extra = 0
        mask = 0x40
        while first & mask and n_extra < 6:
            n_extra += 1
            mask >>= 1
        if n_extra == 0:
            return None
        value = first & (mask - 1)
    if i + 1 + n_extra > size:
        return None
    for k in range(1, n_extra + 1):
        cont = data[i + k]
        if cont & 0xC0 != 0x80:
            return None
        value = (value << 6) | (cont & 0x3F)
    i += 1 + n_extra
    if bs_code == 6:
        block = data[i] + 1
        i += 1
    elif bs_code == 7:
        block = int.from_bytes(data[i:i + 2], "big") + 1
        i += 2
    elif bs_code == 1:
        block = 192
    elif bs_code <= 5:
        block = 576 << (bs_code - 2)
    else:
        block = 256 << (bs_code - 8)
    if sr_code == 12:
        i += 1
    elif sr_code in (13, 14):
        i += 2
    if i >= size or crc8(data[pos:i]) != data[i]:
        return None
    return value, block, i + 1 - pos


MAX_HEADER = 16   # the longest frame header: sync, 2 bytes, a 7-byte number, block size, rate, CRC-8


class Scanner:
    """`build_index`, fed the file's bytes in order as they arrive (a download). It knows where
    the audio starts once the metadata is in, and the end sample of the last complete frame so far
    (a frame is complete once the next one's header has arrived). `finish(size)` after the whole
    file gives exactly what build_index gives. Once past the metadata only a few bytes are kept."""

    def __init__(self):
        self._buf = bytearray()
        self._base = 0               # file offset of _buf[0]
        self.first_frame: int | None = None
        self.sample_rate = 0
        self.total = 0
        self.samples: list[int] = []
        self.offsets: list[int] = []
        self._pos = 0                # next file offset to look for a frame at
        self._sync: bytes | None = None
        self._nominal = 0
        self._expected = 0
        self.fed = 0

    def feed(self, data: bytes) -> None:
        self._buf += data
        self.fed += len(data)
        if self.first_frame is None:
            try:
                start, rate, total = _audio_start(self._buf)
            except (IndexError, ValueError):
                return   # the metadata isn't all here yet
            if start > len(self._buf):
                return
            self.first_frame, self.sample_rate, self.total = start, rate, total
            self._pos = start
        self._scan(final=False)

    def _scan(self, final: bool) -> None:
        buf, base = self._buf, self._base
        size = len(buf)
        pos = self._pos - base
        while True:
            if self._sync is None:
                found = [x for x in (buf.find(b"\xff\xf8", pos), buf.find(b"\xff\xf9", pos)) if x >= 0]
                at = min(found) if found else -1
            else:
                at = buf.find(self._sync, pos)
            if at < 0:
                pos = size if final else max(pos, size - 1)   # (a sync code may straddle the end)
                break
            if not final and at + MAX_HEADER > size:
                pos = at   # its header isn't all here yet
                break
            parsed = _parse_header(buf, at, size)
            if parsed is None or (self._sync is not None and parsed[0] != self._expected):
                pos = at + 1   # sync bytes inside compressed audio, not a frame
                continue
            number, block, header_len = parsed
            if self._sync is None:
                self._sync = bytes(buf[at:at + 2])
                self._nominal = block
            variable = self._sync == b"\xff\xf9"
            self.samples.append(number if variable else number * self._nominal)
            self.offsets.append(base + at)
            self._expected = number + block if variable else number + 1
            pos = at + header_len + 1
        pos = min(pos, size)
        self._pos = base + pos
        if pos:
            del self._buf[:pos]
            self._base += pos

    def end_sample(self) -> int | None:
        """Where the last complete frame in what has arrived ends (None before the first)."""
        return self.samples[-1] if self.samples else None

    def byte_at(self, sample: int) -> int | None:
        """The start of the frame holding `sample`, once the scanner has seen that far."""
        if not self.samples or sample > self.samples[-1]:
            return None
        k = int(np.searchsorted(np.asarray(self.samples), sample, side="right")) - 1
        return self.offsets[max(0, k)]

    def finish(self, size: int) -> FrameIndex:
        """The whole file has been fed: the same index build_index makes."""
        if self.first_frame is None:
            raise ValueError("not a FLAC file")
        self._scan(final=True)
        samples, offsets = list(self.samples), list(self.offsets)
        end_sample = self.total or (samples[-1] + self._nominal if samples else 0)
        samples.append(end_sample)
        offsets.append(size)
        return FrameIndex(np.array(samples, dtype=np.int64), np.array(offsets, dtype=np.int64), self.sample_rate)


def build_index(path: str | Path) -> FrameIndex:
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        size = len(data)
        pos, sample_rate, total = _audio_start(data)
        samples: list[int] = []
        offsets: list[int] = []
        sync = None              # b"\xff\xf8" fixed block size, b"\xff\xf9" variable
        nominal_block = 0
        expected = 0             # the frame/sample number the next real frame must carry
        while True:
            if sync is None:
                found = [x for x in (data.find(b"\xff\xf8", pos), data.find(b"\xff\xf9", pos)) if x >= 0]
                pos = min(found) if found else -1
            else:
                pos = data.find(sync, pos)
            if pos < 0:
                break
            parsed = _parse_header(data, pos, size)
            if parsed is None or (sync is not None and parsed[0] != expected):
                pos += 1  # sync bytes inside compressed audio, not a frame
                continue
            number, block, header_len = parsed
            if sync is None:
                sync = bytes(data[pos:pos + 2])
                nominal_block = block
            variable = sync == b"\xff\xf9"
            samples.append(number if variable else number * nominal_block)
            offsets.append(pos)
            expected = number + block if variable else number + 1
            pos += header_len + 1
        end_sample = total or (samples[-1] + nominal_block if samples else 0)
        samples.append(end_sample)
        offsets.append(size)
    return FrameIndex(np.array(samples, dtype=np.int64), np.array(offsets, dtype=np.int64), sample_rate)

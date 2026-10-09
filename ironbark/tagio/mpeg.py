"""MP3 files: the audio facts (from the MPEG frame headers and the Xing / LAME header) and the
ID3 tags (id3.py). No UI.

Written from published descriptions (clean-room — no other tag library's code was read):
- The MPEG audio frame header, ISO/IEC 11172-3 / 13818-3 as tabulated at mp3-tech.org
  ("MPEG Audio Layer I/II/III frame header", read 05/10/2026): 11 sync bits; version (00 =
  MPEG-2.5, 10 = MPEG-2, 11 = MPEG-1); layer (01 = III, 10 = II, 11 = I); a protection bit (0 =
  a 16-bit CRC follows); bitrate index; sample-rate index; padding bit; private bit; channel mode
  (00 stereo, 01 joint stereo, 10 dual channel, 11 mono). A frame holds 384 samples (Layer I),
  1152 (Layer II, and Layer III at MPEG-1) or 576 (Layer III at MPEG-2/2.5), so its length in
  bytes is samples ÷ 8 × bitrate ÷ sample rate (+1 byte of padding; Layer I counts 4-byte slots).
- The Xing / "Info" header and the LAME extension, "Mp3 Info Tag rev 1 specifications"
  (gabriel.mp3-tech.org/mp3infotag.html, read 05/10/2026): inside the first frame, after the
  side information (MPEG-1: 32 bytes stereo / 17 mono; MPEG-2/2.5: 17 / 9), "Xing" (VBR) or
  "Info" (CBR), 4 flag bytes (1 frames, 2 bytes, 4 table of contents, 8 quality), the fields
  flagged; then the LAME extension: a 9-character encoder version ("LAME3.100"), revision + VBR
  method (low 4 bits: 1 CBR, 2 ABR, 3–6 VBR, 8 CBR 2-pass, 9 ABR 2-pass), lowpass, peak (32-bit
  float), radio ReplayGain (16 bits: 3 name, 3 originator, 1 sign, 9 value in 0.1 dB),
  audiophile gain, flags, ABR / minimum bitrate, encoder delay + padding (12 + 12 bits), …
- The Fraunhofer "VBRI" header (32 bytes after the frame header: "VBRI", version, delay,
  quality, bytes, frames — big-endian) — from the same mp3-tech.org pages; unverified against
  Fraunhofer's own document.

Length and bitrate: with a Xing / VBRI frame count, length = frames × samples per frame ÷ rate
and the average bitrate = the audio bytes it reports (less the header frame itself) × 8 ÷
length. Without one, the first frame's bitrate is taken as constant and the length worked out
from the file's size. (The same figures the player showed before 0.27.0, checked on a real
library.)
"""

from __future__ import annotations

import struct
from pathlib import Path

from . import id3
from ._files import TagError, file_size, read_at, reading, replace_region

SEARCH = 1 << 20      # how far past the ID3 tag to look for the first audio frame
NEW_PADDING = 1024    # ID3 padding left when the tag outgrows its space

_BITRATES = {  # kbps by (MPEG-1?, layer), from the mp3-tech.org table; index 0 = free, 15 = bad
    (True, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_BITRATES[(False, 3)] = _BITRATES[(False, 2)]
_RATES = {1: (44100, 48000, 32000), 2: (22050, 24000, 16000), 2.5: (11025, 12000, 8000)}
_VERSIONS = {0: 2.5, 2: 2, 3: 1}
_LAYERS = {1: 3, 2: 2, 3: 1}
CBR, VBR, ABR, UNKNOWN = "CBR", "VBR", "ABR", ""
_LAME_METHODS = {1: CBR, 8: CBR, 2: ABR, 9: ABR, 3: VBR, 4: VBR, 5: VBR, 6: VBR}


class FrameHeader:
    __slots__ = ("version", "layer", "protected", "bitrate", "sample_rate", "padding", "mode", "length",
                 "samples")

    @classmethod
    def parse(cls, data: bytes) -> FrameHeader | None:
        if len(data) < 4 or data[0] != 0xFF or data[1] & 0xE0 != 0xE0:
            return None
        version = _VERSIONS.get((data[1] >> 3) & 3)
        layer = _LAYERS.get((data[1] >> 1) & 3)
        rate_index, bitrate_index = (data[2] >> 2) & 3, data[2] >> 4
        if version is None or layer is None or rate_index == 3 or bitrate_index in (0, 15):
            return None   # reserved values, or free-format (not supported)
        h = cls()
        h.version, h.layer = version, layer
        h.protected = not data[1] & 1
        h.bitrate = _BITRATES[(version == 1, layer)][bitrate_index] * 1000
        h.sample_rate = _RATES[version][rate_index]
        h.padding = (data[2] >> 1) & 1
        h.mode = data[3] >> 6
        if layer == 1:
            h.samples = 384
            h.length = (12 * h.bitrate // h.sample_rate + h.padding) * 4
        else:
            h.samples = 576 if layer == 3 and version != 1 else 1152
            h.length = h.samples // 8 * h.bitrate // h.sample_rate + h.padding
        return h

    def same_stream(self, other: FrameHeader) -> bool:
        return (other.version, other.layer, other.sample_rate) == (self.version, self.layer, self.sample_rate)


class MPEGInfo:
    """What the player shows about an MP3's audio."""

    def __init__(self):
        self.version = 1
        self.layer = 3
        self.mode = 0
        self.channels = 2
        self.sample_rate = 0
        self.bitrate = 0
        self.length = 0.0
        self.protected = False
        self.bitrate_mode = UNKNOWN
        self.encoder_info = ""       # "LAME 3.100" (from a LAME header)
        self.encoder_settings = ""   # "-V 2", "-b 320", "--abr 160" (worked out from the LAME header)
        self.track_gain: float | None = None
        self.track_peak: float | None = None
        self.sketchy = False          # the frame structure didn't check out


def _first_frame(f, start: int, end: int) -> tuple[int, FrameHeader, bool]:
    """(where, header, confirmed) of the first audio frame at or after `start`: a valid header
    followed by another valid frame of the same stream. Unconfirmed → the first valid header."""
    window = read_at(f, start, max(0, min(SEARCH, end - start)))
    first = None
    at = window.find(b"\xff")
    while 0 <= at < len(window) - 3:
        header = FrameHeader.parse(window[at: at + 4])
        if header is not None:
            if first is None:
                first = (start + at, header)
            nxt = at + header.length
            if nxt + 4 <= len(window):
                follow = FrameHeader.parse(window[nxt: nxt + 4])
                if follow is not None and header.same_stream(follow):
                    return start + at, header, True
            elif start + nxt >= end - 4:   # the stream's last frame
                return start + at, header, True
        at = window.find(b"\xff", at + 1)
    if first is None:
        raise TagError("no MPEG audio frame found (not an MP3, or damaged)")
    return first[0], first[1], False


def read_info(f, start: int, end: int) -> MPEGInfo:
    """The audio facts, from the first frame at/after `start`; `end` = where the audio stops
    (before an ID3v1 tag)."""
    where, header, confirmed = _first_frame(f, start, end)
    info = MPEGInfo()
    info.version, info.layer, info.mode = header.version, header.layer, header.mode
    info.channels = 1 if header.mode == 3 else 2
    info.sample_rate, info.protected = header.sample_rate, header.protected
    info.sketchy = not confirmed
    frame = read_at(f, where, min(header.length, end - where)) if header.length else b""
    side = (32 if info.channels == 2 else 17) if header.version == 1 else (17 if info.channels == 2 else 9)
    xing_at = 4 + side
    frames = audio_bytes = trim = None
    tag = frame[xing_at: xing_at + 4]
    if header.layer == 3 and tag in (b"Xing", b"Info"):
        flags = struct.unpack_from(">I", frame, xing_at + 4)[0] if len(frame) >= xing_at + 8 else 0
        at = xing_at + 8
        if flags & 1 and len(frame) >= at + 4:
            frames = struct.unpack_from(">I", frame, at)[0]
            at += 4
        if flags & 2 and len(frame) >= at + 4:
            audio_bytes = struct.unpack_from(">I", frame, at)[0]
            at += 4
        if flags & 4:
            at += 100
        quality = None
        if flags & 8 and len(frame) >= at + 4:
            quality = struct.unpack_from(">I", frame, at)[0]
            at += 4
        info.bitrate_mode = VBR if tag == b"Xing" else CBR
        trim = _lame(info, frame[at: at + 36], quality, header.bitrate // 1000)
    elif frame[36:40] == b"VBRI" and len(frame) >= 36 + 18:
        audio_bytes, frames = struct.unpack_from(">II", frame, 36 + 10)
        info.bitrate_mode = VBR
    if frames:
        info.length = frames * header.samples / header.sample_rate
        if audio_bytes and audio_bytes > header.length:
            info.bitrate = int((audio_bytes - header.length) * 8 / info.length + 0.5)
        else:
            info.bitrate = int((end - where - header.length) * 8 / info.length + 0.5)
        if trim:   # LAME's encoder delay + end padding: the exact length (the spec, bytes $B1-$B3)
            info.length = max(0, frames * header.samples - trim) / header.sample_rate
    else:
        info.bitrate = header.bitrate
        info.length = (end - where) * 8 / header.bitrate if header.bitrate else 0.0
    return info


def _lame(info: MPEGInfo, ext: bytes, quality: int | None, kbps: int) -> int:
    """The LAME extension (only from LAME itself: other encoders put their own name there and
    leave the fields empty). Returns the samples LAME added (encoder delay + end padding), 0 if
    not known."""
    if len(ext) < 36 or not ext.startswith(b"LAME"):
        return 0
    version = ext[:9].rstrip(b"\x00 ").decode("latin-1", "replace")
    info.encoder_info = "LAME " + version[4:].strip() if len(version) > 4 else "LAME"
    method = ext[9] & 0x0F
    mode = _LAME_METHODS.get(method)
    if mode:
        info.bitrate_mode = mode
    (peak,) = struct.unpack_from(">f", ext, 11)
    radio = struct.unpack_from(">H", ext, 15)[0]
    name, origin, sign, value = radio >> 13, (radio >> 10) & 7, (radio >> 9) & 1, radio & 0x1FF
    if name == 1 and origin != 0:
        info.track_gain = (-value if sign else value) / 10
        info.track_peak = peak if peak > 0 else None
    rate = ext[20]   # ABR: the target; CBR: the bitrate (255 = "255 or more" — the frame header's then)
    preset = int.from_bytes(ext[26:28], "big") & 0x7FF   # bytes $B6-$B7: LAME's preset number
    if mode == ABR and 8 <= preset <= 320:
        # (In LAME 3.100's ABR files the preset number is the target kbps — seen in files made by
        # libsndfile's LAME encoder, 05/10/2026; the spec only says "the internal preset enum".)
        info.encoder_settings = f"--preset {preset}"
    elif mode == ABR and rate:
        info.encoder_settings = f"--abr {rate}"
    elif mode == CBR and rate:
        info.encoder_settings = f"-b {rate if rate < 255 else kbps}"
    elif mode == VBR and quality is not None and 0 <= quality <= 100:
        info.encoder_settings = f"-V {(100 - quality) // 10}"   # quality = 100 − 10 × V − q (the spec, byte $9B)
    delays = int.from_bytes(ext[21:24], "big")
    return (delays >> 12) + (delays & 0xFFF)


class MP3File:
    """An MP3's audio facts (`info`) and ID3 tag (`tags`: id3.ID3Tag or None — from the ID3v1
    tag when there's no ID3v2 one)."""

    @reading
    def __init__(self, path):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            size = file_size(f)
            head = read_at(f, 0, min(size, 10))
            self.v2_size = id3.tag_size(head)
            self.tags: id3.ID3Tag | None = None
            # An ID3 tag that won't parse (read as none), or one whose frames couldn't all be
            # followed (read as far as they go): never rewritten, so nothing in it is lost.
            self.damaged_tag = False
            if self.v2_size:
                if self.v2_size > size:
                    raise TagError("the ID3 tag runs past the end of the file")
                try:
                    self.tags = id3.ID3Tag.parse(read_at(f, 0, self.v2_size))
                    self.damaged_tag = bool(self.tags.problem)
                except TagError:
                    self.tags, self.damaged_tag = None, True
            start = self.v2_size
            # Some files carry a second ID3v2 tag straight after the first: skip it too.
            while start + 10 <= size:
                extra = id3.tag_size(read_at(f, start, 10))
                if not extra:
                    break
                start += extra
            self.v1 = read_at(f, size - 128, 128) if size >= 128 else b""
            if self.v1[:3] != b"TAG":
                self.v1 = b""
            end = size - (128 if self.v1 else 0)
            self.info = read_info(f, start, end)
        if self.tags is None and self.v1:
            self.tags = id3.ID3Tag.from_v1(self.v1)

    def add_tags(self) -> id3.ID3Tag:
        if self.tags is None:
            self.tags = id3.ID3Tag([], (2, 3, 0))
        return self.tags

    def save(self, major: int) -> None:
        """Write the ID3v2 tag as v2.`major` (in the old tag's space when it fits); an existing
        ID3v1 tag is brought up to date too."""
        if self.damaged_tag:
            raise TagError(id3.damaged_reason(self.tags))
        tag = self.add_tags()
        body = tag.render(major)
        padding = self.v2_size - len(body) if len(body) <= self.v2_size else NEW_PADDING
        replace_region(self.path, 0, self.v2_size, tag.render(major, padding))
        if self.v1:
            with open(self.path, "r+b") as f:
                f.seek(-128, 2)
                f.write(id3.render_v1(tag, self.v1))
        self.__init__(self.path)

    def delete(self) -> None:
        """Remove the ID3v2 and ID3v1 tags."""
        if self.damaged_tag:
            raise TagError(id3.damaged_reason(self.tags))
        if self.v1:
            with open(self.path, "r+b") as f:
                f.seek(0, 2)
                f.truncate(f.tell() - 128)
        if self.v2_size:
            replace_region(self.path, 0, self.v2_size, b"")
        self.__init__(self.path)

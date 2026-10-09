"""Format flags (no UI): things a file can carry that change what it *should* sound like —
found and reported only. Nothing here decodes or alters the sound, so bit-perfect playback is
untouched; Now Playing → Check just says what the file carries.

HDCD (High Definition Compatible Digital, a 1990s Pacific Microsonics CD encoding)
  A few CDs hide control codes in the lowest bit of their 16-bit samples, telling an HDCD
  decoder to expand loud peaks back out ("peak extend") and to turn parts down again ("gain
  adjust", 0 to -7.5 dB in 0.5 dB steps). Played without a decoder — as here — you hear the
  CD as it was pressed. The packet format is read from FFmpeg's `libavfilter/af_hdcd.c`
  (read for the format only; no code copied):
  - per channel, the lowest bit of each sample is shifted into a window, newest bit lowest;
  - descrambled as w ^ (w >> 5) ^ (w >> 23), kept to 32 bits;
  - 0x7e0fa005 starts packet A (8 more bits), 0x7e0fa006 packet B (16 more bits);
  - A is valid when (w & 0x0fa00500) == 0x0fa00500 and (w & 0xc8) == 0; its control byte is
    (w & 255) + (w & 7). B is valid when ((w ^ (~w >> 8 & 255)) & 0xffff00ff) == 0xa0060000
    (the code followed by its inverse); control = w >> 8 & 255;
  - control bits 0–3: gain (-(g >> 1) - 0.5·(g & 1) dB), bit 4: peak extend, bit 5: transient
    filter;
  - "detected" once every channel has had a valid packet within the code-detect time of each
    other (FFmpeg's default 2000 ms); it changes the sound when a gain below 0 dB or peak extend
    is ever used. Peak extend: never / intermittently / permanently (on in every packet).
  FFmpeg only checks the window at certain points (a skip table, for speed); here every sample
  position is checked, which finds the same packets.

MQA (Master Quality Authenticated)
  MQA hides its data below the music's noise floor. Following the MQA_identifier tool
  (github.com/purpl3F0x/MQA_identifier, `mqa_identifier.h`, read 04/10/2026): stereo 16- or
  24-bit only; the first 3 seconds; per frame one bit = bit 16 of the 32-bit left-justified
  (left XOR right) — i.e. bit (depth - 16) of the file's samples — also tried one and two bits
  higher; a match when the last 36 bits read 0xbe0498c88 (newest bit lowest). Then, counting
  from the frame that completed the match: frames 3–7 give a 5-bit code (first = highest bit) for
  the original sample rate — 48 kHz family if its lowest bit is set, else 44.1 kHz, times
  1 << (bit3 | bit2 << 1 | bit1 << 2), doubled again above ×16 (DSD); frames 29–33 give the
  "provenance", above 8 = MQA Studio. The `MQAENCODER` tag (a FLAC Vorbis comment) is read too.

Pre-emphasis
  A few early CDs boost the treble when mastered, and set a flag telling the player to cut it
  again (de-emphasis). Without that the music sounds bright. Found from: the FLAC file's own
  CUESHEET block (per-track pre-emphasis bit, RFC 9639 §8.7.1), a `.cue` file in the folder
  (`FLAGS PRE` on this file's track — see cuesheet.py), or a tag. Tags: PRE_EMPHASIS and
  PRE-EMPHASIS with the value 1 / on / yes are what foobar2000's de-emphasis plugin
  (foo_dsp_deemph) reads, per dBpoweramp's forum; PREEMPHASIS and EMPHASIS are accepted too
  (unverified — seen in the wild, not found documented).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import audiofile, cuesheet, tagio
from .audiofile import FLOAT_SUBTYPES, SUBTYPE_BITS, read_blocks

# -- HDCD ---------------------------------------------------------------------------
HDCD_PREFIX_A = 0x7E0FA005
HDCD_PREFIX_B = 0x7E0FA006
HDCD_DETECT_MS = 2000          # FFmpeg's default code-detect timer
_TAIL = 23 + 31 + 16           # bits kept between blocks: scrambler reach + window + longest packet


def hdcd_gain_db(gain: int) -> float:
    """The 4-bit gain code (3.1 fixed point, stored positive) in dB: 0 … -7.5."""
    return -(gain >> 1) - 0.5 * (gain & 1) if gain else 0.0


def _windows(bits: np.ndarray, width: int) -> np.ndarray:
    """For each position n: Σ bits[n-k] << k over k < width (newest bit lowest; bits before the
    start count as 0), as uint32 up to 32 bits wide, else uint64. Built by doubling —
    log2(width) steps, not width."""
    kind = np.uint32 if width <= 32 else np.uint64
    out = bits.astype(kind)
    have = 1
    while have < width:
        step = min(have, width - have)          # older bits to add this round
        shifted = np.zeros_like(out)
        shifted[have:] = (out[:-have] & kind((1 << step) - 1)) << kind(have)
        out |= shifted
        have += step
    return out


@dataclass
class HdcdResult:
    detected: bool = False
    packets: int = 0                 # valid packets, all channels
    packet_type: str = ""            # "A", "B", "A+B" (a mix is most likely an encoding error)
    peak_extend: str = "never"       # "never" / "intermittent" / "permanent"
    max_gain_db: float = 0.0         # the deepest gain adjustment asked for, 0 … -7.5 dB
    transient_filter: bool = False
    errors: int = 0                  # packets that started but failed their check

    @property
    def effectual(self) -> bool:
        """Changes the sound when decoded: any gain below 0 dB, or peak extend."""
        return self.detected and (self.max_gain_db != 0.0 or self.peak_extend != "never")


class HdcdScanner:
    """Looks for HDCD packets in a 16-bit file fed in blocks of left-justified int32 samples
    (the analysis pass's blocks). Mono or stereo."""

    def __init__(self, channels: int, rate: int, detect_ms: int = HDCD_DETECT_MS):
        self.channels = channels
        self.detect_frames = detect_ms * rate // 1000
        self.tails = [np.zeros(_TAIL, dtype=np.uint8) for _ in range(channels)]
        self.done = 0                       # frames fed so far
        self.last = [None] * channels       # where each channel's latest valid packet ended
        self.detected = False
        self.count_a = self.count_b = self.errors = 0
        self.peak_extend = 0
        self.transient = 0
        self.max_gain = 0

    def add(self, block: np.ndarray) -> None:
        if not len(block):
            return
        found: list[tuple[int, int]] = []   # (frame, channel) of each valid packet
        for ch in range(self.channels):
            low = ((block[:, ch].view(np.uint32) >> np.uint32(16)) & np.uint32(1)).astype(np.uint8)
            buf = np.concatenate([self.tails[ch], low])
            self.tails[ch] = buf[-_TAIL:]
            d = buf.copy()
            d[23:] = buf[23:] ^ buf[18:-5] ^ buf[:-23]      # descrambled bit at each position
            d[:23] = 0
            w = _windows(d, 32)                             # 32-bit descrambled window per position
            # Packet starts in [54, len - 16): each one is finished within the buffer.
            search = w[_TAIL - 16: len(buf) - 16]
            starts = np.flatnonzero((search == HDCD_PREFIX_A) | (search == HDCD_PREFIX_B)) + (_TAIL - 16)
            for i in starts:
                ahead = (int(w[i]) & 3) * 8                 # 8 bits for A, 16 for B
                wbits = int(w[i + ahead])
                control = None
                if (wbits & 0x0FA00500) == 0x0FA00500:
                    if (wbits & 0xC8) == 0:
                        control = (wbits & 255) + (wbits & 7)
                        self.count_a += 1
                    else:
                        self.errors += 1
                elif (wbits & 0xA0060000) == 0xA0060000:
                    if ((wbits ^ (~wbits >> 8 & 255)) & 0xFFFF00FF) == 0xA0060000:
                        control = wbits >> 8 & 255
                        self.count_b += 1
                    else:
                        self.errors += 1
                if control is None:
                    continue
                if control & 16:
                    self.peak_extend += 1
                if control & 32:
                    self.transient += 1
                self.max_gain = max(self.max_gain, control & 15)
                # Position in the whole file: buffer index -> frame (the tail came first).
                found.append((self.done + int(i + ahead) - _TAIL, ch))
        for frame, ch in sorted(found):
            self.last[ch] = frame
            if all(f is not None and frame - f < self.detect_frames for f in self.last):
                self.detected = True
        self.done += len(block)

    def result(self) -> HdcdResult:
        packets = self.count_a + self.count_b
        kind = "+".join(k for k, n in (("A", self.count_a), ("B", self.count_b)) if n)
        if not self.peak_extend:
            pe = "never"
        elif self.peak_extend == packets:
            pe = "permanent"
        else:
            pe = "intermittent"
        return HdcdResult(detected=self.detected, packets=packets, packet_type=kind, peak_extend=pe,
                          max_gain_db=hdcd_gain_db(self.max_gain), transient_filter=bool(self.transient),
                          errors=self.errors)


# -- MQA ----------------------------------------------------------------------------
MQA_SYNC = 0xBE0498C88
MQA_SYNC_BITS = 36
MQA_SCAN_SECONDS = 3
_MQA_AFTER = 34                    # frames read after the sync (rate code, provenance)


def mqa_original_rate(code: int) -> int:
    """The original sample rate from MQA's rate code (as MQA_identifier's
    OriginalSampleRateDecoder)."""
    base = 48000 if code & 1 else 44100
    multiplier = 1 << (((code >> 3) & 1) | (((code >> 2) & 1) << 1) | (((code >> 1) & 1) << 2))
    if multiplier > 16:
        multiplier *= 2              # DSD
    return base * multiplier


@dataclass
class MqaResult:
    detected: bool = False
    original_rate: int | None = None
    studio: bool = False
    encoder: str = ""                # the MQAENCODER tag, if any
    bit: int | None = None           # which bit of the left-justified sample carried it (16–18)


class MqaScanner:
    """Collects the first 3 seconds (plus a few frames) of a stereo 16/24-bit file's
    left XOR right, then looks for MQA's sync word."""

    def __init__(self, rate: int, channels: int, bits: int | None):
        self.usable = channels == 2 and bits in (16, 24)
        self.limit = rate * MQA_SCAN_SECONDS
        self.parts: list[np.ndarray] = []
        self.have = 0

    @property
    def full(self) -> bool:
        return not self.usable or self.have >= self.limit + _MQA_AFTER

    def add(self, block: np.ndarray) -> None:
        if self.full or not len(block):
            return
        take = block[: self.limit + _MQA_AFTER - self.have]
        self.parts.append((take[:, 0] ^ take[:, 1]).view(np.uint32))
        self.have += len(take)

    def result(self, encoder: str = "") -> MqaResult | None:
        if not self.usable:
            return MqaResult(encoder=encoder) if encoder else None
        xor = np.concatenate(self.parts) if self.parts else np.zeros(0, dtype=np.uint32)
        found = find_mqa(xor, self.limit)
        if found is None:
            return MqaResult(encoder=encoder)
        at, bit = found
        stream = ((xor >> np.uint32(bit)) & np.uint32(1)).astype(np.int64)

        def field_value(first: int, last: int) -> int | None:
            if at + last >= len(stream):
                return None
            value = 0
            for m in range(first, last + 1):
                value = (value << 1) | int(stream[at + m])
            return value

        code, provenance = field_value(3, 7), field_value(29, 33)
        return MqaResult(detected=True, original_rate=mqa_original_rate(code) if code is not None else None,
                         studio=provenance is not None and provenance > 8, encoder=encoder, bit=bit)


def find_mqa(xor: np.ndarray, limit: int | None = None) -> tuple[int, int] | None:
    """(frame index completing the sync, bit) for the first MQA sync word in left XOR right
    (left-justified uint32), trying bits 16, 17, 18 at each frame in that order; None if absent.
    Only syncs completed within the first `limit` frames count."""
    if limit is not None:
        span = xor[:limit]
    else:
        span = xor
    best: tuple[int, int] | None = None
    for bit in (16, 17, 18):
        stream = ((span >> np.uint32(bit)) & np.uint32(1)).astype(np.uint8)
        windows = _windows(stream, MQA_SYNC_BITS)
        hits = np.flatnonzero(windows == MQA_SYNC)
        if len(hits) and (best is None or int(hits[0]) < best[0]):
            best = (int(hits[0]), bit)
    return best


# -- pre-emphasis and tags -------------------------------------------------------------
PRE_EMPHASIS_TAGS = ("PRE_EMPHASIS", "PRE-EMPHASIS", "PREEMPHASIS", "EMPHASIS")
_YES = {"1", "on", "yes", "true", "y"}


@dataclass
class PreEmphasis:
    flagged: bool = False
    sources: list[str] = field(default_factory=list)   # where it was found, in plain English


def _tag_values(path: Path) -> tuple[dict[str, list[str]], int | None]:
    """The file's tags as {UPPER NAME: [values]} (Vorbis comments; ID3 text frames by frame ID
    and TXXX by description; MP4 free-form items by name; RIFF INFO not included) and its track
    number, read by tagio (by the file's contents). ({}, None) if unreadable."""
    import struct


    try:
        audio = tagio.open_any(path)
        tags = tagio.text_values(audio) if audio is not None else {}
    except (tagio.TagError, OSError, IndexError, struct.error):
        return {}, None
    if not tags:
        return {}, None
    number = None
    for key in ("TRACKNUMBER", "TRCK"):
        for value in tags.get(key, []):
            digits = value.strip().split("/")[0]
            if digits.isdecimal():   # (not isdigit: "²" is a digit int() can't read)
                number = int(digits)
                break
        if number is not None:
            break
    return tags, number


def pre_emphasis(path: str | Path, tags: dict[str, list[str]] | None = None,
                 track_no: int | None = None) -> PreEmphasis:
    """Is this file flagged as pre-emphasised? Checks its FLAC CUESHEET block, any .cue file in
    its folder that lists it, and its tags. For one track of a CUE image (audiofile.CuePath),
    only that track's flags count."""
    one = audiofile.cue_of(path)  # one track of a CUE image
    path = audiofile.real_path(path)
    if one is not None:
        track_no = one.number
    if tags is None:
        tags, read_no = _tag_values(path)
        track_no = track_no if track_no is not None else read_no
    found = PreEmphasis()

    sheet = cuesheet.from_flac(path) if path.suffix.lower() == ".flac" else None
    if sheet is not None:
        marked = [t.number for t in sheet.tracks if t.pre_emphasis and t.audio
                  and (one is None or t.number == one.number)]
        if marked:
            found.sources.append(f"the file's own cue sheet, {_tracks(marked)}")

    folder_files = None
    for cue in cuesheet.cue_files(path.parent):
        try:
            sheet = cuesheet.read(cue)
        except OSError:
            continue
        tracks = sheet.tracks_in(path.name)
        if one is not None:
            tracks = [t for t in tracks if t.number == one.number]
        if not tracks and track_no is not None:
            # The sheet's FILE names may be stale (files renamed after ripping): when none of
            # them is in the folder, fall back to the track number.
            if folder_files is None:
                try:
                    folder_files = {p.name.lower() for p in path.parent.iterdir()}
                except OSError:
                    folder_files = set()
            if not any(cuesheet._base(f) in folder_files for f in sheet.files):
                track = sheet.track(track_no)
                tracks = [track] if track else []
        marked = [t.number for t in tracks if t.pre_emphasis and t.audio]
        if marked:
            found.sources.append(f"{cue.name}: FLAGS PRE on {_tracks(marked)}")

    for name in PRE_EMPHASIS_TAGS:
        if any(v.strip().lower() in _YES for v in tags.get(name, [])):
            found.sources.append(f"the {name} tag")
            break
    found.flagged = bool(found.sources)
    return found


def _tracks(numbers: list[int]) -> str:
    return ("track " if len(numbers) == 1 else "tracks ") + ", ".join(str(n) for n in numbers)


# -- all together ---------------------------------------------------------------------
@dataclass
class FormatFlags:
    hdcd: HdcdResult | None = None        # None = not checked (only 16-bit files can carry it)
    mqa: MqaResult | None = None          # None = not checked (stereo 16/24-bit only)
    pre_emphasis: PreEmphasis = field(default_factory=PreEmphasis)

    @property
    def any(self) -> bool:
        return bool((self.hdcd and self.hdcd.detected) or (self.mqa and (self.mqa.detected or self.mqa.encoder))
                    or self.pre_emphasis.flagged)


class FormatScan:
    """The sample side of the flags, fed the analysis pass's int32 blocks; `finish()` adds the
    metadata side (pre-emphasis, the MQAENCODER tag)."""

    def __init__(self, rate: int, channels: int, bits: int | None, lossy: bool = False,
                 float_samples: bool = False):
        pcm = not lossy and not float_samples
        self.hdcd = HdcdScanner(channels, rate) if pcm and bits == 16 and channels <= 2 else None
        self.mqa = MqaScanner(rate, channels, bits if pcm else None)

    def add(self, block: np.ndarray) -> None:
        if self.hdcd is not None:
            self.hdcd.add(block)
        self.mqa.add(block)

    def finish(self, path: str | Path) -> FormatFlags:
        tags, track_no = _tag_values(Path(path))
        encoder = (tags.get("MQAENCODER") or [""])[0].strip()
        return FormatFlags(hdcd=self.hdcd.result() if self.hdcd is not None else None,
                           mqa=self.mqa.result(encoder),
                           pre_emphasis=pre_emphasis(path, tags, track_no))


def scan(path: str | Path, seconds: float | None = None) -> FormatFlags:
    """All the flags for one file, reading it on its own (the player does this inside the
    Check's one pass instead). `seconds` limits the read (MQA needs only the first 3 s; HDCD
    packets repeat throughout an HDCD disc, so a stretch of each track finds them)."""

    with audiofile.open(path) as f:
        bits = SUBTYPE_BITS.get(f.subtype)
        lossy = audiofile.is_lossy(f)
        formats = FormatScan(f.samplerate, f.channels, bits, lossy, f.subtype in FLOAT_SUBTYPES)
        frames = None if seconds is None else max(int(f.samplerate * seconds), f.samplerate * 4)
        read = 0
        damaged: list = []   # a file that stops decoding part-way: flags from what decodes
        for block in read_blocks(f, damaged, blocksize=f.samplerate * 3, dtype="int32", always_2d=True):
            formats.add(block)
            read += len(block)
            if frames is not None and read >= frames:
                break
    return formats.finish(path)

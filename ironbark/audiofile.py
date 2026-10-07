"""Opening music files for decoding: the one way every part of the player reads audio. No UI.

Playback (engine.py), the bit-perfect proof's independent second decode (proof.py), the Check
(analysis.py) and every other reader call `open(path)` here, so they all decode a file the same
way — which is what keeps the proof honest. Everything is decoded by libsndfile (soundfile):

- FLAC, WAV, AIFF, MP3, Ogg Vorbis and Ogg Opus: opened directly.
- Apple Lossless in .m4a: libsndfile's own ALAC decoder, reading the file through `m4a.CafStream`.
- Ogg Vorbis: wrapped in `_ExactSeek`, because libsndfile 1.2.2's Vorbis seek often lands
  thousands of samples away from where it was asked (found by experiment 04/10/2026: 97 of 200
  random seeks wrong, and `tell()` doesn't notice). Reading straight through is exact, so a seek
  forward reads on to the spot and a seek back starts again from the top. Opus, FLAC and ALAC
  seeks were exact in the same test.

CUE sheet tracks (one long file, a `.cue` saying where each track starts) are `CuePath`s: the
real file's path, plus which stretch of it is the track. `open()` gives a `_Range` over that
stretch, so frame 0 is the track's first sample and reading stops at its last — playback, the
proof, the Check and the meters all see just the track, without each having to know.

Not decoded (they raise `Unsupported`, a RuntimeError, so callers' existing error handling
applies): AAC in .m4a (libsndfile has no AAC decoder) and FLAC inside an Ogg file (libsndfile
1.2.2 fails on it: "unknown error in flac decoder").

Reading int32 (bit-perfect playback, its proof, the Check) goes through `read` / `blocks`
here, never straight to libsndfile: its own integer read of a floating-point file isn't scaled
(0.5 comes out as 0), and it wraps Vorbis and Opus overs round to negative full scale. Those
files are decoded as floats and converted by `to_int32`, the same way for every reader.

An MP3 with no length header (no Xing / Info / VBRI frame) has its length estimated from its
first frame, and libsndfile won't decode past that estimate: a VBR one can stop early.
`mp3_shortfall` spots that by counting the frames.
"""

from __future__ import annotations

import io
import os
import struct
import threading
from dataclasses import dataclass
from pathlib import Path, PurePath

import numpy as np
import soundfile as sf

from . import m4a, mp3frames

_BasePath = type(Path())   # WindowsPath on Windows, PosixPath on a Mac
LOSSY_SUBTYPES = {"VORBIS", "OPUS"}
FLOAT_SUBTYPES = {"FLOAT", "DOUBLE"}
ALAC_BITS = {"ALAC_16": 16, "ALAC_20": 20, "ALAC_24": 24, "ALAC_32": 32}
SEEK_CHUNK = 65536  # frames read at a time while a Vorbis seek reads its way to the spot


class Unsupported(RuntimeError):
    """A music file the player can recognise but not decode."""


# -- CUE sheet tracks ---------------------------------------------------------------
@dataclass(frozen=True)
class CueRange:
    """Where a CUE sheet track sits in its file, in samples (frames)."""
    sheet: str            # the .cue file (or the FLAC holding a CUESHEET block)
    number: int           # the track number in the sheet
    start: int            # its first sample (INDEX 01)
    end: int | None       # one past its last sample (the next track's INDEX 01); None = end of file


class CuePath(_BasePath):
    """One track of a CUE sheet. Everywhere a path is used it's the real file (str(), open,
    stat, the tag reader, the folder for cover art), so nothing else needs to change; but it carries
    `cue` (a CueRange), and two tracks of the same file are different paths: equal only with
    the same file and the same range. Paths derived from it (`parent`, `with_name`…) are plain."""

    def __init__(self, *args, cue: CueRange):
        super().__init__(*args)
        self.cue = cue

    def with_segments(self, *pathsegments):
        return Path(*pathsegments)

    def __eq__(self, other):
        if isinstance(other, CuePath):
            return super().__eq__(other) and self.cue == other.cue
        return False if isinstance(other, PurePath) else NotImplemented

    def __hash__(self):
        return hash((super().__hash__(), self.cue))

    def __repr__(self):
        return f"CuePath({str(self)!r}, track {self.cue.number})"

    def __reduce__(self):
        return (_make_cue_path, (str(self), self.cue))


def _make_cue_path(path: str, cue: CueRange) -> CuePath:
    return CuePath(path, cue=cue)


def cue_of(path) -> CueRange | None:
    return getattr(path, "cue", None)


def real_path(path) -> Path:
    """The file itself (a CUE track's whole file)."""
    return Path(os.fspath(path))


def key(path) -> str:
    """A text key that tells CUE tracks of one file apart (for saved resume points and caches
    keyed by text): the path, plus "#<track number>" for a CUE track."""
    cue = cue_of(path)
    return f"{os.fspath(path)}#{cue.number}" if cue else os.fspath(path)


def display_name(path) -> str:
    """The file's name, plus which CUE track it is ("Album.flac · track 3")."""
    cue = cue_of(path)
    name = getattr(path, "name", None) or os.path.basename(os.fspath(path))
    return f"{name} · track {cue.number}" if cue else name


# -- what a file holds --------------------------------------------------------------
def sniff(path) -> str:
    """What the file's first bytes say it is, where libsndfile alone can't tell us:
    "mp4" (an .m4a / MP4 container), "oggflac" (FLAC inside Ogg), else ""."""
    try:
        with io.open(os.fspath(path), "rb") as f:
            head = f.read(64)
    except OSError:
        return ""
    if m4a.is_mp4(head):
        return "mp4"
    if head.startswith(b"OggS") and head[28:33] == b"\x7fFLAC":
        return "oggflac"
    return ""


def is_lossy(f) -> bool:
    """MP3, Vorbis or Opus: what was discarded when it was encoded is gone."""
    return f.format in ("MP3", "MPEG") or f.subtype.startswith("MPEG") or f.subtype in LOSSY_SUBTYPES


def is_mp3(f) -> bool:
    return f.format in ("MP3", "MPEG") or f.subtype.startswith("MPEG")


def decodes_as_float(f) -> bool:
    """Floating-point files and lossy codecs: the decoder's own samples are floats."""
    return f.subtype in FLOAT_SUBTYPES or is_lossy(f)


def to_int32(x: np.ndarray) -> np.ndarray:
    """Float samples (full scale = ±1.0) as left-justified int32: rounded to the nearest step,
    overs held at full scale (never wrapped round to the other side)."""
    return np.clip(np.rint(x * 2.0**31), -2.0**31, 2.0**31 - 1).astype(np.int32)


def read(f, frames: int, dtype: str, always_2d: bool = True) -> np.ndarray:
    """`f.read(frames, dtype)`, except that int32 from a floating-point or lossy file is decoded as
    float64 and converted by `to_int32` (see the module note). Every int32 reader uses this, so
    playback, the proof's second decode and the Check all get the very same samples."""
    if dtype == "int32" and decodes_as_float(f):
        return to_int32(f.read(frames, dtype="float64", always_2d=always_2d))
    return f.read(frames, dtype=dtype, always_2d=always_2d)


def blocks(f, blocksize: int, dtype: str = "float64", always_2d: bool = False):
    """`f.blocks()` through `read`: blocks of `blocksize` frames (the last one shorter), read one
    after another until the decoder has no more. (soundfile's own SoundFile.blocks() counts down
    the length the file's header states instead, and when the decoder stops short of it, as on
    an MP3 cut off part-way, it hands back the last block's samples again until the count runs
    out.) Every whole-file reader uses this."""
    while True:
        block = read(f, blocksize, dtype, always_2d)
        if not len(block):
            return
        yield block


def set_cancel(f, cancel) -> None:
    """Let a slow seek (Vorbis reads its way to the spot) be abandoned part-way: `cancel()` true
    means something newer wants the file (another seek, or a stop). Nothing for other files."""
    hook = getattr(f, "set_cancel", None)
    if hook is not None:
        hook(cancel)


# -- opening ------------------------------------------------------------------------
class _Wrapped:
    """The parts of soundfile.SoundFile the player uses, passed through to `inner`."""

    def __init__(self, inner):
        self._inner = inner

    samplerate = property(lambda self: self._inner.samplerate)
    channels = property(lambda self: self._inner.channels)
    subtype = property(lambda self: self._inner.subtype)
    format = property(lambda self: self._inner.format)
    name = property(lambda self: self._inner.name)
    closed = property(lambda self: self._inner.closed)

    def blocks(self, blocksize: int, dtype: str = "float64", always_2d: bool = False, **_ignored):
        return blocks(self, blocksize, dtype, always_2d)

    def set_cancel(self, cancel):
        set_cancel(self._inner, cancel)

    def close(self):
        self._inner.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _ExactSeek(_Wrapped):
    """Vorbis: seeks by reading (see the module note), so every seek lands on the exact sample."""

    def __init__(self, inner: sf.SoundFile, path: str):
        super().__init__(inner)
        self._path = path
        self._pos = 0
        self._cancel = None

    frames = property(lambda self: self._inner.frames)

    def tell(self) -> int:
        return self._pos

    def set_cancel(self, cancel):
        self._cancel = cancel

    def seek(self, frames: int, whence: int = io.SEEK_SET) -> int:
        """Lands on the exact sample — unless the cancel check (see set_cancel) says to give up
        part-way, when it stops where it got to (tell() says where)."""
        target = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.frames}[whence] + frames
        target = max(0, min(int(target), self.frames))
        if target < self._pos:
            self._inner.close()
            self._inner = sf.SoundFile(self._path)
            self._pos = 0
        while self._pos < target:
            if self._cancel is not None and self._cancel():
                break
            got = len(self._inner.read(min(target - self._pos, SEEK_CHUNK), dtype="float32"))
            if not got:
                break
            self._pos += got
        return self._pos

    def read(self, frames: int = -1, dtype: str = "float64", always_2d: bool = False, **kwargs):
        block = self._inner.read(frames, dtype=dtype, always_2d=always_2d, **kwargs)
        self._pos += len(block)
        return block


class _Range(_Wrapped):
    """One CUE track's stretch of a file: frame 0 is its first sample, and it ends at its last."""

    def __init__(self, inner, start: int, end: int | None):
        super().__init__(inner)
        total = inner.frames
        self.start = max(0, min(int(start), total))
        self.end = total if end is None else max(self.start, min(int(end), total))
        self._inner.seek(self.start)
        self._pos = 0

    frames = property(lambda self: self.end - self.start)

    def tell(self) -> int:
        return self._pos

    def seek(self, frames: int, whence: int = io.SEEK_SET) -> int:
        target = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.frames}[whence] + frames
        self._pos = max(0, min(int(target), self.frames))
        self._inner.seek(self.start + self._pos)
        return self._pos

    def read(self, frames: int = -1, dtype: str = "float64", always_2d: bool = False, **kwargs):
        left = self.frames - self._pos
        frames = left if frames is None or frames < 0 else min(frames, left)
        block = self._inner.read(frames, dtype=dtype, always_2d=always_2d, **kwargs)
        self._pos += len(block)
        return block


class _StreamFile(sf.SoundFile):
    """A SoundFile reading from a file object it owns (closed with it)."""

    def __init__(self, stream):
        self._stream = stream
        try:
            super().__init__(stream)
        except BaseException:
            stream.close()
            raise

    def close(self):
        try:
            super().close()
        finally:
            self._stream.close()


def _open_file(path):
    real = os.fspath(path)
    kind = sniff(real)
    if kind == "mp4":
        try:
            return _StreamFile(m4a.CafStream(real))
        except m4a.NotAlac as error:
            raise Unsupported(str(error)) from error
        except (struct.error, OverflowError, MemoryError, IndexError, ValueError) as error:
            # A damaged index that slipped past m4a's own checks: still the file's fault, not the player's.
            raise Unsupported(f"this .m4a file's index is damaged ({error})") from error
    if kind == "oggflac":
        raise Unsupported("FLAC inside an Ogg file can't be played (its decoder doesn't support it) — "
                          "a normal .flac copy of it will play")
    f = sf.SoundFile(real)
    if f.subtype == "VORBIS":
        return _ExactSeek(f, real)
    return f


def open(path):  # noqa: A001 — mirrors soundfile.SoundFile; always called as audiofile.open
    """Open a music file (or one CUE track of it) for decoding. Use as a context manager, or
    close() it. Raises soundfile's errors, OSError, or Unsupported (a RuntimeError)."""
    f = _open_file(path)
    cue = cue_of(path)
    if cue is None:
        return f
    try:
        return _Range(f, cue.start, cue.end)
    except BaseException:
        f.close()
        raise


@dataclass
class Info:
    format: str
    subtype: str
    samplerate: int
    channels: int
    frames: int

    @property
    def duration(self) -> float:
        return self.frames / self.samplerate if self.samplerate else 0.0


def info(path) -> Info:
    """Like soundfile.info, for every format (and CUE track) open() reads."""
    with open(path) as f:
        return Info(f.format, f.subtype, f.samplerate, f.channels, f.frames)


# -- MP3s with no length header -------------------------------------------------------
_LENGTH_TAGS = (b"Xing", b"Info", b"VBRI")
_SHORTFALLS_KEPT = 64
_shortfalls: dict = {}
_shortfalls_lock = threading.Lock()


def has_length_header(path) -> bool:
    """Whether the MP3's first frame is a Xing / Info / VBRI frame, which gives the exact length."""
    with io.open(path, "rb") as f:
        data = f.read(1 << 16)
    pos = mp3frames._skip_id3v2(data)
    if pos >= len(data):
        with io.open(path, "rb") as f:   # a big ID3 tag (cover art): read past it
            f.seek(pos)
            data, pos = f.read(1 << 16), 0
    while pos + 4 <= len(data):
        if mp3frames._frame(data, pos, len(data)) is not None:
            return any(tag in data[pos: pos + 64] for tag in _LENGTH_TAGS)
        pos = data.find(b"\xff", pos + 1)
        if pos < 0:
            break
    return False


def mp3_shortfall(f, path) -> tuple[int, int] | None:
    """For an MP3 with no length header: (frames libsndfile will decode, frames the file holds)
    when the first falls short — libsndfile stops at its estimate, so the rest can't be played or
    measured. None for anything else. `f` is the file opened with `open(path)`. The frames are
    counted once per file (a few tens of ms) and remembered."""
    if not is_mp3(f) or cue_of(path) is not None:
        return None
    real = os.fspath(path)
    try:
        stat = os.stat(real)
    except OSError:
        return None
    key = (real, stat.st_size, stat.st_mtime_ns, f.frames)
    with _shortfalls_lock:
        if key in _shortfalls:
            return _shortfalls[key]
    result = None
    try:
        if not has_length_header(real):
            index = mp3frames.build_index(real)
            held = int(index.samples[-1])
            if held - f.frames > max(index.sample_rate, held // 100):   # (decoder delay is far less)
                result = (int(f.frames), held)
    except (OSError, ValueError):
        result = None
    with _shortfalls_lock:
        while len(_shortfalls) >= _SHORTFALLS_KEPT:
            _shortfalls.pop(next(iter(_shortfalls)))
        _shortfalls[key] = result
    return result

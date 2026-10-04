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
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path, PurePath

import soundfile as sf

from . import m4a

_BasePath = type(Path())   # WindowsPath on Windows, PosixPath on a Mac
LOSSY_SUBTYPES = {"VORBIS", "OPUS"}
ALAC_BITS = {"ALAC_16": 16, "ALAC_20": 20, "ALAC_24": 24, "ALAC_32": 32}


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
        while True:
            block = self.read(blocksize, dtype=dtype, always_2d=always_2d)
            if not len(block):
                return
            yield block

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

    frames = property(lambda self: self._inner.frames)

    def tell(self) -> int:
        return self._pos

    def seek(self, frames: int, whence: int = io.SEEK_SET) -> int:
        target = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.frames}[whence] + frames
        target = max(0, min(int(target), self.frames))
        if target < self._pos:
            self._inner.close()
            self._inner = sf.SoundFile(self._path)
            self._pos = 0
        while self._pos < target:
            got = len(self._inner.read(min(target - self._pos, 65536), dtype="float32"))
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

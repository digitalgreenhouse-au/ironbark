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

An .m4a cut short (its index lists packets past the end of the file) raises "damaged" at the
first read that runs off the end, instead of handing on the noise libsndfile decodes from the
missing packets.

Music on a network drive can be played from a copy on this computer (the network playback cache,
ironbark/netcache). Nothing here changes until that is installed (`set_resolver`). Then:
- The player opens a `LocalTrack`: a plain local file, or one pinned version of one cached copy.
  A copy still downloading is read through a waiting reader (netcache.growing.GrowingFile) with
  a `Gate` shared by every reader of one handle; `_Guarded` checks the gate after every decoder
  call. A call during which a read gave up (the bytes didn't come in time) has its output thrown
  away and raises `Waiting` with the frame to carry on from; that handle is dead from then on and a
  fresh one is opened when the bytes are there. (A decoder fed part of what it asked for decodes
  noise without an error — measured on ALAC — so its output is never trusted after a give-up.)
- Any other reader that opens a network path by name gets the complete, checked copy only
  (`NotCached` while there is none), so every whole-file tool works unchanged.
"""

from __future__ import annotations

import io
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Callable

import numpy as np
import soundfile as sf

from . import m4a, mp3frames

_BasePath = type(Path())   # WindowsPath on Windows, PosixPath on a Mac
LOSSY_SUBTYPES = {"VORBIS", "OPUS"}
FLOAT_SUBTYPES = {"FLOAT", "DOUBLE"}
ALAC_BITS = {"ALAC_16": 16, "ALAC_20": 20, "ALAC_24": 24, "ALAC_32": 32}
SEEK_CHUNK = 65536  # frames read at a time while a Vorbis seek reads its way to the spot
OPEN_PATIENCE_S = 0.25   # how long opening a downloading copy waits for bytes it needs


class Unsupported(RuntimeError):
    """A music file the player can recognise but not decode."""


class Waiting(Exception):
    """A copy still downloading didn't have the bytes a decoder call needed in time. That call's
    output has been thrown away and the handle is dead: open a fresh one at `frame` once more of
    the copy is there. Deliberately not an OSError / RuntimeError / ValueError, so no damage
    handler can mistake waiting for the network for a damaged file."""

    def __init__(self, frame: int, reason: str):
        super().__init__(f"waiting for the network at frame {frame} ({reason})")
        self.frame = frame     # track-relative frame to carry on from
        self.reason = reason   # "patience" | "cancel" | "failed"


class NotCached(OSError):
    """A whole-file reader asked for a network song that has no complete copy here yet, and
    couldn't (or wasn't allowed to) wait for one."""

    def __init__(self, message: str, progress: float | None = None, reason: str = ""):
        super().__init__(message)
        self.progress = progress   # fraction downloaded, when known
        self.reason = reason


class DownloadFailed(OSError):
    """The song's source is gone or refused (deleted, not audio…): no copy until it's asked for again."""


@dataclass(eq=False)
class Gate:
    """What every raw reader of one opened handle on a downloading copy shares: how long a read
    may wait for bytes (`patience`, turned into `deadline` before each decoder call), whether
    something newer wants the handle (`cancel()`), and — once any read gave up — why. A gate that
    has given up never opens again: the handle is dead."""
    patience: float = OPEN_PATIENCE_S
    deadline: float = 0.0
    cancel: Callable[[], bool] | None = None
    gave_up: str = ""        # "" | "patience" | "cancel" | "failed"
    waiting: bool = False    # a read is waiting for the network right now
    furthest: int = 0        # the furthest byte any of its reads has needed
    wakers: list = field(default_factory=list)   # wake a waiting read (so it looks at cancel again)
    spans: list = field(default_factory=list)    # the byte ranges its reads have needed, merged: [[start, end], …]

    def start_call(self) -> None:
        self.deadline = time.monotonic() + self.patience

    def note(self, start: int, end: int) -> None:
        """Record that a read needed bytes [start, end) (kept merged and in order)."""
        spans = self.spans
        if spans and spans[-1][0] <= start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
            return
        spans.append([start, end])
        if len(spans) > 1 and spans[-2][0] > start:
            spans.sort()
            merged = [spans[0]]
            for s, e in spans[1:]:
                if s <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], e)
                else:
                    merged.append([s, e])
            spans[:] = merged

    def wake(self) -> None:
        for waker in list(self.wakers):
            waker()


@dataclass(frozen=True, eq=False)
class LocalTrack:
    """What the player plays: a local file (`item` None), or one version (`gen`) of one copy in
    the network playback cache. `source` is the identity, exactly as the play list holds it.
    Every handle of one play — playback, its reopens, the proof's second decode — opens the same
    LocalTrack, so they all read the same bytes. `item` is netcache's (opaque here)."""
    source: object
    item: object = None
    gen: int = 0

    @staticmethod
    def plain(path) -> "LocalTrack":
        return path if isinstance(path, LocalTrack) else LocalTrack(path)

    @property
    def remote(self) -> bool:
        return self.item is not None

    def ready_path(self):
        """The file to open by name: the local file, or the complete copy (as a CuePath for a CUE
        track). None while the copy is still downloading."""
        if self.item is None:
            return self.source
        return self.item.ready_path(self.gen, self.source)

    def reader(self, gate: Gate, role: str = "play") -> io.RawIOBase:
        """A new waiting reader on the downloading copy."""
        return self.item.reader(self.gen, gate, role)

    def acquire(self) -> None:
        """While held, this version of the copy is never evicted or deleted."""
        if self.item is not None:
            self.item.acquire(self.gen)

    def release(self) -> None:
        if self.item is not None:
            self.item.release(self.gen)

    def copy_state(self) -> str:
        """"" (a local file), "downloading", "checking", "verified", "failed" or "stale"."""
        return "" if self.item is None else self.item.copy_state(self.gen)


# The network playback cache's hooks (netcache.install sets them; None = not installed).
_resolver = None        # (path, cancel=, wait=) -> LocalTrack | None (None: open it as a local file)
_info_hook = None       # (path) -> Info | None: what's already known of a network song, with no I/O
_partial_folder = ""    # part-downloaded files, never opened by name


def set_resolver(resolve, *, info=None, partial_folder=None) -> None:
    """Install (or, with None, remove) the network playback cache's hooks."""
    global _resolver, _info_hook, _partial_folder
    _resolver, _info_hook = resolve, (info if resolve is not None else None)
    _partial_folder = os.path.normcase(os.fspath(partial_folder)) if resolve is not None and partial_folder else ""


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


class UrlPath(_BasePath):
    """A web address (http:// or https://) in a playlist. Not played yet: it's listed as missing
    and written back exactly as it was. str() is the address itself; name / suffix / stem come
    from its last part, decoded; it equals only the very same address. It never stands for a file
    on this computer: it says it isn't there without asking the disk, and opening it to decode is
    refused (Unsupported)."""

    def __init__(self, *args, url: str):
        super().__init__(*args)
        self.url = url

    @classmethod
    def parse(cls, url: str) -> "UrlPath":
        from urllib.parse import unquote, urlsplit
        parts = urlsplit(url)
        # A stand-in path of the host and the decoded parts, so name / suffix / stem mean something.
        segments = [unquote(s).replace("\\", "_").replace("/", "_") for s in parts.path.split("/") if s]
        return cls(parts.netloc.replace(":", "_") or "web", *segments, url=url)

    def with_segments(self, *pathsegments):
        return Path(*pathsegments)

    def __str__(self):
        return self.url

    def __eq__(self, other):
        if isinstance(other, UrlPath):
            return self.url == other.url
        return False if isinstance(other, PurePath) else NotImplemented

    def __hash__(self):
        return hash(("url", self.url))

    def __repr__(self):
        return f"UrlPath({self.url!r})"

    def __reduce__(self):
        return (UrlPath.parse, (self.url,))

    def stat(self, *, follow_symlinks=True):
        raise FileNotFoundError(f"{self.url} is a web address, not a file on this computer")

    def exists(self, *, follow_symlinks=True):
        return False

    def is_file(self):
        return False

    def is_dir(self):
        return False


def is_web_address(text: str) -> bool:
    return text[:8].lower().startswith(("http://", "https://"))


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
    return sniff_head(head)


def sniff_head(head: bytes) -> str:
    """`sniff`, from a file's first 64 bytes."""
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


def set_patience(f, seconds: float) -> None:
    """How long each decoder call on a downloading copy may wait for bytes before giving up
    (raising Waiting). Nothing for other files."""
    hook = getattr(f, "set_patience", None)
    if hook is not None:
        hook(seconds)


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
    """Vorbis: seeks by reading (see the module note), so every seek lands on the exact sample.
    A seek back starts again from the top with `reopen()` (a fresh decoder on the same bytes);
    given a path, that's the file opened again."""

    def __init__(self, inner: sf.SoundFile, reopen):
        super().__init__(inner)
        if not callable(reopen):
            path = os.fspath(reopen)
            reopen = lambda: sf.SoundFile(path)   # noqa: E731
        self._reopen = reopen
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
            self._inner = self._reopen()
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


class _Guarded(_Wrapped):
    """The outermost layer of a handle on a downloading copy (so its frames are the track's).
    Before each decoder call it gives the readers their deadline; after every call it looks at the
    gate: if a read gave up during the call, whatever the decoder produced is thrown away (it may
    have decoded missing bytes as audio) and `Waiting` is raised with the frame to carry on from —
    the position before a read, or a seek's target. The handle is then dead: every later read or
    seek raises the same Waiting; close() always works. A decoder error during a call in which a
    read gave up is waiting too (FLAC and MP3 raise when their bytes stop)."""

    def __init__(self, inner, gate: Gate):
        super().__init__(inner)
        self._gate = gate
        self._dead: tuple[int, str] | None = None

    frames = property(lambda self: self._inner.frames)
    gate = property(lambda self: self._gate)

    def tell(self) -> int:
        return self._dead[0] if self._dead else self._inner.tell()

    def set_patience(self, seconds: float):
        self._gate.patience = max(0.0, float(seconds))

    def set_cancel(self, cancel):
        self._gate.cancel = cancel
        set_cancel(self._inner, cancel)
        self._gate.wake()

    def _call(self, frame: int, call, *args, **kwargs):
        if self._dead is not None:
            raise Waiting(*self._dead)
        gate = self._gate
        gate.start_call()
        try:
            result = call(*args, **kwargs)
        except Exception as error:
            if gate.gave_up:
                self._dead = (frame, gate.gave_up)
                raise Waiting(frame, gate.gave_up) from error
            raise
        if gate.gave_up:
            self._dead = (frame, gate.gave_up)
            raise Waiting(frame, gate.gave_up)
        return result

    def read(self, frames: int = -1, dtype: str = "float64", always_2d: bool = False, **kwargs):
        before = self.tell()
        return self._call(before, self._inner.read, frames, dtype=dtype, always_2d=always_2d, **kwargs)

    def seek(self, frames: int, whence: int = io.SEEK_SET) -> int:
        if self._dead is None:
            base = {io.SEEK_SET: 0, io.SEEK_CUR: self._inner.tell(), io.SEEK_END: self.frames}[whence]
            target = max(0, min(int(base + frames), self.frames))
        else:
            target = self._dead[0]
        return self._call(target, self._inner.seek, frames, whence)


class _StreamFile(sf.SoundFile):
    """A SoundFile reading from a file object it owns (closed with it). If a read or seek finds the
    file cut short there (an .m4a whose index runs past its end), that call raises "damaged", so
    what the decoder made of the missing bytes is never handed on. (libsndfile's open looks past
    the end of the audio too; that's harmless, so the open doesn't count.)"""

    def __init__(self, stream):
        self._stream = stream
        try:
            super().__init__(stream)
        except BaseException:
            stream.close()
            raise

    def _checked(self, call, *args, **kwargs):
        if not hasattr(self._stream, "short"):
            return call(*args, **kwargs)
        self._stream.short = False
        result = call(*args, **kwargs)
        if self._stream.short:
            raise RuntimeError("this file is damaged here: it ends before its own index says it does")
        return result

    def read(self, *args, **kwargs):
        return self._checked(super().read, *args, **kwargs)

    def seek(self, *args, **kwargs):
        return self._checked(super().seek, *args, **kwargs)

    def close(self):
        try:
            super().close()
        finally:
            self._stream.close()


def _open_file(path, exact_opus: bool = False):
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
    # Opus seeks land the same way on every handle, so local files keep them; a cached copy reads
    # forward instead, as it must while it's downloading, so all its handles decode alike.
    if f.subtype == "VORBIS" or (exact_opus and f.subtype == "OPUS"):
        return _ExactSeek(f, real)
    return f


def _open_path(path, exact_opus: bool = False):
    f = _open_file(path, exact_opus)
    cue = cue_of(path)
    if cue is None:
        return f
    try:
        return _Range(f, cue.start, cue.end)
    except BaseException:
        f.close()
        raise


def _is_partial(path) -> bool:
    if not _partial_folder:
        return False
    here = os.path.normcase(os.path.abspath(os.fspath(path)))
    return here.startswith(_partial_folder.rstrip("\\/") + os.sep)


def open(path, *, cancel=None, wait=None, patience=None, role="play"):  # noqa: A001 — mirrors soundfile.SoundFile
    """Open a music file (or one CUE track of it) for decoding. Use as a context manager, or
    close() it. Raises soundfile's errors, OSError, or Unsupported (a RuntimeError).

    With the network playback cache installed: a `LocalTrack` opens the complete copy, or a
    downloading one through the waiting readers (Waiting when bytes don't come in time; `patience`
    sets how long the open itself may wait; `role` says who reads it — "play", "proof", "probe" or
    "verify"). A network path opens its complete, checked copy only
    — waiting for it (up to a limit, or until `cancel()`) unless `wait` is False or this is the
    window's own thread, else NotCached; DownloadFailed when the source is gone."""
    if isinstance(path, UrlPath):
        raise Unsupported("it's a web address: playing music from the internet isn't supported yet")
    if isinstance(path, LocalTrack):
        ready = path.ready_path()
        if ready is None:
            return _open_growing(path, cancel=cancel, patience=patience, role=role)
        return _open_path(ready, exact_opus=path.remote)
    if _resolver is not None:
        if _is_partial(path):
            raise Unsupported("this is a part-downloaded file in the music cache")
        local = _resolver(path, cancel=cancel, wait=wait)
        if local is not None:
            local.acquire()   # (so the copy can't be cleared away between here and the open)
            try:
                return _open_path(local.ready_path(), exact_opus=True)
            finally:
                local.release()
    return _open_path(path)


def _open_growing(t: LocalTrack, *, cancel=None, patience=None, role="play"):
    """A handle on a copy still downloading (see the module note): every raw reader of it shares
    one Gate, and `_Guarded` is outermost. Apple Lossless is never opened this way (it plays only
    from a complete copy: a short read decodes as loud noise)."""
    gate = Gate(patience=OPEN_PATIENCE_S if patience is None else patience, cancel=cancel)
    gate.start_call()
    raw = t.reader(gate, role)
    try:
        head = raw.read(64)
        if gate.gave_up:
            raise Waiting(0, gate.gave_up)
        kind = sniff_head(head)
        if kind == "mp4":
            raise Unsupported("an Apple Lossless (.m4a) song plays only once its copy has downloaded completely")
        if kind == "oggflac":
            raise Unsupported("FLAC inside an Ogg file can't be played (its decoder doesn't support it) — "
                              "a normal .flac copy of it will play")
        raw.seek(0)
    except BaseException:
        raw.close()
        raise
    try:
        f = _StreamFile(raw)
    except Exception as error:
        if gate.gave_up:
            raise Waiting(0, gate.gave_up) from error
        raise
    outer = f
    try:
        if gate.gave_up:
            raise Waiting(0, gate.gave_up)
        if f.subtype in LOSSY_SUBTYPES:   # Vorbis and Opus: read forward to a spot, exactly
            outer = _ExactSeek(f, reopen=lambda: _StreamFile(t.reader(gate, role)))
        cue = cue_of(t.source)
        if cue is not None:
            gate.start_call()
            try:
                outer = _Range(outer, cue.start, cue.end)
            except Exception as error:
                if gate.gave_up:
                    raise Waiting(0, gate.gave_up) from error
                raise
            if gate.gave_up:
                raise Waiting(0, gate.gave_up)
    except BaseException:
        outer.close()
        raise
    return _Guarded(outer, gate)


def open_finished(path):
    """Open a finished download that is being checked before it's put in place (the cache's own
    check, which may read a part-downloaded file by name). Otherwise exactly `open`."""
    return _open_path(path, exact_opus=True)


def local_copy(path):
    """The file to read for `path` without decoding it (frame indexes): the path itself when it's
    on this computer, the complete copy of a network song, or None while there isn't one."""
    if isinstance(path, LocalTrack):
        return path.ready_path()
    if _resolver is None:
        return path
    try:
        local = _resolver(path, cancel=None, wait=False)
    except (NotCached, DownloadFailed):
        return None
    return path if local is None else local.ready_path()


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


def info(path, *, cancel=None, wait=None, header: bool = False) -> Info:
    """Like soundfile.info, for every format (and CUE track) open() reads. A network song the
    cache has already looked into is answered from what it found, without reading anything.
    `header`: only the format is wanted, as a library scan wants it (on a worker thread) — a
    network song is then read where it is, just its opening bytes as the library reads its tags,
    never fetched whole into the cache for it."""
    if _info_hook is not None and not isinstance(path, LocalTrack):
        known = _info_hook(path)
        if known is not None:
            return known
    if header and not isinstance(path, (LocalTrack, UrlPath)):
        with _open_path(path) as f:
            return Info(f.format, f.subtype, f.samplerate, f.channels, f.frames)
    with open(path, cancel=cancel, wait=wait) as f:
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
    counted once per file (a few tens of ms) and remembered. For a cached copy (a LocalTrack) it's
    counted on the complete copy; None while that is still downloading (the cache's own check
    counts it then)."""
    if isinstance(path, LocalTrack):
        path = path.ready_path()
        if path is None:
            return None
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

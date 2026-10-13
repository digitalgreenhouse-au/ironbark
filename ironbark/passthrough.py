"""Encoded streams that must reach the receiver untouched (no UI): DTS music CDs and HDCD.

Some 16-bit files aren't plain music samples. A DTS music CD carries a DTS bitstream that only a
DTS receiver can decode; HDCD hides its control codes in the lowest bit of each sample. Either
breaks if the level is changed at all, even by a fraction of a decibel: every sample changes, so
the receiver gets noise instead of a DTS stream (and plays it, loudly), or an HDCD decoder finds
no codes. So the clipping guard and volume levelling, which only ever change a track's level,
leave these tracks alone: the engine plays them at their own level whatever either asks for
(engine.Player.set_encoded), and Now Playing says why.

A DTS stream looks like noise at or near full scale, which is just what the clipping guard turns
down and levelling turns down hardest; an HDCD master can have inter-sample peaks like any other.

How it's found: the library check's stored flags when it has read the whole file (Detector's
`flags_of`); else the first SCAN_SECONDS of a 16-bit PCM file (formats.DtsScanner, which needs a
few frames' sync words; formats.HdcdScanner, which needs a packet on every channel within two
seconds). Both are found throughout such a track, so its first stretch is enough — unless its
codes only start after a long silent opening, which the library check still catches.
"""

from __future__ import annotations

import logging
import threading

from . import audiofile
from .audiofile import FLOAT_SUBTYPES, SUBTYPE_BITS, read_blocks
from .formats import DtsScanner, HdcdScanner

log = logging.getLogger(__name__)

SCAN_SECONDS = 30
DTS = "DTS"
HDCD = "HDCD"


def detect(path, cancel=None) -> str | None:
    """DTS, HDCD or "" (neither) for a music file; None if `cancel()` came true first. Raises
    audiofile's errors (NotCached for a network song not here yet) and soundfile's."""
    with audiofile.open(path, cancel=cancel) as f:
        if (SUBTYPE_BITS.get(f.subtype) != 16 or f.subtype in FLOAT_SUBTYPES or audiofile.is_lossy(f)
                or f.channels > 2):
            return ""
        dts = DtsScanner() if f.channels == 2 else None
        hdcd = HdcdScanner(f.channels, f.samplerate)
        limit, read = f.samplerate * SCAN_SECONDS, 0
        damaged: list = []
        for block in read_blocks(f, damaged, blocksize=f.samplerate * 3, dtype="int32", always_2d=True):
            if cancel is not None and cancel():
                return None
            if dts is not None:
                dts.add(block)
                if dts.detected:
                    return DTS
            hdcd.add(block)
            if hdcd.detected:
                return HDCD
            read += len(block)
            if read >= limit:
                break
    return ""


class Detector:
    """What each file is, found once (on a worker thread) and remembered for the window's thread
    for the session — like the window's true peaks, never forgotten: the guard waits for both, and
    a file whose kind was forgotten while its peak was kept would never be guarded again.
    A file not looked at yet is "not known": the clipping guard and levelling wait for it.
    `flags_of(path)`: the library check's stored formats.FormatFlags for a file, or None."""

    def __init__(self, flags_of=None):
        self._kinds: dict = {}
        self._lock = threading.Lock()
        self._flags_of = flags_of

    def known(self, path) -> bool:
        return path in self._kinds

    def kind(self, path) -> str:
        """DTS, HDCD, or "" (neither, or not looked at yet)."""
        return self._kinds.get(path, "")

    def check(self, path, cancel=None) -> str | None:
        """(On a worker.) Look at a file unless it's known already: its kind, or None if cancelled
        meanwhile (nothing remembered). A file that can't be read counts as neither (its true peak
        can't be measured either, so nothing would change its level); nothing else is raised, as the
        worker must carry on, except audiofile.NotCached, for the caller to try again later."""
        if path in self._kinds:
            return self._kinds[path]
        try:
            found = _from_flags(self._stored_flags(path))
            if not found:
                found = detect(path, cancel)
        except audiofile.NotCached:
            raise
        except (OSError, RuntimeError, ValueError):
            log.info("couldn't look for an encoded stream in %s", path, exc_info=True)
            found = ""
        except Exception:
            log.exception("couldn't look for an encoded stream in %s", path)
            found = ""
        if found is None:
            return None
        with self._lock:
            self._kinds = {**self._kinds, path: found}   # replaced whole: read without the lock
        return found


    def _stored_flags(self, path):
        """The library check's flags for a file, or None (none stored, or they can't be read now)."""
        if self._flags_of is None:
            return None
        try:
            return self._flags_of(path)
        except Exception:
            log.info("couldn't read the stored check of %s", path, exc_info=True)
            return None


def _from_flags(flags) -> str:
    """DTS or HDCD from a whole-file check's flags; "" when they say neither or there are none."""
    if flags is None:
        return ""
    if flags.dts is not None and flags.dts.detected:
        return DTS
    if flags.hdcd is not None and flags.hdcd.detected:
        return HDCD
    return ""


def note(kind: str) -> str:
    """What the Output line adds for an encoded track."""
    if kind == DTS:
        return "DTS stream: clipping guard and levelling kept off"
    if kind == HDCD:
        return "HDCD: clipping guard and levelling kept off"
    return ""

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

How it's found: the first SCAN_SECONDS of a 16-bit PCM file (formats.DtsScanner, which needs a few
frames' sync words; formats.HdcdScanner, which needs a packet on every channel within two
seconds). Both are found throughout such a track, so its first stretch is enough.
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
    """What each file is, found once (on a worker thread) and remembered for the window's thread.
    A file not looked at yet is "not known": the clipping guard and levelling wait for it, as they
    wait for its true peak."""

    KEPT = 256   # files remembered (the newest kept)

    def __init__(self):
        self._kinds: dict = {}
        self._lock = threading.Lock()

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
            kinds = {p: k for p, k in self._kinds.items() if p != path}
            kinds[path] = found
            while len(kinds) > self.KEPT:
                kinds.pop(next(iter(kinds)))
            self._kinds = kinds   # replaced whole: the window's thread reads it without the lock
        return found


def note(kind: str) -> str:
    """What the Output line adds for an encoded track."""
    if kind == DTS:
        return "DTS stream: clipping guard and levelling kept off"
    if kind == HDCD:
        return "HDCD: clipping guard and levelling kept off"
    return ""

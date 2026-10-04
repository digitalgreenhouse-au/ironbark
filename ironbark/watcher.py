"""Spotting new music (0.17.0), no UI: at start-up and every few minutes, compare the library
folder with the saved index (library_index.py) — a walk of names, sizes and modified times, so it's
cheap; tags are read only for what changed — and say what's new, changed or gone.

- **Polling**, on a thread of its own (never the Tk thread). It works the same on Windows and Mac;
  Windows' change notifications (ReadDirectoryChangesW) could replace it later for a huge library.
  Results go into `results` (a queue); the player's own `after` loop picks them up (nothing
  here touches Tk).
- **Files still being copied wait.** A new or changed file is left for the next look while its
  modified time is within `SETTLE_S` of now, or while it differs from how it looked last time
  (its size still growing). While anything is waiting, the next look comes after `RECHECK_S`
  rather than minutes later. (Explorer gives a copied file its original modified time once the
  copy finishes, so a finished copy usually looks old straight away; a file that can't be read
  yet is simply tried again on the next look — library_index._read_all.)
- **New albums** are worked out from the index, not remembered separately: an album is new when
  one of its files was first seen after watching began (`since`, saved in the settings) and after
  the index's first build (the whole library isn't "new" on a first run), and it hasn't been
  opened since. A tag edit is a *change* (the file keeps its first-seen time), so it never makes
  an album new.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .library import album_folder
from .library_index import Diff, LibraryIndex

EVERY_S = 180.0    # "every few minutes": how often the folder is looked at while the player is open
RECHECK_S = 10.0   # the next look, while a file is still being copied
SETTLE_S = 10.0    # a file modified more recently than this is taken to be still being written


@dataclass
class Report:
    """One look at the library folder."""
    diff: Diff = field(default_factory=Diff)
    error: str = ""
    first_build: bool = False   # the index was built for the first time: everything "added", nothing new

    @property
    def news(self) -> bool:
        """Worth a word in the status line: music arrived or went (a change alone isn't —
        usually that's your own tag edit)."""
        return not self.first_build and bool(self.diff.added or self.diff.removed)


class Settling:
    """Decides whether a new or changed file is still being written (the `hold` library_index's
    refresh asks about). Remembers how each waiting file looked last time."""

    def __init__(self, settle_s: float = SETTLE_S, clock: Callable[[], float] = time.time):
        self.settle_s = settle_s
        self.clock = clock
        self.last: dict[str, tuple[int, int]] = {}   # waiting file -> (size, modified ns) last time
        self._asked: set[str] = set()

    def begin(self):
        self._asked = set()

    def __call__(self, path: str, stat: tuple) -> bool:
        self._asked.add(path)
        size, mtime_ns = stat[0], stat[1]
        recent = abs(self.clock() - mtime_ns / 1e9) < self.settle_s   # (abs: a clock ahead of ours)
        moved = path in self.last and self.last[path] != (size, mtime_ns)
        if recent or moved:
            self.last[path] = (size, mtime_ns)
            return True
        self.last.pop(path, None)
        return False

    def end(self):
        """Forget files that weren't asked about this time (they settled, or went away)."""
        for path in [p for p in self.last if p not in self._asked]:
            del self.last[path]


class Watcher:
    """Looks at the library folder now and then, on a thread of its own.

    `index_for()` gives the index to bring up to date (read fresh each look, so a change of
    library folder is followed). `check()` is one look, run by the thread or straight from a test."""

    def __init__(self, index_for: Callable[[], LibraryIndex], every_s: float = EVERY_S,
                 recheck_s: float = RECHECK_S, settle_s: float = SETTLE_S, clock: Callable[[], float] = time.time):
        self.index_for = index_for
        self.every_s = every_s
        self.recheck_s = recheck_s
        self.settling = Settling(settle_s, clock)
        self.results: queue.SimpleQueue[Report] = queue.SimpleQueue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._root: Path | None = None

    def check(self) -> Report:
        """One look: the index is brought up to date (only changed files read), files still being
        copied left for later. Never raises: a failure is reported, and the next look tries again."""
        try:
            index = self.index_for()
            if index.root != self._root:          # another library folder: start afresh
                self._root = index.root
                self.settling = Settling(self.settling.settle_s, self.settling.clock)
            if not index.root.is_dir():
                return Report(error=f"Can't see the library folder {index.root}")
            first_build = not index.built
            self.settling.begin()
            diff = index.refresh(hold=self.settling)
            self.settling.end()
            return Report(diff, first_build=first_build)
        except Exception as error:  # noqa: BLE001 — the index is only a speed-up; never fatal
            return Report(error=str(error))

    # -- the thread ----------------------------------------------------------------------------
    def start(self, delay_s: float = 0.0):
        """Begin watching: the first look after `delay_s`, then every `every_s`."""
        if self.running:
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._run, args=(delay_s,), name="new-music", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def stop(self):
        """Stop watching (a look under way finishes; its result is still reported)."""
        self._stop.set()
        self._wake.set()

    def look_now(self):
        """Look again straight away (e.g. after the library folder changed)."""
        self._wake.set()

    def _run(self, delay_s: float):
        wait = delay_s
        while True:
            self._wake.wait(wait)
            self._wake.clear()
            if self._stop.is_set():
                return
            report = self.check()
            if self._stop.is_set():
                return
            self.results.put(report)
            wait = self.recheck_s if report.diff.waiting else self.every_s

    def drain(self) -> list[Report]:
        """The looks finished since last asked (called from the Tk thread's own loop)."""
        reports = []
        while True:
            try:
                reports.append(self.results.get_nowait())
            except queue.Empty:
                return reports


# -- new albums --------------------------------------------------------------------------------
def new_albums(index: LibraryIndex, since: float, opened: dict[str, float]) -> list[Path]:
    """The album folders with music first seen after `since` (and after the index's first
    build) that haven't been opened since it arrived; newest first. `opened`: {folder: when}."""
    floor = max(since, index.built)
    newest: dict[Path, float] = {}
    for path, seen in index.seen_times().items():
        if seen > floor:
            folder = album_folder(path)
            newest[folder] = max(seen, newest.get(folder, 0.0))
    fresh = [(seen, folder) for folder, seen in newest.items() if seen > opened.get(str(folder), 0.0)]
    return [folder for _seen, folder in sorted(fresh, key=lambda pair: (-pair[0], str(pair[1]).lower()))]


def tidy_opened(opened: dict[str, float], index: LibraryIndex, since: float) -> dict[str, float]:
    """Drop what no longer matters: albums no longer in the library, and openings from before
    watching began (nothing seen then counts as new anyway)."""
    folders = {str(album_folder(path)) for path in index.seen_times()}
    return {folder: when for folder, when in opened.items() if folder in folders and when > since}


def arrived(report: Report, new: list[Path]) -> list[Path]:
    """The new albums (from `new_albums`) that this look brought."""
    touched = {album_folder(path) for path in report.diff.added}
    return [folder for folder in new if folder in touched]


def note(report: Report, new_count: int) -> str:
    """The one line for the status line, e.g. "New music: 3 new albums in the Library · 2 songs
    gone from the Library" ("" when there's nothing worth saying). `new_count`: the new albums
    this look brought (`arrived`)."""
    if report.error or not report.news:
        return ""
    parts = []
    if report.diff.added:
        if new_count:
            parts.append(f"{new_count} new album{'' if new_count == 1 else 's'} in the Library")
        else:
            count = len(report.diff.added)
            parts.append(f"{count} new song{'' if count == 1 else 's'} in the Library")
    if report.diff.removed:
        count = len(report.diff.removed)
        parts.append(f"{count} song{'' if count == 1 else 's'} gone from the Library")
    return "New music: " + " · ".join(parts) if report.diff.added else "Library: " + " · ".join(parts)

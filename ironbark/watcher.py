"""Spotting new music (0.17.0), no UI: one look at the library folders — at start-up, after the
library folders change, and when you press Rescan — compares them with the library database
(indexer.py, through library_index.py): a walk of names, sizes and modified times, so it's cheap;
tags are read only for what changed. The report says what's new, changed or gone. (0.38.0: there is
no thread looking every few minutes any more — nothing runs in the background.)

- **One look, on a worker thread** (never the Tk thread): the player runs `check()` on its worker and
  leaves the report in `results`; its own `after` loop picks it up (nothing here touches Tk). Each
  report already carries the new albums (worked out from the database), so the player's thread only
  has to show them.
- **Files still being copied wait.** A new or changed file is left for the next look while its
  modified time is within `SETTLE_S` of now, or while it differs from how it looked last time
  (its size still growing). (Explorer gives a copied file its original modified time once the copy
  finishes, so a finished copy usually looks old straight away; a file that can't be read yet is
  simply tried again on the next look.)
- **New albums** are worked out from the database, not remembered separately: an album is new when
  one of its files was first seen after watching began (`since`, saved in the settings) and after
  its library folder's first look (the whole library isn't "new" on a first run), and it hasn't
  been opened since. A tag edit is a *change* (the file keeps its first-seen time), so it never
  makes an album new.
"""

from __future__ import annotations

import os
import queue
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .indexer import Diff
from .library import album_folder
from .library_index import LibraryIndex

SETTLE_S = 10.0    # a file modified more recently than this is taken to be still being written


@dataclass
class Report:
    """One look at the library folders."""
    diff: Diff = field(default_factory=Diff)
    # A library folder couldn't be looked at (said; nothing stored for it changed). What the others
    # brought is still news.
    error: str = ""
    # Music came from a library folder's first look: everything there is "added", nothing new.
    first_build: bool = False
    # When watching (Watcher.since) and something changed: (album folder, when its newest music
    # was first seen) for the albums with music new since watching began, newest first — whether
    # each was opened since is the player's to say — and every album folder in the library (for
    # tidying the opened list). None: not worked out this time (nothing changed).
    new: list[tuple[Path, float]] | None = None
    album_folders: frozenset[str] | None = None

    @property
    def news(self) -> bool:
        """Worth a word in the status line: music arrived or went (a change alone isn't —
        usually that's your own tag edit)."""
        return not self.first_build and bool(self.diff.added or self.diff.removed)


class Settling:
    """Decides whether a new or changed file is still being written (the `hold` a look asks
    about). Remembers how each waiting file looked last time."""

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
    """One look at the library folders, when asked (on a worker thread, never Tk's).

    `index_for()` gives the index to bring up to date (read fresh each look, so a change of
    library folders is followed). `check()` is one look. Set `since` (when new music started to
    count) and each report carries the new albums."""

    def __init__(self, index_for: Callable[[], LibraryIndex], settle_s: float = SETTLE_S,
                 clock: Callable[[], float] = time.time):
        self.index_for = index_for
        self.settling = Settling(settle_s, clock)
        self.since: float | None = None
        self.results: queue.SimpleQueue[Report] = queue.SimpleQueue()
        self._roots: list[Path] | None = None

    def check(self, folders: list[str] | None = None, new_albums: bool = False) -> Report:
        """One look: the library is brought up to date (only changed files read), files still
        being copied left for later. `folders`: a quick look at just these. `new_albums`: work
        the new albums out even if nothing changed. Never raises: a failure is reported, and the
        next look tries again."""
        try:
            index = self.index_for()
            if index.roots != self._roots:          # other library folders: start afresh
                self._roots = list(index.roots)
                self.settling = Settling(self.settling.settle_s, self.settling.clock)
            if not index.roots:   # (no library folders: the library was cleared, or each one is on the network with Local network access off)
                return Report()
            if not any(root.is_dir() for root in index.roots):
                return Report(error=f"Can't see the library folder {index.root}")
            unbuilt = _unbuilt(index)
            self.settling.begin()
            if folders is None:
                diff = index.refresh(hold=self.settling)
            else:
                diff = index.refresh(hold=self.settling, folders=folders)
            self.settling.end()
            # A drive that went mid-look, or an empty mount point: said so, what's stored left as it was.
            # (A library folder that's never been reachable doesn't stop the others' news.)
            report = Report(diff, error=diff.error, first_build=_inside_any(diff.added, unbuilt))
            if self.since is not None and (new_albums or not diff.nothing):
                report.new = index.new_album_times(self.since)
                report.album_folders = frozenset(index.album_folders())
            return report
        except Exception as error:  # noqa: BLE001 — the index is only a speed-up; never fatal
            return Report(error=str(error))

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
    """The album folders with music first seen after `since` (and after their library folder's
    first look) that haven't been opened since it arrived; newest first. `opened`: {folder: when}.
    (From the database: for a worker thread.)"""
    return still_new(index.new_album_times(since), opened)


def _unbuilt(index) -> list[Path]:
    """The library folders whose first look hasn't finished (all of them while `built` is 0 for
    an index that can't say which)."""
    if hasattr(index, "unbuilt"):
        return index.unbuilt()
    return [] if index.built else list(index.roots)


def _inside_any(paths: list[Path], folders: list[Path]) -> bool:
    """Is any of these files in one of these folders?"""
    tops = tuple(os.path.join(os.path.normcase(str(folder)), "") for folder in folders)
    return bool(tops) and any(os.path.normcase(str(path)).startswith(tops) for path in paths)


def still_new(found: list[tuple[Path, float]], opened: dict[str, float]) -> list[Path]:
    """The albums among `found` ((folder, newest first seen), newest first) not opened since."""
    return [folder for folder, seen in found if seen > opened.get(str(folder), 0.0)]


def tidy_opened(opened: dict[str, float], index_or_folders: LibraryIndex | set[str] | frozenset[str],
                since: float) -> dict[str, float]:
    """Drop what no longer matters: albums no longer in the library (`index_or_folders`: the
    index, or every album folder already worked out), and openings from before watching began
    (nothing seen then counts as new anyway)."""
    folders = (index_or_folders.album_folders() if isinstance(index_or_folders, LibraryIndex)
               else index_or_folders)
    return {folder: when for folder, when in opened.items() if folder in folders and when > since}


def arrived(report: Report, new: list[Path]) -> list[Path]:
    """The new albums (from `new_albums`) that this look brought."""
    touched = {album_folder(path) for path in report.diff.added}
    return [folder for folder in new if folder in touched]


def note(report: Report, new_count: int) -> str:
    """The one line for the status line, e.g. "New music: 3 new albums in the Library · 2 songs
    gone from the Library" ("" when there's nothing worth saying). `new_count`: the new albums
    this look brought (`arrived`)."""
    if not report.news:   # (an unreachable library folder changes nothing: what's here is what the others brought)
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

"""Spotting new music (0.17.0), no UI: at start-up and every few minutes, compare the library
folders with the library database (indexer.py, through library_index.py) — a walk of names, sizes
and modified times, so it's cheap; tags are read only for what changed — and say what's new,
changed or gone.

- **Polling**, on a thread of its own (never the Tk thread). It works the same on Windows and Mac;
  Windows' change notifications (ReadDirectoryChangesW) could replace it later for a huge library.
  Results go into `results` (a queue); the player's own `after` loop picks them up (nothing
  here touches Tk). Each report already carries the new albums (worked out on this thread from
  the database), so the player's thread only has to show them.
- **Files still being copied wait.** A new or changed file is left for the next look while its
  modified time is within `SETTLE_S` of now, or while it differs from how it looked last time
  (its size still growing). While anything is waiting, the next look comes after `RECHECK_S` —
  a quick one, at just the folders with copies in progress (the whole library is still walked
  every `EVERY_S`) — and slows down (`QUICK_LOOKS`) for a file that keeps growing for minutes, a
  download or a recording into the library. (Explorer gives a copied file its original modified
  time once the copy finishes, so a finished copy usually looks old straight away; a file that
  can't be read yet is simply tried again on the next look.)
- **New albums** are worked out from the database, not remembered separately: an album is new when
  one of its files was first seen after watching began (`since`, saved in the settings) and after
  its library folder's first look (the whole library isn't "new" on a first run), and it hasn't
  been opened since. A tag edit is a *change* (the file keeps its first-seen time), so it never
  makes an album new.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .indexer import Diff
from .library import album_folder
from .library_index import LibraryIndex

EVERY_S = 180.0    # "every few minutes": how often the folders are looked at while the player is open
RECHECK_S = 10.0   # the next (quick) look, while a file is still being copied
SETTLE_S = 10.0    # a file modified more recently than this is taken to be still being written
QUICK_LOOKS = 6    # quick looks every RECHECK_S for a copy in progress; after that each waits twice as long


@dataclass
class Report:
    """One look at the library folders."""
    diff: Diff = field(default_factory=Diff)
    error: str = ""
    first_build: bool = False   # a library folder's first look: everything "added", nothing new
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
    """Looks at the library folders now and then, on a thread of its own.

    `index_for()` gives the index to bring up to date (read fresh each look, so a change of
    library folders is followed). `check()` is one look, run by the thread or straight from a
    test. Set `since` (when watching began) and each report carries the new albums."""

    def __init__(self, index_for: Callable[[], LibraryIndex], every_s: float = EVERY_S,
                 recheck_s: float = RECHECK_S, settle_s: float = SETTLE_S, clock: Callable[[], float] = time.time):
        self.index_for = index_for
        self.every_s = every_s
        self.recheck_s = recheck_s
        self.settling = Settling(settle_s, clock)
        self.since: float | None = None
        self.results: queue.SimpleQueue[Report] = queue.SimpleQueue()
        # Each start() gets its own stop and wake-up: a thread told to stop never misses it because
        # a new one has started meanwhile, and the new one waits for it to finish its look.
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._whole = threading.Event()   # look_now: the next look is at the whole library
        self._thread: threading.Thread | None = None
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
            if not any(root.is_dir() for root in index.roots):
                return Report(error=f"Can't see the library folder {index.root}")
            first_build = not index.built
            self.settling.begin()
            if folders is None:
                diff = index.refresh(hold=self.settling)
            else:
                diff = index.refresh(hold=self.settling, folders=folders)
            self.settling.end()
            # A drive that went mid-look, or an empty mount point: said so, what's stored left as it was.
            report = Report(diff, error=diff.error, first_build=first_build)
            if self.since is not None and (new_albums or not diff.nothing):
                report.new = index.new_album_times(self.since)
                report.album_folders = frozenset(index.album_folders())
            return report
        except Exception as error:  # noqa: BLE001 — the index is only a speed-up; never fatal
            return Report(error=str(error))

    # -- the thread ----------------------------------------------------------------------------
    def start(self, delay_s: float = 0.0):
        """Begin watching: the first look after `delay_s`, then every `every_s`."""
        if self.running:
            return
        previous = self._thread
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._run, args=(delay_s, self._stop, self._wake, previous),
                                        name="new-music", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def stop(self):
        """Stop watching (a look under way finishes; its result isn't reported)."""
        self._stop.set()
        self._wake.set()

    def look_now(self):
        """Look again at the whole library straight away (e.g. after the library folders changed)."""
        self._whole.set()
        self._wake.set()

    def _run(self, delay_s: float, stop: threading.Event, wake: threading.Event,
             previous: threading.Thread | None):
        if previous is not None:
            previous.join()   # (a thread stopped mid-look finishes it first: never two at once)
        wait, waiting, quick_looks, last_whole = delay_s, [], 0, None
        while True:
            wake.wait(wait)
            wake.clear()
            if stop.is_set():
                return
            now = time.monotonic()
            whole = (not waiting or last_whole is None or self._whole.is_set()
                     or now - last_whole >= self.every_s)
            if whole:
                self._whole.clear()
            report = self.check(None if whole else waiting, new_albums=last_whole is None)
            if stop.is_set():
                return
            self.results.put(report)
            if whole:
                last_whole = now
            # Folders with copies in progress get a quick look soon; a whole look stays on time.
            waiting = sorted({str(Path(path).parent) for path in report.diff.waiting})
            next_whole = max(0.0, last_whole + self.every_s - time.monotonic())
            if waiting:
                quick_looks += 1
                wait = min(next_whole, self.recheck_s * 2 ** max(0, quick_looks - QUICK_LOOKS))
            else:
                quick_looks = 0
                wait = next_whole

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

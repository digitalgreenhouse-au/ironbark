"""The library's background work, no UI: the indexer — the only part of the player that writes the
library database (library_db.py) — and the query thread that reads it for the windows.

**The indexer** (`Indexer.check`) brings the database up to date with the library folders:

1. Each library folder is checked first. One that isn't there (a drive unplugged, a network share
   asleep), can't be listed, or is suddenly empty while music is stored for it (the mount point of
   an unplugged drive) changes nothing: an `Unreachable` event, and the diff's `error` says why. So
   does another drive in its place (a backup drive given the music drive's letter): the drive a
   library folder's music was found on is noted (`volume_of`), and a different one counts only
   once it holds the same music (copied onto a new drive), or has stayed a week (`MISSING_DAYS`).
   A library folder replaced by one holding the same music (Settings → Library → Change…: the
   drive back under another letter) is moved, not read again (LibraryDB.move_root).
2. A walk of its folders with os.scandir: each file's size and times come with the listing, so
   there's no extra call per file (that matters on a network drive). A folder that can't be listed
   is "unknown": everything stored for it and below it stays as it was.
3. Each folder is compared with what's stored: new, changed, gone and back-again files, its .cue
   files, and files read by an older tag reader. A folder with CUE sheets, or that held CUE tracks,
   is read whole (how its files split into tracks depends on all of them).
4. The folders that need it are read — in worker processes for a big job (`FAST_WORKERS` on a
   library folder's first look, `GENTLE_WORKERS` later, at below-normal priority on Windows so
   playback is never starved), in this process for a small job or when no worker can start.
5. Each folder is stored in its own transaction (LibraryDB.apply), so a cancel or a crash loses at
   most the folder in hand: the next look carries on from there, since what wasn't read yet still
   looks new or changed.
6. Folders gone from their parent's listing are marked missing before anything is stored (kept,
   with their dates, in case a drive comes back or they turn up elsewhere: a file found under a
   new name with a missing one's size and time is that file, moved — LibraryDB.apply); missing
   files go for good after a week (LibraryDB.purge_missing), so an album put back from the Recycle
   Bin within it is as it was. A library folder's first look counts as finished
   (LibraryDB.mark_built: only then can anything be new music) once a look at all of it has gone
   cleanly. A folder the system won't let anyone list (access denied) doesn't hold that up — it
   would be the same on every look — and nor, from its second look, does one its parent lists but
   that can't be opened by that name (a Mac's "R.E.M." on a shared drive, a path too long for
   Windows): the diff's `unreadable` names those. One that failed some other way (a network drive
   dropping out) does hold it up: its music would otherwise all look new once it could be read.

Files a caller says are still being written (`hold`: watcher.Settling) are left for the next look.
What a look finds is said on every listener's queue (`listen`): Started, Progress, AlbumsChanged,
Finished, Failed, Unreachable. Nothing here touches Tk: windows drain their queue from their own
`after` loop.

**Queries** is one "library-query" thread with its own connection: a window hands it reads (the
album list, a search) and picks up the answers from its own loop. A newer ask of the same kind
replaces one that hasn't started yet, and can stop one that has (LibraryDB.interrupt).

Worker processes run only `read_folder` (tags and folding, no database, no Tk): this module and
what it imports never import tkinter, and the packaged program's own start (multiprocessing's
freeze_support) runs a worker's task instead of opening a second player.
"""

from __future__ import annotations

import itertools
import logging
import multiprocessing
import os
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from . import audiofile, library_db
from .library import Track, is_music_name, skip_folder
from .library_db import (FileState, FolderResult, FolderState, LibraryDB, cue_signature_of, folder_result, outermost,
                         read_folder)

log = logging.getLogger(__name__)

POOL_MIN = 2000          # files to read before worker processes are worth starting (they take a moment)
GENTLE_WORKERS = 2       # worker processes for a look at a library that's already been looked at
PROGRESS_EVERY_S = 0.25  # Progress events at most this often
ALBUMS_EVERY_S = 0.5     # AlbumsChanged events at most this often while a look stores folders
_IN_FLIGHT = 2           # folders handed to each worker at a time (more would only wait, and delay a cancel)


def fast_workers() -> int:
    """Worker processes for a library folder's first look: all but one of the computer's cores,
    at most 8 (more only wait on the disk)."""
    return max(1, min(8, (os.cpu_count() or 2) - 1))


# -- what a look found, and what it says while it works -----------------------------------------------
@dataclass
class Diff:
    """What changed in the library folders since the database last looked."""
    added: list[Path] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    waiting: list[Path] = field(default_factory=list)   # still being copied: left for next time
    unreadable: list[Path] = field(default_factory=list)   # folders listed that can't be opened by their names
    error: str = ""          # a library folder couldn't be looked at: nothing stored for it was changed
    cancelled: bool = False  # stopped part-way (cancel): the next look carries on from there

    @property
    def nothing(self) -> bool:
        return not (self.added or self.changed or self.removed)

    def note(self) -> str:
        """e.g. "3 new · 1 changed · 2 gone" ("" when nothing changed)."""
        parts = [f"{len(self.added)} new" if self.added else "",
                 f"{len(self.changed)} changed" if self.changed else "",
                 f"{len(self.removed)} gone" if self.removed else "",
                 f"{len(self.waiting)} still copying" if self.waiting else "",
                 self.unreadable_note()]
        return " · ".join(p for p in parts if p)

    def unreadable_note(self) -> str:
        """e.g. "2 folders can't be read: R.E.M., Vol." ("" when every folder could be)."""
        if not self.unreadable:
            return ""
        names = ", ".join(path.name or str(path) for path in self.unreadable[:2])
        more = " …" if len(self.unreadable) > 2 else ""
        return f"{len(self.unreadable)} folder{'' if len(self.unreadable) == 1 else 's'} can't be read: {names}{more}"

    def sort(self) -> Diff:
        for part in (self.added, self.changed, self.removed, self.waiting, self.unreadable):
            part.sort(key=lambda p: str(p).lower())
        return self


@dataclass(frozen=True)
class Progress:
    """Where a look has got to (also an event). `phase`: "" (not looking: `done` of `total` files
    were read by the last look), "upgrading" (bringing the old library_index.json across: `done`
    of `total` files), "looking" (listing folders: `done` so far), "reading" (`done` of `total`
    files read), "tidying"."""
    phase: str = ""
    done: int = 0
    total: int = 0
    folder: str = ""         # the folder in hand
    started: float = 0.0     # when the look began (seconds since 1970)


@dataclass(frozen=True)
class Started:
    reason: str
    roots: tuple[str, ...]


@dataclass(frozen=True)
class AlbumsChanged:
    """Albums were stored, changed or went: re-read them (the database's generation is now this)."""
    generation: int
    album_ids: frozenset[int]


@dataclass(frozen=True)
class Finished:
    diff: Diff


@dataclass(frozen=True)
class Failed:
    """The database couldn't be written (a full disk, a damaged file): what went wrong."""
    text: str


@dataclass(frozen=True)
class Unreachable:
    """A library folder couldn't be looked at just now; nothing stored for it was changed."""
    root: str
    why: str


class _Cancelled(Exception):
    pass


@dataclass
class _Job:
    """One folder to store: read `names` of its files (None: all of them; [] — nothing to read, just
    what's gone or back)."""
    root: str
    folder: str
    listing: dict[str, tuple[int, int, float]]
    names: list[str] | None
    cue_sig: str
    files: int = 0           # how many files reading it takes (for the progress count)
    listed_ok: bool = True   # False: it couldn't be listed (everything stored for it stays)
    added: list[str] = field(default_factory=list)     # what the diff says once it's stored
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    retried: list[str] = field(default_factory=list)   # couldn't be read last time: "changed" once read


@dataclass
class _Root:
    """One library folder's part of a look."""
    path: str
    jobs: list[_Job] = field(default_factory=list)
    vanished: list[int] = field(default_factory=list)     # stored folders gone from their parent's listing
    vanished_files: list[str] = field(default_factory=list)
    # Every folder could be listed, or was refused for good (access denied, or a name that can't be
    # opened): a first look can count as finished.
    clean: bool = True


# -- worker processes ------------------------------------------------------------------------------------
def _worker_start() -> None:
    """Each worker process, as it starts: lower its priority, so reading tags never takes the CPU
    from the player's own process."""
    lower_priority()


def lower_priority() -> None:
    """This process at below-normal priority (a worker process: the library check's too)."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32")   # (its own handle: argtypes set here change nothing else)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except (OSError, AttributeError):
        pass


def _read_job(root: str, folder: str, listing: dict, names: list[str] | None, cue_sig: str,
              reader: Callable[[Path], Track] | None) -> FolderResult:
    """A folder read in a worker process (or in this one)."""
    if reader is None:
        return read_folder(root, folder, listing, names, cue_sig)
    return read_folder(root, folder, listing, names, cue_sig, reader=reader)


def worker_check() -> dict:
    """Run in a worker process (the self-check, tests): what the worker has loaded. Neither Tk
    nor the player should ever be there."""
    return {"pid": os.getpid(), "tkinter": "tkinter" in sys.modules,
            "player": any(name in sys.modules for name in ("ironbark.ui", "ironbark.engine"))}


def make_pool(workers: int) -> ProcessPoolExecutor | None:
    """Worker processes for reading tags (started as each is needed), or None if they can't be."""
    try:
        return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                   initializer=_worker_start)
    except (OSError, ValueError, ImportError, NotImplementedError, RuntimeError):
        return None


# -- the indexer -----------------------------------------------------------------------------------------
class Indexer:
    """The only writer of the library database (see the module notes). One look at a time: a
    second caller waits for the first to finish. Any thread may call it except Tk's."""

    def __init__(self, db: LibraryDB, reader: Callable[[Path], Track] | None = None, pool_min: int = POOL_MIN,
                 fast: int | None = None, gentle: int = GENTLE_WORKERS,
                 pool_factory: Callable[[int], ProcessPoolExecutor | None] = make_pool):
        self.db = db
        self.reader = reader              # None: library.read_track
        self.pool_min = pool_min
        self.fast = fast if fast is not None else fast_workers()
        self.gentle = gentle
        self.pool_factory = pool_factory
        self.progress = Progress()        # replaced whole as a look goes on: read from any thread
        self.last_diff: Diff | None = None
        self.looked = 0.0                 # when a look last finished (this session)
        self.used_pool = False            # the last look read in worker processes
        self._lock = threading.Lock()     # one look (or update) at a time
        self._cancel = threading.Event()
        self._priority: deque[str] = deque()
        self._listeners: list[queue.SimpleQueue] = []
        self._listeners_lock = threading.Lock()
        self._tried_json: set[str] = set()   # library folders whose old JSON index has been tried
        self._thread: threading.Thread | None = None
        self._sent_progress = 0.0
        self._albums: set[int] = set()    # albums stored since the last AlbumsChanged
        self._sent_albums = 0.0
        self._read = (0, 0)               # (files read, files to read) by the look under way
        self._clock = 0.0                 # the last time handed out by _now

    # -- listeners and control --
    def listen(self) -> queue.SimpleQueue:
        """A queue that gets every event from now on (unlisten when done with it)."""
        events: queue.SimpleQueue = queue.SimpleQueue()
        with self._listeners_lock:
            self._listeners.append(events)
        return events

    def unlisten(self, events: queue.SimpleQueue) -> None:
        with self._listeners_lock:
            if events in self._listeners:
                self._listeners.remove(events)

    def _send(self, event) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        for events in listeners:
            events.put(event)

    @property
    def running(self) -> bool:
        """A look (or update) is under way."""
        return self._lock.locked()

    def cancel(self) -> None:
        """Stop the look under way after the folder in hand (what's stored so far stays; the next
        look carries on from there). Nothing happens if none is running."""
        self._cancel.set()

    def idle(self, timeout: float | None = None) -> bool:
        """Wait until no look is under way (at most `timeout` seconds); whether none is."""
        if not self._lock.acquire(timeout=-1 if timeout is None else timeout):
            return False
        self._lock.release()
        return True

    def prioritise(self, folder: str | Path) -> None:
        """Read this folder (and what's in it) next, e.g. an album opened during a first look."""
        self._priority.append(os.fspath(folder))

    def start(self, roots: Iterable[str | Path], reason: str = "", **options) -> threading.Thread:
        """check() on a thread of its own ("library-index"); the one already started if it's
        still running. Its result arrives as a Finished event (and in `last_diff`)."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            return thread
        roots = list(roots)

        def work():
            try:
                self.check(roots, reason=reason, **options)
            finally:
                self.db.release()

        self._thread = thread = threading.Thread(target=work, name="library-index", daemon=True)
        thread.start()
        return thread

    # -- a look --
    def check(self, roots: Iterable[str | Path], hold: Callable[[str, tuple], bool] | None = None,
              folders: Iterable[str | Path] | None = None, reason: str = "",
              json_index: Path | None = None, dry: bool = False) -> Diff:
        """Bring the database up to date with these library folders (they become the library's
        folders: one no longer listed is removed with everything stored for it). Returns what
        changed. `hold(path, (size, modified ns, created s))`: True leaves a new or changed file for
        the next look (it goes in the diff's `waiting`). `folders`: look only at these folders (not
        what's below them), e.g. again soon for a copy in progress; everything else is left for the
        next whole look. `json_index`: an old library_index.json to bring across before a library
        folder's first look. `dry`: only compare, nothing read or stored (`changes`). A folder
        inside another library folder is left out (library_db.outermost); no folders at all
        changes nothing."""
        roots = [str(r) for r in outermost(roots)]
        only = None if folders is None else {os.fspath(f) for f in folders}
        if not roots:
            return Diff(error="No library folder")
        with self._lock:
            self._cancel.clear()
            started = self._now()
            if not dry:
                self._send(Started(reason, tuple(roots)))
            diff = Diff()
            self._read = (0, 0)
            try:
                self._check(roots, hold, only, json_index, dry, started, diff)
            except _Cancelled:
                diff.cancelled = True
            except Exception as error:   # (a full disk, a damaged file): said, and the next look tries again
                log.exception("the library look failed")
                text = _failed(self.db, error)
                self._send(Failed(text))
                diff.error = f"{diff.error}; {text}" if diff.error else text
            finally:
                if not dry:
                    self._albums_changed(force=True)
                self._set_progress(Progress("", *self._read))   # (idle: what the look read)
            diff.sort()
            if not dry:
                self.last_diff = diff
                self.looked = time.time()
                self._send(Finished(diff))
            return diff

    def changes(self, roots: Iterable[str | Path]) -> Diff:
        """What's added / changed / gone since the database last looked; nothing is read or stored."""
        return self.check(roots, dry=True)

    def _check(self, roots: list[str], hold, only: set[str] | None, json_index: Path | None, dry: bool,
               started: float, diff: Diff) -> None:
        db = self.db
        if not dry:
            self._store_roots(roots)
        plans: list[_Root] = []
        for root in self._as_stored(roots):
            plan = _Root(root)
            why = self._plan(plan, hold, only, diff, started, json_index, dry)
            if why:
                diff.error = f"{diff.error}; {why}" if diff.error else why
                self._send(Unreachable(root, why))
                continue
            plans.append(plan)
        if dry:
            for plan in plans:
                for job in plan.jobs:
                    _note(diff, job)
                diff.removed += [Path(p) for p in plan.vanished_files]
            return
        built = {plan.path: bool(db.built(plan.path)) for plan in plans}
        for plan in plans:   # (first: a folder renamed or moved is then found as moved, not as new music)
            if plan.vanished:
                self._albums |= db.mark_missing(plan.vanished, now=self._now())
                diff.removed += [Path(p) for p in plan.vanished_files]
        self._store([job for plan in plans for job in plan.jobs],
                    self.gentle if all(built.values()) else self.fast, diff, started)
        self._set_progress(Progress("tidying", started=started), force=True)
        for plan in plans:
            if only is not None:
                continue
            # What's been missing a week goes for good — never sooner: another drive in the place of
            # this one, or an album put back from the Recycle Bin, keeps everything stored till then.
            db.purge_missing(plan.path)
            if plan.clean and not built[plan.path]:
                db.mark_built(plan.path, self._now())
        if only is None and db.needs_refold:
            # The songs' search texts were folded another way (searchfold changed), or stored before
            # this SQLite could index them: brought up to date, a batch at a time (a cancel resumes later).
            db.refold(cancel=self._cancel.is_set)

    def _as_stored(self, roots: list[str]) -> list[str]:
        """Each library folder as the database spells it (asked for in other capitals, say): what's
        stored under it is found by that spelling, so it's the one walked."""
        stored = self.db.roots()
        return [next((s for s in stored if Path(s) == Path(root)), root) for root in roots]

    def _store_roots(self, roots: list[str]) -> None:
        """The library folders are these: the database's list follows (one taken off it goes, with
        everything stored for it — only here, under the look's lock, so no look still under way
        for it can store anything after). One taken off whose music a new one holds (the same
        files, sizes and times: a drive back under another letter, the music copied to a new
        drive) is moved instead, keeping its dates, plays, favourites and checks."""
        stored = self.db.roots()
        if {Path(r) for r in stored} == {Path(r) for r in roots}:
            return
        new = [r for r in roots if not any(Path(r) == Path(s) for s in stored)]
        for old in [s for s in stored if not any(Path(s) == Path(r) for r in roots)]:
            samples = self.db.sample_files(old)
            place = next((r for r in new if samples and os.path.isdir(r) and _same_music(r, samples)), None)
            if place is not None and self.db.move_root(old, place):
                new.remove(place)
        self.db.set_roots(roots)

    def _same_drive(self, root: str, dry: bool) -> str:
        """"" when the library folder is on the drive its music was found on; else why it isn't
        looked at. A different drive in its place counts as the library's only once it holds the
        same music (copied onto a new drive), or has been there MISSING_DAYS (the drive replaced
        for good); until then nothing stored for the folder changes."""
        found = volume_of(root)
        if found is None:
            return ""
        db, now = self.db, time.time()
        stored = db.volume(root)
        if stored is None or stored[0] == found:
            if not dry and (stored is None or stored[1] is not None):
                db.set_volume(root, found)
            return ""
        since = stored[1] if stored[1] is not None else now
        if _same_music(root, db.sample_files(root)) or now - since >= library_db.MISSING_DAYS * 86400:
            if not dry:
                db.set_volume(root, found)
            return ""
        if not dry and stored[1] is None:
            db.set_volume(root, stored[0], since)
        return f"A different drive is at {root}"

    def _plan(self, plan: _Root, hold, only: set[str] | None, diff: Diff, started: float,
              json_index: Path | None, dry: bool) -> str:
        """Walk one library folder and compare it with what's stored, filling in `plan`; or why it
        can't be looked at just now ("" when it can)."""
        root, db = plan.path, self.db
        if not os.path.isdir(root):
            return f"Can't see the library folder {root}"
        why = self._same_drive(root, dry)
        if why:
            return why
        if not dry and only is None:
            self._bring_across(root, json_index, started)
        stored = db.folders(root)
        denied: set[str] = set()
        closed: set[str] = set()
        if only is None:
            listed, unlisted, denied, closed = self._walk(root, started)
            if root in unlisted:
                return f"Can't read the library folder {root}"
        else:
            listed, unlisted = _list_some(root, only), []
        if only is not None:   # (a quick look: only its own folders' stored files)
            stored = {folder: state for folder, state in stored.items() if folder in listed}
        states = db.file_states([state.id for state in stored.values()])
        if only is None and not any(listing for listing, _sig, _unknown in listed.values()) \
                and any(not s.missing for files in states.values() for s in files.values()):
            try:
                empty = not os.listdir(root)
            except OSError:
                empty = True
            if empty:
                return f"The library folder {root} is empty"
        if not os.path.isdir(root):   # (a drive can go while it's being walked)
            return f"Can't see the library folder {root}"
        # A folder its parent lists but that can't be opened by that name counts as refused for good
        # once it has failed before (its row says so): it would be the same on every look.
        plan.clean = all(folder in denied or (folder in closed and folder in stored
                                              and not stored[folder].listed_ok) for folder in unlisted)
        diff.unreadable += [Path(folder) for folder in closed]
        cue_folders = db.cue_track_folders([state.id for state in stored.values()])
        for folder, (listing, cue_sig, unknown) in listed.items():
            state = stored.get(folder)
            job = self._compare(root, folder, listing, cue_sig, unknown, state,
                                states.get(state.id, {}) if state is not None else {},
                                state is not None and state.id in cue_folders, hold, diff)
            if job is not None:
                plan.jobs.append(job)
        for folder in unlisted:   # said so (it keeps everything stored for it)
            state = stored.get(folder)
            if state is not None and state.listed_ok:
                plan.jobs.append(_Job(root, folder, {}, [], state.cue_sig, listed_ok=False))
            elif state is None and folder in closed:   # (noted, so the next look knows it failed before)
                plan.jobs.append(_Job(root, folder, {}, [], "", listed_ok=False))
        if only is None:
            hidden = tuple(os.path.join(folder, "") for folder in unlisted)
            for folder, state in stored.items():
                if folder in listed or folder in unlisted or (hidden and os.path.join(folder, "").startswith(hidden)):
                    continue
                gone = [name for name, s in states.get(state.id, {}).items() if not s.missing]
                if gone:
                    plan.vanished.append(state.id)
                    plan.vanished_files += [os.path.join(folder, name) for name in gone]
        plan.jobs.sort(key=lambda job: job.folder.lower())
        return ""

    def _walk(self, root: str, started: float):
        """Every folder under `root` (skipping system folders): {folder: (music files {name: (size,
        modified ns, created s)}, cue_signature, names that couldn't be looked at)}; the folders
        that couldn't be listed; those among them the system refused (access denied); and those
        its parent listed that can't be opened by that name (_cant_open)."""
        listed: dict[str, tuple[dict, str, set[str]]] = {}
        unlisted: list[str] = []
        denied: set[str] = set()
        closed: set[str] = set()
        stack = [root]
        while stack:
            if self._cancel.is_set():
                raise _Cancelled
            folder = stack.pop()
            try:
                listing, sig, unknown, subfolders = _list(folder)
            except OSError as error:
                unlisted.append(folder)
                if isinstance(error, PermissionError):
                    denied.add(folder)
                elif folder != root and _cant_open(error):
                    closed.add(folder)
                continue
            listed[folder] = (listing, sig, unknown)
            stack += sorted(subfolders, key=str.lower, reverse=True)
            self._set_progress(Progress("looking", len(listed), 0, folder, started))
        return listed, unlisted, denied, closed

    def _compare(self, root: str, folder: str, listing: dict, cue_sig: str, unknown: set[str],
                 state: FolderState | None, files: dict[str, FileState], held_cue_tracks: bool,
                 hold, diff: Diff) -> _Job | None:
        """One folder against what's stored: the job that brings it up to date, or None."""
        for name in unknown:   # (couldn't be looked at just now: taken to be as stored)
            old = files.get(name)
            if old is not None and not old.missing:
                listing[name] = (old.size, old.mtime_ns, 0.0)
        added, changed, reread, retried, back, waiting = [], [], [], [], [], []
        for name, stat in list(listing.items()):
            old = files.get(name)
            if old is None:
                found = added
            elif old.mtime_ns == -1 and old.size == stat[0] and not old.missing:
                retried.append(name)  # couldn't be read last time: tried again ("changed" only once read)
                continue
            elif (old.size, old.mtime_ns) != stat[:2]:
                found = changed      # (a file back again but different, too: it keeps its dates)
            elif old.missing:
                back.append(name)
                continue
            elif old.read_format < library_db.READ_FORMAT:
                reread.append(name)  # read by an older tag reader: read again quietly
                continue
            else:
                continue
            if hold is not None and hold(os.path.join(folder, name), stat):
                waiting.append(name)
                if old is None:
                    del listing[name]                                  # not there yet
                else:
                    listing[name] = (old.size, old.mtime_ns, stat[2])  # as it was, for now
                continue
            found.append(name)
        removed = [name for name, s in files.items() if not s.missing and name not in listing]
        diff.waiting += [Path(folder, name) for name in waiting]
        if not listing and not files:
            return None   # no music here, and none stored (a folder of .cue files alone, say)
        cue_changed = cue_sig != (state.cue_sig if state is not None else "")
        relisted = state is not None and not state.listed_ok
        if not (added or changed or reread or retried or back or removed or cue_changed or relisted):
            return None
        whole = bool(cue_sig) or held_cue_tracks or cue_changed
        if whole and waiting:
            return None   # its files split into tracks together: the whole folder waits for the copy
        to_read = added + changed + reread + retried
        if whole and listing and (to_read or back or removed or cue_changed):
            names, count = None, len(listing)
        else:
            names, count = to_read, len(to_read)
        return _Job(root, folder, listing, names, cue_sig, count, added=added, changed=changed, removed=removed,
                    retried=retried)

    def _bring_across(self, root: str, json_index: Path | None, started: float) -> None:
        """Before a library folder's first look: the old library_index.json, if it was for this
        folder, is brought across (dates and all), so nothing has to be read again."""
        if json_index is None or root in self._tried_json:
            return
        self._tried_json.add(root)
        if self.db.built(root) or self.db.folders(root) or not Path(json_index).is_file():
            return

        def progress(done: int, total: int):
            self._set_progress(Progress("upgrading", done, total, "", started), force=True)

        progress(0, 0)
        try:
            if self.db.import_json_index(json_index, progress=progress):
                self._albums_changed(everything=True)
        except Exception:   # (the old index is only a head start: without it, the look reads everything)
            log.exception("couldn't bring %s across", json_index)

    # -- reading and storing --
    def _store(self, jobs: list[_Job], workers: int, diff: Diff, started: float) -> None:
        """Read and store the jobs: the quick ones (nothing to read) first, then the reading, in
        worker processes when there's enough of it."""
        quick = [job for job in jobs if job.names == []]
        reads = deque(job for job in jobs if job.names != [])
        total = sum(job.files for job in reads)
        self._read = (0, total)
        done = 0
        for job in quick:
            self._check_cancel()
            self._apply(folder_result(job.root, job.folder, job.listing, {}, cue_sig=job.cue_sig,
                                      listed_ok=job.listed_ok), job, diff)
        self._set_progress(Progress("reading", 0, total, "", started), force=True)
        pool = self.pool_factory(workers) if reads and total >= self.pool_min else None
        self.used_pool = pool is not None
        in_flight: dict[Future, _Job] = {}
        try:
            while reads or in_flight:
                self._check_cancel()
                self._take_priority(reads)
                if pool is None:
                    job = reads.popleft()
                    result = _read_job(job.root, job.folder, job.listing, job.names, job.cue_sig, self.reader)
                else:
                    while reads and len(in_flight) < workers * _IN_FLIGHT:
                        job = reads.popleft()
                        in_flight[pool.submit(_read_job, job.root, job.folder, job.listing, job.names, job.cue_sig,
                                              self.reader)] = job
                    finished, _rest = wait(in_flight, timeout=0.25, return_when=FIRST_COMPLETED)
                    if not finished:
                        continue
                    future = next(iter(finished))
                    job = in_flight.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:   # the workers stopped, or couldn't take the job: carry on here
                        log.warning("reading tags in this process instead: %r", error)
                        reads.extendleft(reversed([job, *in_flight.values()]))
                        in_flight.clear()
                        pool.shutdown(wait=False, cancel_futures=True)
                        pool = None
                        continue
                self._apply(result, job, diff)
                done += job.files
                self._read = (done, total)
                self._set_progress(Progress("reading", done, total, job.folder, started))
        finally:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

    def _now(self) -> float:
        """The time, never the same twice: what a look marks missing is always after the look
        began (so it isn't purged by that look), even where the clock ticks coarsely (Windows)."""
        self._clock = max(time.time(), self._clock + 1e-6)
        return self._clock

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled

    def _take_priority(self, reads: deque[_Job]) -> None:
        """Move the folders asked for (prioritise) to the front."""
        while self._priority:
            wanted = self._priority.popleft()
            top = os.path.join(wanted, "")
            first = [job for job in reads if job.folder == wanted or job.folder.startswith(top)]
            for job in first:
                reads.remove(job)
            reads.extendleft(reversed(first))

    def _apply(self, result: FolderResult, job: _Job, diff: Diff) -> None:
        self._albums |= self.db.apply(result, now=self._now())
        _note(diff, job)
        failed = set(result.retry)   # (a file that still can't be read is tried again next time, unsaid)
        diff.changed += [Path(job.folder, name) for name in job.retried if name not in failed]
        self._albums_changed()

    def _albums_changed(self, force: bool = False, everything: bool = False) -> None:
        """An AlbumsChanged event for the albums stored since the last one, at most every
        ALBUMS_EVERY_S (`force`: now, if there are any; `everything`: now, for every album)."""
        now = time.monotonic()
        if everything or (self._albums and (force or now - self._sent_albums >= ALBUMS_EVERY_S)):
            albums, self._albums, self._sent_albums = self._albums, set(), now
            self._send(AlbumsChanged(self.db.generation, frozenset(() if everything else albums)))

    def _set_progress(self, progress: Progress, force: bool = False) -> None:
        self.progress = progress
        now = time.monotonic()
        if force or not progress.phase or now - self._sent_progress >= PROGRESS_EVERY_S:
            self._sent_progress = now
            self._send(progress)

    # -- a few files at once --
    def update(self, roots: Iterable[str | Path], paths: Iterable[str | Path]) -> Diff:
        """These files have just changed (a tag edit was saved): read them again now, even if
        their size and time look the same — just them (a folder with CUE sheets whole)."""
        roots = [str(r) for r in outermost(roots)]
        by_folder: dict[str, set[str]] = {}
        for path in paths:
            text = os.fspath(audiofile.real_path(path))
            by_folder.setdefault(os.path.dirname(text), set()).add(os.path.basename(text))
        diff = Diff()
        with self._lock:
            self._cancel.clear()
            try:
                stored_roots = self.db.roots()
                for folder, names in sorted(by_folder.items()):
                    root = next((r for r in roots if os.path.normcase(folder) == os.path.normcase(r)
                                 or os.path.normcase(folder).startswith(os.path.normcase(os.path.join(r, "")))), None)
                    stored = next((s for s in stored_roots if root is not None and Path(s) == Path(root)), None)
                    if stored is None:
                        continue
                    root, folder = stored, stored + folder[len(root):]
                    try:
                        listing, cue_sig = library_db.list_folder(folder)
                    except OSError:
                        continue
                    state = self.db.folders(root).get(folder)
                    files = self.db.file_states([state.id])[state.id] if state is not None else {}
                    whole = bool(cue_sig) or (state is not None and (
                        state.cue_sig != cue_sig or bool(self.db.cue_track_folders([state.id]))))
                    wanted = sorted(name for name in names if name in listing)
                    job = _Job(root, folder, listing, None if whole else wanted, cue_sig,
                               added=[n for n in wanted if n not in files],
                               changed=[n for n in wanted if n in files])
                    self._apply(_read_job(root, folder, listing, job.names, cue_sig, self.reader), job, diff)
            except Exception as error:
                log.exception("the library update failed")
                text = _failed(self.db, error)
                self._send(Failed(text))
                diff.error = text
            finally:
                self._albums_changed(force=True)
            diff.sort()
            self.last_diff = diff
            self._send(Finished(diff))
            return diff


def _failed(db: LibraryDB, error: BaseException) -> str:
    """What to say when the database couldn't be written. A damaged file is noted, to be put right
    at the next start (LibraryDB.note_damage)."""
    if db.note_damage(error):
        return f"The library database is damaged ({error}): it will be repaired when the player next starts"
    return f"The library database couldn't be updated: {error}"


def _note(diff: Diff, job: _Job) -> None:
    diff.added += [Path(job.folder, name) for name in job.added]
    diff.changed += [Path(job.folder, name) for name in job.changed]
    diff.removed += [Path(job.folder, name) for name in job.removed]


def volume_of(folder: str) -> int | None:
    """Which drive a folder is on — os.stat's st_dev: on Windows the volume's serial number, which
    stays with the drive whatever letter it's given — or None where that can't be told."""
    try:
        return os.stat(folder).st_dev or None
    except OSError:
        return None


def _same_music(root: str, samples: list[tuple[str, int, int]]) -> bool:
    """Does `root` hold these stored files (path below the library folder, size, modified ns: a
    sample from across it) — most of them, as they were? A modified time may differ by the 2 s a
    FAT-formatted drive keeps. No samples: there's nothing stored to tell by, so yes."""
    if not samples:
        return True
    found = 0
    for rel, size, mtime in samples:
        try:
            st = os.stat(os.path.join(root, rel))
        except OSError:
            continue
        found += st.st_size == size and abs(st.st_mtime_ns - mtime) <= 2_000_000_000
    return found * 4 >= len(samples) * 3


def _cant_open(error: OSError) -> bool:
    """A folder its parent has just listed that can't be opened by that name — a name Windows
    lists but won't open (ending in a dot or a space: made on a Mac), or a path longer than it
    takes — rather than one that failed just now."""
    return (isinstance(error, (FileNotFoundError, NotADirectoryError))
            or getattr(error, "winerror", None) in (3, 123, 206))   # (path not found, bad name, too long)


def _list_some(root: str, only: set[str]):
    """Just these folders under `root` (not what's below them), as _walk lists them; one that
    can't be listed now is left for the next whole look."""
    listed: dict[str, tuple[dict, str, set[str]]] = {}
    top = os.path.join(root, "")
    for folder in sorted(only):
        if folder != root and not folder.startswith(top):
            continue
        try:
            listing, sig, unknown, _subfolders = _list(folder)
        except OSError:
            continue
        listed[folder] = (listing, sig, unknown)
    return listed


def _list(folder: str) -> tuple[dict[str, tuple[int, int, float]], str, set[str], list[str]]:
    """One folder's listing: its music files {name: (size, modified ns, created s)} (the times come
    with the listing: no call per file), its cue_signature, the music files that couldn't be looked
    at, and the folders in it to go into (system folders and links left out). OSError when it
    can't be listed."""
    music: dict[str, tuple[int, int, float]] = {}
    sheets, unknown, subfolders = [], set(), []
    with os.scandir(folder) as entries:
        for entry in entries:
            name = entry.name
            try:
                if entry.is_dir():
                    if not skip_folder(name) and not entry.is_symlink():
                        subfolders.append(entry.path)
                elif is_music_name(name):
                    try:
                        st = entry.stat()
                    except OSError:
                        unknown.add(name)
                        continue
                    music[name] = (st.st_size, st.st_mtime_ns, float(getattr(st, "st_birthtime", st.st_mtime)))
                elif name.lower().endswith(".cue") and not name.startswith("._"):
                    st = entry.stat()
                    sheets.append((name, st.st_size, st.st_mtime_ns))
            except OSError:
                continue
    return music, cue_signature_of(sheets), unknown, subfolders


# -- the query thread ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Answer:
    kind: str
    ask: int                 # the number ask() gave (a window keeps only the newest of each kind)
    value: object = None
    error: BaseException | None = None


class Queries:
    """One "library-query" thread with its own database connection (see the module notes). Asks
    run in order; an ask that a newer one of its kind has replaced before it started is dropped.
    Answers go on `answers` (drain it from the window's own loop: nothing here touches Tk)."""

    def __init__(self, db: LibraryDB, name: str = "library-query"):
        self.db = db
        self.answers: queue.SimpleQueue[Answer] = queue.SimpleQueue()
        self._asks: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._latest: dict[str, int] = {}
        self._running: str | None = None
        self._numbers = itertools.count(1)
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def ask(self, kind: str, fn: Callable, *args, interrupt: bool = False) -> int:
        """Run fn(*args) on the thread; returns the ask's number (its Answer carries it). With
        `interrupt`, an ask of the same kind still running is stopped (a search typed over)."""
        with self._lock:
            number = next(self._numbers)
            self._latest[kind] = number
            running = self._running
        self._asks.put((kind, number, fn, args))
        if interrupt and running == kind:
            self.db.interrupt(self._thread)
        return number

    def drain(self) -> list[Answer]:
        found = []
        while True:
            try:
                found.append(self.answers.get_nowait())
            except queue.Empty:
                return found

    def stop(self) -> None:
        """Let the thread go (after the ask in hand); its connection is closed."""
        self._asks.put(None)

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            try:
                self.db._con()   # (connected before the first ask, so the first can already be interrupted)
            except Exception:    # (the ask that needs it says what's wrong)
                pass
            while True:
                item = self._asks.get()
                if item is None:
                    return
                kind, number, fn, args = item
                answer = self._answer(kind, number, fn, args)
                if answer is not None:
                    self.answers.put(answer)
        finally:
            self.db.release()

    def _answer(self, kind: str, number: int, fn: Callable, args: tuple) -> Answer | None:
        """Run one ask; None when a newer one of its kind replaced it (before it started, or while
        it ran: stopped). An interrupt meant for the ask before can land on this one, if that one
        finished just as it was sent: then this one, still the newest, simply runs again."""
        while True:
            with self._lock:
                if self._latest.get(kind) != number:
                    return None
                self._running = kind
            try:
                return Answer(kind, number, fn(*args))
            except Exception as error:   # (handed to the window, which says so)
                if not library_db.is_interrupt(error):
                    self.db.note_damage(error)
                    return Answer(kind, number, error=error)
            finally:
                with self._lock:
                    self._running = None

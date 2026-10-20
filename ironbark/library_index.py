"""The library index: the same methods the player used when the index was one JSON file
(library_index.json), now on top of the library database — library_db.py keeps every track (and
the search), indexer.py is the only part that writes it. Kept for one release, so the windows
move over in small steps. No UI.

Nothing here writes a whole file any more: each folder is stored as it's read, one transaction at
a time, so a big library is never written out again for one change.

- `refresh()` brings the database up to date with the library folders (indexer.Indexer.check):
  a walk of names, sizes and modified times (cheap), then tags are read **only for what changed**
  — a changed or added file just that file; a folder with CUE sheets the whole folder. It returns
  a `Diff` (added / changed / removed files).
- `changes()` is the same comparison without reading or storing anything.
- `update(paths)`: these files changed under us (e.g. a tag edit was saved) — read them now.
- `refresh(hold=…)`: files the caller says are still being written (watcher.py: a copy in
  progress) are left out this time and listed in the diff's `waiting`; `refresh(folders=…)` looks
  only at those folders (again soon, for a copy in progress).
- `added`: on a library folder's first look, the file's creation time (when it was copied into
  the library, on Windows); after that, the moment the database first noticed it. A changed file
  keeps its `added` and `seen` — a tag edit is a change, never something new.
- **Not being able to see something is never taken as it being deleted** (indexer.py): a library
  folder that isn't there, can't be listed or is suddenly empty changes nothing, and the diff
  carries an `error`; a subfolder that can't be listed keeps everything stored for it.
- The reads (`albums`, `album_tracks`, …) go to the database from whichever thread asks. The
  ones that take in the whole library are for worker threads, never Tk's.
- An old library_index.json (`FILE_NAME`, next to the settings) is brought across once, before
  the first look, with every date as it was; it's then renamed library_index.json.migrated.

`shared(folders)` is the one index the player uses: one database and one indexer per settings
folder, so a change of library folders stops a look still under way for the old ones before the
new ones are stored, and two looks never write at once.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import audiofile, library_db, settings
from .indexer import Diff, Indexer, Progress
from .library import Album, Track, read_track
from .library_db import LibraryDB, track_from_dict  # noqa: F401

__all__ = ["Diff", "Entry", "FILE_NAME", "FORMAT", "LibraryIndex", "index_path", "shared", "track_from_dict",
           "track_to_dict"]

FILE_NAME = "library_index.json"
FORMAT = library_db.READ_FORMAT   # the tag reader's version (a JSON index of another one isn't brought across)


def index_path() -> Path:
    """Where the old JSON index was kept (next to the settings): brought across once."""
    return settings.settings_path().parent / FILE_NAME


# -- tracks to and from JSON (the old index's format) ------------------------------------------------
_SKIP = {"path", "embedded_cue", "missing", "unreadable"}
_FIELDS = [f for f in dataclasses.fields(Track) if f.name not in _SKIP]
_DEFAULTS = {f.name: f.default for f in _FIELDS if f.default is not dataclasses.MISSING}


def track_to_dict(track: Track) -> dict:
    """A Track as plain JSON values (fields left at their default are left out)."""
    data = {}
    for f in _FIELDS:
        value = getattr(track, f.name)
        if f.name in _DEFAULTS and value == _DEFAULTS[f.name]:
            continue
        data[f.name] = [list(pair) for pair in value] if f.name == "details" else value
    cue = audiofile.cue_of(track.path)
    if cue is not None:
        data["cue"] = [cue.sheet, cue.number, cue.start, cue.end]
    return data


@dataclass
class Entry:
    """One music file as stored (LibraryIndex.entries)."""
    size: int
    mtime: int                 # modified time, nanoseconds (-1: due to be read again)
    added: float               # when the file arrived (seconds since 1970; see the module notes)
    seen: float                # when the database first saw it
    tracks: list[Track]


def _read(path: Path) -> Track:
    """The tag reader looks go through: read_track, looked up as it's called (so it can be stood in for)."""
    return read_track(path)


def _folders(value) -> list[Path]:
    """One library folder or several, as a list of Paths (each once, in order, none inside
    another: library_db.outermost)."""
    if isinstance(value, (str, os.PathLike)):
        return [Path(value)]
    return library_db.outermost(value)


# -- the one database and indexer per settings folder ------------------------------------------------
_open: tuple[Path, LibraryDB, Indexer] | None = None
_open_lock = threading.Lock()


def _database(path: Path) -> tuple[LibraryDB, Indexer]:
    """The database at `path` and its indexer, made the first time (nothing is read or written
    until the database is first used: library_db.LibraryDB.open lazy)."""
    global _open
    with _open_lock:
        if _open is None or _open[0] != path:
            db = LibraryDB.open(path, lazy=True)
            _open = (path, db, Indexer(db, reader=_read))
        return _open[1], _open[2]


class LibraryIndex:
    """The library: its folders' music, from the database (see the module notes)."""

    def __init__(self, root: str | Path | Iterable[str | Path], path: Path | None = None):
        self.roots: list[Path] = _folders(root)
        self.path = path or index_path()   # the old JSON index, brought across before the first look
        self.refreshed = 0.0               # when it was last brought up to date (this session)
        self.last_diff: Diff | None = None
        self._db_path = library_db.db_path()

    @property
    def root(self) -> Path:
        """The (first) library folder."""
        return self.roots[0]

    @property
    def db(self) -> LibraryDB:
        return _database(self._db_path)[0]

    @property
    def indexer(self) -> Indexer:
        return _database(self._db_path)[1]

    def set_roots(self, roots: str | Path | Iterable[str | Path]) -> None:
        """The library folders are now these. A look still under way for the old ones is stopped;
        the next look stores the change (and removes what was stored for a folder taken off)."""
        roots = _folders(roots)
        if roots != self.roots:
            self.roots = roots
            self.refreshed = 0.0
            self.indexer.cancel()

    def holds(self, path) -> bool:
        """Is this file or folder in one of the library folders?"""
        return self._mine(os.fspath(path))

    def _mine(self, path: str) -> bool:
        """Is this file or folder in one of the library folders?"""
        path = os.path.normcase(path)
        for root in self.roots:
            top = os.path.normcase(str(root))
            if path == top or path.startswith(os.path.join(top, "")):
                return True
        return False

    # -- what the old JSON file had --
    @property
    def built(self) -> float:
        """When the library folders' first look finished (0 while any of them hasn't yet)."""
        times = [self.db.built(root) for root in self.roots]
        return max(times) if times and all(times) else 0.0

    def unbuilt(self) -> list[Path]:
        """The library folders whose first look hasn't finished yet (one on a drive that's never
        been plugged in stays here; the others carry on regardless)."""
        return [root for root in self.roots if not self.db.built(root)]

    @property
    def progress(self) -> tuple[int, int]:
        """(files read, files to read) during a look, or by the last one ((0, 0) while folders
        are being listed)."""
        now = self.indexer.progress
        return (now.done, now.total) if now.phase in ("", "reading", "upgrading") else (0, 0)

    @property
    def entries(self) -> dict[str, Entry]:
        """Every music file stored: {path: Entry}. The whole library in memory, for checks — not for
        the player's windows."""
        return {path: Entry(*values) for path, values in self.db.files().items() if self._mine(path)}

    def load(self) -> bool:
        """Is anything stored for the library folders yet?"""
        return any(self.db.built(root) or self.db.folders(root) for root in self.roots)

    def save(self) -> None:
        """Nothing to do: the database stores each folder as it's read."""

    # -- looking at the folders --
    def changes(self) -> Diff:
        """What's added / changed / removed since the database last looked; nothing is read or
        stored. (Only the diff's `error` when a folder can't be looked at just now.)"""
        return self.indexer.changes(self.roots)

    def refresh(self, hold: Callable[[str, tuple], bool] | None = None,
                folders: Iterable[str | Path] | None = None) -> Diff:
        """Bring the database up to date with the library folders, reading tags only for what
        changed. Safe to call from any thread except Tk's (one look runs at a time).
        `hold(path, (size, modified ns, created s))`: asked about each new or changed file; True =
        it's still being written, leave it for next time (it goes in the diff's `waiting`).
        `folders`: look again at just these. When a library folder can't be looked at just now,
        nothing stored for it is changed and the diff says why in its `error`."""
        diff = self.indexer.check(self.roots, hold=hold, folders=folders, json_index=self.path)
        if folders is None and not diff.cancelled and not diff.error:
            self.refreshed = time.time()
        self.last_diff = diff
        return diff

    def rebuild(self) -> Diff:
        """Forget everything stored for the library and read its folders again from scratch
        (Library → ⋯ → Rebuild the library): for when the Library still shows music that has gone.
        Plays, favourites and quality checks stay. Safe from any thread except Tk's."""
        diff = self.indexer.check(self.roots, fresh=True)
        if not diff.cancelled and not diff.error:
            self.refreshed = time.time()
        self.last_diff = diff
        return diff

    def update(self, paths: Iterable[str | Path]) -> Diff:
        """These files have just changed (a tag edit was saved): read them again now."""
        diff = self.indexer.update(self.roots, paths)
        self.last_diff = diff
        return diff

    def listen(self):
        """The indexer's events from now on (indexer.Indexer.listen)."""
        return self.indexer.listen()

    def unlisten(self, events) -> None:
        self.indexer.unlisten(events)

    # -- what the Library and tabs read (any thread but Tk's) --
    def tracks(self) -> list[Track]:
        found: list[Track] = []
        for root in self.roots:
            found += self.db.album_tracks(root, whole_tree=True, check=False) or []
        return found

    def added_times(self) -> dict[Path, float]:
        return {Path(path): added for path, (added, _seen) in self.db.file_times().items() if self._mine(path)}

    def seen_times(self) -> dict[Path, float]:
        """When the database first saw each file (a tag edit keeps it: watcher.new_albums)."""
        return {Path(path): seen for path, (_added, seen) in self.db.file_times().items() if self._mine(path)}

    def albums(self) -> list[Album]:
        """Every album in the library folders, with the details the browse views need (browse.py)."""
        return [album for album in self.db.albums() if self._mine(os.fspath(album.path))]

    def album_tracks(self, folder: Path, whole_tree: bool = False) -> list[Track] | None:
        """The tracks library.scan_album (or scan_folder, `whole_tree`) would give for `folder`,
        in play order, from the database — or None when the folder isn't in the library or anything
        in it has changed since it was stored (the caller then reads the files itself)."""
        if not self._mine(os.fspath(folder)):
            return None
        return self.db.album_tracks(Path(folder), whole_tree=whole_tree)

    def albums_tracks(self, folders: Iterable[str | Path]) -> dict[Path, list[Track]]:
        """Many albums' tracks at once, in play order, from the database alone (nothing on disk is
        looked at): {album folder: tracks}; albums not in the library are left out."""
        return self.db.albums_tracks(f for f in folders if self._mine(os.fspath(f)))

    def album_of(self, folder: Path) -> Album | None:
        """One album's browse details (e.g. for a tab), from the database; None if not stored."""
        return self.db.album_of(folder) if self._mine(os.fspath(folder)) else None

    def new_album_times(self, since: float) -> list[tuple[Path, float]]:
        """library_db.LibraryDB.new_album_times, for the library folders."""
        return [(folder, seen) for folder, seen in self.db.new_album_times(since) if self._mine(os.fspath(folder))]

    def album_folders(self) -> set[str]:
        """Every album folder in the library folders."""
        return {folder for folder in self.db.album_folders() if self._mine(folder)}


_shared: LibraryIndex | None = None
_shared_lock = threading.Lock()


def building() -> tuple[str, Progress] | None:
    """A first look ("build") or a rebuild ("rebuild") under way, and how far it has got — or None.
    Read from memory, nothing from the database: safe on Tk's thread, every poll. (A routine look
    for changes isn't one: it's quick, and nothing needs saying.)"""
    opened = _open
    if opened is None:
        return None
    indexer = opened[2]
    kind = indexer.building
    return (kind, indexer.progress) if kind else None


def shared(root: str | Path | Iterable[str | Path]) -> LibraryIndex:
    """The one index the player uses for its library folders. Asked for other folders, the same
    index takes them on (set_roots); a new one is made only when the settings folder changes."""
    global _shared
    with _shared_lock:
        if _shared is None or _shared.path != index_path() or _shared._db_path != library_db.db_path():
            _shared = LibraryIndex(root)
        else:
            _shared.set_roots(root)
        return _shared


def close_shared(timeout: float = 5.0) -> None:
    """Let go of the database (e.g. before the folder it's in is deleted): a look under way is
    stopped first (waiting up to `timeout` seconds for it)."""
    global _shared, _open
    with _shared_lock:
        _shared = None
    with _open_lock:
        opened, _open = _open, None
    if opened is None:
        return
    _path, db, indexer = opened
    indexer.cancel()
    indexer.idle(timeout)
    db.close()

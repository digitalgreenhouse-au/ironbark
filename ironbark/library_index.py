"""The saved library index: every track's tags and details, kept in `library_index.json` next to
the settings, so the Library opens without re-reading every music file. No UI.

What's stored, per music file under the library folder: its size and modified time (to tell
when it has changed), when it arrived (`added`, for **Recently added**) and when the index first
saw it (`seen`), and the tracks read from it — one, or one per track for a CUE image. Per folder:
the size and modified time of its `.cue` files, because a sheet changes how its folder's files
are listed (library.split_cue_images).

- `refresh()` brings the index up to date: a walk of the folder (names, sizes, modified times —
  cheap), then tags are read **only for what changed**. A changed or added file re-reads just
  that file; a folder with CUE sheets re-reads the whole folder (the split depends on all of it).
  It returns a `Diff` (added / changed / removed files).
- `changes()` is the same comparison without reading anything (spotting new music).
- `update(paths)`: these files changed under us (e.g. a tag edit was saved) — re-read them now.
- `refresh(hold=…)`: files the caller says are still being written (watcher.py: a copy
  in progress) are left out this time — a new one isn't added yet, a changed one keeps its old
  entry — and listed in the diff's `waiting`, to be looked at again shortly.
- `added`: on the very first build, the file's creation time (when it was copied into the
  library, on Windows); after that, the moment the index first noticed it. A changed file keeps
  its `added` and `seen` — a tag edit is a change, never something new.
- **Not being able to see something is never taken as it being deleted.** A library folder that
  isn't there (a drive unplugged, a network share asleep), can't be listed, or is suddenly empty
  while the index holds music leaves the index exactly as it was: the diff carries an `error`.
  A subfolder that can't be listed, or a file that can't be looked at, keeps its entries as
  they were; a file that couldn't be read just now (library.Track.unreadable) is read again on
  the next look, as a change — so none of them comes back later as "new" with reset dates.

The file is written atomically (a temporary file, flushed to the disk, then a swap), like
playlists.py, so a crash can't leave half an index behind; a damaged or out-of-date index is
simply rebuilt.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__, audiofile, settings
from .audiofile import CuePath, CueRange
from .browse import albums_from
from .library import (_DISC_FOLDER, Album, Track, album_folder, is_music_name, play_order, read_track,
                      skip_folder, split_cue_images)

FILE_NAME = "library_index.json"
FORMAT = 1  # bump when read_track() learns something new, so every file is read again once


def index_path() -> Path:
    return settings.settings_path().parent / FILE_NAME


# -- tracks to and from JSON ---------------------------------------------------------------
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


def track_from_dict(path: Path, data: dict) -> Track:
    values = {k: v for k, v in data.items() if k in _DEFAULTS or k == "title"}
    values["details"] = tuple(tuple(pair) for pair in data.get("details", ()))
    cue = data.get("cue")
    if cue:
        path = CuePath(path, cue=CueRange(str(cue[0]), int(cue[1]), int(cue[2]),
                                          None if cue[3] is None else int(cue[3])))
    return Track(path=path, **values)


# -- the index -------------------------------------------------------------------------------
@dataclass
class Diff:
    """What changed in the library folder since the index last looked."""
    added: list[Path] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    waiting: list[Path] = field(default_factory=list)   # still being copied: left for next time
    error: str = ""     # the library folder couldn't be looked at: nothing was compared or changed

    @property
    def nothing(self) -> bool:
        return not (self.added or self.changed or self.removed)

    def note(self) -> str:
        """e.g. "3 new · 1 changed · 2 gone" ("" when nothing changed)."""
        parts = [f"{len(self.added)} new" if self.added else "",
                 f"{len(self.changed)} changed" if self.changed else "",
                 f"{len(self.removed)} gone" if self.removed else "",
                 f"{len(self.waiting)} still copying" if self.waiting else ""]
        return " · ".join(p for p in parts if p)


@dataclass
class Entry:
    size: int
    mtime: int                 # modified time, nanoseconds
    added: float               # when the file arrived (seconds since 1970; see the module notes)
    seen: float                # when the index first saw it
    tracks: list[Track]


@dataclass
class _Walk:
    files: dict[str, tuple[int, int, float]]   # path -> (size, modified ns, created s)
    cues: dict[str, list[list]]                # folder -> [[name, size, modified ns], …] of its .cue files
    folders: dict[str, list[str]]              # folder -> its music files
    unlisted: list[str] = field(default_factory=list)   # folders that couldn't be listed


def _same_file(entry: Entry | None, stat: tuple) -> bool:
    return entry is not None and entry.size == stat[0] and entry.mtime == stat[1]


class LibraryIndex:
    def __init__(self, root: str | Path, path: Path | None = None):
        self.root = Path(root)
        self.path = path or index_path()
        self.entries: dict[str, Entry] = {}
        self.cues: dict[str, list[list]] = {}
        self.built = 0.0           # when the index was first built (0 = never)
        self.refreshed = 0.0       # when it was last brought up to date (this session)
        self.progress = (0, 0)     # (files read, files to read) during a refresh
        self.last_diff: Diff | None = None
        self._lock = threading.RLock()            # guards entries / cues
        self._refreshing = threading.Lock()       # one refresh at a time
        self.load()

    # -- the file --------------------------------------------------------------------------
    def load(self) -> bool:
        """Read the saved index. False (and an empty index) when there's none, it's damaged, it
        was made by an older reader (FORMAT) or for a different library folder."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("format") != FORMAT or Path(data.get("root", "")) != self.root:
                return False
            entries = {}
            for name, item in data["files"].items():
                path = Path(name)
                entries[name] = Entry(int(item["size"]), int(item["mtime"]), float(item["added"]),
                                      float(item["seen"]), [track_from_dict(path, t) for t in item["tracks"]])
            cues = {str(k): list(v) for k, v in data.get("cues", {}).items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False
        with self._lock:
            self.entries, self.cues, self.built = entries, cues, float(data.get("built", 0.0))
        return True

    def save(self) -> None:
        """Write the index atomically. A failure is ignored: the index is only a speed-up."""
        try:
            with self._lock:
                data = {"format": FORMAT, "app": __version__, "root": str(self.root), "built": self.built,
                        "saved": time.time(), "cues": self.cues,
                        "files": {name: {"size": e.size, "mtime": e.mtime, "added": e.added, "seen": e.seen,
                                         "tracks": [track_to_dict(t) for t in e.tracks]}
                                  for name, e in self.entries.items()}}
                # ASCII only: a file name that isn't valid Unicode (Windows allows some) is written
                # as an escape that reads back as the same name, where UTF-8 couldn't hold it.
                text = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
            settings.write_file(self.path, text)  # never a half-written index
        except Exception:  # noqa: BLE001 — the index is only a speed-up; never fatal
            pass

    # -- looking at the folder ---------------------------------------------------------------
    def _walk(self) -> _Walk:
        """The folder as it is now. What couldn't be seen is taken as it was: a music file whose
        size and time couldn't be read, or anything under a folder that couldn't be listed, keeps
        its indexed details (and its folder's CUE sheets), so it's neither changed nor gone."""
        files: dict[str, tuple[int, int, float]] = {}
        cues: dict[str, list[list]] = {}
        folders: dict[str, list[str]] = {}
        unlisted: list[str] = []
        unknown: set[str] = set()

        def cant_list(error: OSError):
            unlisted.append(error.filename if isinstance(error.filename, str) else str(self.root))

        for folder, dirs, names in os.walk(self.root, onerror=cant_list):
            dirs[:] = [d for d in dirs if not skip_folder(d)]
            music = []
            for name in names:
                if not is_music_name(name):
                    continue
                path = os.path.join(folder, name)
                try:
                    st = os.stat(path)
                except OSError:
                    unknown.add(path)
                    continue
                files[path] = (st.st_size, st.st_mtime_ns, float(getattr(st, "st_birthtime", st.st_mtime)))
                music.append(path)
            if music:
                folders[folder] = music
                sheet = []
                for name in sorted(n for n in names if n.lower().endswith(".cue") and not n.startswith("._")):
                    try:
                        st = os.stat(os.path.join(folder, name))
                    except OSError:
                        continue
                    sheet.append([name, st.st_size, st.st_mtime_ns])
                if sheet:
                    cues[folder] = sheet
        hidden = tuple(os.path.join(folder, "") for folder in unlisted)   # (each ends in a separator)
        if unknown or hidden:
            with self._lock:
                for name, entry in self.entries.items():
                    if name not in files and (name in unknown or name.startswith(hidden)):
                        files[name] = (entry.size, entry.mtime, entry.added)
                for folder, sheet in self.cues.items():
                    if folder not in cues and os.path.join(folder, "").startswith(hidden):
                        cues[folder] = sheet
        return _Walk(files, cues, folders, unlisted)

    def _unreachable(self, walk: _Walk | None = None) -> str:
        """Why the library folder can't be looked at just now ("" when it can): it isn't there (a
        drive unplugged, a network share asleep), it couldn't be listed, or — after a walk that
        found no music at all — it's suddenly empty while the index holds music, as the mount
        point of an unplugged drive is."""
        if not self.root.is_dir():
            return f"Can't see the library folder {self.root}"
        if walk is None:
            return ""
        if str(self.root) in walk.unlisted:
            return f"Can't read the library folder {self.root}"
        if not walk.files and self.entries:
            try:
                empty = not os.listdir(self.root)
            except OSError:
                empty = True
            if empty:
                return f"The library folder {self.root} is empty"
        return ""

    def _look(self) -> tuple[_Walk | None, str]:
        """A walk of the folder, or why it can't be looked at just now (checked again after the
        walk: a drive can go while it's being walked)."""
        problem = self._unreachable()
        if problem:
            return None, problem
        walk = self._walk()
        return walk, self._unreachable(walk)

    def _compare(self, walk: _Walk) -> tuple[Diff, set[str]]:
        """The diff, and the folders whose tracks must be read again."""
        diff, dirty = Diff(), set()
        with self._lock:
            for name, stat in walk.files.items():
                entry = self.entries.get(name)
                if entry is None:
                    diff.added.append(Path(name))
                elif not _same_file(entry, stat):
                    diff.changed.append(Path(name))
                else:
                    continue
                dirty.add(os.path.dirname(name))
            for name in self.entries:
                if name not in walk.files:
                    diff.removed.append(Path(name))
                    dirty.add(os.path.dirname(name))
            for folder in set(walk.cues) | set(self.cues):
                if walk.cues.get(folder) != self.cues.get(folder):
                    dirty.add(folder)
        for part in (diff.added, diff.changed, diff.removed):
            part.sort(key=lambda p: str(p).lower())
        return diff, dirty

    def changes(self) -> Diff:
        """What's added / changed / removed since the index last looked; nothing is read or saved.
        (Only the diff's `error` when the folder can't be looked at just now.)"""
        walk, problem = self._look()
        return Diff(error=problem) if problem else self._compare(walk)[0]

    # -- bringing it up to date ----------------------------------------------------------------
    def refresh(self, hold: Callable[[str, tuple], bool] | None = None) -> Diff:
        """Bring the index up to date with the folder, reading tags only for what changed; saved
        when anything did. Safe to call from any thread (one refresh runs at a time).
        `hold(path, (size, modified ns, created s))`: asked about each new or changed file; True =
        it's still being written, leave it for next time (it goes in the diff's `waiting`).
        When the folder can't be looked at just now, nothing is changed or saved and the diff
        says why in its `error` (the next refresh looks again)."""
        with self._refreshing:
            walk, problem = self._look()
            if problem:
                self.last_diff = Diff(error=problem)
                return self.last_diff
            waiting = self._hold_back(walk, hold) if hold is not None else []
            diff, dirty = self._compare(walk)
            diff.waiting = waiting
            first_build = not self.built
            changed = {str(p) for p in diff.added + diff.changed}
            now = time.time()
            with self._lock:
                for path in diff.removed:
                    self.entries.pop(str(path), None)
            plan = []  # (folder, files to read, whole folder?)
            for folder in sorted(dirty):
                music = walk.folders.get(folder, [])
                if not music:
                    continue
                whole = bool(walk.cues.get(folder)) or self._has_cue_tracks(folder)
                plan.append((folder, music if whole else [p for p in music if p in changed], whole))
            total = sum(len(files) for _folder, files, _whole in plan)
            self.progress = (0, total)
            done = 0
            for folder, files, whole in plan:
                tracks = _read_all(files)
                if not whole and any(t.embedded_cue is not None for t in tracks):
                    # a file with its own CUESHEET: whether it's split depends on the whole folder
                    others = [p for p in walk.folders[folder] if p not in files]
                    tracks += _read_all(others)
                    files = files + others
                    whole = True
                if whole:
                    tracks = split_cue_images(tracks)
                by_file: dict[str, list[Track]] = {p: [] for p in files}
                for track in tracks:
                    by_file.setdefault(str(audiofile.real_path(track.path)), []).append(track)
                with self._lock:
                    for name, found in by_file.items():
                        old = self.entries.get(name)
                        if name not in walk.files:
                            self.entries.pop(name, None)
                            continue
                        if not found:  # couldn't be read just now: what was known stays, read again next time
                            if old is not None:
                                old.mtime = -1
                            continue
                        size, mtime, created = walk.files[name]
                        if any(t.unreadable for t in found):
                            mtime = -1  # only its name could be read just now: read it again next time
                        added = old.added if old else (min(created, now) if first_build else now)
                        self.entries[name] = Entry(size, mtime, added, old.seen if old else now,
                                                   [dataclasses.replace(t, embedded_cue=None) for t in found])
                done += len(files)
                self.progress = (min(done, total), total)
            with self._lock:
                self.cues = walk.cues
                if first_build:
                    self.built = now
            if not diff.nothing or dirty or first_build:
                self.save()
            self.refreshed = time.time()
            self.last_diff = diff
            return diff

    def _hold_back(self, walk: _Walk, hold: Callable[[str, tuple], bool]) -> list[Path]:
        """Take the files `hold` says are still being written out of the walk: a new one as if it
        weren't there yet, a changed one as if it were still as indexed."""
        waiting = []
        with self._lock:
            for name, stat in list(walk.files.items()):
                entry = self.entries.get(name)
                if _same_file(entry, stat) or not hold(name, stat):
                    continue
                waiting.append(Path(name))
                if entry is None:
                    del walk.files[name]
                    folder = os.path.dirname(name)
                    walk.folders[folder] = [p for p in walk.folders.get(folder, []) if p != name]
                    if not walk.folders[folder]:
                        del walk.folders[folder]
                else:
                    walk.files[name] = (entry.size, entry.mtime, stat[2])
        waiting.sort(key=lambda p: str(p).lower())
        return waiting

    def update(self, paths: list[Path]) -> Diff:
        """These files have just changed (a tag edit was saved): re-read them (and
        anything else that changed meanwhile) now."""
        with self._lock:
            for path in paths:
                entry = self.entries.get(str(audiofile.real_path(Path(path))))
                if entry is not None:
                    entry.mtime = -1  # forces a re-read, keeping added / seen
        return self.refresh()

    def _has_cue_tracks(self, folder: str) -> bool:
        with self._lock:
            return any(os.path.dirname(name) == folder and any(t.cue is not None for t in e.tracks)
                       for name, e in self.entries.items())

    # -- what the Library and tabs read ---------------------------------------------------------
    def tracks(self) -> list[Track]:
        with self._lock:
            return [t for e in self.entries.values() for t in e.tracks]

    def added_times(self) -> dict[Path, float]:
        with self._lock:
            return {Path(name): e.added for name, e in self.entries.items()}

    def seen_times(self) -> dict[Path, float]:
        """When the index first saw each file (a tag edit keeps it: watcher.new_albums)."""
        with self._lock:
            return {Path(name): e.seen for name, e in self.entries.items()}

    def albums(self) -> list[Album]:
        """Every album in the index, with the details the browse views need (browse.py)."""
        with self._lock:
            added = {name: e.added for name, e in self.entries.items()}   # (keyed by text: no Path per file)
        return albums_from(self.tracks(), added, self.root)

    def album_tracks(self, folder: Path, whole_tree: bool = False) -> list[Track] | None:
        """The tracks library.scan_album (or scan_folder, `whole_tree`) would give for `folder`,
        in play order, from the index — or None when the folder isn't in the library or anything
        in it has changed since it was indexed (the caller then reads the files itself)."""
        folder = Path(folder)
        try:
            folder.relative_to(self.root)
        except ValueError:
            return None
        paths: list[str] = []
        try:
            if whole_tree:
                for here, dirs, names in os.walk(folder):
                    dirs[:] = [d for d in dirs if not skip_folder(d)]
                    paths += [os.path.join(here, n) for n in names if is_music_name(n)]
            else:
                for entry in folder.iterdir():
                    if entry.is_file() and is_music_name(entry.name):
                        paths.append(str(entry))
                    elif entry.is_dir() and _DISC_FOLDER.match(entry.name):
                        paths += [str(p) for p in entry.iterdir() if p.is_file() and is_music_name(p.name)]
        except OSError:
            return None
        tracks: list[Track] = []
        with self._lock:
            for name in paths:
                entry = self.entries.get(name)
                try:
                    st = os.stat(name)
                except OSError:
                    return None
                if not _same_file(entry, (st.st_size, st.st_mtime_ns)):
                    return None
                tracks += entry.tracks
            for here in {os.path.dirname(p) for p in paths}:
                if _cue_sheet(here) != self.cues.get(here):
                    return None
        return play_order(folder, tracks)

    def album_of(self, folder: Path) -> Album | None:
        """One album's browse details (e.g. for a tab), from the index; None if not indexed."""
        folder = Path(folder)
        with self._lock:
            tracks = [t for name, e in self.entries.items() for t in e.tracks
                      if album_folder(Path(name)) == folder]
        found = albums_from(tracks, self.added_times(), self.root) if tracks else []
        return found[0] if found else None


def _read_all(files: list[str]) -> list[Track]:
    """read_track for each file; one that vanished meanwhile is left out (the next refresh sees
    it as removed, or tries again). One that couldn't be read just now (locked, a network drive
    dropping out) comes back marked `unreadable`, and refresh reads it again next time."""
    tracks = []
    for name in files:
        try:
            tracks.append(read_track(Path(name)))
        except (OSError, RuntimeError, ValueError):
            continue
    return tracks


def _cue_sheet(folder: str) -> list[list] | None:
    sheet = []
    try:
        names = sorted(n for n in os.listdir(folder) if n.lower().endswith(".cue") and not n.startswith("._"))
    except OSError:
        return None
    for name in names:
        try:
            st = os.stat(os.path.join(folder, name))
        except OSError:
            continue
        sheet.append([name, st.st_size, st.st_mtime_ns])
    return sheet or None


_shared: LibraryIndex | None = None
_shared_lock = threading.Lock()


def shared(root: str | Path) -> LibraryIndex:
    """The one index the player uses for its library folder (loaded from disk the first time)."""
    global _shared
    with _shared_lock:
        if _shared is None or _shared.root != Path(root) or _shared.path != index_path():
            _shared = LibraryIndex(root)
        return _shared

"""Favourite songs, held in memory for the windows: the track list's right-click menu, the Library and
the song search show a ♥ beside each one and offer "Add to favourites" / "Remove from favourites".
No UI.

The list lives in the library database (LibraryDB.favourites, keyed by audiofile.key, so a song
keeps its ♥ however its rows are rebuilt). It's read once on a worker thread; after that a change
shows at once here and is stored on a worker of its own ("library-favourites"), one change at a
time and in order — waiting, when a long look of the library holds the database, rather than being
dropped. A window never waits for the database, and nothing here touches Tk.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

from . import audiofile, library_db

log = logging.getLogger(__name__)

HEART = "♥"


def key_of(song) -> str:
    """A song's favourite key: a Track's (or a path's) audiofile.key, or the key itself."""
    if isinstance(song, str):
        return song
    return audiofile.key(getattr(song, "path", song))


class Favourites:
    """`db_of()` gives the library database (library_index.shared(…).db), asked for on the workers
    only. `changed` callbacks run (on the caller's thread) after every change made here; `ready` is
    True once the stored list has been read (`take_loaded`, from the window's own loop)."""

    def __init__(self, db_of: Callable[[], object]):
        self._db_of = db_of
        self._keys: set[str] = set()
        self._made: dict[str, bool] = {}   # changes made before the stored list arrived (they win)
        self._loaded: queue.SimpleQueue = queue.SimpleQueue()
        self._writes: queue.SimpleQueue = queue.SimpleQueue()
        self._writer: threading.Thread | None = None
        self._lock = threading.Lock()
        self._writing = False              # a writer is running (or about to)
        self.ready = False
        self.changed: list[Callable[[], None]] = []

    def load(self) -> threading.Thread:
        """Read the stored list on a worker; `take_loaded` puts it to use."""
        def work():
            db = None
            try:
                db = self._db_of()
                self._loaded.put(set(db.favourites()))
            except Exception as error:   # (no favourites shown is better than no player)
                log.warning("couldn't read the favourites: %s", error)
                self._loaded.put(set())
            finally:
                if db is not None:
                    db.release()

        thread = threading.Thread(target=work, name="library-favourites-read", daemon=True)
        thread.start()
        return thread

    def take_loaded(self) -> bool:
        """The stored list, once it has been read: True the moment it's taken in."""
        try:
            keys = self._loaded.get_nowait()
        except queue.Empty:
            return False
        for key, on in self._made.items():
            (keys.add if on else keys.discard)(key)
        self._keys, self._made, self.ready = keys, {}, True
        self._tell()
        return True

    def __contains__(self, song) -> bool:
        return bool(self._keys) and key_of(song) in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    def keys(self) -> set[str]:
        return set(self._keys)

    def set(self, songs, on: bool) -> int:
        """Make these songs (Tracks, paths or keys) favourites, or not: shown at once, stored on the
        worker. Returns how many changed."""
        changed = 0
        for song in songs:
            key = key_of(song)
            if (key in self._keys) == on:
                continue
            (self._keys.add if on else self._keys.discard)(key)
            if not self.ready:
                self._made[key] = on
            self._writes.put((key, on))
            changed += 1
        if changed:
            self._start_writer()
            self._tell()
        return changed

    def toggle(self, songs) -> bool:
        """Add to favourites, or — when every one of them is a favourite already — remove. Returns
        whether they're favourites now."""
        songs = list(songs)
        on = not all(song in self for song in songs)
        self.set(songs, on)
        return on

    def _tell(self):
        for callback in list(self.changed):
            callback()

    def _start_writer(self):
        with self._lock:
            if self._writing:
                return
            self._writing = True
        self._writer = threading.Thread(target=self._write_all, name="library-favourites", daemon=True)
        self._writer.start()

    def _write_all(self):
        """Store the changes in the order they were made; the thread ends once none are left (or
        at `flush`'s word)."""
        db = None
        try:
            while True:
                try:
                    item = self._writes.get(timeout=2.0)
                except queue.Empty:
                    with self._lock:   # (none left: a change made from now on starts a new writer)
                        if self._writes.empty():
                            self._writing = False
                            return
                    continue
                if item is None:
                    with self._lock:
                        if self._writes.empty():
                            self._writing = False
                            return
                    continue
                key, on = item
                try:
                    db = db or self._db_of()
                    # (a long look can hold the database a while: the change waits for it, never dropped)
                    library_db.when_free(lambda: db.set_favourite(key, on))
                except Exception as error:
                    if db is not None:
                        db.note_damage(error)
                    log.warning("couldn't store a favourite (%s): %s", key, error)
        finally:
            if db is not None:
                db.release()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait for the changes made so far to be stored (closing, tests). Whether they were."""
        writer = self._writer
        if writer is None or not writer.is_alive():
            return True
        self._writes.put(None)
        writer.join(timeout)
        return not writer.is_alive()

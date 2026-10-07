"""A tab of the whole library without a Track held for every song: `LibraryTrackList`.

The Library's "Open all albums in a tab" used to make a Track for each of a library's songs (778 MB
for 200,000) and the engine copied that list again. A LibraryTrackList is a sequence of the songs'
ids in the tab's order (8 bytes each), the albums' runs (`groups`, for the album header rows), and
an LRU of about KEEP Track objects, filled an album at a time from the library database on the
list's own loader thread. So:

- The track list (playlist_view.PlaylistView) draws a row from `peek(i)`; a song not read yet shows
  "…" and is asked for (`want(i)`); the list notices the fill by `serial` and draws again.
- The window keeps the songs round the one playing read ahead (`hold(indices)`: the song, the one
  before and the next few in play order), and they're never let go while held — so the engine,
  which reads the list under its lock as songs join gaplessly, finds every song it asks for
  already here. It never makes the list read the database itself in normal use; should it ask for
  one anyway (Next pressed many times faster than the reads), that song's album is read there and
  then, and `misses` counts it.
- The engine finds a song by its path through `index_of_path` (songs read, then a C-speed scan of
  the ids) instead of looking at every song.
- The in-tab search runs in the database (`matching`: the folded text it keeps for each song, the
  same the in-tab filter searches), and Ctrl+A's actions read their songs in one go (`many`).
- Songs added to the tab later are held as Tracks after the ids (`extras`); `+` makes a new list
  sharing the same reads.

Nothing here touches Tk. The loader thread reads through its own database connection and lets it
go when it has nothing left to do.
"""

from __future__ import annotations

import os
import threading
from array import array

from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from pathlib import Path

from . import audiofile, searchfold
from .library import LOSSY_CODECS, Track, group_by_album

KEEP = 5_000   # Track objects kept read (albums at a time); the songs held round the one playing come on top


class _Shared:
    """What every list made from one read of the library shares (a list and the ones `+` made from
    it): the Track objects read so far and the thread reading more."""

    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.cache: OrderedDict[int, Track] = OrderedDict()   # track id -> its Track, the newest last
        self.where: dict[Path, int] = {}                       # a read song's path -> its id
        self.held: set[int] = set()                            # ids never let go (round the song playing)
        self.serial = 0                                        # bumped with every album read
        self.misses = 0                                        # songs asked for that had to be read there and then


class LibraryTrackList(Sequence):
    """See the module notes. `ids`: the songs' ids in the tab's order; `groups`: (album folder, first
    index, end index) per album; `seconds`: how long they all last; `varies`: the optional columns
    whose value isn't the same on every song."""

    def __init__(self, db, ids: Sequence[int], groups: list[tuple[Path, int, int]], seconds: float = 0.0,
                 varies: Iterable[str] = (), extras: Sequence[Track] = (), shared: _Shared | None = None):
        self.ids = ids if isinstance(ids, array) else array("q", ids)
        self.extras: tuple[Track, ...] = tuple(extras)
        self._base = (list(groups), seconds)   # (the albums read, and how long they last: before the extras)
        self.groups = list(groups) + [(folder, start + len(self.ids), end + len(self.ids))
                                      for folder, start, end in group_by_album(list(self.extras))]
        self.seconds = seconds + sum(track.duration for track in self.extras)
        self.varies = frozenset(varies)
        self._shared = shared or _Shared(db)
        self._starts = [start for _folder, start, _end in self.groups]
        self._wanted: dict[int, None] = {}   # groups asked for, newest last
        self._thread: threading.Thread | None = None

    # -- a sequence ------------------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.ids) + len(self.extras)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self.many(range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        track = self.peek(index)
        if track is None:
            shared = self._shared
            with shared.lock:
                shared.misses += 1
            self._fill(self._group_of(index))
            track = self.peek(index)
            if track is None:
                track = _gone(self.ids[index])
        return track

    def __add__(self, other):
        """This list with more songs after it (a song or album added to the tab)."""
        groups, seconds = self._base
        return LibraryTrackList(self._shared.db, self.ids, groups, seconds, self.varies,
                                self.extras + tuple(other), shared=self._shared)

    def __iter__(self):
        for start in range(0, len(self), 1000):
            yield from self.many(range(start, min(len(self), start + 1000)))

    def __repr__(self) -> str:
        return f"LibraryTrackList({len(self):,} songs, {len(self.groups):,} albums)"

    @property
    def serial(self) -> int:
        return self._shared.serial

    @property
    def misses(self) -> int:
        return self._shared.misses

    @property
    def db(self):
        return self._shared.db

    # -- reading ------------------------------------------------------------------------------------
    def peek(self, index: int) -> Track | None:
        """Song `index` if it has been read (nothing is read here)."""
        if index >= len(self.ids):
            return self.extras[index - len(self.ids)]
        shared = self._shared
        with shared.lock:
            return shared.cache.get(self.ids[index])

    def _group_of(self, index: int) -> int:
        return bisect_right(self._starts, index) - 1

    def want(self, index: int):
        """Song `index` is on show: have its album read (on the loader thread)."""
        if index >= len(self.ids) or self.peek(index) is not None:
            return
        group = self._group_of(index)
        with self._shared.lock:
            self._wanted.pop(group, None)
            self._wanted[group] = None   # (the newest asked is read first)
            if self._thread is None:
                self._thread = threading.Thread(target=self._load, name="library-tab", daemon=True)
                self._thread.start()

    def waiting(self) -> bool:
        """Albums asked for and not read yet."""
        with self._shared.lock:
            return bool(self._wanted) or self._thread is not None

    def hold(self, indices: Iterable[int]):
        """Keep these songs read and never let go of them (the one playing, the one before it and the
        next few in play order: the engine reads them as songs join); the ones held before go back
        to the LRU. Those not read yet are read on the loader thread."""
        indices = list(indices)
        ids = {self.ids[i] for i in indices if 0 <= i < len(self.ids)}

        with self._shared.lock:
            self._shared.held = ids
        for index in indices:
            self.want(index)

    def ready(self, index: int, around: Iterable[int] = ()):
        """About to play song `index`: read its album here and now if it isn't read (one small read,
        so the engine finds it), and hold it and the songs `around` it (read on the loader thread)."""
        if 0 <= index < len(self.ids) and self.peek(index) is None:
            self._fill(self._group_of(index))
        self.hold([index, *around])

    def _load(self):
        shared = self._shared
        try:
            while True:
                with shared.lock:
                    if not self._wanted:
                        self._thread = None
                        return
                    group, _ = self._wanted.popitem()
                self._fill(group)
        except Exception:   # (the list shows "…" for what couldn't be read; asked again when next on show)
            with shared.lock:
                self._wanted.clear()
                self._thread = None
        finally:
            shared.db.release()

    def _fill(self, group: int):
        """Read album `group`'s songs into the LRU (any thread)."""
        _folder, start, end = self.groups[group]
        end = min(end, len(self.ids))
        if start >= end:
            return
        shared = self._shared
        ids = list(self.ids[start:end])
        with shared.lock:
            if all(i in shared.cache for i in ids):
                return
        read = shared.db.tracks_by_id(ids)
        with shared.lock:
            for track_id in ids:
                track = read.get(track_id) or _gone(track_id)
                shared.cache[track_id] = track
                shared.cache.move_to_end(track_id)
                shared.where[track.path] = track_id
            self._trim()
            shared.serial += 1

    def _trim(self):
        """Let go of the songs read longest ago, past KEEP (never the ones held). Called under the lock."""
        shared = self._shared
        cache, held = shared.cache, shared.held
        turns = len(cache)
        while len(cache) > KEEP + len(held) and turns > 0:
            turns -= 1
            track_id, track = cache.popitem(last=False)
            if track_id in held:
                cache[track_id] = track   # (back at the newest end)
                continue
            if shared.where.get(track.path) == track_id:
                del shared.where[track.path]

    def many(self, indices: Iterable[int]) -> list[Track]:
        """These songs, in this order: the ones read from the LRU, the rest from the database in one go
        (not kept: a Ctrl+A over the whole tab mustn't push out the songs on show)."""
        indices = list(indices)
        count = len(self.ids)
        shared = self._shared
        found: dict[int, Track] = {}
        missing = []
        with shared.lock:
            for i in indices:
                if i < count:
                    track = shared.cache.get(self.ids[i])
                    if track is None:
                        missing.append(self.ids[i])
                    else:
                        found[self.ids[i]] = track
        if missing:
            found.update(shared.db.tracks_by_id(missing))
        result = []
        for i in indices:
            if i >= count:
                result.append(self.extras[i - count])
            else:
                track = found.get(self.ids[i])
                if track is not None:
                    result.append(track)
        return result

    def loaded(self) -> list[Track]:
        """The songs read now (and the ones added after): what the list holds in memory."""
        with self._shared.lock:
            return list(self._shared.cache.values()) + list(self.extras)

    def forget(self, paths: Iterable[Path]):
        """These songs' tags changed: read them again when next asked for."""
        shared = self._shared
        with shared.lock:
            for path in paths:
                track_id = shared.where.pop(path, None)
                if track_id is not None:
                    shared.cache.pop(track_id, None)
            shared.serial += 1

    # -- finding ---------------------------------------------------------------------------------
    def index_of_path(self, path) -> int | None:
        """Where a song is in the list, by its path — found among the songs read (the engine asks only
        for songs it has played or lined up, which are), then by a scan of the ids. None: not here,
        or not read."""
        shared = self._shared
        with shared.lock:
            track_id = shared.where.get(path)
        if track_id is not None:
            try:
                return self.ids.index(track_id)
            except ValueError:
                pass
        for at, track in enumerate(self.extras):
            if track.path == path:
                return len(self.ids) + at
        return None

    def keys_in(self, keys: list[str]) -> set[str]:
        """Which of these songs (play keys: audiofile.key) the list holds — asked of the database,
        not by reading every song."""
        keys = list(keys)
        if not keys:
            return set()
        ids = set(self.ids)
        found = {key for key, hit in self._shared.db.songs_for_keys(keys) if hit.id in ids}
        extras = {audiofile.key(track.path) for track in self.extras}
        return found | {key for key in keys if key in extras}

    def index_of_key(self, key: str) -> int:

        """Where the song with this play key (audiofile.key) is: -1 when it isn't here. Asks the
        database (a worker's, or a moment on Tk's thread: one small read)."""
        found = self._shared.db.songs_by_keys([key])
        if found:
            try:
                return self.ids.index(found[0].id)
            except ValueError:
                pass
        for at, track in enumerate(self.extras):
            if audiofile.key(track.path) == key:
                return len(self.ids) + at
        return -1

    def matching(self, words: list[str]) -> list[int]:
        """The songs whose search text holds every word, in list order — searched by the database
        (its folded text for each song: the in-tab filter's own rule)."""
        found = self._shared.db.ids_matching(words)
        positions = [at for at, track_id in enumerate(self.ids) if track_id in found]
        count = len(self.ids)
        for at, track in enumerate(self.extras):
            haystack = searchfold.haystack(track)
            if all(word in haystack for word in words):
                positions.append(count + at)
        return positions


def _gone(track_id: int) -> Track:
    """A song gone from the library since the tab was read: listed as missing (never played)."""
    return Track(Path(f"song {track_id} (no longer in the library)"), "", missing=True)


def build(db, albums: Sequence) -> LibraryTrackList:

    """(A worker thread.) A tab of these albums (library.Album, in the order given) from the library
    database: their songs' ids in play order, without a Track made for each. An album the database
    doesn't hold is left out."""
    orders = db.album_orders([album.path for album in albums])
    ids, groups = array("q"), []
    seconds = 0.0
    artists, names, qualities = set(), set(), set()
    for album in albums:
        found = orders.get(os.fspath(album.path))
        if found is None or not found.ids:
            continue
        start = len(ids)
        ids.extend(found.ids)
        groups.append((album.path, start, len(ids)))
        seconds += found.seconds
        for into, values in ((artists, found.artists), (names, found.names), (qualities, found.qualities)):
            if len(into) < 2:
                into.update(values)
    varies = {column for column, values in (("artist", artists), ("album", names), ("quality", _shown(qualities)))
              if len(values) > 1}
    return LibraryTrackList(db, ids, groups, seconds, varies)


def _shown(qualities: set[tuple]) -> set[str]:
    """Qualities as the Format column shows them ("16/44.1", "MP3")."""
    return {codec
 if codec in LOSSY_CODECS else (f"{bits}/{rate / 1000:g}" if rate else "")
            for codec, bits, rate in qualities}

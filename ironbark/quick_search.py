"""Searching the whole library — the parts shared by the track list's Whole library search (0.40.0),
the Library's views (library_view.py) and the right-click menus of songs found there. (Until 0.40.0 this
was also a Ctrl+K panel over the main window; Ctrl+K now goes to the Library tab's search box.)

What's found (searchfold.py's folding: case and accents ignored, "lodz" finds Łódź):
- **Songs**, from the library database's search index (LibraryDB.search_songs): every word must
  start a word in the title, artist, album, album artist, composer, genre, year or file name; the
  best 200 are listed, with how many there are ("1,234 songs", or "1,000+" for the slow kind of
  search). A song found by something other than its title, artist or album says where
  ("composer: Bach").
- **Albums** and **Artists**, from a list of the library's albums (`Catalogue`) read on a query
  thread and read again whenever the library has changed — one copy, shared with the Library's
  views (`shared_catalogue`).

`Actions` is what the Library's song views and the track list's library rows ask the player to do;
`add_song_items` builds a song's right-click menu from them (Play, Play next, Add to Up next, Add to
playlist…, favourites, Show in Library, Show in Explorer); `song_values` is a song's row.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import library_db, platforms, searchfold
from .browse import album_label
from .favourites import HEART
from .library import Album, Track, sort_name
from .library_db import SongHit, SongHits
from .playlist_view import clock_text, quality_text

ALBUM_LIMIT = 50        # albums listed (the count says how many match)
ARTIST_LIMIT = 30
COLUMNS = ("title", "artist", "album", "year", "quality", "time")
HEADINGS = {"title": "Title", "artist": "Artist", "album": "Album", "year": "Year", "quality": "Format",
            "time": "Time"}


def count_text(count: int, word: str, capped: bool = False) -> str:
    """"1,234 songs", "1 album", "1,000+ songs"."""
    if capped:
        return f"{count:,}+ {word}s"
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def show_in_explorer(path) -> None:
    """The file's folder in Explorer, the file selected (Finder on a Mac). A CUE track: its whole file."""
    path = os.fspath(path)
    try:
        if platforms.IS_WINDOWS:
            subprocess.Popen(f'explorer /select,"{path}"')   # noqa: S603 — a path of the user's own library
        elif platforms.IS_MAC:
            subprocess.Popen(["open", "-R", path])   # noqa: S603, S607
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])   # noqa: S603, S607
    except OSError:
        pass


@dataclass
class Actions:
    """What the search panel and the Library's song views ask the player to do (ui.PlayerWindow
    fills these in; any left as they are does nothing)."""
    play_song: Callable[[Track, Path], None] = lambda track, album: None    # its album in a tab, playing from it
    queue_tracks: Callable[[list[Track], bool], None] = lambda tracks, front: None   # Up next (front: play next)
    open_album: Callable[[Path, bool], None] = lambda album, play: None
    queue_album: Callable[[Path, bool], None] = lambda album, front: None
    playlists: Callable[[], list[Path]] = lambda: []
    add_to_playlist: Callable[[Path, list[Track]], None] = lambda path, tracks: None
    new_playlist: Callable[[list[Track]], None] = lambda tracks: None
    show_in_library: Callable[[Path], None] = lambda album: None
    show_artist: Callable[[str], None] = lambda name: None
    show_in_explorer: Callable[[object], None] = show_in_explorer
    drag: Callable[[object, int, int, str], None] = lambda carried, x, y, phase: None   # songs (list) or album (Path)
    favourites: object = None   # favourites.Favourites, or None


# -- the search, on the query thread (nothing here touches Tk) --------------------------------------
@dataclass
class Catalogue:
    """The library's albums, for the Albums and Artists sections and the Library window's album views:
    read on a query thread, and read again there when the library has changed (LibraryDB.generation)
    — for the search panel at most every REREAD_S while it keeps changing (its first look changes it
    with every folder, and reading and folding every album would slow each search typed meanwhile);
    for the Library window as soon as it has (`update(db, max_age=0)`). One is shared by both
    (`shared_catalogue`): the album list and each album's folded text (browse.matches' own, the
    text the Library's album search looks through) are held once. A read swaps the lists whole, under
    a lock, so any query thread may read or update it."""
    REREAD_S = 10.0
    generation: int | None = None
    read_at: float = 0.0
    albums: list[Album] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)                     # each album's folded search text
    artists: list[tuple[str, str, int]] = field(default_factory=list)  # (name, folded, albums)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def update(self, db, max_age: float | None = None) -> None:
        """Read the albums again if the library has changed since they were read (and they were read
        more than `max_age` seconds ago: REREAD_S by default)."""
        wait = self.REREAD_S if max_age is None else max_age
        with self._lock:
            generation = db.generation
            if generation == self.generation or (
                    self.generation is not None and time.monotonic() - self.read_at < wait):
                return
            albums = db.albums()
            texts = [album_text(a) for a in albums]
            counts: dict[str, list] = {}
            for album in albums:
                if album.artist:
                    counts.setdefault(searchfold.fold(album.artist), [album.artist, 0])[1] += 1
            artists = sorted(((name, folded, n) for folded, (name, n) in counts.items()),
                             key=lambda item: sort_name(item[0]))
            self.albums, self.texts, self.artists = albums, texts, artists
            self.generation, self.read_at = generation, time.monotonic()

    def snapshot(self) -> tuple[list[Album], list[str]]:
        """(the albums, each one's folded search text) as last read: lists never changed in place."""
        with self._lock:
            return self.albums, self.texts

    def find(self, words: list[str]) -> tuple[list[Album], int, list[tuple[str, int]], int]:
        """(albums matching, how many, artists matching (name, albums), how many)."""
        with self._lock:
            listed, texts, known = self.albums, self.texts, self.artists
        albums = [a for a, text in zip(listed, texts) if all(w in text for w in words)]
        albums.sort(key=lambda a: (sort_name(a.artist), a.year or 0, sort_name(album_label(a))))
        artists = [(name, n) for name, folded, n in known if all(w in folded for w in words)]
        return albums[:ALBUM_LIMIT], len(albums), artists[:ARTIST_LIMIT], len(artists)


def album_text(album: Album) -> str:
    """What an album search looks through (browse.matches' text): name, title, artist, year, genres
    and composers, folded."""
    return searchfold.fold(" ".join([album.name, album.title, album.artist, str(album.year or ""),
                                     *album.genres, *album.composers]))


_catalogues: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()   # library database -> its Catalogue
_catalogues_lock = threading.Lock()


def shared_catalogue(db) -> Catalogue:
    """The one Catalogue of this library database (the search panel's and the Library window's)."""
    with _catalogues_lock:
        found = _catalogues.get(db)
        if found is None:
            found = _catalogues[db] = Catalogue()
        return found


@dataclass(frozen=True)
class Results:
    query: str
    songs: SongHits
    albums: list[Album]
    album_total: int
    artists: list[tuple[str, int]]
    artist_total: int

    @property
    def nothing(self) -> bool:
        return not (self.songs.rows or self.albums or self.artists)


def search_library(db, catalogue: Catalogue, query: str, limit: int = library_db.SEARCH_LIMIT) -> Results:
    """The panel's search (run on the query thread): songs from the index, albums and artists from
    the catalogue."""
    songs = db.search_songs(query, limit)
    catalogue.update(db)
    albums, album_total, artists, artist_total = catalogue.find(searchfold.words(query))
    return Results(query, songs, albums, album_total, artists, artist_total)


# -- rows and menus (shared with the Library's song views) ----------------------------------------
def matched_text(hit: SongHit) -> str:
    """'composer: Bach' — where a song was found, when it wasn't its title, artist or album."""
    if not hit.where:
        return ""
    track = hit.track
    value = {"album artist": track.album_artist, "composer": track.composer, "genre": track.genre,
             "year": str(track.year or ""), "file name": Path(os.fspath(track.path)).stem}.get(hit.where, "")
    return f"{hit.where}: {value}" if value else hit.where


def song_name(track: Track) -> str:
    """A song's title, or its file's name when it has none."""
    return track.title or Path(os.fspath(track.path)).stem


def song_values(hit: SongHit, inline_artist: bool = False, favourite: bool = False) -> tuple:
    """A song's cells (title, artist, album, year, quality, time). `inline_artist`: the artist rides
    in the title (no Artist column on show)."""
    track = hit.track
    title = song_name(track)
    if inline_artist and track.artist:
        title += f"  ·  {track.artist}"
    where = matched_text(hit)
    if where:
        title += f"  ·  {where}"
    if favourite:
        title = f"{HEART} {title}"
    return (title, track.artist, track.album, track.year or "", quality_text(track), clock_text(track.duration))


def add_song_items(menu, actions: Actions, hits: Sequence[SongHit], hints: dict[str, str] | None = None,
                   show_in_library: bool = True) -> None:
    """A song's right-click items (one song, or several): Play, Play next, Add to Up next, Open
    album, Add to playlist…, favourites, Show in Library, Show in Explorer. `hints`: the keys shown
    beside them (play / next / queue / album)."""
    hints = hints or {}
    first = hits[0]
    tracks = [hit.track for hit in hits]
    menu.add("Play", lambda: actions.play_song(first.track, first.album), hints.get("play", ""))
    menu.add("Play next", lambda: actions.queue_tracks(tracks, True), hints.get("next", ""))
    menu.add("Add to Up next", lambda: actions.queue_tracks(tracks, False), hints.get("queue", ""))
    menu.add("Open album", lambda: actions.open_album(first.album, False), hints.get("album", ""))
    menu.add_separator()
    add = menu.add_submenu("Add to playlist" if len(hits) == 1 else f"Add these {len(hits)} songs to playlist")
    for path in actions.playlists():
        add.add(path.stem, lambda p=path: actions.add_to_playlist(p, tracks))
    add.add_separator()
    add.add("New playlist…", lambda: actions.new_playlist(tracks))
    favourites = actions.favourites
    if favourites is not None:
        on = all(track in favourites for track in tracks)
        menu.add("Remove from favourites" if on else "Add to favourites",
                 lambda: favourites.set(tracks, not on), HEART)
    menu.add_separator()
    if show_in_library:
        menu.add("Show in Library", lambda: actions.show_in_library(first.album))
    menu.add("Show in Explorer", lambda: actions.show_in_explorer(first.track.path))


# -- the panel ---------------------------------------------------------------------------------------

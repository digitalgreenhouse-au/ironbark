"""The Library's views, shown inside the main Library tab in place of the track list (a frame the
player packs and unpacks; until 0.40.0 a window of its own): every album in the library folders, from
the library database (library_index.py over library_db.py), so it opens at once and only changed files
are read again. The album list is fetched on the library's query thread (indexer.Queries), never on
Tk's: the view's own `after` loop picks it up, with the indexer's progress and news of changed albums.

The views sit in one drop-down at the left of the search box (eleven words don't fit a 300 px window):
Songs (the tab's own list, grouped by album), Albums, Artists, Genres, Years, Composers, Recently added,
Most played, Recently played, Favourites, Needs a look. The ⋯ button beside the box holds Rescan, "Open
all albums in a tab" and "Add folder…". Narrower than NARROW the lists show fewer columns: album views
"Artist – Album" and Year (grouped views just the album), song views Title and Time with the artist
riding in the title; the rest come back when the window is wider. Esc, an album opened or a song played
put the track list back (`on_close`); "Songs" in the drop-down does the same (`on_songs`).

Browse by Albums · Artists · Genres · Years · Composers · Recently added (the grouping rules are in
browse.py); click a column heading to sort by it (again to reverse). Search matches album, artist,
year, genre and composer. Double-click an album to open it in a tab (it starts playing only if
nothing else is); right-click for Play next / Add to Up next. An album can also be dragged onto the
player: onto a tab to add it there, anywhere else on the player to open it in a new tab.

Songs (a second row of views): **Songs** — every song in the library, sortable by any column, the
search box searching every song through the database's index; **Most played** and **Recently
played** (from the plays the database counts); **Favourites** (♥: right-click any song — here, in a
tab or in the search panel — and choose Add to favourites). A song plays in its album (Enter or a
double-click: the album opens in a tab and plays from that song); right-click for the rest. Several
songs can be selected (Shift+click, Ctrl+click, Shift+↑ / ↓, Ctrl+A): Play next, Add to Up next, Add
to playlist, the favourites and Add to the tab take them all — a selection running past the rows
of the Songs view read so far is read on the query thread first — and a drag carries those read.
Plays and Last played can be added as columns (right-click the headings); they're read for the
rows on show only, on the query thread.

**Needs a look** (the last song view): what the whole-library quality check (library_check.py) found —
damaged files and MD5 mismatches, clues to a lossy or upsampled source or a padded bit depth,
clipping, misnamed files, and for information HDCD / MQA / pre-emphasis — worst first, in Now
Playing's words. The check is opt-in: "Check the whole library…" says how long it will take and
asks first; then it runs in the background (Pause / Resume / Stop here), its progress in the status
line of every view, and the list fills in as it goes.

The list is a virtual_list.VirtualTree, so a library of any size lists as quickly as a small one:
only the rows on show are in the Treeview. The Songs view reads its rows a page at a time on the
query thread as they come into view; the other song views are read and sorted there, with each
song's search text, so a list of tens of thousands (Needs a look) costs Tk's thread only the rows on
show and a quick pick of what's searched for. The album column shows the album's tag (its folder's
name only when it isn't tagged), text that doesn't fit ends with "…", and the narrow columns (Year,
Quality, Tracks, Added) always keep their room. Typing letters in the list jumps to the first name
that starts with them.

New music (0.17.0, watcher.py): an album that arrived since watching began carries a green dot
until it's opened, the status line says how many ("3 new albums"), and Recently added lists them
first (newest first).

(0.22.0) The main window's finish: its search box (magnifier, hint, ×), flat pill buttons, the views
as a row of words with a green bar under the one on show, the slim scrollbar that shows only when
needed, and the row under the pointer lit up as in the track list.

Keys (the keyboard table, keys.py; `key_handlers` is what they do here): Enter opens the album (or
plays the song), Ctrl+Enter / Ctrl+Shift+Enter put it in Up next, Ctrl+Tab / Ctrl+Shift+Tab change
the view, F5 rescans, Ctrl+F goes to the search box."""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter import font as tkfont

from . import (
    audiofile,
    browse,
    controls,
    indexer,
    keys,
    library_check,
    library_db,
    library_index,
    places,
    popups,
    searchfold,
    theme,
)
from .browse import album_label
from .favourites import HEART
from .library import Album, Track, sort_name
from .library_db import SongHit
from .playlist_view import TreeDrag, fit_columns
from .quick_search import Actions, add_song_items, album_text, count_text, shared_catalogue, song_name, song_values
from .searchfold import fold
from .virtual_list import CLOSED_MARK, OPEN_MARK, ListSource, VirtualTree

NEW_DOT = "●  "   # in front of a new album's name (until it's opened)
SEARCH_DELAY_MS = 150   # the list follows the search box after a pause in typing, as the track list does
TICK_MS = 100           # how often the window picks up the album list and the indexer's news
SONG_VIEWS = ("songs", "most_played", "recently_played", "favourites", "needs_look")
VIEWS = browse.VIEWS + SONG_VIEWS
NARROW = 480             # px at 100%: narrower than this the lists show fewer columns (NARROW_DISPLAYED)
VIEW_TITLES = {**browse.VIEW_TITLES, "songs": "Songs", "most_played": "Most played",
               "recently_played": "Recently played", "favourites": "Favourites", "needs_look": "Needs a look"}
# Every column the list can show; each view shows some of them (DISPLAYED).
COLUMNS = ("album", "artist", "year", "quality", "tracks", "added", "title", "time", "plays", "found", "last")
HEADINGS = {"album": "Album", "artist": "Artist", "year": "Year", "quality": "Format", "tracks": "Tracks",
            "added": "Added", "title": "Title", "time": "Time", "plays": "Plays", "found": "Found",
            "last": "Last played"}
SONG_EXTRAS = (("plays", "Plays"), ("last", "Last played"))   # optional song-view columns (right-click a heading)
DISPLAYED = {"songs": ("title", "artist", "album", "year", "quality", "time"),
             "most_played": ("title", "artist", "album", "plays", "time"),
             "recently_played": ("title", "artist", "album", "added", "time"),
             "favourites": ("title", "artist", "album", "year", "quality", "time"),
             "needs_look": ("title", "artist", "album", "found")}
NARROW_DISPLAYED = {"songs": ("title", "time"), "most_played": ("title", "plays"), "recently_played": ("title", "added"),
                    "favourites": ("title", "time"), "needs_look": ("title", "found")}
ALBUM_COLUMNS = browse.COLUMNS
NARROW_ALBUM_COLUMNS = ("album", "year")   # (the artist rides in the album's name: "Artist – Album")
# The sort each song view starts with ("": the order the list came in — newest favourite first).
SONG_SORT = {"songs": ("title", False), "most_played": ("plays", True), "recently_played": ("added", True),
             "favourites": ("", False), "needs_look": ("found", False)}
CHECK_REREAD_S = 3.0     # Needs a look reads the check's new results at most this often while it runs
SONG_LIMIT = 1000        # songs the Songs view's search lists (the best ones; the status says how many match)
PLAYED_LIMIT = 500       # songs Most played / Recently played list
PAGE = 200               # the Songs view's rows are read this many at a time
PAGES_KEPT = 25          # …and this many pages kept (others are read again when scrolled back to)
LOAD_ALL_WARN = 100_000  # songs: opening more than this in one tab asks first (it takes a few seconds)
ALBUM_THREAD_AT = 5_000  # albums: a library this big is grouped, searched and sorted on the query thread
COUNTS_KEPT = 20_000     # play counts / last-played times remembered (the rows shown lately)
_NUMERIC = {"year", "tracks", "time", "plays"}


def adding_folder(roots: list[Path], folder: Path) -> tuple[list[Path], str]:
    """"Add folder…": the library folders with `folder` added, and what to say about it ("" when
    it's simply added). A folder already in the library — one of its folders, or inside one —
    changes nothing; one that holds library folders takes their place (library_db.outermost: no
    album is ever listed twice)."""
    def key(path: Path) -> str:
        return os.path.join(os.path.normcase(str(path)), "")

    for root in roots:
        if key(folder) == key(root):
            return list(roots), f"“{folder}” is already in the Library"
        if key(folder).startswith(key(root)):
            return list(roots), f"“{folder}” is already in the Library (it's inside “{root}”)"
    kept = library_db.outermost([*roots, folder])
    taken = [root for root in roots if root not in kept]
    if not taken:
        return kept, ""
    names = ", ".join(f"“{root}”" for root in taken)
    return kept, (f"Added “{folder}”: it takes in {names}, so "
                  + ("that folder isn't listed on its own any more" if len(taken) == 1
                     else "those folders aren't listed on their own any more"))


def unsearched_note(folders: list[Path], prefs: dict | None) -> str:
    """What a whole-library search can't look through: the library folders on the network while
    Local network access is off (listed in Settings → Library, greyed, and never indexed). "" when
    every folder is read."""
    left_out = places.refused(folders, prefs)
    if not left_out:
        return ""
    names = ", ".join(f"“{folder}”" for folder in left_out)
    return f"Not searched: {names} — on your network, and Local network access is off (Settings → Library)"


def added_text(seconds: float) -> str:
    """When an album arrived, Australian style: 04/10/2026."""
    return time.strftime("%d/%m/%Y", time.localtime(seconds)) if seconds > 0 else ""


def _jump_text(text) -> str:
    """A name as type-to-jump compares it: folded, "The " left off (as names sort)."""
    folded = fold(str(text)).strip()
    return folded[4:] if folded.startswith("the ") else folded


# -- the song lists, read on the query thread (no Tk) --------------------------------------------------
def _most_played(db, n: int = PLAYED_LIMIT) -> list[tuple[SongHit, int]]:
    """The songs played most, with how often. A song whose plays were kept under two keys (its file
    written two ways) is listed once, its plays added up."""
    counts = db.most_played(n)
    times = dict(counts)
    merged: dict[int, list] = {}
    for key, hit in db.songs_for_keys(key for key, _count in counts):
        if hit.id in merged:
            merged[hit.id][1] += times[key]
        else:
            merged[hit.id] = [hit, times[key]]
    return sorted(((hit, plays) for hit, plays in merged.values()), key=lambda pair: -pair[1])


def _recently_played(db, n: int = PLAYED_LIMIT) -> list[tuple[SongHit, float]]:
    """The songs played last, with when (each song once, at its latest play)."""
    played = db.recently_played(n)
    when = dict(played)
    latest: dict[int, list] = {}
    for key, hit in db.songs_for_keys(key for key, _when in played):
        if hit.id in latest:
            latest[hit.id][1] = max(latest[hit.id][1], when[key])
        else:
            latest[hit.id] = [hit, when[key]]
    return sorted(((hit, at) for hit, at in latest.values()), key=lambda pair: -pair[1])


def _favourite_songs(db, keys: frozenset[str]) -> list[tuple[SongHit, None]]:
    """These favourites (as the window has them), newest first: stored ones in the database's order,
    any not stored yet before them."""
    stored = db.favourites()
    known = set(stored)
    ordered = [k for k in keys if k not in known] + [k for k in stored if k in keys]
    return [(hit, None) for hit in db.songs_by_keys(ordered)]


@dataclass(frozen=True)
class SongList:
    """A song view's songs as the query thread made them ready: (song, extra) pairs in the order
    asked for, each one's folded search text (`song_text`), and — Needs a look — how many songs
    have each finding. The window then only picks out what's searched for."""
    pairs: list
    texts: list[str]
    counts: dict[str, int] | None = None

    def __len__(self) -> int:
        return len(self.pairs)


def _song_list(pairs: list, column: str, reverse: bool) -> SongList:
    """(The query thread.) A song view's pairs sorted by `column` ("" = as they came), with their
    search texts."""
    pairs = list(pairs)
    if column:
        pairs.sort(key=_song_sort_key(column), reverse=reverse)
    looks = [extra for _hit, extra in pairs if isinstance(extra, library_check.Look)]
    return SongList(pairs, [song_text(pair) for pair in pairs],
                    library_check.counts(looks) if looks else None)


def song_text(pair: tuple) -> str:
    """What a song view's search looks through: the song's names (and in Needs a look, what was found)."""
    text = searchfold.haystack(pair[0].track)
    if isinstance(pair[1], library_check.Look):
        text += " " + searchfold.fold(pair[1].text)
    return text


def _plays_and_last(db, keys: list[str]) -> tuple[dict[str, int], dict[str, float]]:
    """(The query thread.) How often each song was played, and when last."""
    return db.play_counts(keys), db.last_played(keys)


def _library_albums(index) -> tuple[list[Album], dict[int, str]]:
    """(The query thread.) The library folders' albums, and each one's folded search text by the
    album's id() — from the album list the search panel shares (read again first if the library has
    changed since: quick_search.Catalogue), so the two windows hold one copy."""
    catalogue = shared_catalogue(index.db)
    catalogue.update(index.db, max_age=0)
    albums, texts = catalogue.snapshot()
    kept = [(album, text) for album, text in zip(albums, texts) if index.holds(album.path)]
    return [album for album, _text in kept], {id(album): text for album, text in kept}


@dataclass(frozen=True)
class AlbumRows:
    """An album view's list as `_album_rows` works it out: the rows, what each stands for, the albums
    listed (each once) and the row keys of albums and groups."""
    rows: list[tuple]
    items: list[tuple[str, object]]
    shown: list[Album]
    rows_map: dict[str, Album]
    groups: dict[str, str]
    searched: bool


def _album_values(album: Album, new, with_artist: bool = False) -> tuple:
    name = album_label(album)
    if with_artist and album.artist:   # (the narrow list: no Artist column)
        name = f"{album.artist} – {name}"
    name = NEW_DOT + name if album.path in new else name
    return (name, album.artist, album.year or "", album.quality, album.track_count, added_text(album.added),
            "", "", "", "", "")


def _album_rows(albums: list[Album], texts: dict[int, str], view: str, words: list[str], column: str,
                reverse: bool, closed: frozenset[str], new: frozenset, with_artist: bool = False) -> AlbumRows:
    """(The query thread for a big library, else Tk's.) A browse view's rows: its groups (artists,
    genres, …) with their albums, those the search words find (an album by its folded text; a whole
    group when its own name holds them), sorted; a folded group shows just its heading. Nothing here
    touches Tk. Rows are keyed by the group's name and the album's folder, not by their place, so a
    list read again (an album added, a tag edited) keeps its selection and scroll position."""
    grouped = view not in ("albums", "recent")
    rows: list[tuple] = []
    items: list[tuple[str, object]] = []
    shown: dict[Path, Album] = {}
    rows_map: dict[str, Album] = {}
    groups: dict[str, str] = {}
    for group in browse.group_albums(albums, view):
        if words and grouped and all(w in fold(group.name) for w in words):
            listed = group.albums  # the artist / genre / year itself matches: all of it
        elif words:
            listed = [a for a in group.albums if all(w in (texts.get(id(a)) or album_text(a)) for w in words)]
        else:
            listed = group.albums
        if not listed:
            continue
        listed = browse.sort_albums(listed, column if column in ALBUM_COLUMNS else "album", reverse)
        for album in listed:
            shown.setdefault(album.path, album)
        parent = ""
        if grouped:
            parent = f"g|{group.name}"
            folded = not words and group.name in closed
            songs = sum(a.track_count for a in listed)
            rows.append((parent, (group.name, count_text(len(listed), "album"), "", "", songs, "", "", "", "", "", ""),
                         ("group",), CLOSED_MARK if folded else OPEN_MARK))
            items.append(("group", group.name))
            groups[parent] = group.name
            if folded:
                continue
        for album in listed:
            iid = f"{parent}|{os.path.normcase(str(album.path))}"
            rows.append((iid, _album_values(album, new, with_artist), ("new",) if album.path in new else ()))
            items.append(("album", album))
            rows_map[iid] = album
    return AlbumRows(rows, items, list(shown.values()), rows_map, groups, bool(words))


def _same_songs(have: tuple | None, wanted: tuple) -> bool:
    """Do the songs read for `have` show what the view now wants (`wanted`)? A newer read of Needs a
    look's results (the quality check has stored more since) is still the same list, sorted the
    same way: the one on show stands until it's here."""
    if have is None:
        return False
    return have == wanted or (have[0] == wanted[0] == "needs_look" and have[2:] == wanted[2:])


def _read_page(db, pages: SongPages, page: int, ids: list[int]) -> list[SongHit] | None:
    """(The query thread.) A page of the Songs view's rows — None, unread, when the list has moved
    well away from it before its turn came (a scrollbar dragged through a big library asks for
    every page it passes: reading them all would hold up the one it stops at)."""
    if abs(pages.near - page * PAGE) > 2 * PAGE:
        return None
    return db.songs(ids)


class SongRows:
    """A song view's RowSource: each row made as it comes into view (a list of any length costs
    the window nothing to show)."""

    def __init__(self, pairs: list, make_row: Callable[[SongHit, object], tuple]):
        self.pairs = pairs
        self.make_row = make_row
        self._index: dict[str, int] | None = None

    def __len__(self) -> int:
        return len(self.pairs)

    def row(self, index: int) -> tuple:
        return self.make_row(*self.pairs[index])

    def key(self, index: int) -> str:
        return f"s{self.pairs[index][0].id}"

    def index_of(self, key: str) -> int | None:
        if self._index is None:
            self._index = {f"s{hit.id}": i for i, (hit, _extra) in enumerate(self.pairs)}
        return self._index.get(key)


def _song_sort_key(column: str):
    """Sorting a song view's rows ((hit, extra) pairs) by a column."""
    if column == "artist":
        return lambda pair: (sort_name(pair[0].track.artist), sort_name(pair[0].track.album))
    if column == "album":
        return lambda pair: (sort_name(pair[0].track.album), pair[0].track.disc_no or 0, pair[0].track.track_no or 0)
    if column == "year":
        return lambda pair: (pair[0].track.year is None, pair[0].track.year or 0)
    if column == "quality":
        return lambda pair: (not pair[0].track.lossy, pair[0].track.bits, pair[0].track.sample_rate)
    if column == "time":
        return lambda pair: pair[0].track.duration
    if column in ("plays", "added"):
        return lambda pair: pair[1] or 0
    if column == "found":
        return library_check.worst_first
    return lambda pair: sort_name(pair[0].track.title)


class SongPages:
    """The Songs view's source: every song's id in the order on show, each row read with its page
    (PAGE songs) on the query thread as it comes into view — `ask(page, ids)` asks for one, `take`
    puts it in. A row not read yet shows "…". Only PAGES_KEPT pages are kept."""

    def __init__(self, ids: list[int], make_row: Callable[[SongHit], tuple], ask: Callable[[int, list[int]], None]):
        self.ids = ids
        self.make_row = make_row
        self.ask = ask
        self.pages: OrderedDict[int, dict[int, SongHit]] = OrderedDict()
        self.asked: set[int] = set()
        self.near = 0   # the last row drawn (where the list is now: read by the query thread)

    def __len__(self) -> int:
        return len(self.ids)

    def hit(self, index: int) -> SongHit | None:
        """The song at `index`, or None while its page is on its way (asked for here)."""
        page = index // PAGE
        found = self.pages.get(page)
        if found is None:
            if page not in self.asked:
                self.asked.add(page)
                self.ask(page, self.ids[page * PAGE:(page + 1) * PAGE])
            return None
        self.pages.move_to_end(page)
        return found.get(self.ids[index])

    def peek(self, index: int) -> SongHit | None:
        """The song at `index` if its page has been read (nothing is asked for)."""
        found = self.pages.get(index // PAGE)
        return found.get(self.ids[index]) if found is not None else None

    def key(self, index: int) -> str:
        return f"s{self.ids[index]}"

    def row(self, index: int) -> tuple:
        self.near = index
        hit = self.hit(index)
        if hit is None:
            return (f"s{self.ids[index]}", ("",) * 6 + ("…", "", "", "", ""), ("loading",))
        return self.make_row(hit, None)

    def index_of(self, key: str) -> int | None:
        """A row's place by its key ("s<id>"), to keep the selection when the list is read again."""
        try:
            return self.ids.index(int(key[1:]))
        except ValueError:
            return None

    def take(self, page: int, hits: list[SongHit]):
        self.asked.discard(page)
        self.pages[page] = {hit.id: hit for hit in hits}
        while len(self.pages) > PAGES_KEPT:
            self.pages.popitem(last=False)


class LibraryView:
    _sorts: dict[str, tuple[str, bool]] = {}      # per view: (column, reversed), kept for the session
    _closed: dict[str, set[str]] = {}             # per view: groups folded shut

    def __init__(self, parent: tk.Misc, root_folder: Path | list[Path], current: Path | None,
                 on_choose: Callable[[Path], None], on_root_changed: Callable[[list[Path]], None],
                 on_load_all: Callable[[Path, str], None],
                 on_add: Callable[[Path, Callable[[str], None]], None] | None = None,
                 add_target: Callable[[], str | None] = lambda: None,
                 on_drag: Callable[[Path, int, int, str], None] | None = None,
                 on_queue: Callable[[Path, bool], None] | None = None,
                 view: str = "albums", on_view: Callable[[str], None] = lambda view: None,
                 on_edit_tags: Callable[[Path], None] | None = None,
                 new_albums: Callable[[], list[Path]] = lambda: [],
                 actions: Actions | None = None,
                 on_add_tracks: Callable[[list[Track]], str] | None = None,
                 checker: library_check.Checker | None = None,
                 song_columns: tuple[str, ...] = (),
                 on_song_columns: Callable[[list[str]], None] = lambda columns: None,
                 on_close: Callable[[], None] | None = None, on_songs: Callable[[], None] | None = None):
        # `parent`: the frame the view is packed into (the player's body; a test's root). `on_close`:
        # what closing means — the player puts its track list back (None: the frame is destroyed).
        # `on_songs`: the drop-down's "Songs", the tab's own list (None: the Songs view is offered instead).
        # The library folders (one, or several); on_root_changed(folders) when "Add folder…" adds one.
        self.roots: list[Path] = library_db.outermost([root_folder] if isinstance(root_folder, (str, Path))
                                                      else root_folder)
        self.root_folder = self.roots[0]
        self.current = current
        self.on_choose = on_choose
        self.on_root_changed = on_root_changed
        self.on_load_all = on_load_all
        self.on_add = on_add
        self.on_queue = on_queue
        self.on_view = on_view
        self.on_edit_tags = on_edit_tags    # the window's tag editor (None: not offered here)
        self.add_target = add_target  # the title of the player's tab on show, read fresh each time
        self.new_albums = new_albums  # the new albums' folders (0.17.0), read fresh at every fill
        self.actions = actions or Actions()   # what a song's Play / Add to Up next / … do (the player's)
        self.on_add_tracks = on_add_tracks    # songs onto the tab on show; returns what to say
        self.checker = checker                # the whole-library quality check (the player's), if any
        # The song views' optional columns (Plays, Last played) and what's known for the rows shown.
        self.song_columns = tuple(c for c, _label in SONG_EXTRAS if c in song_columns)
        self.on_song_columns = on_song_columns
        self._counts: OrderedDict[str, tuple[int, float | None]] = OrderedDict()
        self._counts_wanted: dict[str, None] = {}
        self._counts_job = None
        self._counts_asked: int | None = None      # the newest read of them on its way
        self._selection_asked: int | None = None   # a selection being read on the query thread…
        self._selection_then: Callable | None = None   # …and what's done with it once it's here
        self._new: set[Path] = set()
        self.on_close, self.on_songs = on_close, on_songs
        self.shown = True                      # on show (the player's `hidden` / `opened`): the tick runs only then
        self._narrow = False                   # narrower than NARROW: fewer columns
        self._view_text = ""
        self._load_all_text, self._load_all_ok = "Open all albums in a tab", False
        self.view = view if view in VIEWS else "albums"
        self.albums: list[Album] = []
        self._shown: list[Album] = []          # albums listed now (each once, folded groups' too)
        self._rows: dict[str, Album] = {}      # row key -> its album (the rows listed, on show or not)
        self._groups: dict[str, str] = {}      # row key -> group name
        # per row of the list: ("group", name) / ("album", Album) / ("song", (SongHit, plays or when played))
        self._items: list[tuple[str, object]] = []
        self.index = library_index.shared(self.roots)
        # A refresh running in the background: a dict until the album list it brought is on show.
        self._refresh: dict | None = None
        self._fill_job = None                  # a search waiting for a pause in typing
        self._album_texts: dict[int, str] = {}   # id(album) -> its folded search text (with each album list)
        self._album_rows_asked: int | None = None   # the album list being worked out on the query thread…
        self._album_rows_keep: bool | None = None   # …and whether the list stays where it was when it's here
        self._queries = indexer.Queries(self.index.db)   # the album list is read here, off Tk's thread
        self._events: queue.SimpleQueue = self.index.listen()
        self._asked: int | None = None         # the album list asked for and not yet here
        self._again = False                    # albums changed while it was on its way: ask again after
        self._loading = True                   # no album list yet
        self._problem = ""                     # the album list couldn't be read
        self._tick_job = None
        self._status_text = ""
        # The song views: what's wanted, what's here (see _want_songs).
        self._songs_wanted: tuple | None = None
        self._songs_asked: int | None = None
        self._songs_have: tuple | None = None
        self._songs_listed: tuple | None = None   # the songs on show were for this (a new copy keeps the place)
        self._songs_pending: str | None = None    # ↓ ("down") / Enter ("play") pressed before the songs came
        self._songs: object = None             # a SongList, SongHits (Songs, searched) or a list of ids (Songs)
        self._pages: SongPages | None = None
        self._page_serial = 0
        self._song_problem = ""
        # The quality check: its results already listed (checker.changes), when they were last read
        # and how long that took, the status last painted, the buttons' look, and the "how long?"
        # asked of the query thread.
        self._check_seen = checker.changes if checker is not None else 0
        self._check_read = 0.0
        self._check_took = 0.0
        self._songs_asked_at = 0.0
        self._check_status = None
        self._check_look: tuple | None = None
        self._estimate_asked: int | None = None

        # The view is a frame in `parent` (packed; the player unpacks it until it's wanted). Pop-overs
        # and prompts open over the window it's in.
        self.top = parent.winfo_toplevel()
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="both", expand=True)
        frame = self.frame

        top = ttk.Frame(frame)
        top.pack(fill="x")
        # The views, in one drop-down at the left; then the search box (controls.SearchBox: Esc empties
        # it — empty already, Esc goes on to the window, which puts the track list back); then the ⋯
        # menu (Rescan, Open all albums in a tab, Add folder…).
        self.view_button = controls.FlatButton(top, "", self._view_menu, fit="Recently played ● ▾",
                                               label="Which view: albums, artists, genres, years, composers, recently "
                                                     "added; most played, recently played, favourites, needs a look "
                                                     "(Ctrl+Tab steps through them)")
        self.view_button.pack(side="left", padx=theme.pad(0, theme.GAP_MEDIUM))
        self.search = controls.SearchBox(top, "Search albums, artists, years, genres", on_clear=self._clear_search)
        self.search_var = self.search.query
        self._search_trace = self.search_var.trace_add("write", lambda *_: self._search_changed())
        self.more_button = controls.FlatButton(top, "⋯", self._more_menu,
                                               label="Rescan · Open all albums in a tab · Add folder…")
        self.more_button.pack(side="right", padx=theme.pad(theme.GAP, 0))
        self.search.pack(side="left", fill="x", expand=True)
        search = self.search.entry
        search.bind("<Down>", lambda e: self._focus_list())
        search.bind("<Return>", lambda e: self._choose_first())
        self.view_var = tk.StringVar(value=self.view)
        self._paint_views()
        frame.bind("<Configure>", self._resized)

        # Album, group and playing rows are semibold (a named font: the list measures its words with it).
        bold = tkfont.Font(root=frame._root(), font=theme.font(theme.SIZE_BODY, "semibold"))
        self._bold = bold
        # The slim scrollbar sits in the window's right margin, so the list's words line up with the
        # search box.
        self.list = VirtualTree(frame, COLUMNS, show="headings", on_select=lambda index: self._paint_buttons(),
                                on_activate=self._activated, on_toggle=self._toggle, fonts={"group": bold},
                                margin=True, scroll_parent=parent)
        self.list.frame.pack(fill="both", expand=True, pady=theme.pad(theme.GAP, 0))
        self.tree = self.list.tree
        self.scrollbar = self.list.scrollbar
        widths = {"album": 220, "artist": 140, "year": 48, "quality": 64, "tracks": 60, "added": 96, "title": 220,
                  "time": 52, "plays": 52, "found": 260, "last": 96}
        for column in COLUMNS:
            anchor = "e" if column in _NUMERIC else "w"
            self.tree.heading(column, text=HEADINGS[column], anchor=anchor,
                              command=lambda c=column: self.sort_by(c))
            self.tree.column(column, anchor=anchor, width=theme.px(widths[column]),
                             stretch=column in ("album", "artist", "title", "found"))
        self.tree.column("#0", width=theme.px(22), stretch=False)  # the fold arrow of a group
        self._fit = fit_columns(self.tree, {"album": 3, "artist": 2, "title": 3, "found": 6}, minimum=theme.px(60))
        self._colour_tags()
        theme.on_change(self._palette_changed)
        self.drag: TreeDrag | None = None
        self.tree.bind("<Button-3>", self._context)
        for key in ("<App>", "<Shift-F10>"):   # (0.21.0) the keyboard's menu key: the focused row's menu
            self.tree.bind(key, lambda e: self._context(self.list.menu_event()) or "break")
        self._on_drag = on_drag
        self.drag = TreeDrag(self.tree, self._carried, self._dragged)

        # The selected album: open it in a tab of its own (the same as double-clicking), or add
        # it to the bottom of the tab on show in the player. Adding leaves this window open,
        # so you can add a few in a row. (A song: play it, or add it to the tab.)
        # (Both rows below are packed from the bottom, ahead of the album list, so a short
        # window squeezes the list rather than pushing the buttons out of sight.)
        selected = ttk.Frame(frame)
        self.open_button = controls.FlatButton(selected, "Open in new tab", self._choose_selected, kind="primary",
                                               state="disabled")
        self.open_button.pack(side="left")
        self.add_button = controls.FlatButton(selected, "Add to tab", self._add_selected, state="disabled")
        if on_add is not None:
            self.add_button.pack(side="left", padx=theme.pad(theme.GAP, 0))

        bottom = ttk.Frame(frame)
        bottom.pack(side="bottom", fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0), before=self.list.frame)
        selected.pack(side="bottom", fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0), before=self.list.frame)
        # (The buttons are packed first, so a long status line wraps beside them rather than pushing them
        # out of sight or running under them.)
        # Needs a look's own: start the quality check (or resume it), and pause / stop it.
        self.check_button = controls.FlatButton(bottom, "Check the whole library…", self._ask_check, kind="primary",
                                                label="Read every song once, in the background, for what needs a look")
        self.check_other = controls.FlatButton(bottom, "Pause", self._pause_check)
        self.status = ttk.Label(bottom, text="", style="Muted.TLabel", anchor="w", justify="left")
        self.status.pack(side="left", fill="x", expand=True)
        self.status.bind("<Configure>", lambda e: self.status.configure(wraplength=max(1, e.width)), add="+")

        frame.bind("<Destroy>", self._destroyed, add="+")
        self._load()
        self._tick_job = frame.after(TICK_MS, self._tick)

    # -- on show, or not ------------------------------------------------------------------------------
    def opened(self):
        """The player put the view on show: the tick runs (it stopped while hidden), the buttons
        follow the tabs as they are now."""
        self.shown = True
        if self._tick_job is None and self.frame.winfo_exists():
            self._tick_job = self.frame.after(TICK_MS, self._tick)
        self._paint_buttons()

    def hidden(self):
        """The player took the view off show: the tick stops at its next turn (what the library and
        the query thread send waits in their queues until the view is next opened)."""
        self.shown = False

    def _resized(self, event):
        """The frame's width decides the columns: fewer when narrower than NARROW."""
        if event.widget is not self.frame or event.width <= 1:
            return
        narrow = event.width < theme.px(NARROW)
        if narrow != self._narrow:
            self._narrow = narrow
            if not self._loading:
                self._fill(keep=True)

    def _destroyed(self, event):
        """Closed (Esc, a choice, or the window's ×): let go of what would keep this window alive or
        must be freed on Tk's thread. The search box's variable carries this window's search in a
        trace, which Tk would hold for good; the window's own Tk variable and font are dropped
        here, not left to Python's cycle collector, which may run on any thread (a Tk object
        freed there fails)."""
        if event.widget is not self.frame:
            return
        for job in (self._fill_job, self._tick_job, self._counts_job):
            if job is not None:
                self.frame.after_cancel(job)
        self._fill_job = self._tick_job = self._counts_job = None
        self._selection_then = None
        self.index.unlisten(self._events)
        self._queries.stop()
        if self.search_var is not None:
            try:
                self.search_var.trace_remove("write", self._search_trace)
            except tk.TclError:
                pass
        self.search_var = self.view_var = None
        self._bold = None
        self.albums, self._shown, self._items = [], [], []
        self._songs = self._pages = None
        self._rows.clear()
        self._album_texts = {}

    # -- the index -------------------------------------------------------------------------------
    def _load(self):
        """Show what the library database already knows (asked for on the query thread: it's on
        show a moment later); bring it up to date in the background (only changed files are read).
        The first time ever, every file is read once."""
        self.index = library_index.shared(self.roots)
        self._ask_albums()
        self._fill()
        if not self.index.refreshed:  # not yet checked this session
            self._start_refresh()

    def _place(self) -> str:
        """The library folder's name ("Music"), or how many there are."""
        if len(self.roots) > 1:
            return f"{len(self.roots)} folders"
        return self.root_folder.name or str(self.root_folder)

    def _ask_albums(self) -> int:
        """Ask the query thread for the album list (the newest ask is the one shown)."""
        self._asked = self._queries.ask("albums", _library_albums, self.index)
        self._again = False
        return self._asked

    def _want_albums(self):
        """The albums changed: ask for them again (once the list on its way has arrived)."""
        if self._asked is None:
            self._ask_albums()
        else:
            self._again = True

    def _tick(self):
        """Every TICK_MS: take in the album list, the song lists and what the indexer says. Tk is
        only touched when something came in."""
        self._tick_job = None
        if not self.frame.winfo_exists():
            return
        news = False
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            news = True
            if isinstance(event, indexer.AlbumsChanged) and self._refresh is None:
                self._want_albums()   # (during this window's own refresh, the list comes once at its end)
                self._songs_have = None   # (the songs may have changed too: read again when next shown)
        pages = False
        for answer in self._queries.drain():
            if answer.kind.startswith("page:"):
                pages = self._take_page(answer) or pages
                continue
            if answer.kind == "estimate":
                self._take_estimate(answer)
                continue
            if answer.kind == "songs":
                news = self._take_songs(answer) or news
                continue
            if answer.kind == "counts":
                pages = self._take_counts(answer) or pages
                continue
            if answer.kind == "selection":
                self._take_selection(answer)
                continue
            if answer.kind == "album_rows":
                news = self._take_album_rows(answer) or news
                continue
            if answer.kind != "albums" or answer.ask != self._asked:
                continue   # one replaced by a newer ask
            news = True
            self._asked = None
            first, self._loading = self._loading, False
            if answer.error is None:
                self._problem = ""
                self._set_albums(answer.value)
                self._fill(keep=not first)   # (read again: the list stays where it was, its selection kept)
            elif library_db.is_damage(answer.error):   # (noted by the query thread: put right at the next start)
                self._problem = "The library database is damaged: it will be repaired when the player next starts"
            else:
                self._problem = f"Couldn't read the library: {answer.error}"
            job = self._refresh
            if job is not None and job.get("final") == answer.ask:
                self._refresh = None   # the refresh's albums are on show: it's finished
                self._songs_have = None   # (a song view read before it ended is read again)
            if self._again:
                self._ask_albums()
        if pages:
            self.list.refresh()
        job = self._refresh
        if job is not None and job.get("done") and "final" not in job:
            job["final"] = self._ask_albums()
        if (self.view in SONG_VIEWS and self._songs_have is None and self._songs_asked is None
                and not self._song_problem and self._refresh is None):
            self._want_songs()
        news = self._follow_check() or news
        if news or job is not None:
            self._paint_status()
        self._tick_job = self.frame.after(TICK_MS, self._tick) if self.shown else None

    def _start_refresh(self):
        if self._refresh is not None:
            return
        job: dict = {"folders": list(self.roots), "index": self.index}
        self._refresh = job

        def work():   # (holds the job and the index only: nothing of the window's, nothing of Tk's)
            try:
                job["diff"] = job["index"].refresh()
            except Exception as error:  # noqa: BLE001 — shown, never fatal: the index is only a speed-up
                job["error"] = str(error)
            finally:
                job["done"] = True
                job["index"].db.release()

        threading.Thread(target=work, name="library-refresh", daemon=True).start()
        self._paint_status()

    def rescan(self):
        self._start_refresh()

    def reload(self):
        """Show the library again (after a tag edit updated it), keeping the view, sort and
        search: the album list (and a song view's songs) is asked for afresh."""
        if self.frame.winfo_exists() and self._refresh is None:
            self._want_albums()
            if self.view in SONG_VIEWS:
                self._songs_have = None
                self._want_songs()

    def refill(self):
        """List the albums again as they are (e.g. one is no longer new), without asking for them.
        The list stays where it was, its selection kept (adding a few albums in a row)."""
        if self.frame.winfo_exists():
            self._fill(keep=True)

    def favourites_changed(self):
        """A song's ♥ came or went (here, in a tab or in the search panel): Favourites lists them
        again; the other song views redraw their marks."""
        if not self.frame.winfo_exists() or self.view not in SONG_VIEWS:
            return
        if self.view == "favourites":
            self._want_songs()
        else:
            self._fill_songs(keep=True)

    def _set_albums(self, found: tuple[list[Album], dict[int, str]]):
        """The album list (shared with the search panel: quick_search.shared_catalogue) and each
        album's folded search text, by the album's id()."""
        self.albums, self._album_texts = found

    # -- views and sorting ---------------------------------------------------------------------------
    def set_view(self, view: str):
        if view == self.view or view not in VIEWS:
            self.view_var.set(self.view)   # (a word clicked for a view that doesn't exist: no change)
            return
        self.view = view
        self.view_var.set(view)
        self.on_view(view)
        self._songs_pending = None   # (an Enter waiting for the last view's songs is for them only)
        self._paint_views()
        if view in SONG_VIEWS:
            self._songs_have = None   # (plays and favourites change while the window is open: read afresh)
        if view == "needs_look" and self.checker is not None:
            self._check_seen, self._check_read = self.checker.changes, time.monotonic()
        self._fill()

    def _colour_tags(self):
        # ("hover" is configured first, by the list itself, so it wins over these: when two
        # ttk.Treeview tags set the same option, the first-configured wins.)
        self.tree.tag_configure("group", foreground=theme.ACCENT_TEXT, font=self._bold)
        self.tree.tag_configure("new", foreground=theme.ACCENT_TEXT)   # a new album: green until opened
        self.tree.tag_configure("loading", foreground=theme.MUTED)
        self.tree.tag_configure("note", foreground=theme.MUTED)
        self.tree.tag_configure("information", foreground=theme.MUTED)   # Needs a look: nothing wrong, just said

    def _palette_changed(self, _palette):
        """Treeview row colours keep their colours: redo them."""
        if self.frame.winfo_exists():
            self._colour_tags()

    def _paint_views(self):
        """The drop-down's button: the view on show (Recently added carries a dot while there's new music)."""
        new = bool(self._new)
        text = VIEW_TITLES[self.view] + (" ●" if new and self.view == "recent" else "") + " ▾"
        if text != self._view_text:
            self._view_text = text
            self.view_button.set_text(text)

    def _view_menu(self):
        """The drop-down: every view, the one on show ticked; "Songs" (the tab's own list) first when
        the player gave one."""
        x, y = self.view_button.winfo_rootx(), self.view_button.winfo_rooty() + self.view_button.winfo_height()
        view_menu(self.top, self.view, self.set_view, self.on_songs, (x, y + theme.px(2)), new=bool(self._new))

    def _more_menu(self):
        """The ⋯ button: Rescan, Open all (or these) albums in a tab, Add folder…"""
        menu = popups.PopupMenu(self.top)
        menu.add("Rescan: look for added or changed music", self.rescan, keys.hint("library_rescan"))
        menu.add(self._load_all_text, self._load_all, enabled=self._load_all_ok and self.view not in SONG_VIEWS)
        menu.add_separator()
        menu.add("Add folder…", self._add_folder)
        x, y = self.more_button.winfo_rootx(), self.more_button.winfo_rooty() + self.more_button.winfo_height()
        menu.popup(x, y + theme.px(2))

    # -- hover -------------------------------------------------------------------------------
    @property
    def hovered(self) -> str | None:
        return self.list.iid_of(self.list.hovered) if self.list.hovered is not None else None

    def _set_hover(self, iid: str | None):
        """Light up the row under the pointer (None: none)."""
        self.list.set_hover(self.list.index_of_iid(iid) if iid else None)

    def _search_changed(self):
        if self._fill_job is not None:
            self.frame.after_cancel(self._fill_job)
        self._fill_job = self.frame.after(SEARCH_DELAY_MS, self._search_now)   # wait for a pause in typing

    def _search_now(self):
        self._fill_job = None
        self._fill()

    def _flush_search(self):
        """Words typed but not yet searched for (or the search still on its way from the query
        thread): search now, so a quick Enter or Down acts on them."""
        if self._fill_job is not None or self._album_rows_asked is not None:
            if len(self.albums) >= ALBUM_THREAD_AT:
                self._fill(now=True)
            else:
                self._fill()

    def _clear_search(self):
        """The search box's × or Esc: empty it (Esc in an empty box closes the window, as before)."""
        if self.search_var.get():
            self.search_var.set("")
            return "break"
        return None

    @property
    def sort(self) -> tuple[str, bool]:
        default = SONG_SORT.get(self.view) or browse.DEFAULT_SORT.get(self.view, ("album", False))
        return self._sorts.get(self.view, default)

    @property
    def sort_on_show(self) -> tuple[str, bool]:
        """The order the list is in, as its headings say: the Songs view's search results come best
        first ("": no column) until a heading is clicked."""
        if self.view == "songs" and "songs" not in self._sorts and self._searching_songs():
            return "", False
        return self.sort

    def sort_by(self, column: str):
        """Click a heading: sort by it; click it again to reverse."""
        current, reverse = self.sort_on_show
        if column == "last" or (column == "plays" and self.view != "most_played"):
            return   # (read for the rows on show only: the list can't be sorted by them)
        newest_first = column in ("added", "plays")
        self._sorts[self.view] = (column, not reverse if column == current else newest_first)
        self._fill()

    def _paint_headings(self):
        column, reverse = self.sort_on_show
        for key, text in HEADINGS.items():
            if key == "added" and self.view == "recently_played":
                text = "Played"
            arrow = (" ▼" if reverse else " ▲") if key == column else ""
            self.tree.heading(key, text=text + arrow)

    # -- list ----------------------------------------------------------------
    def _values(self, album: Album) -> tuple:
        return _album_values(album, self._new)

    def _fill(self, now: bool = False, keep: bool = False):
        """List the view on show. `keep`: the same albums read again (or listed again as they are):
        the album list stays where it was and keeps its selection."""
        if self._fill_job is not None:   # (this shows the search as it stands: a pending one would only repeat it)
            self.frame.after_cancel(self._fill_job)
            self._fill_job = None
        self._new = set(self.new_albums())
        self._paint_views()
        self._paint_headings()
        songs = self.view in SONG_VIEWS
        self.list.multiple = songs   # (several songs can be picked; albums one at a time)
        self.tree.configure(displaycolumns=self._song_display() if songs
                            else NARROW_ALBUM_COLUMNS if self._narrow else ALBUM_COLUMNS)
        self.search.hint.configure(text="Search every song: title, artist, album, composer…" if songs
                                   else "Search albums, artists, years, genres")
        self._paint_check()
        if songs:
            self.open_button.configure(text="Play")
            self._fill_songs()
            return
        self.open_button.configure(text="Open in new tab")
        self._fill_albums(keep=keep, now=now)

    def _fill_albums(self, keep: bool = False, now: bool = False):
        """List the albums of the view on show, as searched for and sorted. A big library (from
        ALBUM_THREAD_AT albums) is grouped, searched and sorted on the query thread, the list on show
        staying until the new one is here — unless `now` (Enter or ↓ straight after typing, Show in
        Library: they act on the list at once)."""
        self._songs_listed = None   # (the list holds albums now)
        words = [fold(w) for w in self.search_var.get().split()]
        column, reverse = self.sort
        grouped = self.view not in ("albums", "recent")
        self.tree.configure(show="tree headings" if grouped else "headings")
        self._fit()
        args = (self.albums, self._album_texts, self.view, words, column, reverse,
                frozenset(self._closed.setdefault(self.view, set())), frozenset(self._new),
                self._narrow and self.view in ("albums", "recent"))
        if len(self.albums) >= ALBUM_THREAD_AT and not now:
            self._album_rows_asked = self._queries.ask("album_rows", _album_rows, *args)
            self._album_rows_keep = keep if self._album_rows_keep is None else self._album_rows_keep and keep
            return
        self._album_rows_asked = self._album_rows_keep = None
        self._show_albums(_album_rows(*args), keep)

    def _show_albums(self, listed: AlbumRows, keep: bool):
        self._rows, self._groups = listed.rows_map, listed.groups
        self._shown, self._items = listed.shown, listed.items
        select = None
        if not keep and self.current is not None:
            select = next((i for i, (kind, album) in enumerate(listed.items)
                           if kind == "album" and album.path == self.current), None)
        self.list.on_jump = self._jump
        self.list.set_source(ListSource(listed.rows), keep=keep, select=select)
        count = len(self._shown)
        self._load_all_text = f"Open {'these' if listed.searched else 'all'} {count:,} album{'' if count == 1 else 's'} in a tab"
        self._load_all_ok = bool(count)
        self._paint_status()
        self._paint_buttons()

    def _take_album_rows(self, answer) -> bool:
        """The album list worked out on the query thread came in (True: it's on show now)."""
        if answer.ask != self._album_rows_asked:
            return False
        keep, self._album_rows_asked, self._album_rows_keep = self._album_rows_keep, None, None
        if answer.error is not None or self.view in SONG_VIEWS:
            return False
        self._show_albums(answer.value, bool(keep))
        return True

    # -- the song views ---------------------------------------------------------------------------------
    def _query(self) -> str:
        return self.search_var.get().strip() if self.search_var is not None else ""

    def _searching_songs(self) -> bool:
        found = searchfold.tokens(self._query())
        return bool(found) and max(len(word) for word in found) >= library_db.MIN_SEARCH

    def _wanted(self) -> tuple:
        """What the song view on show needs from the database (it's asked for when it changes).
        The views listed whole (all but Songs) end with their sort: they're sorted on the query
        thread, as they can run to tens of thousands of songs (Needs a look)."""
        if self.view == "songs":
            if self._searching_songs():
                return ("songs", "search", self._query())
            column, reverse = self.sort
            return ("songs", "all", column or "title", reverse)
        column, reverse = self.sort
        if self.view == "favourites":
            favourites = self.actions.favourites
            return ("favourites", frozenset(favourites.keys()) if favourites is not None else frozenset(),
                    column, reverse)
        if self.view == "needs_look":
            return ("needs_look", self._check_seen, column, reverse)   # (read again as the check stores more)
        return (self.view, column, reverse)

    def _want_songs(self):
        """Ask the query thread for the songs the view needs, unless they're here or on their way."""
        self._song_problem = ""
        wanted = self._wanted()
        if wanted == self._songs_have:
            self._fill_songs()
            return
        if wanted == self._songs_wanted and self._songs_asked is not None:
            return
        db = self.index.db
        self._songs_wanted = wanted
        if wanted[0] == "songs" and wanted[1] == "search":
            fn, args = db.search_songs, (wanted[2], SONG_LIMIT)
        elif wanted[0] == "songs":
            fn, args = db.song_ids, (wanted[2], wanted[3])
        else:
            if wanted[0] == "most_played":
                read, args = _most_played, (db,)
            elif wanted[0] == "recently_played":
                read, args = _recently_played, (db,)
            elif wanted[0] == "needs_look":
                read, args = library_check.needs_look, (db,)
            else:
                read, args = _favourite_songs, (db, wanted[1])
            column, reverse = wanted[-2], wanted[-1]

            def fn(*args):
                return _song_list(read(*args), column, reverse)

        self._songs_asked = self._queries.ask("songs", lambda: (wanted, fn(*args)), interrupt=True)
        self._songs_asked_at = time.monotonic()
        self._paint_status()

    def _take_songs(self, answer) -> bool:
        if answer.ask != self._songs_asked:
            return False
        self._songs_asked = None
        if answer.error is not None:   # (said in the status line; asked again on the next change)
            self._song_problem = ("The library database is damaged: it will be repaired when the player next starts"
                                  if library_db.is_damage(answer.error) else f"Couldn't read the songs: {answer.error}")
            self._songs_have, self._songs, self._songs_wanted = None, None, None
            self._songs_pending = None
            self._paint_status()
            return True
        self._song_problem = ""
        wanted, value = answer.value
        if wanted[0] == "needs_look":
            self._check_took = time.monotonic() - self._songs_asked_at
        self._songs_have, self._songs = wanted, value
        self._pages = None
        if self.view in SONG_VIEWS:
            if wanted != self._wanted():
                self._want_songs()
            else:
                self._fill_songs()
                pending, self._songs_pending = self._songs_pending, None
                if pending == "play":
                    self._choose_first()
                elif pending == "down":
                    self._focus_list()
        return True

    def _fill_songs(self, keep: bool | None = None):
        """List the song view's songs (asking for them first when they aren't here). `keep`: stay
        where the list is — by default, when the songs on show were for what the view still wants
        (the same list read again after the library changed, or a sort or search of a short list)."""
        wanted = self._wanted()
        listed = self._songs_listed
        if keep is None:   # (a view listed whole read again — re-sorted, a ♥ more, the check gone on)
            keep = wanted == listed or (listed is not None and wanted[0] == listed[0] != "songs")
        self.tree.configure(show="headings")
        self._fit()
        self._rows.clear()
        self._groups.clear()
        self._shown = []
        if self._songs_have != wanted:
            # (The same songs again, or the Songs view's search typed on: the songs on show stay until
            # the new ones are here.)
            searched_on = listed is not None and listed[:2] == wanted[:2] == ("songs", "search")
            if not keep and not searched_on:
                self._items = []
                self.list.on_jump = None
                self.list.set_source(ListSource([]))
                self._songs_listed = None
            self._want_songs()
            self._paint_status()
            self._paint_buttons()
            return
        value = self._songs
        self._songs_listed = self._songs_have
        if self._songs_have[0] == "songs" and self._songs_have[1] == "all":
            if self._pages is None or self._pages.ids is not value:
                self._page_serial += 1
                self._pages = SongPages(value, self._song_row, self._ask_page)
            self._items = []
            self.list.on_jump = None
            self.list.set_source(self._pages, keep=keep)
        elif isinstance(value, library_db.SongHits):   # (the Songs view's search: the best SONG_LIMIT)
            pairs = [(hit, None) for hit in value.rows]
            column, reverse = self.sort
            if column and "songs" in self._sorts:   # (best first, unless sorted)
                pairs.sort(key=_song_sort_key(column), reverse=reverse)
            self._list_songs(pairs, keep)
        else:   # (a SongList, sorted already: only what's searched for is picked out here)
            words = searchfold.words(self._query())
            pairs = value.pairs
            if words:
                pairs = [pair for pair, text in zip(value.pairs, value.texts) if all(w in text for w in words)]
            self._list_songs(pairs, keep)
        self._paint_status()
        self._paint_buttons()

    def _list_songs(self, pairs: list, keep: bool):
        self._items = [("song", pair) for pair in pairs]
        self.list.on_jump = self._jump
        self.list.set_source(SongRows(pairs, self._song_row), keep=keep)

    def _song_display(self) -> tuple[str, ...]:
        """The columns a song view shows: its own, and the optional ones ticked (before Time); narrow,
        just the two that fit (NARROW_DISPLAYED)."""
        if self._narrow:
            return NARROW_DISPLAYED[self.view]
        base = DISPLAYED[self.view]
        if self.view == "needs_look":
            return base
        extras = tuple(c for c in self.song_columns if c not in base
                       and not (c == "last" and self.view == "recently_played"))   # (its Played is that)
        if "time" not in base:
            return base + extras
        at = base.index("time")
        return base[:at] + extras + base[at:]

    def _song_row(self, hit: SongHit, extra) -> tuple:
        favourites = self.actions.favourites
        title, artist, album, year, quality, clock = song_values(hit, inline_artist=self._narrow,
                                                                 favourite=favourites is not None
                                                                 and hit.track in favourites)
        added = added_text(extra) if self.view == "recently_played" and extra else ""
        plays = extra if self.view == "most_played" and extra else ""
        found = extra.text if isinstance(extra, library_check.Look) else ""
        last = ""
        shown = self.song_columns
        if shown and self.view != "needs_look":
            counted = self._counted(hit.track)
            if counted is not None:
                if "plays" in shown and self.view != "most_played":
                    plays = f"{counted[0]:,}" if counted[0] else ""
                if "last" in shown and counted[1]:
                    last = added_text(counted[1])
        return (f"s{hit.id}", (album, artist, year, quality, "", added, title, clock, plays, found, last),
                ("information",) if isinstance(extra, library_check.Look) and extra.information_only else ())

    # -- plays and last played, for the rows on show ------------------------------------------------
    def _counted(self, track: Track) -> tuple[int, float | None] | None:
        """(times played, when last) for a song on show; None while it's being read (asked for here)."""
        key = audiofile.key(track.path)
        found = self._counts.get(key)
        if found is not None:
            self._counts.move_to_end(key)
            return found
        if key not in self._counts_wanted:
            self._counts_wanted[key] = None
            if self._counts_job is None:
                self._counts_job = self.frame.after_idle(self._ask_counts)
        return None

    def _ask_counts(self):
        """The rows drawn asked for counts not known yet: read them on the query thread (a newer ask,
        which takes in every key still wanted, replaces one on its way)."""
        self._counts_job = None
        if self._counts_wanted:
            self._counts_asked = self._queries.ask("counts", _plays_and_last, self.index.db, list(self._counts_wanted))

    def _take_counts(self, answer) -> bool:
        """Plays and last-played times came in: True to draw the rows on show again. (Only the newest
        ask's: one asked before a play was counted would bring the counts from before it.)"""
        if answer.ask != self._counts_asked:
            return False
        self._counts_asked = None
        if answer.error is not None or answer.value is None:
            self._counts_wanted.clear()   # (asked again when next drawn)
            return False
        counts, lasts = answer.value
        for key, count in counts.items():
            self._counts[key] = (count, lasts.get(key))
            self._counts_wanted.pop(key, None)
        while len(self._counts) > COUNTS_KEPT:
            self._counts.popitem(last=False)
        return self.view in SONG_VIEWS

    def plays_changed(self):
        """A play was counted: the Plays and Last played columns read their counts again."""
        self._counts.clear()
        self._counts_wanted.clear()
        self._counts_asked = None   # (one on its way has the counts from before)
        if self.frame.winfo_exists() and self.view in SONG_VIEWS and self.song_columns:
            self.list.refresh()

    def set_song_columns(self, columns):
        """The song views' optional columns (Plays, Last played) on show; saved by the player."""
        self.song_columns = tuple(c for c, _label in SONG_EXTRAS if c in columns)
        self.on_song_columns(list(self.song_columns))
        if self.view in SONG_VIEWS:
            self.tree.configure(displaycolumns=self._song_display())
            self._fit()
            self.list.refresh()

    def _heading_menu(self, event):
        """Right-click a song view's headings: its optional columns."""
        menu = popups.PopupMenu(self.top)
        for column, label in SONG_EXTRAS:
            on = column in self.song_columns
            menu.add_check(label, on, lambda c=column, on=on: self.set_song_columns(
                [x for x in self.song_columns if x != c] + ([] if on else [c])))
        menu.popup(event.x_root, event.y_root)

    def _ask_page(self, page: int, ids: list[int]):
        serial = self._page_serial
        self._queries.ask(f"page:{serial}:{page}", _read_page, self.index.db, self._pages, page, ids)

    def _take_page(self, answer) -> bool:
        """A page of the Songs view's rows came in (True: draw the rows on show again). One left
        unread because the list had moved on is asked for again if it's on show after all."""
        _kind, serial, page = answer.kind.split(":")
        pages = self._pages
        if pages is None or int(serial) != self._page_serial or answer.error is not None:
            if pages is not None and int(serial) == self._page_serial:
                pages.asked.discard(int(page))
            return False
        if answer.value is None:
            pages.asked.discard(int(page))
        else:
            pages.take(int(page), answer.value)
        return self.view == "songs"

    def _hit_at(self, index: int | None) -> SongHit | None:
        if index is None:
            return None
        if self._pages is not None and self.list.source is self._pages:
            return self._pages.hit(index) if 0 <= index < len(self._pages) else None
        if 0 <= index < len(self._items) and self._items[index][0] == "song":
            return self._items[index][1][0]
        return None

    def _song_count_text(self) -> str:
        have, value = self._songs_have, self._songs
        if self._song_problem:
            return self._song_problem
        if not _same_songs(have, self._wanted()):
            return "Reading the songs…"
        if have[0] == "songs" and have[1] == "all":
            return f"{count_text(len(value), 'song')} in the library" if value else "No songs in the library yet"
        if have[0] == "songs":
            if not value.rows:
                return f"No songs match “{have[2]}”"
            if value.total > len(value.rows):
                return f"The best {len(value.rows):,} of {count_text(value.total, 'song', value.capped)}"
            return count_text(value.total, "song")
        shown = len(self._items)
        if self.view == "needs_look":
            return self._needs_look_text(value, shown)
        if not value:
            return {"most_played": "Nothing played yet: a song counts once half of it has been heard",
                    "recently_played": "Nothing played yet: a song counts once half of it has been heard",
                    "favourites": f"No favourites yet: right-click a song and choose Add to favourites ({HEART})"
                    }[self.view]
        if self._query() and shown < len(value):
            return f"{shown:,} of {len(value):,} songs"
        return {"most_played": f"The {count_text(shown, 'song')} played most",
                "recently_played": f"The {count_text(shown, 'song')} played last",
                "favourites": count_text(shown, "favourite")}[self.view]

    # -- the quality check ("Needs a look") ------------------------------------------------------------
    def _needs_look_text(self, value: SongList, shown: int) -> str:
        """Needs a look's status line: the check's progress, then what it found (worst first)."""
        status = self.checker.status if self.checker is not None else None
        if not value:
            if status is None or status.state == "off":
                found = ("“Check the whole library…” reads every song once, in the background, and lists here "
                         "what needs a look")
            elif status.finished:
                found = "Every song checked: nothing needs a look"
            else:
                found = "Nothing needs a look so far"
        else:
            what = library_check.summary(value.counts or {})   # (counted on the query thread)
            if self._query() and shown < len(value):
                found = f"{shown:,} of {len(value):,} songs: {what}"
            else:
                found = f"{count_text(len(value), 'song')} {'needs' if len(value) == 1 else 'need'} a look: {what}"
        note = self._check_note()
        return f"{note} · {found}" if note else found

    def _check_note(self) -> str:
        """Where the quality check has got to, for the status line ("" when there's nothing to say)."""
        if self.checker is None:
            return ""
        status = self.checker.status
        of = f"{status.done:,} of {count_text(status.total, 'song')}" if status.total else ""
        if status.state == "paused":
            return f"Quality check paused at {of}" if of else "Quality check paused"
        if status.state != "on":
            return ""
        if status.error:
            return f"The quality check stopped ({status.error}): it tries again in a few minutes"
        if status.held:
            return "Quality check waiting for the measurement to finish"
        if status.running:
            left = f" · {library_check.duration_text(status.seconds_left)} left" if status.seconds_left else ""
            return f"Checking quality: {of}{left}" if of else "Checking quality…"
        if status.finished and self.view == "needs_look":
            return f"Every song checked ({count_text(status.total, 'song')})"
        return ""

    def _follow_check(self) -> bool:
        """(The tick.) Take in the quality check's progress; whether there was any. Needs a look reads
        its results again as more are stored: at most every CHECK_REREAD_S, and — a long list, slow
        to read — no more than a tenth of the time (the query thread's reading holds up the rest)."""
        checker = self.checker
        if checker is None:
            return False
        news = False
        if checker.status is not self._check_status:
            self._check_status = checker.status
            self._paint_check()
            news = True
        if (self.view == "needs_look" and checker.changes != self._check_seen and self._songs_asked is None
                and time.monotonic() - self._check_read >= max(CHECK_REREAD_S, 10 * self._check_took)):
            self._check_seen, self._check_read = checker.changes, time.monotonic()
            self._want_songs()
        return news

    def _paint_check(self):
        """Needs a look's buttons, as the check stands: Check the whole library… (off); Pause (on);
        Resume and Stop (paused); Stop checking new music (on, every song checked)."""
        checker = self.checker
        if self.view != "needs_look" or checker is None:
            look = None
        else:
            status = checker.status
            look = (status.state, status.finished, self._estimate_asked is not None)
        if look == self._check_look:
            return
        self._check_look = look
        self.check_button.pack_forget()
        self.check_other.pack_forget()
        if look is None:
            return
        state, finished, estimating = look
        if state in ("off", "paused"):
            self.check_button.configure(text="Check the whole library…" if state == "off" else "Resume",
                                        command=self._ask_check if state == "off" else self._resume_check,
                                        state="disabled" if estimating else "normal")
            self.check_button.pack(side="right", before=self.status)
        if state != "off":
            self.check_other.configure(text="Stop checking new music" if state == "on" and finished
                                       else "Pause" if state == "on" else "Stop",
                                       command=self._pause_check if state == "on" and not finished
                                       else self._stop_check)
            self.check_other.pack(side="right", before=self.status, padx=theme.pad(0, theme.GAP))

    def _ask_check(self):
        """"Check the whole library…": how much there is to read is worked out on the query thread,
        then the question (with how long it takes) is asked."""
        if self.checker is None or self._estimate_asked is not None:
            return
        self._estimate_asked = self._queries.ask("estimate", library_check.plan, self.index.db, False)
        self._paint_check()
        self._say("Working out how long the check takes…")

    def _take_estimate(self, answer):
        if answer.ask != self._estimate_asked:
            return
        self._estimate_asked = None
        self._paint_check()
        if answer.error is not None:
            self._say(f"Couldn't read the library: {answer.error}")
            return
        self.frame.after_idle(self._confirm_check, answer.value)   # (asked outside the tick)

    def _confirm_check(self, plan: library_check.Plan):
        if not self.frame.winfo_exists() or self.checker is None or self.checker.state != "off":
            return
        count = plan.count
        if count:
            already = (f" {count_text(plan.done, 'song')} {'is' if plan.done == 1 else 'are'} checked already."
                       if plan.done else "")
            seconds = library_check.estimate(plan.work, self.checker.workers)
            if not popups.confirm(
                    self.top, "Check every song in the library?",
                    f"{count_text(count, 'song')} ({library_check.music_text(plan.music_s)} of music) "
                    f"{'is' if count == 1 else 'are'} read once each, to find damaged files, clues to a lossy or "
                    f"upsampled source or a padded bit depth, and clipping.{already}",
                    detail=(f"That takes {library_check.duration_text(seconds)} on a computer like this one "
                            "(it's measured as it goes; a network drive can be slower). It runs in the background "
                            "at low priority, so you can keep listening. Pause it any time: it carries on where it "
                            "left off, after a restart too. New music is checked as it arrives."),
                    ok="Start checking", cancel="Not now"):
                self._paint_status()
                return
        self.checker.start()
        self._paint_check()
        self._paint_status()

    def _resume_check(self):
        if self.checker is not None:
            self.checker.resume()
            self._paint_check()
            self._paint_status()

    def _pause_check(self):
        if self.checker is not None:
            self.checker.pause()
            self._paint_check()
            self._paint_status()

    def _stop_check(self):
        """Stop (or stop checking new music): what's been found stays listed."""
        if self.checker is not None:
            self.checker.stop()
            self._paint_check()
            self._paint_status()

    # -- type to jump ------------------------------------------------------------------------------
    def _jump(self, typed: str) -> int | None:
        """The first row whose name starts with what was typed: a group's name in a grouped view,
        else the name in the column sorted by (album, artist or title; the album's or the song's
        title when another column is sorted)."""
        typed = _jump_text(typed)
        column, _reverse = self.sort
        grouped = self.view in ("artists", "genres", "years", "composers")
        for index, (kind, thing) in enumerate(self._items):
            if grouped:
                if kind != "group":
                    continue
                text = thing
            elif kind == "album":
                text = thing.artist if column == "artist" else album_label(thing)
            elif kind == "song":
                track = thing[0].track
                text = track.artist if column == "artist" else track.album if column == "album" else track.title
            else:
                continue
            if _jump_text(text).startswith(typed):
                return index
        return None

    # -- status ---------------------------------------------------------------------------------------
    def _paint_status(self):
        if not self.frame.winfo_exists():
            return
        total = len(self.albums)
        job = self._refresh
        place = str(self.root_folder) if len(self.roots) == 1 else f"{len(self.roots)} library folders"
        if job is not None and not total:
            done, of = self.index.progress
            text = (f"Building the library index (first time only)… {done} of {of} files" if of
                    else f"Looking through {place}…")
        elif self._problem and not total:
            text = self._problem
        elif self._loading and not total:
            text = "Opening the library…"
        elif not total:
            text = f"No music found in {place}"
        elif self.view in SONG_VIEWS:
            text = self._song_count_text()
        else:
            words = self.search_var.get().strip()
            text = f"{len(self._shown)} of {total} albums" if words else f"{total} albums in {place}"
            if job is not None:
                done, of = self.index.progress
                text += f" · checking for changes… {done} of {of}" if of else " · checking for changes…"
            elif self.index.last_diff is not None and self.index.last_diff.error:
                text += f" · {self.index.last_diff.error} just now, so these may be out of date"
            elif self.index.last_diff is not None and not self.index.last_diff.nothing:
                text += f" · updated: {self.index.last_diff.note()}"
            elif self.index.last_diff is not None and self.index.last_diff.unreadable:
                text += f" · {self.index.last_diff.unreadable_note()}"
            note = self._check_note()
            if note:   # (before the folder's name too, which can be long)
                text = f"{note} · {text}"
            new = len(self._new)
            if new:  # first, so a long folder name can't push it out of sight
                text = f"{new} new album{'' if new == 1 else 's'} · {text}"
        if text != self._status_text:   # (painted often while a refresh runs: Tk only when it changed)
            self._say(text)

    def _say(self, text: str):
        self._status_text = text
        self.status.configure(text=text)

    # -- groups ---------------------------------------------------------------------------------------
    def _toggle(self, index: int):
        """Fold a group shut, or open it (its arrow, a double-click, ← / →, or Enter on it)."""
        if not 0 <= index < len(self._items) or self._items[index][0] != "group":
            return
        name = self._items[index][1]
        closed = self._closed.setdefault(self.view, set())
        (closed.discard if name in closed else closed.add)(name)
        self._fill_albums(keep=True)

    def column_values(self, column: str, group: str | None = None) -> list:
        """A column's text for every row listed (on show or scrolled away), as the list has them:
        the top-level rows (groups, or the albums of an ungrouped view), or — `group`, a group row's
        key — the albums under that group."""
        source = self.list.source
        at = COLUMNS.index(column)
        rows = [source.row(i) for i in range(len(source))]
        if group is None:
            grouped = any("group" in row[2] for row in rows)
            return [row[1][at] for row in rows if not grouped or "group" in row[2]]
        start = next(i for i, row in enumerate(rows) if row[0] == group) + 1
        values = []
        for row in rows[start:]:
            if "group" in row[2]:
                break
            values.append(row[1][at])
        return values

    # -- the selection --------------------------------------------------------------------------------
    def _selected_album(self) -> Album | None:
        index = self.list.selection()
        if index is None or not 0 <= index < len(self._items):
            return None
        kind, thing = self._items[index]
        return thing if kind == "album" else None

    def _selected_song(self) -> SongHit | None:
        return self._hit_at(self.list.selection()) if self.view in SONG_VIEWS else None

    def _peek_hit(self, index: int) -> SongHit | None:
        """The song at `index` if it has been read (a page of the Songs view not read yet: None)."""
        if self._pages is not None and self.list.source is self._pages:
            return self._pages.peek(index) if 0 <= index < len(self._pages) else None
        return self._hit_at(index)

    def _selected_songs(self, then: Callable[[list[SongHit]], None]) -> bool:
        """Every song selected, handed to `then`: at once when they've all been read, else once the
        query thread has read the rest (Ctrl+A over the whole Songs view). False with none selected."""
        if self.view not in SONG_VIEWS:
            return False
        self.list.sync()
        indices = self.list.selected_indices()
        if not indices:
            return False
        hits = [self._peek_hit(i) for i in indices]
        if all(hit is not None for hit in hits):
            then(hits)
            return True
        pages = self._pages if self._pages is not None and self.list.source is self._pages else None
        if pages is None:
            then([hit for hit in hits if hit is not None])
            return True
        ids = [pages.ids[i] for i in indices]
        db = self.index.db
        self._selection_then = then
        self._selection_asked = self._queries.ask("selection", db.songs, ids)
        self._say(f"Reading {count_text(len(ids), 'song')}…")
        return True

    def _take_selection(self, answer):
        if answer.ask != self._selection_asked:
            return
        then, self._selection_then, self._selection_asked = self._selection_then, None, None
        if answer.error is not None:
            self._say(f"Couldn't read the songs: {answer.error}")
        elif then is not None and self.frame.winfo_exists():
            self._paint_status()
            then(answer.value)

    def select_all(self) -> bool:
        """Ctrl+A in a song view: every song listed (albums are picked one at a time)."""
        return self.view in SONG_VIEWS and self.list.select_all()

    def _paint_buttons(self):
        if not self.frame.winfo_exists():
            return
        chosen = self._selected_song() if self.view in SONG_VIEWS else self._selected_album()
        state = "normal" if chosen is not None else "disabled"
        self.open_button.configure(state=state)
        target = self.add_target()
        if target is None:
            self.add_button.configure(text="Add to tab", state="disabled")  # no tab open to add to
        else:
            short = target if len(target) <= 28 else target[:27] + "…"
            self.add_button.configure(text=f"Add to “{short}”", state=state)

    def _add_selected(self):
        if self.view in SONG_VIEWS:
            if self.on_add_tracks is not None:
                self._selected_songs(lambda hits: self._say(self.on_add_tracks([hit.track for hit in hits])))
            return
        album = self._selected_album()
        if album is not None and self.on_add is not None:
            self._say(f"Adding “{album_label(album)}”…")
            self.on_add(album.path, self._added)

    def _added(self, message: str):
        if self.frame.winfo_exists():
            self._say(message)
            self._paint_buttons()

    def _queue(self, album: Album, front: bool):
        if self.on_queue is not None:
            self._say(f"Adding “{album_label(album)}” to Up next…")
            self.on_queue(album.path, front)

    def _queue_songs(self, hits: list[SongHit], front: bool):
        if not hits:
            return
        self.actions.queue_tracks([hit.track for hit in hits], front)
        what = f"“{song_name(hits[0].track)}”" if len(hits) == 1 else count_text(len(hits), "song")
        self._say(f"{what} will play next" if front else f"Added {what} to Up next")

    def _context(self, event):
        """Right-click an album: open it, add it to the tab on show, or line it up in Up next. A song:
        play it, line it up, add it to a playlist or to the favourites."""
        if self.view in SONG_VIEWS and self.tree.identify_region(event.x, event.y) == "heading":
            if self.view != "needs_look":   # (its columns are its own: Plays and Last played aren't offered)
                self._heading_menu(event)
            return
        index = self.list.index_at(event.y)
        if index is None:
            return
        self.list.sync()
        if not self.list.is_selected(index):   # (a right-click inside the selection keeps it)
            self.list.select(index, see=False)
        hit = self._hit_at(index) if self.view in SONG_VIEWS else None
        if hit is not None:
            x, y = event.x_root, event.y_root
            self._selected_songs(lambda hits: self._song_menu(hits, x, y))
            return
        album = self._selected_album()
        if album is None:
            return
        menu = popups.PopupMenu(self.top)   # (0.21.0) in the app's colours, arrow keys work
        menu.add("Open in new tab", lambda: self._choose(album), keys.hint("library_open"))
        target = self.add_target()
        if self.on_add is not None and target is not None:
            short = target if len(target) <= 28 else target[:27] + "…"
            menu.add(f"Add to “{short}”", self._add_selected)
        if self.on_queue is not None:
            menu.add_separator()
            menu.add("Play next", lambda: self._queue(album, True), keys.hint("library_play_next"))
            menu.add("Add to Up next", lambda: self._queue(album, False), keys.hint("library_queue"))
        if self.on_edit_tags is not None:
            menu.add_separator()
            menu.add("Edit tags…", lambda: self.on_edit_tags(album.path))
        menu.add_separator()
        menu.add("Show in Explorer", lambda: self.actions.show_in_explorer(album.path))
        menu.popup(event.x_root, event.y_root)

    def _song_menu(self, hits: list[SongHit], x_root: int, y_root: int):
        if not hits or not self.frame.winfo_exists():
            return
        menu = popups.PopupMenu(self.top)
        add_song_items(menu, self.actions, hits, {"play": keys.hint("library_open"),
                                                  "next": keys.hint("library_play_next"),
                                                  "queue": keys.hint("library_queue")}, show_in_library=False)
        menu.popup(x_root, y_root)

    # -- dragging ---------------------------------------------------------------------------------
    def _carried(self, iid: str):
        """What a dragged row carries: an album's folder, or songs (a list: the song, or every song
        selected that has been read, when it's one of several selected)."""
        index = self.list.index_of_iid(iid)
        if self.view in SONG_VIEWS:
            if index is not None and self.list.is_selected(index) and self.list.selected_count() > 1:
                hits = [self._peek_hit(i) for i in self.list.selected_indices()]
                return [hit.track for hit in hits if hit is not None] or None
            hit = self._hit_at(index)
            return [hit.track] if hit is not None else None
        album = self._rows.get(iid)
        return album.path if album is not None and self._on_drag is not None else None

    def _dragged(self, carried, x_root: int, y_root: int, phase: str):
        if isinstance(carried, Path):
            if self._on_drag is not None:   # drag an album onto the player (ui.PlayerWindow._album_drag)
                self._on_drag(carried, x_root, y_root, phase)
        else:
            self.actions.drag(carried, x_root, y_root, phase)

    def _load_all(self):
        """Every album shown (all of them, or just the search results), in this order, in one tab.
        A very big one asks first: it takes a few seconds, and the Library tab's search finds any song without it."""
        songs = sum(album.track_count for album in self._shown)
        if songs > LOAD_ALL_WARN and not popups.confirm(
                self.top, f"Open {count_text(songs, 'song')} in one tab?",
                "A tab that big can take a few seconds to open.",
                detail="To find a song, the Library tab's search box (Ctrl+K) looks through the whole library "
                       "without opening it.",
                ok="Open them all", tone="warn"):
            return
        self.on_load_all(self.root_folder, self.search_var.get().strip())
        self.close()

    def _focus_list(self):
        self._flush_search()
        if self._songs_on_the_way("down"):
            return
        if len(self.list.source):
            self.tree.focus_set()
            if self.list.selection() is None:
                self.list.select(self._first_row())
            else:
                self.list.select(self.list.selection())

    def _first_row(self) -> int:
        """The first album (or song) in the list: past a group's heading."""
        return next((i for i, (kind, _thing) in enumerate(self._items) if kind != "group"), 0)

    def _activated(self, index: int):
        """A double-click on a row: open the album, or play the song."""
        if self.view in SONG_VIEWS:
            hit = self._hit_at(index)
            if hit is not None:
                self._play(hit)
        elif 0 <= index < len(self._items) and self._items[index][0] == "album":
            self._choose(self._items[index][1])

    def _choose_selected(self) -> bool:
        """Open the selected album (play the selected song). On a group row: fold it or open it.
        False with nothing selected."""
        index = self.list.selection()
        if index is None:
            return False
        if self.view in SONG_VIEWS:
            hit = self._hit_at(index)
            if hit is None:
                return False
            self._play(hit)
            return True
        if 0 <= index < len(self._items) and self._items[index][0] == "group":
            self._toggle(index)
            return True
        album = self._selected_album()
        if album is None:
            return False
        self._choose(album)
        return True

    def _play(self, hit: SongHit):
        self.actions.play_song(hit.track, hit.album)
        self.close()

    # -- the keyboard ------------------------------------------------------------------------------
    def key_handlers(self) -> dict:
        """What the keyboard table's Library keys do here (keys.Keys.attach)."""
        return {"library_search": lambda: self.focus_search(), "library_open": lambda: self._choose_selected(),
                "library_select_all": lambda: self.select_all(),
                "library_queue": lambda: self.queue_selected(front=False),
                "library_play_next": lambda: self.queue_selected(front=True),
                "library_view": lambda key: self.cycle_view(-1 if "Shift" in key else 1),
                "library_rescan": lambda: self.rescan()}

    def focus_search(self):
        self.search.entry.focus_set()
        self.search.entry.select_range(0, "end")

    def queue_selected(self, front: bool):
        """Ctrl+Enter / Ctrl+Shift+Enter: the selected album (or songs) into Up next (at the end / next)."""
        if self.view in SONG_VIEWS:
            return self._selected_songs(lambda hits: self._queue_songs(hits, front))
        album = self._selected_album()
        if album is None:
            return False
        self._queue(album, front)
        return True

    def cycle_view(self, step: int):
        """Ctrl+Tab / Ctrl+Shift+Tab: the next (or previous) view, round the ends (the Songs view
        skipped when the tab's own list stands for it)."""
        views = [v for v in VIEWS if v != "songs" or self.on_songs is None]
        at = views.index(self.view) if self.view in views else -1
        self.set_view(views[(at + step) % len(views)])

    def show(self, album: Path | None = None, search: str | None = None):
        """Show an album (selected, scrolled to: "Show in Library") or search for something (an
        artist from the search panel)."""
        if search is not None:
            if self.view in SONG_VIEWS:
                self.set_view("artists")
            self.search_var.set(search)
            self._flush_search()
        if album is not None:
            self.current = Path(album)
            if self.view in SONG_VIEWS:
                self.set_view("albums")
            if self._album_rows_asked is not None:   # (on its way: list the albums here and now)
                self._fill(now=True)
            index = self._album_index(self.current)
            if index is None and self.search_var.get():
                self.search_var.set("")   # (not among the results on show: list them all)
                self._flush_search()
                index = self._album_index(self.current)
            if index is None and self.view not in ("albums", "recent"):
                self._closed.setdefault(self.view, set()).clear()   # (in a folded group: open them)
                self._fill_albums(keep=True, now=True)
                index = self._album_index(self.current)
            if index is not None:
                self.list.select(index)

    def _album_index(self, folder: Path) -> int | None:
        return next((i for i, (kind, album) in enumerate(self._items) if kind == "album" and album.path == folder),
                    None)

    def _songs_on_the_way(self, pending: str) -> bool:
        """↓ or Enter in the search box of a song view whose songs are still being read (a search
        just typed): done once they're here, as the search panel does. True when it waits."""
        if (self.view not in SONG_VIEWS or self._songs_asked is None
                or _same_songs(self._songs_listed, self._wanted())):
            return False
        self._songs_pending = pending
        return True

    def _choose_first(self):
        self._flush_search()
        if self._songs_on_the_way("play"):
            return
        if self.view in SONG_VIEWS:
            hit = self._hit_at(0)
            if hit is not None:
                self._play(hit)
            return
        album = next((thing for kind, thing in self._items if kind == "album"), None)
        if album is not None:
            self._choose(album)

    def _choose(self, album: Album):
        self.on_choose(album.path)
        self.close()

    def _add_folder(self):
        folder = filedialog.askdirectory(parent=self.top, title="Add a folder of music to the Library",
                                         initialdir=str(self.root_folder), mustexist=True)
        if not folder:
            return
        roots, note = adding_folder(self.roots, Path(folder))
        if roots != self.roots:
            self.on_root_changed(roots)
            self.set_roots(roots)
        if note:
            self._say(note)

    def set_roots(self, roots: list[Path]):
        """The library folders changed (here, or in Settings): the refresh under way for the old
        ones is let go (the indexer stops its look before the new folders are stored), and the
        list follows the new ones (none inside another: library_db.outermost)."""
        roots = library_db.outermost(roots)
        if roots == self.roots or not roots:
            return
        self.roots, self.root_folder = roots, roots[0]
        self.search_var.set("")
        self._refresh = None
        self._songs_have = None
        self._load()

    def close(self):
        """Back to the player (Esc, an album opened, a song played): the player puts its track list
        back (`on_close`); on its own, the frame is destroyed."""
        if self.on_close is not None:
            self.on_close()
        else:
            self.destroy()

    def destroy(self):
        if self.frame.winfo_exists():
            self.frame.destroy()


def view_menu(top: tk.Misc, current: str | None, on_pick: Callable[[str], None], on_songs: Callable[[], None] | None,
              at: tuple[int, int], new: bool = False):
    """The views drop-down (the view's button, and the Library tab's "Songs ▾" over its own list):
    "Songs" first when `on_songs` is given (the tab's list; `current` "songs" ticks it), then the album
    views, then the song views; `on_pick(view)` for any other. `new`: a dot by Recently added."""
    menu = popups.PopupMenu(top)
    if on_songs is not None:
        menu.add_check("Songs", current == "songs", on_songs)
        menu.add_separator()
    for key in browse.VIEWS:
        label = VIEW_TITLES[key] + (" ●" if new and key == "recent" else "")
        menu.add_check(label, current == key, lambda k=key: on_pick(k))
    menu.add_separator()
    for key in SONG_VIEWS:
        if key == "songs" and on_songs is not None:
            continue
        menu.add_check(VIEW_TITLES[key], current == key, lambda k=key: on_pick(k))
    menu.popup(*at)
    return menu

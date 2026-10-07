"""Search the whole library (Ctrl+K or Ctrl+Shift+F): a panel over the main window that finds songs,
albums and artists as you type.

What's found (searchfold.py's folding: case and accents ignored, "lodz" finds Łódź):
- **Songs**, from the library database's search index (LibraryDB.search_songs): every word must
  start a word in the title, artist, album, album artist, composer, genre, year or file name; the
  best 200 are listed, with how many there are ("1,234 songs", or "1,000+" for the slow kind of
  search). A song found by something other than its title, artist or album says where
  ("composer: Bach").
- **Albums** and **Artists**, from a list of the library's albums (`Catalogue`) read on a query
  thread and read again whenever the library has changed — one copy, shared with the Library window
  (`shared_catalogue`).

The search runs on the panel's own "library-query" thread (indexer.Queries): 120 ms after the last
key, at 2 letters or more; a newer search stops the one running (latest wins). Results come back
through a queue that the panel's `after` loop drains, so typing never waits for the database.

Keys: ↓ goes from the box to the results (↑ on the first row goes back). On a result — or, in the
box, on the first one — Enter plays (a song: its album opens in a tab and plays from that song, the
album carrying on), Shift+Enter plays it next, Ctrl+Enter adds it to Up next, Alt+Enter opens its
album. Esc empties the box, then closes the panel; so does a click outside it. Right-click a result
for the rest (Add to playlist…, Show in Library, Show in Explorer, favourites), or drag it onto a
tab to add it there.

The panel sits just under the tab strip, as wide as the window (centred, and no wider than
PANEL_MAX_WIDTH) and down to the window's foot — once centred, no taller than PANEL_MAX_HEIGHT;
its columns follow its width: Title and Time when narrow (the artist then rides in the title), then
Artist and Album, then Year and Quality. It looks like the app's other pop-overs (a 1 px EDGE rim),
its list like the track list (frameless, the selected row a rounded green plate, section rows
semibold, the slim scrollbar in the panel's right margin).

A wide main window can also show the results docked in its right pane, under the track list's own
search box (its "This tab | Whole library" choice): `QuickSearch(..., host=…)` makes such a panel,
which has no box of its own — it searches what's typed in the box it's given (`query`, `entry`),
only while it's open, and takes no space of the window's but its host's.

`Actions` is what the panel (and the Library's song views) ask the player to do.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

from . import controls, indexer, library_db, popups, searchfold, theme
from .browse import album_label
from .favourites import HEART
from .library import Album, Track, sort_name
from .library_db import SongHit, SongHits
from .playlist_view import TreeDrag, _clock, _quality, fit_columns
from .tabbar import Popover
from .virtual_list import CLOSED_MARK, OPEN_MARK, ListSource, VirtualTree

SEARCH_DELAY_MS = 120   # the search follows the box after this pause in typing
POLL_MS = 30            # how often a search on its way is looked for
ALBUM_LIMIT = 50        # albums listed (the count says how many match)
ARTIST_LIMIT = 30
PANEL_MAX_WIDTH = 960   # px at 100%: a wide window gets the panel centred, no wider than this
PANEL_MAX_HEIGHT = 760  # px at 100%: …and no taller than this (narrower, it runs to the window's foot)
NARROW, MEDIUM = 560, 820   # px at 100%: below NARROW, Title + Time; below MEDIUM, + Artist and Album
COLUMNS = ("title", "artist", "album", "year", "quality", "time")
HEADINGS = {"title": "Title", "artist": "Artist", "album": "Album", "year": "Year", "quality": "Quality",
            "time": "Time"}
LAYOUTS = {"narrow": ("title", "time"), "medium": ("title", "artist", "album", "time"), "wide": COLUMNS}
SECTIONS = ("songs", "albums", "artists")


def count_text(count: int, word: str, capped: bool = False) -> str:
    """"1,234 songs", "1 album", "1,000+ songs"."""
    if capped:
        return f"{count:,}+ {word}s"
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def show_in_explorer(path) -> None:
    """The file's folder in Explorer, the file selected (Finder on a Mac). A CUE track: its whole file."""
    path = os.fspath(path)
    try:
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select,"{path}"')   # noqa: S603 — a path of the user's own library
        elif sys.platform == "darwin":
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
    return (title, track.artist, track.album, track.year or "", _quality(track), _clock(track.duration))


def add_song_items(menu, actions: Actions, hits: Sequence[SongHit], hints: dict[str, str] | None = None,
                   show_in_library: bool = True) -> None:
    """A song's right-click items (one song, or several): Play, Play next, Add to queue, Open
    album, Add to playlist…, favourites, Show in Library, Show in Explorer. `hints`: the keys shown
    beside them (play / next / queue / album)."""
    hints = hints or {}
    first = hits[0]
    tracks = [hit.track for hit in hits]
    menu.add("Play", lambda: actions.play_song(first.track, first.album), hints.get("play", ""))
    menu.add("Play next", lambda: actions.queue_tracks(tracks, True), hints.get("next", ""))
    menu.add("Add to queue", lambda: actions.queue_tracks(tracks, False), hints.get("queue", ""))
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
class QuickSearch:
    """The search panel (see the module notes). `index_of()` gives the library index
    (library_index.shared: its database is searched); `top()` is where the panel's top edge goes in
    the window (under the tab strip). Made once, then opened and closed (`open`, `close`).

    Docked (`host`: the frame it sits in — the window packs it): no box of its own; it follows
    `query` (another search box's text) and gives the keyboard back to `entry`; `scroll_parent` is
    the padded frame its scrollbar sits in the margin of; `on_close()` when an action closes it (a
    song played, an album opened)."""

    def __init__(self, root: tk.Misc, index_of: Callable[[], object], actions: Actions,
                 top: Callable[[], int] = lambda: 0, *, host: tk.Misc | None = None,
                 query: tk.StringVar | None = None, entry: tk.Misc | None = None,
                 scroll_parent: tk.Misc | None = None, on_close: Callable[[], None] | None = None):
        self.root = root
        self.index_of = index_of
        self.actions = actions
        self.top = top
        self.docked = host is not None
        self.on_close = on_close
        self.is_open = False
        self.results: Results | None = None
        # per row of the list: ("section", name) / ("song", SongHit) / ("album", Album) / ("artist", name)
        self.items: list[tuple[str, object]] = []
        self.layout = "narrow"
        self._closed: set[str] = set()               # sections folded shut
        self._queries: indexer.Queries | None = None
        self._catalogue: Catalogue | None = None
        self._asked: int | None = None
        self._search_job = self._poll_job = None
        self._focus_before: tk.Misc | None = None
        self._note = ""                              # said in the status line until the next search
        self._pending: tuple[str, str] | None = None   # ↓ / Enter pressed in the box before its search came back

        rim = 0 if self.docked else 1
        self.panel = tk.Frame(host if self.docked else root, bg=theme.BG, highlightthickness=rim,
                              highlightbackground=theme.EDGE, highlightcolor=theme.EDGE)
        inner = ttk.Frame(self.panel, padding=0 if self.docked else theme.pad(theme.GAP_LARGE, theme.GAP_LARGE,
                                                                              theme.GAP_LARGE, theme.GAP_MEDIUM))
        inner.pack(fill="both", expand=True)
        if self.docked:
            self.search = None
            self.query, self.entry = query, entry
        else:
            self.search = controls.SearchBox(inner, "Search every song, album and artist", on_clear=self._escape)
            self.search.pack(fill="x")
            self.query, self.entry = self.search.query, self.search.entry
            self.entry.bind("<Down>", lambda e: self._into_results() or "break")
            for key, how in self._enter_keys():
                self.entry.bind(key, lambda e, h=how: self._act(h, first=True) or "break")
        self._trace = self.query.trace_add("write", lambda *_: self._changed())

        # Section rows are semibold (a named font: the list measures its words with it).
        bold = tkfont.Font(root=root._root(), font=theme.font(theme.SIZE_BODY, "semibold"))
        self._bold = bold
        self.list = VirtualTree(inner, COLUMNS, show="tree headings", on_activate=lambda i: self._act("play"),
                                on_toggle=self._toggle, on_top_edge=self._back_to_box, fonts={"group": bold},
                                margin=True, scroll_parent=scroll_parent)
        self.list.frame.pack(fill="both", expand=True, pady=theme.pad(0 if self.docked else theme.GAP_MEDIUM, 0))
        tree = self.list.tree
        self.tree = tree
        tree.configure(displaycolumns=LAYOUTS[self.layout])
        tree.column("#0", width=theme.px(22), stretch=False)
        for column in COLUMNS:
            anchor = "e" if column in ("year", "time") else "w"
            tree.heading(column, text=HEADINGS[column], anchor=anchor)
            tree.column(column, anchor=anchor, width=theme.px({"year": 48, "quality": 64, "time": 52}.get(column, 160)))
        self._fit = fit_columns(tree, {"title": 3, "artist": 2, "album": 2}, minimum=theme.px(40))
        self._colour_tags()
        theme.on_change(self._palette_changed)
        # (The drag first: Esc during a drag cancels it, before Esc's own meaning below.)
        self.drag = TreeDrag(tree, self._carried, lambda carried, x, y, phase: self.actions.drag(carried, x, y, phase))
        for key, how in self._enter_keys():
            tree.bind(key, lambda e, h=how: self._act(h) or "break")
        tree.bind("<Escape>", lambda e: None if self.drag.active else self._escape() or "break", add="+")
        tree.bind("<Button-3>", self._context)
        for key in ("<App>", "<Shift-F10>"):   # the keyboard's menu key: the selected result's menu
            tree.bind(key, lambda e: self._context(self.list.menu_event()) or "break")

        self.status = ttk.Label(inner, text="", style="Muted.TLabel", anchor="w")
        self.status.pack(fill="x", pady=theme.pad(theme.GAP_SMALL, 0))
        self.panel.bind("<Configure>", self._resized, add="+")
        self.panel.bind("<Destroy>", self._destroyed, add="+")
        if not self.docked:
            root.bind("<ButtonPress-1>", self._click_anywhere, add="+")
            root.bind("<Configure>", lambda e: self._place() if e.widget is root and self.is_open else None, add="+")
        if actions.favourites is not None:
            actions.favourites.changed.append(self._favourites_changed)
        self._say_idle()

    @staticmethod
    def _enter_keys():
        """Enter and its modifiers, and what each does (the keyboard sheet lists them)."""
        for name in ("Return", "KP_Enter"):
            yield f"<{name}>", "play"
            yield f"<Shift-{name}>", "next"
            yield f"<Control-{name}>", "queue"
            yield f"<Alt-{name}>", "album"

    def _destroyed(self, event):
        if event.widget is not self.panel:
            return
        for job in (self._search_job, self._poll_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self._search_job = self._poll_job = None
        if self._queries is not None:
            self._queries.stop()
        favourites = self.actions.favourites
        if favourites is not None and self._favourites_changed in favourites.changed:
            favourites.changed.remove(self._favourites_changed)
        if self.query is not None:
            try:
                self.query.trace_remove("write", self._trace)
            except tk.TclError:
                pass
        self.query = None
        self._bold = None   # (a Tk font: let go here, on Tk's thread)

    def _colour_tags(self):
        self.tree.tag_configure("group", foreground=theme.ACCENT_TEXT, font=self._bold)
        self.tree.tag_configure("note", foreground=theme.MUTED)

    def _palette_changed(self, _palette):
        if self.panel.winfo_exists():
            self.panel.configure(bg=theme.BG, highlightbackground=theme.EDGE, highlightcolor=theme.EDGE)
            self._colour_tags()

    # -- open and close ------------------------------------------------------------------------------
    def open(self, query: str | None = None):
        """Show the panel, the keyboard in its box (what's typed there selected, so typing replaces
        it). `query`: search for this (the track list's "Search the whole library"). Docked: start
        following the box it was given (the window has packed the panel), the keyboard left where it is."""
        if self.docked:
            self.is_open = True
            if query is not None and query != self.query.get():
                self.query.set(query)
            self._search_now()
            return
        if not self.is_open:
            try:
                focus = self.root.focus_get()
            except (tk.TclError, KeyError):
                focus = None
            self._focus_before = focus if focus is not None and not str(focus).startswith(str(self.panel)) else None
            Popover.close_open()
            self.is_open = True
            self._place()
        self.panel.lift()
        if query is not None and query != self.query.get():
            self.query.set(query)
            self._search_now()
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        self.entry.icursor("end")

    def close(self, tell: bool = True):
        """Put the panel away. Docked: stop following the box, and (`tell`) let the window know, which
        takes the panel away and goes back to the tab's list."""
        if not self.is_open:
            return
        self.is_open = False
        if self.docked:
            if self._search_job is not None:
                self.root.after_cancel(self._search_job)
                self._search_job = None
            self._asked = self._pending = None
            if tell and self.on_close is not None:
                self.on_close()
            return
        self.panel.place_forget()
        if self._search_job is not None:
            self.root.after_cancel(self._search_job)
            self._search_job = None
        before, self._focus_before = self._focus_before, None
        try:
            if before is not None and before.winfo_exists():
                before.focus_set()
        except (tk.TclError, KeyError):
            pass

    def _place(self):
        """Under the tab strip, across the window (centred and capped when it's wide or tall)."""
        margin = theme.px(Popover.MARGIN)
        room_w, room_h = self.root.winfo_width(), self.root.winfo_height()
        width = max(1, min(room_w - 2 * margin, theme.px(PANEL_MAX_WIDTH)))
        top = max(0, int(self.top()))
        height = max(1, room_h - top - margin)
        if room_w - 2 * margin > width:   # a big window: centred, and not a wall of results either
            height = min(height, theme.px(PANEL_MAX_HEIGHT))
        self.panel.place(x=(room_w - width) // 2, y=top, width=width, height=height)
        self.panel.lift()

    def _click_anywhere(self, event):
        """A click outside the panel (and outside a menu it opened) closes it."""
        if not self.is_open:
            return
        path = str(event.widget)
        inside = [str(self.panel)] + ([str(Popover.open_one)] if Popover.open_one is not None else [])
        if any(path == p or path.startswith(p + ".") for p in inside):
            return
        self.close()

    def _escape(self):
        """Esc (in the box or the list), or the box's ×: empty the box first, then close."""
        if self.query is not None and self.query.get():
            self.query.set("")
            self.entry.focus_set()
            return "break"
        if self.docked:   # (nothing typed: the keyboard back to the box; the panel stays)
            self.entry.focus_set()
            return "break"
        self.close()
        return "break"

    # -- width ----------------------------------------------------------------------------------------
    def _resized(self, event):
        if event.widget is not self.panel:
            return
        width = event.width / max(theme.scale(), 0.01)
        layout = "narrow" if width < NARROW else "medium" if width < MEDIUM else "wide"
        if layout != self.layout:
            self.layout = layout
            self.tree.configure(displaycolumns=LAYOUTS[layout])
            self._fit()
            self._show(keep=True)

    # -- searching ----------------------------------------------------------------------------------
    def _changed(self):
        if self.docked and not self.is_open:   # (the box is searching its own tab meanwhile)
            return
        if self._search_job is not None:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(SEARCH_DELAY_MS, self._search_now)

    def _words_enough(self, text: str) -> bool:
        found = searchfold.tokens(text)
        return bool(found) and max(len(word) for word in found) >= library_db.MIN_SEARCH

    def _search_now(self):
        """Ask the query thread for what's typed (a search still running is stopped)."""
        if self._search_job is not None:
            self.root.after_cancel(self._search_job)
            self._search_job = None
        text = self.query.get().strip() if self.query is not None else ""
        self._note = ""
        if not self._words_enough(text):
            self._asked = self._pending = None
            self.results = None
            self._show()
            self._say_idle()
            return
        db = self.index_of().db
        if self._queries is None or self._queries.db is not db:
            if self._queries is not None:
                self._queries.stop()
            self._queries = indexer.Queries(db)
            self._catalogue = shared_catalogue(db)
        self._asked = self._queries.ask("search", search_library, db, self._catalogue, text, interrupt=True)
        self._say("Searching…" if self.results is None else self.status.cget("text"))
        if self._poll_job is None:
            self._poll_job = self.root.after(POLL_MS, self._poll)

    def flush(self):
        """Words typed but not yet searched for: search now (a quick Enter or ↓ acts on them)."""
        if self._search_job is not None:
            self._search_now()

    def _poll(self):
        self._poll_job = None
        if self._queries is None or not self.panel.winfo_exists():
            return
        for answer in self._queries.drain():
            if answer.kind != "search" or answer.ask != self._asked:
                continue   # one a newer search replaced
            self._asked = None
            if answer.error is not None:
                self.results = None
                self._show()
                self._say("The library database is damaged: it will be repaired when the player next starts"
                          if library_db.is_damage(answer.error) else f"Couldn't search the library: {answer.error}")
            else:
                self.results = answer.value
                self._show()
                self._say_found()
            pending, self._pending = self._pending, None
            if pending is not None and pending[0] == "down":
                self._into_results()
            elif pending is not None:
                self._act(pending[1], first=True)
        if self._asked is not None:
            self._poll_job = self.root.after(POLL_MS, self._poll)

    @property
    def searching(self) -> bool:
        """A search is waiting for a pause in typing, or on its way back."""
        return self._search_job is not None or self._asked is not None

    def wait(self, timeout: float = 10.0) -> bool:
        """Until the search asked for has come back and is on show — for tests and scripts only (the
        panel itself never waits: a ↓ or Enter pressed early acts once the results are here).
        Whether it has."""
        deadline = time.monotonic() + timeout
        self.flush()
        while self._asked is not None and time.monotonic() < deadline:
            time.sleep(0.01)
            self._poll()
        return self._asked is None

    # -- the results -------------------------------------------------------------------------------------
    def _show(self, keep: bool = False):
        """List the results in their sections (a folded section shows just its heading)."""
        rows, items = [], []
        results = self.results
        inline = self.layout == "narrow"
        favourites = self.actions.favourites
        if results is not None:
            songs = results.songs
            sections = (
                ("songs", count_text(songs.total, "song", songs.capped), songs.rows),
                ("albums", count_text(results.album_total, "album"), results.albums),
                ("artists", count_text(results.artist_total, "artist"), results.artists),
            )
            for name, heading, found in sections:
                if not found:
                    continue
                closed = name in self._closed
                rows.append((f"s:{name}", (heading, "", "", "", "", ""), ("group",),
                             CLOSED_MARK if closed else OPEN_MARK))
                items.append(("section", name))
                if closed:
                    continue
                for n, thing in enumerate(found):
                    if name == "songs":
                        favourite = favourites is not None and thing.track in favourites
                        rows.append((f"t{thing.id}", song_values(thing, inline, favourite), ()))
                        items.append(("song", thing))
                    elif name == "albums":
                        label = album_label(thing)
                        if inline and thing.artist:
                            label += f"  ·  {thing.artist}"
                        rows.append((f"a{n}", (label, thing.artist, count_text(thing.track_count, "song"),
                                               thing.year or "", thing.quality, ""), ()))
                        items.append(("album", thing))
                    else:
                        artist, albums = thing
                        rows.append((f"r{n}", (artist, "", count_text(albums, "album"), "", "", ""), ()))
                        items.append(("artist", artist))
            if results.nothing:
                rows.append(("none", (f"Nothing in the library matches “{results.query}”", "", "", "", "", ""),
                             ("note",)))
                items.append(("note", None))
        self.items = items
        self.list.set_source(ListSource(rows), keep=keep)

    def _say(self, text: str):
        if self.status.cget("text") != text:
            self.status.configure(text=text)

    def _say_idle(self):
        self._say(f"Type {library_db.MIN_SEARCH} or more letters to search every song in the library")

    def _say_found(self):
        results = self.results
        if results is None:
            return
        songs = results.songs
        parts = []
        if songs.total > len(songs.rows):
            parts.append(f"The best {len(songs.rows):,} of {count_text(songs.total, 'song', songs.capped)}")
        elif songs.rows:
            parts.append(count_text(songs.total, "song"))
        if results.album_total:
            parts.append(count_text(results.album_total, "album"))
        if results.artist_total:
            parts.append(count_text(results.artist_total, "artist"))
        text = " · ".join(parts) if parts else "No matches"
        try:
            if self.index_of().indexer.running:
                text += " · the library is still being read, so more may turn up"
        except AttributeError:
            pass
        self._say(self._note or text)

    def _favourites_changed(self):
        if self.results is not None and self.panel.winfo_exists():
            self._show(keep=True)

    def _toggle(self, index: int):
        kind, name = self.items[index]
        if kind == "section":
            (self._closed.discard if name in self._closed else self._closed.add)(name)
            self._show(keep=True)

    # -- the keyboard and actions --------------------------------------------------------------------------
    def _first_result(self) -> int | None:
        return next((i for i, (kind, _thing) in enumerate(self.items) if kind in ("song", "album", "artist")), None)

    def _into_results(self):
        self.flush()
        if self._asked is not None:   # (on its way: done once it's here)
            self._pending = ("down", "")
            return
        first = self._first_result()
        if first is None:
            return
        self.tree.focus_set()
        self.list.select(first)

    def _back_to_box(self):
        self.list.select(None)
        self.entry.focus_set()

    def selected_item(self) -> tuple[str, object] | None:
        index = self.list.selection()
        return self.items[index] if index is not None and 0 <= index < len(self.items) else None

    def _act(self, how: str, first: bool = False):
        """Enter (play), Shift+Enter (next), Ctrl+Enter (queue) or Alt+Enter (album) on the selected
        result — `first`: on the first one (pressed in the box)."""
        if first:
            self.flush()
            if self._asked is not None:   # (on its way: done once it's here)
                self._pending = ("act", how)
                return
            index = self._first_result()
            item = self.items[index] if index is not None else None
        else:
            item = self.selected_item()
        if item is None:
            return
        kind, thing = item
        actions = self.actions
        if kind == "section":
            if how == "play":
                self._toggle(self.list.selection())
            return
        if kind == "song":
            if how == "play":
                self.close()
                actions.play_song(thing.track, thing.album)
            elif how == "album":
                self.close()
                actions.open_album(thing.album, False)
            else:
                actions.queue_tracks([thing.track], how == "next")
                self._note_queued(f"“{song_name(thing.track)}”", how == "next")
        elif kind == "album":
            if how in ("play", "album"):
                self.close()
                actions.open_album(thing.path, how == "play")
            else:
                actions.queue_album(thing.path, how == "next")
                self._note_queued(f"“{album_label(thing)}”", how == "next")
        elif kind == "artist" and how in ("play", "album"):
            self.close()
            actions.show_artist(thing)

    def _note_queued(self, what: str, front: bool):
        self._note = f"{what} will play next" if front else f"Added {what} to Up next"
        self._say(self._note)

    def _carried(self, iid: str):
        """What a dragged result carries: a song (as a list of one), or an album's folder."""
        index = self.list.index_of_iid(iid)
        if index is None:
            return None
        kind, thing = self.items[index]
        if kind == "song":
            return [thing.track]
        if kind == "album":
            return thing.path
        return None

    def _context(self, event):
        index = self.list.index_at(event.y)
        if index is None:
            return
        kind, thing = self.items[index]
        if kind not in ("song", "album", "artist"):
            return
        self.list.select(index, see=False)
        actions = self._closing_actions()
        menu = popups.PopupMenu(self.root)
        if kind == "song":
            add_song_items(menu, actions, [thing], {"play": "Enter", "next": "Shift+Enter", "queue": "Ctrl+Enter",
                                                     "album": "Alt+Enter"})
        elif kind == "album":
            menu.add("Play", lambda: actions.open_album(thing.path, True), "Enter")
            menu.add("Play next", lambda: actions.queue_album(thing.path, True), "Shift+Enter")
            menu.add("Add to queue", lambda: actions.queue_album(thing.path, False), "Ctrl+Enter")
            menu.add("Open album", lambda: actions.open_album(thing.path, False), "Alt+Enter")
            menu.add_separator()
            menu.add("Show in Library", lambda: actions.show_in_library(thing.path))
            menu.add("Show in Explorer", lambda: actions.show_in_explorer(thing.path))
        else:
            menu.add("Show in Library", lambda: actions.show_artist(thing), "Enter")
        menu.popup(event.x_root, event.y_root)

    def _closing_actions(self) -> Actions:
        """The actions, the ones that take you elsewhere (play, open, show) closing the panel first."""
        actions = self.actions

        def closing(act):
            return lambda *args: (self.close(), act(*args))

        return dataclasses.replace(actions, play_song=closing(actions.play_song),
                                   open_album=closing(actions.open_album),
                                   show_in_library=closing(actions.show_in_library),
                                   show_artist=closing(actions.show_artist))

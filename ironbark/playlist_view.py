"""The track list in the main window, with a search box above it.

One album: a plain numbered list. Several albums (e.g. the whole library): each album
gets a header row above its tracks. A playlist: a flat list in its own order, with artists.
Right-click a song (or an album header) for Play / Add to playlist / Remove, etc. Typing in the search box narrows the list to the
songs whose title, artist, album, album artist, composer, genre, year or file name hold every word
typed (accents and case ignored: searchfold.py); double-clicking a result plays it in place, and the
album carries on from there.

The list is a virtual_list.VirtualTree: only the rows on show are in the Treeview, worked out as
they're drawn, so a tab of a whole library (200,000 songs) opens, scrolls and searches as quickly as
an album. A search of a list that long runs on the list's own thread (`_Worker`), off Tk's; a tab
whose songs come from the library database a page at a time (indexer.LibraryTrackList: it has
`peek`, `want`, `matching` and `groups`) is searched there by the database, and a row whose song
hasn't been read yet shows "…" until it has.

Several songs can be selected (Shift+click, Ctrl+click, Shift+↑ / ↓, Ctrl+A); everything that takes
songs takes them all (`selected_rows`): Play next, Add to queue, Add to playlist, Remove from the
playlist, Edit tags, the favourites, and dragging.

Songs (or an album, by its header) can be dragged: onto a tab to add them there, or up and down
a playlist to reorder it. The view only reports the drag (`on_drag`); the window decides what
it means (see TreeDrag and ui.PlayerWindow._song_drag).

Artist, Album and Plays columns can be added (off by default: the window is narrow); right-click the
headings, or a song, to switch them (`set_columns`, saved by the window). Plays (how often each song
has been played, from the library database's count) is read for the rows on show only, on the list's
own thread.

In a wide window (layout.py's split tiers, `set_tier`) the list has the window's full height and
its own column setting (`set_wide_columns`): Artist, Album and Format show by themselves wherever
the tier has room — except a column whose value is the same on every row of the tab (`varies`,
worked out once per list), so one album shows # · Title · Time and a playlist everything. A header
row above the list holds the search box and the tab's song count (`set_count`); the rows are
roomier, and from 1920 logical px the text is larger.

The list's keys (Enter plays, Ctrl+Enter / Ctrl+Shift+Enter queue, Delete removes, Alt+↑ / ↓
move a song, Ctrl+A selects every song) are in the keyboard table (keys.py); the window binds them,
using `activate`, `selected_rows`, `selected_track` and `select_all`.

The look (0.20.0): the row under the pointer lights up, the playing song is marked (♪ in place of
its number, green semibold text) even when another row is selected, album header rows read
"Album · Artist" in green semibold with the quality on the right, the scrollbar is slim and only
there when the list is longer than the window, and the search box has a magnifier and a × to clear
it. The list is frameless on the window's colour, the selected row a rounded green plate, and the
scrollbar sits in the window's right margin, so the list's words line up with the search box.

A search that finds nothing in the tab offers the whole library instead: a "Search the whole
library (Ctrl+Shift+F)" row under "No matches" (click it, or Enter on it) hands the words to the
song search panel (`on_search_library`). A favourite song shows a ♥ before its title
(`is_favourite`; `refresh_titles` after the favourites change redraws only the rows on show).
"""

from __future__ import annotations

import bisect
import queue
import threading
import tkinter as tk
from array import array
from collections import OrderedDict
from collections.abc import Sequence
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from . import audiofile, controls, layout, popups, searchfold, theme
from .library import Track, album_folder, group_by_album
from .virtual_list import ELLIPSIS, VirtualTree

SEARCH_DELAY_MS = 150
SEARCH_THREAD_AT = 20_000   # songs: a list this long is searched on the list's own thread, off Tk's
POLL_MS = 40                # how often the list looks for what its thread (or the database) has brought
PLAYS_KEPT = 20_000         # play counts remembered (the rows shown lately)
OPTIONAL_COLUMNS = ("artist", "album", "plays")  # columns that can be switched on in the narrow window
WIDE_COLUMNS = layout.OPTIONAL           # …and in a wide one (where Format is one too)
COLUMNS = ("no", "title", "artist", "album", "quality", "plays", "time")
QUALITY_W = 72      # px at 100%: the Format column ("24/96", "FLAC", "Opus")
PLAYS_W = 52        # px at 100%: the Plays column
SEARCH_MAX = 520    # px at 100%: the search box's widest, in a wide window's header row
# How a wide window shares the list's width: by weight, Artist and Album never past their caps (px at
# 100%), whatever is left to the title. The narrow window shares 2 / 1 / 1 with no caps.
NARROW_WEIGHTS = {"title": 2, "artist": 1, "album": 1}
WIDE_WEIGHTS = {"title": 3, "artist": 2, "album": 2}
WIDE_CAPS = {"artist": 320, "album": 360}
TREE_STYLES = {layout.COLUMN: "", layout.SPLIT: "", layout.SPLIT_M: theme.ROOMY_TREE,   # ("": Treeview's own)
               layout.SPLIT_L: theme.ROOMY_LARGE_TREE}
DRAG_START = 6  # px at 100%: move this far with the button down and a press becomes a drag
PLAYING_MARK = "♪"   # (0.20.0) in the # column of the playing song
FAVOURITE_MARK = "♥"
SEARCH_LIBRARY = "Search the whole library (Ctrl+Shift+F)"


def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# The search's folding and the text it looks through (kept under their old names here too).
_fold = searchfold.fold
_haystack = searchfold.haystack


def matches(track: Track, words: list[str]) -> bool:
    haystack = _haystack(track)
    return all(word in haystack for word in words)


def album_heading(tracks: list[Track], folder) -> str:
    """An album header row's text: "Album · Artist" from the tags (the folder's name if the album
    isn't tagged; no artist for a compilation, nor when it's the album's name too — a self-titled
    album)."""
    first = tracks[0]
    name = first.album or folder.name
    artist = first.album_artist or ("" if first.compilation else first.artist)
    return f"{name}  ·  {artist}" if artist and _fold(artist) != _fold(name) else name


def _quality(track: Track) -> str:
    if track.lossy:
        return track.codec
    return f"{track.bits}/{track.sample_rate / 1000:g}" if track.sample_rate else ""


def fit_columns(tree: ttk.Treeview, weights: dict[str, float], minimum: int = 40,
                caps: dict[str, int] | None = None) -> Callable[[], None]:
    """Share a list's width between its stretchy columns (by `weights`) once the fixed ones have
    their room, whenever it's resized or columns are switched on / off. (Tk itself never shrinks
    columns to fit, so extra columns would push the last ones out of sight.) `caps`: real px a column
    named in it never grows past; what it leaves goes to the others. A stretchy column gets at least
    `minimum` — unless the list is too narrow for that, when they share what's left, so the fixed
    columns (a year, a time) always keep their room. Both dicts are read on every fit, so changing
    them in place (then calling the fitter) changes the sharing. Returns the fitter."""
    for column in weights:
        tree.column(column, stretch=False)  # sized here, not by Tk

    def fit(_event=None):
        width = tree.winfo_width()
        if width <= 1:
            return  # not on screen yet: the first <Configure> fits it
        shown = [str(c) for c in tree.cget("displaycolumns")]
        if shown == ["#all"]:
            shown = [str(c) for c in tree.cget("columns")]
        fixed = sum(int(tree.column(c, "width")) for c in shown if c not in weights)
        if "tree" in [str(part) for part in tree.cget("show")]:
            fixed += int(tree.column("#0", "width"))
        stretchy = [c for c in shown if c in weights]
        room = max(0, width - fixed - 4)
        capped: dict[str, int] = {}
        while True:   # a column that would pass its cap gets the cap; the others share what's left
            free = [c for c in stretchy if c not in capped]
            total = sum(weights[c] for c in free) or 1
            left = room - sum(capped.values())
            over = [c for c in free if caps and c in caps and int(left * weights[c] / total) > caps[c]]
            if not over or len(over) == len(free):
                break
            capped.update((c, caps[c]) for c in over)
        least = minimum if room >= minimum * len(stretchy) else 1   # (too narrow: they give way, not the fixed ones)
        widths = {c: capped[c] if c in capped else max(least, int(left * weights[c] / total)) for c in stretchy}
        excess = sum(widths.values()) - room
        for column in sorted(stretchy, key=lambda c: -widths[c]):   # (each at its minimum: the widest give way)
            if excess <= 0:
                break
            cut = min(excess, widths[column] - least)
            widths[column] -= cut
            excess -= cut
        for column in stretchy:
            tree.column(column, width=widths[column])

    tree.bind("<Configure>", fit, add="+")
    return fit


class TreeDrag:
    """Turns press–move–release on a Treeview row into a drag (after a few pixels, so clicks and
    double-clicks still work as before). `what(iid)` says what a row carries (None = not
    draggable); `on_drag(carried, x_root, y_root, phase)` hears "move" for each movement and
    "drop" or "cancel" (Escape) at the end. Nothing is moved here."""

    def __init__(self, tree: ttk.Treeview, what: Callable[[str], object | None],
                 on_drag: Callable[[object, int, int, str], None]):
        self.tree, self.what, self.on_drag = tree, what, on_drag
        self._state: dict | None = None
        self._start = theme.px(DRAG_START)
        tree.bind("<ButtonPress-1>", self._press, add="+")
        tree.bind("<B1-Motion>", self._motion, add="+")
        tree.bind("<ButtonRelease-1>", self._release, add="+")
        tree.bind("<Escape>", self._cancel, add="+")

    @property
    def active(self) -> bool:
        return bool(self._state and self._state["moving"])

    def _press(self, event):
        iid = self.tree.identify_row(event.y)
        carried = self.what(iid) if iid and self.tree.identify_region(event.x, event.y) == "cell" else None
        self._state = None if carried is None else {"carried": carried, "x0": event.x_root, "y0": event.y_root,
                                                     "moving": False}

    def _motion(self, event):
        state = self._state
        if state is None:
            return
        if not state["moving"]:
            if max(abs(event.x_root - state["x0"]), abs(event.y_root - state["y0"])) < self._start:
                return
            state["moving"] = True
            self.tree.configure(cursor="hand2")
        self.on_drag(state["carried"], event.x_root, event.y_root, "move")

    def _release(self, event):
        state, self._state = self._state, None
        if state is not None and state["moving"]:
            self.tree.configure(cursor="")
            self.on_drag(state["carried"], event.x_root, event.y_root, "drop")

    def _cancel(self, _event=None):
        """Esc: a drag under way is called off — and that's all the key does."""
        state, self._state = self._state, None
        if state is not None and state["moving"]:
            self.tree.configure(cursor="")
            self.on_drag(state["carried"], 0, 0, "cancel")
            return "break"
        return None


# -- work off Tk's thread ------------------------------------------------------------------------------
class _Worker:
    """The track list's own thread, for work too slow for Tk's: a big list's search, play counts from
    the library database. One job of each kind is wanted at a time (a newer one replaces it before it
    starts); results come back on `results`, which the list's `after` loop drains. Jobs are plain
    functions of plain data: nothing here touches Tk or holds the list."""

    def __init__(self):
        self.results: queue.SimpleQueue = queue.SimpleQueue()
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._latest: dict[str, int] = {}
        self._lock = threading.Lock()
        self._count = 0
        self._thread: threading.Thread | None = None
        self._stopped = False

    def ask(self, kind: str, fn: Callable, *args) -> int:
        with self._lock:
            self._count += 1
            number = self._latest[kind] = self._count
            if self._thread is None and not self._stopped:
                self._thread = threading.Thread(target=self._run, name="track-list", daemon=True)
                self._thread.start()
        self._jobs.put((kind, number, fn, args))
        return number

    def wanted(self, kind: str, number: int) -> bool:
        with self._lock:
            return self._latest.get(kind) == number

    def forget(self, kind: str):
        """A job of this kind on its way is no longer wanted (its result is dropped)."""
        with self._lock:
            self._latest.pop(kind, None)

    def stop(self):
        with self._lock:
            self._stopped = True
        self._jobs.put(None)

    def join(self, timeout: float | None = None):
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self):
        while True:
            item = self._jobs.get()
            if item is None:
                return
            kind, number, fn, args = item
            item = None
            if not self.wanted(kind, number):
                continue
            try:
                self.results.put((kind, number, fn(*args), None))
            except Exception as error:   # (handed back: the list says so, or carries on without)
                self.results.put((kind, number, None, error))
            fn = args = None


def _search(tracks: Sequence[Track], haystacks: list[str] | None, words: list[str], last) -> tuple:
    """(The list's thread, or Tk's for a short list.) The songs matching every word, in list order,
    and each song's folded search text (folded once per list, then kept). While the words only grow
    (more typing), only the songs found last time are looked through again. A list from the library
    database is searched there (its `matching`)."""
    finder = getattr(tracks, "matching", None)
    if finder is not None:
        return None, finder(words)
    if haystacks is None:
        haystacks = [_haystack(track) for track in tracks]
    candidates = range(len(tracks))
    if last is not None and all(any(old in new for new in words) for old in last[0]):
        candidates = last[1]   # every song these words find, the last ones found too
    return haystacks, [i for i in candidates if all(word in haystacks[i] for word in words)]


class Rows(list):
    """The songs a right-click menu is for (track indexes); `album`: they're one album header's."""
    album = False


def _play_counts(db, keys: list[str]) -> dict[str, int]:
    return db.play_counts(keys)


class _Listing:
    """The rows the track list shows, for its VirtualTree: album headers and songs (every song, or the
    ones a search found) and the "No matches" rows. `entries` holds a row's song (its index) or album
    header (-1 - its group); the rows themselves are made only as they're drawn."""

    def __init__(self, view: PlaylistView, entries: Sequence[int], tail: list[str]):
        self.view, self.entries, self.tail = view, entries, tail

    def __len__(self) -> int:
        return len(self.entries) + len(self.tail)

    def entry(self, row: int) -> int | str:
        n = len(self.entries)
        return self.tail[row - n] if row >= n else self.entries[row]

    def key(self, row: int) -> str:
        found = self.entry(row)
        if isinstance(found, str):
            return found
        return str(found) if found >= 0 else f"a{-1 - found}"

    def row(self, row: int) -> tuple:
        found = self.entry(row)
        if isinstance(found, str):
            return self.view._note_row(found)
        return self.view._song_row(found) if found >= 0 else self.view._header_row(-1 - found)

    def index_of(self, key: str) -> int | None:
        if key in self.tail:
            return len(self.entries) + self.tail.index(key)
        if key.isdigit():
            return self.view._row_of(int(key))
        if key.startswith("a") and key[1:].isdigit():
            return self.view._header_row_of(int(key[1:]))
        return None


class PlaylistView:
    def __init__(self, parent: tk.Misc, on_play: Callable[[int], None],
                 on_menu: Callable[[list[int], tk.Event], None] = lambda rows, event: None,
                 on_drag: Callable[[list[int], int, int, str], None] = lambda rows, x, y, phase: None,
                 on_heading_menu: Callable[[tk.Event], None] = lambda event: None,
                 on_search_library: Callable[[str], None] | None = None,
                 is_favourite: Callable[[Track], bool] = lambda track: False,
                 plays_db: Callable[[], object | None] = lambda: None):
        self.on_play = on_play
        self.on_heading_menu = on_heading_menu
        self.on_search_library = on_search_library
        self.is_favourite = is_favourite
        self.plays_db = plays_db                # the library database the Plays column counts from (None: none)
        self.columns: tuple[str, ...] = ()  # the narrow window's optional columns (see set_columns)
        self.wide_columns: tuple[str, ...] = tuple(c for c in WIDE_COLUMNS if c != "plays")   # a wide window's
        self.tier = layout.COLUMN           # the window's layout (set_tier)
        self.varies: frozenset[str] = frozenset()   # optional columns whose value isn't the same on every row
        self.shown: tuple[str, ...] = ()    # the optional columns on show now
        self.on_menu = on_menu
        self.flat = False
        self._tracks: Sequence[Track] = []
        self._peek: Callable[[int], Track | None] | None = None   # a list read from the database a page at a time
        self._serial_seen = None
        self._search_job = None
        self._poll_job = None
        self.playing: int | None = None         # (0.20.0) the playing song's index, marked in the list
        # Kept per list (set_tracks), so a pause in typing doesn't redo them over a whole library:
        self._groups: list = []                 # its albums: (folder, first index, end index)
        self._starts: list[int] = []            # …each one's first index (to find a song's album)
        self._grouped = False
        self._haystacks: list[str] | None = None   # each song's folded search text (made at the first search)
        self._last_search: tuple[list[str], list[int]] | None = None   # words, and the songs they found
        self._list_serial = 0                   # bumped with each new list (a search on its way for another is dropped)
        # What's listed now: the words searched for, the songs found (None: all), the albums listed.
        self._words: list[str] = []
        self._found: Sequence[int] | None = None
        self._listed_groups: Sequence[int] = ()
        self._listing = _Listing(self, (), [])
        self._worker = _Worker()
        self._asked: dict[str, int | None] = {"search": None, "plays": None}
        self._plays: OrderedDict[str, int] = OrderedDict()   # song key -> times played (the rows shown lately)
        self._plays_wanted: list[str] = []
        self._plays_job = None

        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        self.frame = frame
        # A wide window's header row: the search box (moved in) and the tab's count on the right.
        # Made before the search box: a widget packed into a frame other than its parent must be
        # above that frame in Tk's stacking order. Not packed in the narrow window.
        self.header = ttk.Frame(frame)
        self.count_label = ttk.Label(self.header, text="", style="Sub.TLabel", anchor="e")
        self.count_label.pack(side="right")
        self.header.bind("<Configure>", lambda e: self._fit_search(), add="+")
        self._search_pad = 0

        # (0.20.0) A magnifier inside the box on the left, and a × on the right while there's
        # something typed; (0.22.0) the box itself is controls.SearchBox, shared with the Library.
        self.search = controls.SearchBox(frame, "Search songs, artists, albums", on_clear=self.clear_search,
                                         keys="Ctrl+F")
        self.search.pack(fill="x", pady=theme.pad(0, theme.GAP))
        self.query, self.entry = self.search.query, self.search.entry
        self.magnifier, self.clear_button, self.hint = self.search.magnifier, self.search.clear_button, self.search.hint
        self.query.trace_add("write", lambda *_: self._query_changed())
        self.entry.bind("<Down>", lambda e: self._focus_results())
        self.entry.bind("<Return>", lambda e: self._play_first_result())

        # The album and playing rows' semibold: one font, made larger in place from 1920 logical px.
        self._bold = tkfont.Font(root=frame._root(), font=theme.font(theme.SIZE_BODY, "semibold"))
        # (0.20.0) The slim scrollbar, only there when the list is longer than its window — in the
        # window's right margin (`parent` is the padded frame round the list).
        self.list = VirtualTree(frame, COLUMNS, show="headings", on_activate=lambda index: self.activate(),
                                fonts={"album": self._bold, "playing": self._bold}, playing_column="no",
                                playing_mark=PLAYING_MARK, margin=True, multiple=True, scroll_parent=parent)
        table = self.list.frame
        table.pack(fill="both", expand=True)
        self.table = table
        self.tree = self.list.tree
        self.scrollbar = self.list.scrollbar
        self.tree.configure(displaycolumns=("no", "title", "time"))
        self.tree.heading("no", text="#", anchor="e")
        self.tree.heading("title", text="Title", anchor="w")
        self.tree.heading("artist", text="Artist", anchor="w")
        self.tree.heading("album", text="Album", anchor="w")
        self.tree.heading("quality", text="Format", anchor="w")
        self.tree.heading("plays", text="Plays", anchor="e")
        self.tree.heading("time", text="Time", anchor="e")
        self.tree.column("no", width=theme.px(38), stretch=False, anchor="e")
        self.tree.column("title", width=theme.px(240), stretch=True, anchor="w")
        self.tree.column("artist", width=theme.px(120), stretch=True, anchor="w")
        self.tree.column("album", width=theme.px(120), stretch=True, anchor="w")
        self.tree.column("quality", width=theme.px(QUALITY_W), stretch=False, anchor="w")
        self.tree.column("plays", width=theme.px(PLAYS_W), stretch=False, anchor="e")
        self.tree.column("time", width=theme.px(52), stretch=False, anchor="e")
        self._weights = dict(NARROW_WEIGHTS)   # changed in place by set_tier (the fitter reads them)
        self._caps: dict[str, int] = {}
        self._fit = fit_columns(self.tree, self._weights, minimum=theme.px(50), caps=self._caps)
        self._colour_tags()
        theme.on_change(self._palette_changed)
        self.tree.bind("<ButtonRelease-1>", self._released, add="+")
        self.tree.bind("<Button-3>", self._context)
        for key in ("<App>", "<Shift-F10>"):   # (0.21.0) the keyboard's menu key: the focused row's menu
            self.tree.bind(key, lambda e: self._context(popups.row_event(self.tree)) or "break")
        # Dragging songs: a song carries itself, an album header its album's listed songs, a row of a
        # selection of several every song selected.
        self.drag = TreeDrag(self.tree, lambda iid: self._carried(iid) or None, on_drag)
        self._drop_line = tk.Frame(self.tree, bg=theme.ACCENT, height=max(2, theme.px(2)))
        self.tree.bind("<Destroy>", self._destroyed, add="+")

    def _destroyed(self, event):
        """Gone: the list's thread stops, and the font is let go here, on Tk's thread."""
        if event.widget is not self.tree:
            return
        for job in (self._search_job, self._poll_job, self._plays_job):
            if job is not None:
                try:
                    self.tree.after_cancel(job)
                except tk.TclError:
                    pass
        self._search_job = self._poll_job = self._plays_job = None
        self._worker.stop()
        self._bold = None
        self._listing = _Listing(self, (), [])
        self._tracks = []

    def _colour_tags(self):
        # A Treeview gives the tag configured *first* the say when two set the same option (checked
        # on Tk 8.6.12): "hover" (configured by the list itself) comes first so a hovered album /
        # playing row lights up too. Its text turns TEXT: green text on the hover grey would be
        # under WCAG AA's 4.5:1 in Dark.
        self.tree.tag_configure("hover", background=theme.HOVER, foreground=theme.TEXT)
        self.tree.tag_configure("album", background=theme.BG, foreground=theme.ACCENT_TEXT, font=self._bold)
        self.tree.tag_configure("playing", foreground=theme.ACCENT_TEXT, font=self._bold)
        self.tree.tag_configure("note", foreground=theme.MUTED)
        self.tree.tag_configure("loading", foreground=theme.MUTED)
        self.tree.tag_configure("link", foreground=theme.ACCENT_TEXT)

    def _palette_changed(self, _palette):
        """Treeview row colours aren't widget options, so theme.use can't swap them itself. (The
        search box looks after its own.)"""
        if self.tree.winfo_exists():
            self._colour_tags()

    # -- hover -------------------------------------------------------------------
    @property
    def hovered(self) -> str | None:
        """The row under the pointer (its Treeview id), if any."""
        return self.list.iid_of(self.list.hovered) if self.list.hovered is not None else None

    def _set_hover(self, iid: str | None):
        """Light up the row under the pointer (None: none)."""
        self.list.set_hover(self.list.index_of_iid(iid) if iid else None)

    # -- columns and layout ------------------------------------------------------
    def set_columns(self, columns):
        """The narrow window's optional columns (OPTIONAL_COLUMNS), e.g. ("artist",)."""
        self.columns = tuple(c for c in OPTIONAL_COLUMNS if c in columns)
        if self.tier == layout.COLUMN:
            self._show_columns()

    def set_wide_columns(self, columns):
        """A wide window's optional columns (WIDE_COLUMNS): each shows where the tier has room and
        its value varies between rows."""
        self.wide_columns = tuple(c for c in WIDE_COLUMNS if c in columns)
        if self.tier != layout.COLUMN:
            self._update_shown()

    def wanted(self) -> tuple[str, ...]:
        """The column setting in use: the narrow window's, or a wide one's."""
        return self.columns if self.tier == layout.COLUMN else self.wide_columns

    def set_tier(self, tier: str):
        """The window's layout changed (layout.py): the list's style, columns, column sharing and
        header row follow."""
        if tier == self.tier:
            return
        wide, was_wide = tier != layout.COLUMN, self.tier != layout.COLUMN
        self.tier = tier
        self.tree.configure(style=TREE_STYLES[tier])
        self.list._metrics = None   # (the rows' height goes with the style)
        self._weights.clear()
        self._weights.update(WIDE_WEIGHTS if wide else NARROW_WEIGHTS)
        self._caps.clear()
        if wide:
            self._caps.update({c: theme.px(v) for c, v in WIDE_CAPS.items()})
        self._bold.configure(size=theme.SIZE_LARGE if tier == layout.SPLIT_L else theme.SIZE_BODY)
        if wide != was_wide:
            self._pack_header(wide)
        self._update_shown()

    def _update_shown(self):
        """Show the optional columns the setting, the tier and the rows call for."""
        self._show_columns()

    def _show_columns(self, draw: bool = True):
        if self.tier == layout.COLUMN:
            shown = self.columns
        else:
            shown = layout.columns_for(self.tier, self.wide_columns, self.varies)
        self.shown = tuple(shown)
        self.tree.configure(displaycolumns=("no", "title", *self.shown, "time"))
        self._fit()
        if draw:
            self.list.refresh()   # (a title carries its artist only while the Artist column is hidden)

    def _pack_header(self, wide: bool):
        """The search box in the header row (a wide window), or on its own above the list."""
        self.search.pack_forget()
        if wide:
            self.header.pack(fill="x", pady=theme.pad(0, theme.GAP), before=self.table)
            self.search.pack(in_=self.header, side="left", fill="x", expand=True)
            # Just above the header row it's packed in, not above everything: Tab follows the
            # stacking order, so raising it past the list would put the list first.
            self.search.lift(self.header)
            self._search_pad = -1
            self._fit_search()
        else:
            self.header.pack_forget()
            self.search.pack(in_=self.frame, fill="x", pady=theme.pad(0, theme.GAP), before=self.table)

    def _fit_search(self):
        """In the header row the search box grows with the list up to SEARCH_MAX, the count keeping
        its room on the right."""
        if self.tier == layout.COLUMN:
            return
        room = self.header.winfo_width() - self.count_label.winfo_reqwidth()
        pad = max(theme.px(theme.GAP_WIDE), room - theme.px(SEARCH_MAX))
        if abs(pad - self._search_pad) > 2:
            self._search_pad = pad
            self.search.pack_configure(padx=(0, pad))

    def set_count(self, text: str):
        """The tab's count in a wide window's header row, e.g. "Album · 13 songs · 51:57"."""
        if self.count_label.cget("text") != text:
            self.count_label.configure(text=text)
            self._fit_search()

    # -- contents --------------------------------------------------------------
    @staticmethod
    def _varies(tracks: Sequence[Track]) -> frozenset[str]:
        """The optional columns whose value isn't the same on every song (one pass over the list; a
        list from the library database says itself). Plays always may."""
        if not len(tracks):
            return frozenset()
        known = getattr(tracks, "varies", None)
        if known is not None:
            return frozenset(known) | {"plays"}
        first = tracks[0]
        artist, album, quality = first.artist, first.album or album_folder(first.path).name, _quality(first)
        varies = {"plays"}
        for track in tracks[1:]:
            if "artist" not in varies and track.artist != artist:
                varies.add("artist")
            if "album" not in varies and (track.album or album_folder(track.path).name) != album:
                varies.add("album")
            if "quality" not in varies and _quality(track) != quality:
                varies.add("quality")
            if len(varies) == len(WIDE_COLUMNS):
                break
        return frozenset(varies)

    def set_tracks(self, tracks: Sequence[Track], flat: bool = False):
        """flat: a playlist, shown in its own order without album headers. (The playing mark is
        dropped: the window marks the playing song again if this list has it.) `tracks` may be a
        list from the library database (indexer.LibraryTrackList): read a page at a time."""
        if self._search_job:
            self.tree.after_cancel(self._search_job)
            self._search_job = None
        self._worker.forget("search")
        self._asked["search"] = None
        self._tracks = list(tracks) if isinstance(tracks, list) else tracks
        self._peek = getattr(tracks, "peek", None)
        self._serial_seen = getattr(tracks, "serial", None)
        self.playing = None
        self.flat = flat
        groups = getattr(tracks, "groups", None)
        count = len(self._tracks)
        if flat:
            self._groups = [(None, 0, count)]
        else:
            self._groups = list(groups) if groups is not None else group_by_album(self._tracks)
        self._starts = [start for _folder, start, _end in self._groups]
        self._grouped = len(self._groups) > 1
        self.varies = self._varies(self._tracks)
        self._haystacks = None
        self._last_search = None
        self._list_serial += 1
        if self.tier != layout.COLUMN:
            self._show_columns(draw=False)
        words = self._typed()
        if words and count >= SEARCH_THREAD_AT:
            self._list(words=[], found=None, keep=False)   # (every song, until the search comes back)
            self._ask_search(words)
        else:
            self._list(words, self._match_now(words), keep=False)

    def _typed(self) -> list[str]:
        return [_fold(w) for w in self.query.get().split()]

    def _match_now(self, words: list[str]) -> list[int] | None:
        """The songs matching every word, in list order (None: no words, so every song), worked out
        here and now."""
        if not words:
            self._last_search = None
            return None
        haystacks, found = _search(self._tracks, self._haystacks, words, self._last_search)
        if haystacks is not None:
            self._haystacks = haystacks
        self._last_search = (words, found)
        return found

    def _list(self, words: list[str], found: Sequence[int] | None, keep: bool = True):
        """List `found` (None: every song) under their album headers; "No matches" (and the whole
        library's search) when a search found nothing."""
        groups, grouped = self._groups, self._grouped
        tail: list[str] = []
        listed: Sequence[int]
        entries: Sequence[int]
        if found is None:
            listed = range(len(groups)) if grouped else ()
            if grouped:
                made = array("q")
                for g, (_folder, start, end) in enumerate(groups):
                    made.append(-1 - g)
                    made.extend(range(start, end))
                entries = made
            else:
                entries = range(len(self._tracks))
        else:
            if grouped:
                made, listed_groups = array("q"), []
                g, current = 0, -1
                for i in found:
                    while groups[g][2] <= i:
                        g += 1
                    if g != current:
                        made.append(-1 - g)
                        listed_groups.append(g)
                        current = g
                    made.append(i)
                entries, listed = made, listed_groups
            else:
                entries, listed = found, ()
            if not found:
                tail = ["none"] + (["library"] if self.on_search_library is not None else [])
        new_words = words != self._words
        self._words, self._found, self._listed_groups = words, found, listed
        self._listing = _Listing(self, entries, tail)
        self.list.playing = self._row_of(self.playing)
        if keep and new_words:
            self.list.top = 0   # (a new search lists from its first find)
        self.list.set_source(self._listing, keep=keep)
        if keep and new_words and self.list.selected is not None:
            self.list.see(self.list.selected)

    def _render(self):
        """List the songs the search finds (all of them with no search) under their album headers,
        here and now (a search waiting for a pause in typing, or on its way from the list's thread,
        isn't needed any more)."""
        if self._search_job:   # (this shows the search as it stands: a pending one would only repeat it)
            self.tree.after_cancel(self._search_job)
            self._search_job = None
        self._worker.forget("search")
        self._asked["search"] = None
        words = self._typed()
        self._list(words, self._match_now(words))

    # -- the rows ----------------------------------------------------------------
    def _track(self, i: int) -> Track | None:
        """Song i, or None while it's being read (a list from the database: asked for here)."""
        peek = self._peek
        if peek is None:
            return self._tracks[i]
        track = peek(i)
        if track is None:
            self._tracks.want(i)
            self._start_poll()
        return track

    def _start_of(self, i: int) -> int:
        return self._groups[bisect.bisect_right(self._starts, i) - 1][1]

    def _song_row(self, i: int) -> tuple:
        track = self._track(i)
        if track is None:
            return (str(i), ("", ELLIPSIS, "", "", "", "", ""), ("loading",))
        number = (i - self._start_of(i) + 1) if self._grouped else (i + 1)
        plays = self._plays_text(track) if "plays" in self.shown else ""
        return (str(i), (number, self._title(i, bool(self._words), track), track.artist, track.album,
                         _quality(track), plays, _clock(track.duration)), ())

    def _header_row(self, g: int) -> tuple:
        folder, start, _end = self._groups[g]
        track = self._track(start)
        if track is None:
            text, quality = getattr(folder, "name", "") or ELLIPSIS, ""
        else:
            text, quality = album_heading([track], folder), _quality(track)
        return (f"a{g}", ("", text, "", "", quality, "", "" if "quality" in self.shown else quality), ("album",))

    @staticmethod
    def _note_row(key: str) -> tuple:
        if key == "none":
            return ("none", ("", "No matches", "", "", "", "", ""), ("note",))
        return ("library", ("", SEARCH_LIBRARY, "", "", "", "", ""), ("link",))

    def _title(self, i: int, searching: bool, track: Track | None = None) -> str:
        """Song i's title cell: with its artist while searching and in a playlist (unless the
        Artist column shows it), and a note when its file has gone."""
        track = track if track is not None else self._tracks[i]
        show_artist = (searching or self.flat) and track.artist and "artist" not in self.shown
        title = f"{track.title}  ·  {track.artist}" if show_artist else track.title
        if self.is_favourite(track):
            title = f"{FAVOURITE_MARK} {title}"
        if not track.missing:
            return title
        if isinstance(track.path, audiofile.UrlPath):
            return title + "   (web address — not supported yet)"
        return title + "   (file not found)"

    def _row_of(self, i: int | None) -> int | None:
        """Where song i is listed (None: not listed — the search hides it)."""
        if i is None or not 0 <= i < len(self._tracks):
            return None
        found = self._found
        if found is None:
            k = i
        else:
            k = bisect.bisect_left(found, i)
            if k >= len(found) or found[k] != i:
                return None
        if not self._grouped:
            return k
        g = bisect.bisect_right(self._starts, i) - 1
        return k + bisect.bisect_right(self._listed_groups, g)

    def _header_row_of(self, g: int) -> int | None:
        """Where album header g is listed (None: the search found none of its songs)."""
        if not self._grouped or not 0 <= g < len(self._groups):
            return None
        listed = self._listed_groups
        k = bisect.bisect_left(listed, g)
        if k >= len(listed) or listed[k] != g:
            return None
        start = self._groups[g][1]
        before = start if self._found is None else bisect.bisect_left(self._found, start)
        return before + k

    def _group_songs(self, g: int) -> Sequence[int]:
        """Album g's songs as listed now (the ones the search found)."""
        _folder, start, end = self._groups[g]
        found = self._found
        if found is None:
            return range(start, end)
        return found[bisect.bisect_left(found, start):bisect.bisect_left(found, end)]

    @property
    def _songs(self) -> list[str]:
        """The song rows listed now, top to bottom (their ids)."""
        found = self._found
        return [str(i) for i in (range(len(self._tracks)) if found is None else found)]

    def _first_song(self) -> int | None:
        found = self._found
        if found is None:
            return 0 if len(self._tracks) else None
        return found[0] if found else None

    # -- play counts (the Plays column) -------------------------------------------
    def _plays_text(self, track: Track) -> str:
        key = audiofile.key(track.path)
        count = self._plays.get(key)
        if count is None:
            if self.plays_db() is not None and key not in self._plays_wanted:
                self._plays_wanted.append(key)
                if self._plays_job is None:
                    self._plays_job = self.tree.after_idle(self._ask_plays)
            return ""
        self._plays.move_to_end(key)
        return f"{count:,}" if count else ""

    def _ask_plays(self):
        """The rows on show asked for play counts not known yet: read them on the list's thread."""
        self._plays_job = None
        keys, self._plays_wanted = self._plays_wanted, []
        db = self.plays_db()
        if keys and db is not None:
            self._asked["plays"] = self._worker.ask("plays", _play_counts, db, keys)
            self._start_poll()

    def _take_plays(self, counts: dict[str, int] | None):
        if counts:
            self._plays.update(counts)
            while len(self._plays) > PLAYS_KEPT:
                self._plays.popitem(last=False)
            if "plays" in self.shown:
                self.list.refresh()

    def plays_changed(self):
        """A song's play was counted: the Plays column reads its counts again."""
        self._plays.clear()
        if "plays" in self.shown:
            self.list.refresh()

    # -- the list's own loop -------------------------------------------------------
    def _start_poll(self):
        if self._poll_job is None and self.tree.winfo_exists():
            self._poll_job = self.tree.after(POLL_MS, self._poll)

    def _poll(self):
        """Take in what the list's thread has finished, and rows of a database list that have
        arrived; look again while anything is still on its way."""
        self._poll_job = None
        while True:
            try:
                kind, number, value, error = self._worker.results.get_nowait()
            except queue.Empty:
                break
            if not self._worker.wanted(kind, number) or self._asked.get(kind) != number:
                continue
            self._asked[kind] = None
            if kind == "search" and error is None:
                self._take_search(*value)
            elif kind == "plays" and error is None:
                self._take_plays(value)
        tracks = self._tracks
        serial = getattr(tracks, "serial", None)
        if serial is not None and serial != self._serial_seen:
            self._serial_seen = serial
            self.list.refresh()
        waiting = getattr(tracks, "waiting", None)
        if any(n is not None for n in self._asked.values()) or (waiting is not None and waiting()):
            self._start_poll()

    # -- search ----------------------------------------------------------------
    @property
    def searching(self) -> bool:
        return bool(self.query.get().strip())

    def focus_search(self):
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def clear_search(self):
        self.query.set("")
        self.tree.focus_set()

    def _query_changed(self):
        if self._search_job:
            self.tree.after_cancel(self._search_job)
        self._search_job = self.tree.after(SEARCH_DELAY_MS, self._search_now)  # wait for a pause in typing

    def _search_now(self):
        """The pause in typing came: search (a big list on the list's own thread)."""
        self._search_job = None
        words = self._typed()
        if words and len(self._tracks) >= SEARCH_THREAD_AT:
            self._ask_search(words)
        else:
            self._render()

    def _ask_search(self, words: list[str]):
        serial = self._list_serial
        self._asked["search"] = self._worker.ask("search", lambda *args: (serial, words, *_search(*args)),
                                                 self._tracks, self._haystacks, words, self._last_search)
        self._start_poll()

    def _take_search(self, serial: int, words: list[str], haystacks, found):
        if serial != self._list_serial or words != self._typed():
            return   # (for a list since replaced, or words since changed: a newer search follows)
        if haystacks is not None:
            self._haystacks = haystacks
        self._last_search = (words, found)
        self._list(words, found)

    def _flush_search(self):
        """Words typed but not yet searched for (the pause hasn't come, or the search is still on
        its way): search for them now, so a quick Enter or Down acts on what was typed, not on the
        list from before."""
        if self._search_job or self._asked["search"] is not None or self._typed() != self._words:
            self._render()

    def _focus_results(self):
        self._flush_search()
        first = self._first_song()
        if first is not None:
            self.tree.focus_set()
            self.list.select(self._row_of(first))

    def _play_first_result(self):
        self._flush_search()
        first = self._first_song()
        if first is not None:
            self.on_play(first)
        elif "library" in self._listing.tail:   # no matches: Enter searches the whole library
            self.search_library()

    def search_library(self):
        """Hand the words typed to the whole-library search (the "Search the whole library" row)."""
        if self.on_search_library is not None:
            self.on_search_library(self.query.get().strip())

    def _released(self, event):
        if self.tree.identify_row(event.y) == "library" and "library" in self._listing.tail:
            self.search_library()

    def refresh_titles(self):
        """Song rows' titles again (a favourite's ♥ came or went): only the rows on show are drawn."""
        self.list.refresh()

    # -- right-click and editing -------------------------------------------------
    def rows_of(self, iid: str) -> list[int]:
        """Track indexes a row stands for: one song, or every listed song of an album header."""
        if iid.isdigit():
            return [int(iid)]
        if iid.startswith("a") and iid[1:].isdigit() and self._header_row_of(int(iid[1:])) is not None:
            return list(self._group_songs(int(iid[1:])))
        return []

    def _carried(self, iid: str) -> list[int]:
        """What a dragged row carries: every song selected when it's one of several selected, else
        its own song (an album header: its album's)."""
        index = self.list.index_of_iid(iid)
        if index is not None and self.list.is_selected(index) and self.list.selected_count() > 1:
            return self.selected_rows()
        return self.rows_of(iid)

    def _context(self, event):
        if self.tree.identify_region(event.x, event.y) == "heading":
            self.on_heading_menu(event)
            return
        iid = self.tree.identify_row(event.y)
        if not iid or iid in ("none", "library"):
            return
        index = self.list.index_of_iid(iid)
        if index is None:
            return
        self.list.sync()
        if not self.list.is_selected(index):   # (a right-click inside the selection keeps it)
            self.list.select(index, see=False)
        rows = Rows(self.selected_rows())
        rows.album = self.list.selected_count() == 1 and not iid.isdigit()
        self.on_menu(rows, event)


    def selected_rows(self) -> list[int]:
        """The tracks the selected rows stand for (songs, and an album header's songs), in list order."""
        self.list.sync()
        listing = self._listing
        entries, count = listing.entries, len(listing.entries)
        runs = self.list.chosen.runs
        if self._found is None and runs == [(0, len(listing))]:
            return list(range(len(self._tracks)))   # (everything: Ctrl+A)
        songs: list[int] = []
        headers = False
        for start, end in runs:
            for row in range(start, min(end, count)):
                found = entries[row]
                if found >= 0:
                    songs.append(found)
                else:
                    headers = True
                    songs.extend(self._group_songs(-1 - found))
        return sorted(set(songs)) if headers else songs

    def selected_track(self) -> int | None:
        """The selected song's index (None: nothing, or an album header, is selected); with several
        selected, the one the keyboard is on."""
        self.list.sync()
        row = self.list.selected
        if row is None or not self.list.is_selected(row) or row >= len(self._listing):
            return None
        found = self._listing.entry(row)
        return found if isinstance(found, int) and found >= 0 else None

    def selected_count(self) -> int:
        self.list.sync()
        return self.list.selected_count()

    def select(self, index: int):
        """Select song `index` alone (nothing, when the search hides it), scrolled into view."""
        row = self._row_of(index)
        if row is not None:
            self.list.select(row)

    def select_tracks(self, indices, cursor: int | None = None):
        """Select these songs (the ones listed), the keyboard on `cursor` (default: the first)."""
        rows = [r for r in (self._row_of(i) for i in indices) if r is not None]
        self.list.select_indices(rows, cursor=self._row_of(cursor) if cursor is not None else None)

    def select_all(self) -> bool:
        """Ctrl+A: every row listed (False when the list is empty)."""
        if not self._found and self._found is not None:
            return False
        return self.list.select_all()

    # -- reordering by dragging ---------------------------------------------------
    def contains(self, x_root: int, y_root: int) -> bool:
        """Is this screen point over the list itself?"""
        tree = self.tree
        return (tree.winfo_rootx() <= x_root < tree.winfo_rootx() + tree.winfo_width()
                and tree.winfo_rooty() <= y_root < tree.winfo_rooty() + tree.winfo_height())

    def _shown_songs(self) -> list[str]:
        """The song rows on show, top to bottom (their ids)."""
        return [iid for iid in self.tree.get_children() if iid.isdigit()]

    def drop_index(self, y_root: int) -> int:
        """Where a dragged song would land: the index of the song the drop line sits above
        (len = after the last). Only meaningful for a flat list (a playlist) with no search."""
        y = y_root - self.tree.winfo_rooty()
        rows = self._shown_songs()
        if not rows:
            return 0
        iid = self.tree.identify_row(y)
        if not iid or not iid.isdigit():
            first = self.tree.bbox(rows[0])
            return int(rows[0]) if first and y < first[1] else len(self._tracks)
        box = self.tree.bbox(iid)
        index = int(iid)
        return index + 1 if box and y > box[1] + box[3] / 2 else index

    def show_drop_line(self, index: int | None):
        """A green line between rows where a dragged song would go (None hides it)."""
        if index is None:
            self._drop_line.place_forget()
            return
        rows = self._shown_songs()
        if not rows:
            self._drop_line.place_forget()
            return
        box = self.tree.bbox(str(index)) if str(index) in rows else ""
        if box:
            y = box[1]
        else:
            last = self.tree.bbox(rows[-1])
            if not last or index != len(self._tracks) or rows[-1] != str(len(self._tracks) - 1):
                self._drop_line.place_forget()
                return
            y = last[1] + last[3]
        self._drop_line.place(x=0, y=max(0, y - 1), relwidth=1.0)
        self._drop_line.lift()

    # -- selection -------------------------------------------------------------
    def clear_current(self):
        self._mark_playing(None)
        self.list.select(None, see=False)

    def show_current(self, index: int):
        """The playing song: marked (♪, green bold), selected and scrolled into view."""
        self._mark_playing(index)
        row = self._row_of(index)
        if row is not None:   # (a song the search hides keeps its mark, unselected)
            self.list.select(row)

    def _mark_playing(self, index: int | None):
        """Move the playing mark (the row keeps it however the selection moves)."""
        if index == self.playing and self.list.playing == self._row_of(index):
            return
        self.playing = index
        self.list.set_playing(self._row_of(index))

    def activate(self) -> bool:
        """Play the selected song (an album header: its album). False when nothing is selected."""
        self.list.sync()
        row = self.list.selected
        if row is None or not self.list.chosen or row >= len(self._listing):
            return False
        found = self._listing.entry(row)
        if found == "none":
            return False
        if found == "library":
            self.search_library()
            return True
        if found < 0:
            songs = self._group_songs(-1 - found)
            if not len(songs):
                return False
            found = songs[0]
        self.on_play(found)
        return True

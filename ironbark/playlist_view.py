"""The track list in the main window, with a search box above it.

One album: a plain numbered list. Several albums (e.g. the whole library): each album
gets a header row above its tracks. A playlist: a flat list in its own order, with artists.
Right-click a song (or an album header) for Play / Add to playlist / Remove, etc. Typing in the search box narrows the list to the
songs whose title, artist, album, album artist, composer, genre, year or file name hold every word
typed (accents and case ignored: searchfold.py); double-clicking a result plays it in place, and the
album carries on from there.

Songs (or an album, by its header) can be dragged: onto a tab to add them there, or up and down
a playlist to reorder it. The view only reports the drag (`on_drag`); the window decides what
it means (see TreeDrag and ui.PlayerWindow._song_drag).

Artist and Album columns can be added (off by default: the window is narrow); right-click the
headings, or a song, to switch them (`set_columns`, saved by the window).

The list's keys (Enter plays, Ctrl+Enter / Ctrl+Shift+Enter queue, Delete removes, Alt+↑ / ↓
move a song) are in the keyboard table (keys.py); the window binds them, using `activate`,
`selected_rows` and `selected_track`.

The look (0.20.0): the row under the pointer lights up, the playing song is marked (♪ in place of
its number, green bold text) even when another row is selected, album header rows read
"Album · Artist" with the quality on the right, the scrollbar is slim and only there when the list
is longer than the window, and the search box has a magnifier and a × to clear it.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from . import controls, popups, searchfold, theme
from .library import Track, group_by_album

SEARCH_DELAY_MS = 150
OPTIONAL_COLUMNS = ("artist", "album")  # columns that can be switched on
DRAG_START = 6  # px at 100%: move this far with the button down and a press becomes a drag
PLAYING_MARK = "♪"   # (0.20.0) in the # column of the playing song


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


def fit_columns(tree: ttk.Treeview, weights: dict[str, float], minimum: int = 40) -> Callable[[], None]:
    """Share a list's width between its stretchy columns (by `weights`) once the fixed ones have
    their room, whenever it's resized or columns are switched on / off. (Tk itself never shrinks
    columns to fit, so extra columns would push the last ones out of sight.) Returns the fitter."""
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
        total = sum(weights[c] for c in stretchy) or 1
        room = max(0, width - fixed - 4)
        for column in stretchy:
            tree.column(column, width=max(minimum, int(room * weights[column] / total)))

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
        state, self._state = self._state, None
        if state is not None and state["moving"]:
            self.tree.configure(cursor="")
            self.on_drag(state["carried"], 0, 0, "cancel")


class PlaylistView:
    def __init__(self, parent: tk.Misc, on_play: Callable[[int], None],
                 on_menu: Callable[[list[int], tk.Event], None] = lambda rows, event: None,
                 on_drag: Callable[[list[int], int, int, str], None] = lambda rows, x, y, phase: None,
                 on_heading_menu: Callable[[tk.Event], None] = lambda event: None):
        self.on_play = on_play
        self.on_heading_menu = on_heading_menu
        self.columns: tuple[str, ...] = ()  # optional columns on show (see set_columns)
        self.on_menu = on_menu
        self.flat = False
        self._album_start: dict[str, int] = {}  # header row id -> first track index
        self._tracks: list[Track] = []
        self._search_job = None
        self.playing: int | None = None         # (0.20.0) the playing song's index, marked in the list
        self.hovered: str | None = None         # the row under the pointer
        self._numbers: dict[int, int] = {}      # each listed song's number in the # column
        # Kept per list (set_tracks), so a pause in typing doesn't redo them over a whole library:
        self._groups: list = []                 # its albums: (folder, first index, end index)
        self._haystacks: list[str] | None = None   # each song's folded search text (made at the first search)
        self._last_search: tuple[list[str], list[int]] | None = None   # words, and the songs they found
        self._made: set[str] = set()            # rows made in the tree, listed or hidden
        self._titles: dict[int, str] = {}       # the title each made song row shows
        self._listed: set[str] = set()          # rows listed now
        self._songs: list[str] = []             # song rows listed now, top to bottom

        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        self.frame = frame

        # (0.20.0) A magnifier inside the box on the left, and a × on the right while there's
        # something typed; (0.22.0) the box itself is controls.SearchBox, shared with the Library.
        self.search = controls.SearchBox(frame, "Search songs, artists, albums   Ctrl+F", on_clear=self.clear_search)
        self.search.pack(fill="x", pady=theme.pad(0, theme.GAP))
        self.query, self.entry = self.search.query, self.search.entry
        self.magnifier, self.clear_button, self.hint = self.search.magnifier, self.search.clear_button, self.search.hint
        self.query.trace_add("write", lambda *_: self._query_changed())
        self.entry.bind("<Down>", lambda e: self._focus_results())
        self.entry.bind("<Return>", lambda e: self._play_first_result())

        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        # height=4: ask for little, then grow into whatever room the window has.
        self.tree = ttk.Treeview(table, columns=("no", "title", "artist", "album", "time"), show="headings",
                                 selectmode="browse", height=4, displaycolumns=("no", "title", "time"))
        self.tree.heading("no", text="#", anchor="e")
        self.tree.heading("title", text="Title", anchor="w")
        self.tree.heading("artist", text="Artist", anchor="w")
        self.tree.heading("album", text="Album", anchor="w")
        self.tree.heading("time", text="Time", anchor="e")
        self.tree.column("no", width=theme.px(38), stretch=False, anchor="e")
        self.tree.column("title", width=theme.px(240), stretch=True, anchor="w")
        self.tree.column("artist", width=theme.px(120), stretch=True, anchor="w")
        self.tree.column("album", width=theme.px(120), stretch=True, anchor="w")
        self.tree.column("time", width=theme.px(52), stretch=False, anchor="e")
        self._fit = fit_columns(self.tree, {"title": 2, "artist": 1, "album": 1}, minimum=theme.px(50))
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self._bold = bold
        self._colour_tags()
        theme.on_change(self._palette_changed)
        # (0.20.0) The slim scrollbar, only there when the list is longer than its window.
        self.scrollbar = controls.AutoScrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        self.scrollbar.auto_pack(side="right", fill="y", padx=(theme.px(theme.GAP_HAIR), 0), before=self.tree)
        # The row under the pointer lights up.
        self.tree.bind("<Motion>", self._hover_row, add="+")
        self.tree.bind("<Leave>", lambda e: self._set_hover(None), add="+")
        self.tree.bind("<Double-Button-1>", lambda e: self.activate())
        self.tree.bind("<Button-3>", self._context)
        for key in ("<App>", "<Shift-F10>"):   # (0.21.0) the keyboard's menu key: the focused row's menu
            self.tree.bind(key, lambda e: self._context(popups.row_event(self.tree)) or "break")
        # Dragging songs: a song carries itself, an album header its album's listed songs.
        self.drag = TreeDrag(self.tree, lambda iid: self.rows_of(iid) or None, on_drag)
        self._drop_line = tk.Frame(self.tree, bg=theme.ACCENT, height=max(2, theme.px(2)))

    def _colour_tags(self):
        # A Treeview gives the tag configured *first* the say when two set the same option (checked
        # on Tk 8.6.12): "hover" comes first so a hovered album / playing row lights up too. Its
        # text turns TEXT: green text on the hover grey would be under WCAG AA's 4.5:1 in Dark.
        self.tree.tag_configure("hover", background=theme.HOVER, foreground=theme.TEXT)
        self.tree.tag_configure("album", background=theme.BG, foreground=theme.ACCENT_TEXT, font=self._bold)
        self.tree.tag_configure("playing", foreground=theme.ACCENT_TEXT, font=self._bold)
        self.tree.tag_configure("note", foreground=theme.MUTED)

    def _palette_changed(self, _palette):
        """Treeview row colours aren't widget options, so theme.use can't swap them itself. (The
        search box looks after its own.)"""
        self._colour_tags()

    # -- hover -------------------------------------------------------------------
    def _hover_row(self, event):
        iid = self.tree.identify_row(event.y)
        self._set_hover(iid if iid and iid != "none" and not self.drag.active else None)

    def _set_hover(self, iid: str | None):
        """Light up the row under the pointer (None: none)."""
        if iid == self.hovered:
            return
        for row, on in ((self.hovered, False), (iid, True)):
            if row and self.tree.exists(row):
                tags = [t for t in self.tree.item(row, "tags") if t != "hover"]
                self.tree.item(row, tags=(["hover"] + tags) if on else tags)
        self.hovered = iid

    # -- contents --------------------------------------------------------------
    def set_columns(self, columns):
        """Which optional columns (OPTIONAL_COLUMNS) to show, e.g. ("artist",)."""
        self.columns = tuple(c for c in OPTIONAL_COLUMNS if c in columns)
        self.tree.configure(displaycolumns=("no", "title", *self.columns, "time"))
        self._fit()
        self._render()

    def set_tracks(self, tracks: list[Track], flat: bool = False):
        """flat: a playlist, shown in its own order without album headers. (The playing mark is
        dropped: the window marks the playing song again if this list has it.)"""
        self._tracks = list(tracks)
        self.playing = None
        self.flat = flat
        self._groups = [(None, 0, len(self._tracks))] if flat else group_by_album(self._tracks)
        self._haystacks = None
        self._last_search = None
        self.hovered = None
        if self._made:   # every row of the last list, hidden ones too
            self.tree.delete(*self._made)
        self._made.clear()
        self._titles.clear()
        self._numbers.clear()
        self._render()

    def _title(self, i: int, searching: bool) -> str:
        """Song i's title cell: with its artist while searching and in a playlist (unless the
        Artist column shows it), and a note when its file has gone."""
        track = self._tracks[i]
        show_artist = (searching or self.flat) and track.artist and "artist" not in self.columns
        title = f"{track.title}  ·  {track.artist}" if show_artist else track.title
        return title + "   (file not found)" if track.missing else title

    def _matching(self, words: list[str]) -> list[int] | None:
        """The songs matching every word, in list order (None: no words, so every song). Each
        song's search text is folded once per list, and while the words only grow (more typing)
        only the songs found last time are looked through again."""
        if not words:
            self._last_search = None
            return None
        if self._haystacks is None:
            self._haystacks = [_haystack(track) for track in self._tracks]
        haystacks, candidates = self._haystacks, range(len(self._tracks))
        last = self._last_search
        if last is not None and all(any(old in new for new in words) for old in last[0]):
            candidates = last[1]   # every song these words find, the last ones found too
        found = [i for i in candidates if all(word in haystacks[i] for word in words)]
        self._last_search = (words, found)
        return found

    def _render(self):
        """List the songs the search finds (all of them with no search) under their album
        headers. A row is made once per list and after that only listed or hidden, the whole
        order set in one Tk call: deleting and re-making every row on each pause in typing froze
        the window for a second or more on a list of a whole library."""
        if self._search_job:   # (this shows the search as it stands: a pending one would only repeat it)
            self.tree.after_cancel(self._search_job)
            self._search_job = None
        self._set_hover(None)
        tree, tracks = self.tree, self._tracks
        words = [_fold(w) for w in self.query.get().split()]
        found = self._matching(words)
        grouped = len(self._groups) > 1
        listed: list[str] = []
        self._songs = []
        self._album_start.clear()
        at = 0   # where the next album's songs start in `found`
        for g, (folder, start, end) in enumerate(self._groups):
            if found is None:
                picked = range(start, end)
            else:
                stop = at
                while stop < len(found) and found[stop] < end:
                    stop += 1
                picked, at = found[at:stop], stop
            if not picked:
                continue
            if grouped:
                header = f"a{g}"
                if header not in self._made:
                    tree.insert("", "end", iid=header, tags=("album",),
                                values=("", album_heading(tracks[start:end], folder), "", "", _quality(tracks[start])))
                    self._made.add(header)
                listed.append(header)
                self._album_start[header] = picked[0] if words else start
            for i in picked:
                iid, title = str(i), self._title(i, bool(words))
                if iid not in self._made:
                    track = tracks[i]
                    number = self._numbers[i] = (i - start + 1) if grouped else (i + 1)
                    playing = i == self.playing
                    tree.insert("", "end", iid=iid, tags=("playing",) if playing else (),
                                values=(PLAYING_MARK if playing else number, title, track.artist, track.album,
                                        _clock(track.duration)))
                    self._made.add(iid)
                elif self._titles[i] != title:
                    tree.set(iid, "title", title)
                self._titles[i] = title
                listed.append(iid)
                self._songs.append(iid)
        if words and not self._songs:
            if "none" not in self._made:
                tree.insert("", "end", iid="none", tags=("note",), values=("", "No matches", "", "", ""))
                self._made.add("none")
            listed.append("none")
        # A row the search hides can't stay selected or focused: Enter would play a song out of sight.
        listing = set(listed)
        hidden = [iid for iid in tree.selection() if iid not in listing]
        if hidden:
            tree.selection_remove(*hidden)
        if tree.focus() and tree.focus() not in listing:
            tree.focus("")
        tree.set_children("", *listed)
        self._listed = listing

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
        self._search_job = None
        self._render()

    def _flush_search(self):
        """Words typed but not yet searched for (the pause hasn't come): search for them now, so a
        quick Enter or Down acts on what was typed, not on the list from before."""
        if self._search_job:
            self._render()

    def _focus_results(self):
        self._flush_search()
        rows = self._songs
        if rows:
            self.tree.focus_set()
            self.tree.selection_set(rows[0])
            self.tree.focus(rows[0])

    def _play_first_result(self):
        self._flush_search()
        rows = self._songs
        if rows:
            self.on_play(int(rows[0]))

    # -- right-click and editing -------------------------------------------------
    def rows_of(self, iid: str) -> list[int]:
        """Track indexes a row stands for: one song, or every listed song of an album header."""
        if iid.isdigit():
            return [int(iid)]
        if iid in self._album_start:
            rows = list(self.tree.get_children())
            after = rows[rows.index(iid) + 1:]
            songs = []
            for row in after:
                if not row.isdigit():
                    break
                songs.append(int(row))
            return songs
        return []

    def _context(self, event):
        if self.tree.identify_region(event.x, event.y) == "heading":
            self.on_heading_menu(event)
            return
        iid = self.tree.identify_row(event.y)
        if not iid or iid == "none":
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.on_menu(self.rows_of(iid), event)

    def selected_rows(self) -> list[int]:
        """The tracks the selected row stands for (a song, or an album header's songs)."""
        selection = self.tree.selection()
        return self.rows_of(selection[0]) if selection else []

    def selected_track(self) -> int | None:
        """The selected song's index (None: nothing, or an album header, is selected)."""
        selection = self.tree.selection()
        return int(selection[0]) if selection and selection[0].isdigit() else None

    def select(self, index: int):
        iid = str(index)
        if iid in self._listed:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)

    # -- reordering by dragging ---------------------------------------------------
    def contains(self, x_root: int, y_root: int) -> bool:
        """Is this screen point over the list itself?"""
        tree = self.tree
        return (tree.winfo_rootx() <= x_root < tree.winfo_rootx() + tree.winfo_width()
                and tree.winfo_rooty() <= y_root < tree.winfo_rooty() + tree.winfo_height())

    def drop_index(self, y_root: int) -> int:
        """Where a dragged song would land: the index of the song the drop line sits above
        (len = after the last). Only meaningful for a flat list (a playlist) with no search."""
        y = y_root - self.tree.winfo_rooty()
        rows = self._songs
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
        rows = self._songs
        if not rows:
            self._drop_line.place_forget()
            return
        if str(index) in self._listed and self.tree.bbox(str(index)):
            y = self.tree.bbox(str(index))[1]
        else:
            last = self.tree.bbox(rows[-1])
            if not last:
                self._drop_line.place_forget()
                return
            y = last[1] + last[3]
        self._drop_line.place(x=0, y=max(0, y - 1), relwidth=1.0)
        self._drop_line.lift()

    # -- selection -------------------------------------------------------------
    def clear_current(self):
        self._mark_playing(None)
        self.tree.selection_remove(*self.tree.selection())

    def show_current(self, index: int):
        """The playing song: marked (♪, green bold), selected and scrolled into view."""
        self._mark_playing(index)
        iid = str(index)
        if iid in self._listed:   # (a song the search hides keeps its mark, unselected)
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def _mark_playing(self, index: int | None):
        """Move the playing mark (the row keeps it however the selection moves)."""
        if index == self.playing:
            return
        old, self.playing = self.playing, index
        for row, on in ((old, False), (index, True)):
            if row is None or not self.tree.exists(str(row)):
                continue
            iid = str(row)
            tags = [t for t in self.tree.item(iid, "tags") if t != "playing"]
            self.tree.item(iid, tags=tags + ["playing"] if on else tags)
            self.tree.set(iid, "no", PLAYING_MARK if on else self._numbers.get(row, row + 1))

    def activate(self) -> bool:
        """Play the selected song (an album header: its album). False when nothing is selected."""
        selection = self.tree.selection()
        if not selection or selection[0] == "none":
            return False
        iid = selection[0]
        self.on_play(self._album_start[iid] if iid in self._album_start else int(iid))
        return True

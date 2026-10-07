"""The Library window: every album in the library folders, from the library database
(library_index.py over library_db.py), so it opens at once and only changed files are read again.
The album list is fetched on the library's query thread (indexer.Queries), never on Tk's: the
window's own `after` loop picks it up, with the indexer's progress and news of changed albums.

Browse by Albums · Artists · Genres · Years · Composers · Recently added (the grouping rules are in
browse.py); click a column heading to sort by it (again to reverse). Search matches album, artist,
year, genre and composer. Double-click an album to open it in a tab (it starts playing only if
nothing else is); right-click for Play next / Add to queue. An album can also be dragged onto the
player: onto a tab to add it there, anywhere else on the player to open it in a new tab.

New music (0.17.0, watcher.py): an album that arrived since watching began carries a green dot
until it's opened, the status line says how many ("3 new albums"), and Recently added lists them
first (newest first).

(0.22.0) The main window's finish: its search box (magnifier, hint, ×), flat pill buttons, the views
as a row of words with a green bar under the one on show, the slim scrollbar that shows only when
needed, and the row under the pointer lit up as in the track list.

Keys (the keyboard table, keys.py; `key_handlers` is what they do here): Enter opens the album,
Ctrl+Enter / Ctrl+Shift+Enter put it in Up next, Ctrl+Tab / Ctrl+Shift+Tab change the view, F5
rescans, Ctrl+F goes to the search box."""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from tkinter import ttk
from typing import Callable

from . import browse, controls, indexer, keys, library_db, library_index, placement, popups, theme
from .library import Album
from .playlist_view import TreeDrag, _fold, fit_columns

NEW_DOT = "●  "   # in front of a new album's name (until it's opened)
SEARCH_DELAY_MS = 150   # the list follows the search box after a pause in typing, as the track list does
TICK_MS = 100           # how often the window picks up the album list and the indexer's news
HEADINGS = {"album": "Album", "artist": "Artist", "year": "Year", "quality": "Quality", "tracks": "Tracks",
            "added": "Added"}


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


def added_text(seconds: float) -> str:
    """When an album arrived, Australian style: 04/10/2026."""
    return time.strftime("%d/%m/%Y", time.localtime(seconds)) if seconds > 0 else ""


class LibraryWindow:
    _sorts: dict[str, tuple[str, bool]] = {}      # per view: (column, reversed), kept for the session
    _closed: dict[str, set[str]] = {}             # per view: groups folded shut

    def __init__(self, parent: tk.Misc, root_folder: Path | list[Path], current: Path | None,
                 on_choose: Callable[[Path], None], on_root_changed: Callable[[list[Path]], None],
                 on_load_all: Callable[[Path, str], None], geometry: str = "",
                 on_moved: Callable[[str], None] = lambda geometry: None,
                 on_add: Callable[[Path, Callable[[str], None]], None] | None = None,
                 add_target: Callable[[], str | None] = lambda: None,
                 on_drag: Callable[[Path, int, int, str], None] | None = None,
                 on_queue: Callable[[Path, bool], None] | None = None,
                 view: str = "albums", on_view: Callable[[str], None] = lambda view: None,
                 on_edit_tags: Callable[[Path], None] | None = None,
                 can_edit_tags: Callable[[], bool] = lambda: False,
                 new_albums: Callable[[], list[Path]] = lambda: []):
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
        self.on_edit_tags = on_edit_tags    # (0.16.0) Experimental tag editing: offered only while
        self.can_edit_tags = can_edit_tags  # its switch is on (read fresh at every right-click)
        self.add_target = add_target  # the title of the player's tab on show, read fresh each time
        self.new_albums = new_albums  # the new albums' folders (0.17.0), read fresh at every fill
        self._new: set[Path] = set()
        self.on_moved = on_moved
        self.view = view if view in browse.VIEWS else "albums"
        self.albums: list[Album] = []
        self._shown: list[Album] = []          # albums listed now (each once)
        self._rows: dict[str, Album] = {}      # tree row id -> its album
        self._groups: dict[str, str] = {}      # tree row id -> group name
        self.index = library_index.shared(self.roots)
        # A refresh running in the background: a dict until the album list it brought is on show.
        self._refresh: dict | None = None
        self._fill_job = None                  # a search waiting for a pause in typing
        self._folds: dict[str, str] = {}       # text -> folded, for searching (cleared with each new album list)
        self._queries = indexer.Queries(self.index.db)   # the album list is read here, off Tk's thread
        self._events: queue.SimpleQueue = self.index.listen()
        self._asked: int | None = None         # the album list asked for and not yet here
        self._again = False                    # albums changed while it was on its way: ask again after
        self._loading = True                   # no album list yet
        self._problem = ""                     # the album list couldn't be read
        self._tick_job = None
        self._status_text = ""

        self.win = tk.Toplevel(parent, bg=theme.BG)
        self.win.title("Library")
        self.win.geometry(f"{theme.px(680)}x{theme.px(540)}")
        self.win.minsize(theme.px(380), theme.px(300))
        placement.restore(self.win, geometry)
        self.win.bind("<Configure>", self._moved)
        theme.dark_title_bar(self.win)
        self.win.bind("<Escape>", lambda e: self.close())

        frame = ttk.Frame(self.win, padding=theme.px(theme.GAP_WIDE))
        frame.pack(fill="both", expand=True)

        top = ttk.Frame(frame)
        top.pack(fill="x")
        # (0.22.0) The track list's search box (controls.SearchBox); Esc empties it, or closes the
        # window when it's already empty.
        self.search = controls.SearchBox(top, "Search albums, artists, years, genres", on_clear=self._clear_search)
        self.search_var = self.search.query
        self._search_trace = self.search_var.trace_add("write", lambda *_: self._search_changed())
        controls.FlatButton(top, "Rescan", self.rescan, label="Look for added or changed music now").pack(
            side="right", padx=theme.pad(theme.GAP, 0))
        controls.FlatButton(top, "Add folder…", self._add_folder,
                            label="Add another folder of music to the Library (Settings → Library lists them)").pack(
            side="right", padx=theme.pad(theme.GAP_MEDIUM, 0))
        self.search.pack(side="left", fill="x", expand=True)
        search = self.search.entry
        search.bind("<Down>", lambda e: self._focus_list())
        search.bind("<Return>", lambda e: self._choose_first())

        # The browse views: a row of words, the one on show in green with a short green pill under it.
        self.view_var = tk.StringVar(value=self.view)
        self.views = controls.ChoiceRow(frame, [(key, browse.VIEW_TITLES[key]) for key in browse.VIEWS],
                                        self.view_var, command=lambda: self.set_view(self.view_var.get()))
        self.views.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
        self._view_labels: dict[str, tk.Label] = self.views.labels
        self._paint_views()

        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True, pady=theme.pad(theme.GAP, 0))
        self.tree = ttk.Treeview(table, columns=browse.COLUMNS, show="headings", selectmode="browse")
        widths = {"album": 220, "artist": 140, "year": 48, "quality": 64, "tracks": 60, "added": 96}
        for column in browse.COLUMNS:
            anchor = "e" if column in ("tracks", "year") else "w"
            self.tree.heading(column, text=HEADINGS[column], anchor=anchor,
                              command=lambda c=column: self.sort_by(c))
            self.tree.column(column, anchor=anchor, width=theme.px(widths[column]),
                             stretch=column in ("album", "artist"))
        self.tree.column("#0", width=theme.px(22), stretch=False)  # the fold-open arrow of a group
        self._fit = fit_columns(self.tree, {"album": 3, "artist": 2}, minimum=theme.px(60))
        self._colour_tags()
        theme.on_change(self._palette_changed)
        # (0.22.0) The slim scrollbar, only there when the list is longer than its window — in the
        # window's right margin, so the list's words line up with the search box.
        self.scrollbar = controls.AutoScrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        self.scrollbar.in_margin(table)
        # The row under the pointer lights up (as in the track list).
        self.hovered: str | None = None
        self.drag: TreeDrag | None = None
        self.tree.bind("<Motion>", self._hover_row, add="+")
        self.tree.bind("<Leave>", lambda e: self._set_hover(None), add="+")
        self.tree.bind("<Double-Button-1>", self._double_click)
        self.tree.bind("<Button-3>", self._context)
        for key in ("<App>", "<Shift-F10>"):   # (0.21.0) the keyboard's menu key: the focused album's menu
            self.tree.bind(key, lambda e: self._context(popups.row_event(self.tree)) or "break")
        self.tree.bind("<<TreeviewOpen>>", lambda e: self._folded(True))
        self.tree.bind("<<TreeviewClose>>", lambda e: self._folded(False))
        if on_drag is not None:  # drag an album onto the player (ui.PlayerWindow._album_drag)
            self.drag = TreeDrag(self.tree, lambda iid: self._rows[iid].path if iid in self._rows else None,
                                 lambda album, x, y, phase: on_drag(album, x, y, phase))

        # The selected album: open it in a tab of its own (the same as double-clicking), or add
        # it to the bottom of the tab on show in the player. Adding leaves this window open,
        # so you can add a few in a row.
        # (Both rows below are packed from the bottom, ahead of the album list, so a short
        # window squeezes the list rather than pushing the buttons out of sight.)
        selected = ttk.Frame(frame)
        self.open_button = controls.FlatButton(selected, "Open in new tab", self._choose_selected, kind="primary",
                                               state="disabled")
        self.open_button.pack(side="left")
        self.add_button = controls.FlatButton(selected, "Add to tab", self._add_selected, state="disabled")
        if on_add is not None:
            self.add_button.pack(side="left", padx=theme.pad(theme.GAP, 0))
            self.win.bind("<FocusIn>", lambda e: self._paint_buttons(), add="+")  # tabs may have changed
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._paint_buttons(), add="+")

        bottom = ttk.Frame(frame)
        bottom.pack(side="bottom", fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0), before=table)
        selected.pack(side="bottom", fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0), before=table)
        self.status = ttk.Label(bottom, text="", style="Muted.TLabel", anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.load_all_button = controls.FlatButton(bottom, "Load all into playlist", self._load_all)
        self.load_all_button.pack(side="right")

        self.win.bind("<Destroy>", self._destroyed, add="+")
        search.focus_set()
        self._load()
        self._tick_job = self.win.after(TICK_MS, self._tick)

    def _destroyed(self, event):
        """Closed (Esc, a choice, or the window's ×): let go of what would keep this window alive or
        must be freed on Tk's thread. The search box's variable carries this window's search in a
        trace, which Tk would hold for good; the window's own Tk variable and font are dropped
        here, not left to Python's cycle collector, which may run on any thread (a Tk object
        freed there fails)."""
        if event.widget is not self.win:
            return
        for job in (self._fill_job, self._tick_job):
            if job is not None:
                self.win.after_cancel(job)
        self._fill_job = self._tick_job = None
        self.index.unlisten(self._events)
        self._queries.stop()
        if self.search_var is not None:
            try:
                self.search_var.trace_remove("write", self._search_trace)
            except tk.TclError:
                pass
        self.search_var = self.view_var = None
        self.albums, self._shown = [], []
        self._rows.clear()
        self._folds.clear()

    # -- the index -------------------------------------------------------------------------------
    def _load(self):
        """Show what the library database already knows (asked for on the query thread: it's on
        show a moment later); bring it up to date in the background (only changed files are read).
        The first time ever, every file is read once."""
        self.index = library_index.shared(self.roots)
        self._ask_albums()
        self.win.title(f"Library — {self._place()}")
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
        self._asked = self._queries.ask("albums", self.index.albums)
        self._again = False
        return self._asked

    def _want_albums(self):
        """The albums changed: ask for them again (once the list on its way has arrived)."""
        if self._asked is None:
            self._ask_albums()
        else:
            self._again = True

    def _tick(self):
        """Every TICK_MS: take in the album list and what the indexer says. Tk is only touched
        when something came in."""
        self._tick_job = None
        if not self.win.winfo_exists():
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
        for answer in self._queries.drain():
            if answer.kind != "albums" or answer.ask != self._asked:
                continue   # one replaced by a newer ask
            news = True
            self._asked = None
            self._loading = False
            if answer.error is None:
                self._problem = ""
                self._set_albums(answer.value)
                self._fill()
            elif library_db.is_damage(answer.error):   # (noted by the query thread: put right at the next start)
                self._problem = "The library database is damaged: it will be repaired when the player next starts"
            else:
                self._problem = f"Couldn't read the library: {answer.error}"
            job = self._refresh
            if job is not None and job.get("final") == answer.ask:
                self._refresh = None   # the refresh's albums are on show: it's finished
            if self._again:
                self._ask_albums()
        job = self._refresh
        if job is not None and job.get("done") and "final" not in job:
            job["final"] = self._ask_albums()
        if news or job is not None:
            self._paint_status()
        self._tick_job = self.win.after(TICK_MS, self._tick)

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
        search: the album list is asked for afresh."""
        if self.win.winfo_exists() and self._refresh is None:
            self._want_albums()

    def refill(self):
        """List the albums again as they are (e.g. one is no longer new), without asking for them."""
        if self.win.winfo_exists():
            self._fill()

    def _set_albums(self, albums: list[Album]):
        self.albums = albums
        self._folds.clear()   # (the search's folded texts were for the albums before)

    def _fold_text(self, text: str) -> str:
        """`_fold`, remembered: a search pause would otherwise fold every album's text again."""
        folded = self._folds.get(text)
        if folded is None:
            folded = self._folds[text] = _fold(text)
        return folded

    # -- views and sorting ---------------------------------------------------------------------------
    def set_view(self, view: str):
        if view == self.view or view not in browse.VIEWS:
            self.view_var.set(self.view)   # (a word clicked for a view that doesn't exist: no change)
            return
        self.view = view
        self.view_var.set(view)
        self.on_view(view)
        self._paint_views()
        self._fill()

    def _colour_tags(self):
        # "hover" is configured first, so it wins over the others (when two ttk.Treeview tags set the
        # same option, the first-configured wins); its text turns TEXT, as green text on the hover
        # grey would fall under AA in Dark.
        self.tree.tag_configure("hover", background=theme.HOVER, foreground=theme.TEXT)
        self.tree.tag_configure("group", foreground=theme.ACCENT_TEXT, font=theme.font(theme.SIZE_BODY, "semibold"))
        self.tree.tag_configure("new", foreground=theme.ACCENT_TEXT)   # a new album: green until opened

    def _palette_changed(self, _palette):
        """Treeview row colours and the view words keep their colours: redo them."""
        self._colour_tags()
        self._paint_views()

    def _paint_views(self):
        new = bool(self._new)   # a green dot by Recently added: there's something new to see there
        self.views.marks = {"recent"} if new else set()
        self.views.set_text("recent", browse.VIEW_TITLES["recent"] + (" ●" if new else ""))
        self.views.paint()

    # -- hover -------------------------------------------------------------------------------
    def _hover_row(self, event):
        iid = self.tree.identify_row(event.y)
        dragging = self.drag is not None and self.drag.active
        self._set_hover(iid if iid and iid != "none" and not dragging else None)

    def _set_hover(self, iid: str | None):
        """Light up the row under the pointer (None: none)."""
        if iid == self.hovered:
            return
        for row, on in ((self.hovered, False), (iid, True)):
            if row and self.tree.exists(row):
                tags = [t for t in self.tree.item(row, "tags") if t != "hover"]
                self.tree.item(row, tags=(["hover"] + tags) if on else tags)
        self.hovered = iid

    def _search_changed(self):
        if self._fill_job is not None:
            self.win.after_cancel(self._fill_job)
        self._fill_job = self.win.after(SEARCH_DELAY_MS, self._search_now)   # wait for a pause in typing

    def _search_now(self):
        self._fill_job = None
        self._fill()

    def _flush_search(self):
        """Words typed but not yet searched for: search now, so a quick Enter or Down acts on them."""
        if self._fill_job is not None:
            self._fill()

    def _clear_search(self):
        """The search box's × or Esc: empty it (Esc in an empty box closes the window, as before)."""
        if self.search_var.get():
            self.search_var.set("")
            return "break"
        return None

    @property
    def sort(self) -> tuple[str, bool]:
        return self._sorts.get(self.view, browse.DEFAULT_SORT.get(self.view, ("album", False)))

    def sort_by(self, column: str):
        """Click a heading: sort by it; click it again to reverse."""
        current, reverse = self.sort
        self._sorts[self.view] = (column, not reverse if column == current else column == "added")
        self._fill()

    def _paint_headings(self):
        column, reverse = self.sort
        for key, text in HEADINGS.items():
            arrow = (" ▼" if reverse else " ▲") if key == column else ""
            self.tree.heading(key, text=text + arrow)

    # -- list ----------------------------------------------------------------
    def _values(self, album: Album) -> tuple:
        name = NEW_DOT + album.name if album.path in self._new else album.name
        return (name, album.artist, album.year or "", album.quality, album.track_count, added_text(album.added))

    def _fill(self):
        if self._fill_job is not None:   # (this shows the search as it stands: a pending one would only repeat it)
            self.win.after_cancel(self._fill_job)
            self._fill_job = None
        words = [_fold(w) for w in self.search_var.get().split()]
        column, reverse = self.sort
        self._new = set(self.new_albums())
        self._paint_views()
        self.tree.delete(*self.tree.get_children())
        self.hovered = None
        self._rows.clear()
        self._groups.clear()
        self._paint_headings()
        grouped = self.view not in ("albums", "recent")
        self.tree.configure(show="tree headings" if grouped else "headings")
        self._fit()
        shown: dict[Path, Album] = {}
        closed = self._closed.setdefault(self.view, set())
        for g, group in enumerate(browse.group_albums(self.albums, self.view)):
            if words and grouped and all(w in self._fold_text(group.name) for w in words):
                albums = group.albums  # the artist / genre / year itself matches: all of it
            else:
                albums = [a for a in group.albums if not words or browse.matches(a, words, self._fold_text)]
            if not albums:
                continue
            albums = browse.sort_albums(albums, column, reverse)
            parent = ""
            if grouped:
                parent = f"g{g}"
                count = len(albums)
                self.tree.insert("", "end", iid=parent, tags=("group",), open=bool(words) or group.name not in closed,
                                 values=(group.name, f"{count} album{'' if count == 1 else 's'}", "", "",
                                         sum(a.track_count for a in albums), ""))
                self._groups[parent] = group.name
            for n, album in enumerate(albums):
                iid = f"{parent}a{n}"
                self.tree.insert(parent, "end", iid=iid, values=self._values(album),
                                 tags=("new",) if album.path in self._new else ())
                self._rows[iid] = album
                shown.setdefault(album.path, album)
        self._shown = list(shown.values())
        for iid, album in self._rows.items():
            if album.path == self.current:
                self.tree.selection_set(iid)
                self.tree.see(iid)
                break
        count = len(self._shown)
        self.load_all_button.configure(
            text=f"Load {'these' if words else 'all'} {count} album{'' if count == 1 else 's'} into playlist",
            state="normal" if count else "disabled")
        self._paint_status()
        self._paint_buttons()

    def _paint_status(self):
        if not self.win.winfo_exists():
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
            new = len(self._new)
            if new:  # first, so a long folder name can't push it out of sight
                text = f"{new} new album{'' if new == 1 else 's'} · {text}"
        if text != self._status_text:   # (painted often while a refresh runs: Tk only when it changed)
            self._say(text)

    def _say(self, text: str):
        self._status_text = text
        self.status.configure(text=text)

    def _folded(self, opened: bool):
        iid = self.tree.focus()
        name = self._groups.get(iid)
        if name is not None:
            closed = self._closed.setdefault(self.view, set())
            (closed.discard if opened else closed.add)(name)

    def _selected_album(self) -> Album | None:
        selection = self.tree.selection()
        return self._rows.get(selection[0]) if selection else None

    def _paint_buttons(self):
        if not self.win.winfo_exists():
            return
        state = "normal" if self._selected_album() is not None else "disabled"
        self.open_button.configure(state=state)
        target = self.add_target()
        if target is None:
            self.add_button.configure(text="Add to tab", state="disabled")  # no tab open to add to
        else:
            short = target if len(target) <= 28 else target[:27] + "…"
            self.add_button.configure(text=f"Add to “{short}”", state=state)

    def _add_selected(self):
        album = self._selected_album()
        if album is not None and self.on_add is not None:
            self._say(f"Adding “{album.name}”…")
            self.on_add(album.path, self._added)

    def _added(self, message: str):
        if self.win.winfo_exists():
            self._say(message)
            self._paint_buttons()

    def _queue(self, album: Album, front: bool):
        if self.on_queue is not None:
            self._say(f"Adding “{album.name}” to Up next…")
            self.on_queue(album.path, front)

    def _context(self, event):
        """Right-click an album: open it, add it to the tab on show, or line it up in Up next."""
        iid = self.tree.identify_row(event.y)
        album = self._rows.get(iid)
        if album is None:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        menu = popups.PopupMenu(self.win)   # (0.21.0) in the app's colours, arrow keys work
        menu.add("Open in new tab", lambda: self._choose(album), keys.hint("library_open"))
        target = self.add_target()
        if self.on_add is not None and target is not None:
            short = target if len(target) <= 28 else target[:27] + "…"
            menu.add(f"Add to “{short}”", self._add_selected)
        if self.on_queue is not None:
            menu.add_separator()
            menu.add("Play next", lambda: self._queue(album, True), keys.hint("library_play_next"))
            menu.add("Add to queue", lambda: self._queue(album, False), keys.hint("library_queue"))
        if self.on_edit_tags is not None and self.can_edit_tags():
            menu.add_separator()
            menu.add("Edit tags…", lambda: self.on_edit_tags(album.path))
        menu.popup(event.x_root, event.y_root)

    def _load_all(self):
        """Every album shown (all of them, or just the search results), in this order."""
        self.on_load_all(self.root_folder, self.search_var.get().strip())
        self.close()

    def _focus_list(self):
        self._flush_search()
        rows = list(self._rows)
        if rows:
            self.tree.focus_set()
            if not self.tree.selection():
                self.tree.selection_set(rows[0])
            self.tree.focus(self.tree.selection()[0])

    def _double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid in self._rows:
            self._choose(self._rows[iid])

    def _choose_selected(self) -> bool:
        """Open the selected album. False on a group row (or none): Enter then goes on to Tk's own
        Treeview binding, which folds the group open or shut (as a double-click does)."""
        album = self._selected_album()
        if album is None:
            return False
        self._choose(album)
        return True

    # -- the keyboard ------------------------------------------------------------------------------
    def key_handlers(self) -> dict:
        """What the keyboard table's Library keys do here (keys.Keys.attach)."""
        return {"library_search": lambda: self.focus_search(), "library_open": lambda: self._choose_selected(),
                "library_queue": lambda: self.queue_selected(front=False),
                "library_play_next": lambda: self.queue_selected(front=True),
                "library_view": lambda key: self.cycle_view(-1 if "Shift" in key else 1),
                "library_rescan": lambda: self.rescan()}

    def focus_search(self):
        self.search.entry.focus_set()
        self.search.entry.select_range(0, "end")

    def queue_selected(self, front: bool):
        """Ctrl+Enter / Ctrl+Shift+Enter: the selected album into Up next (at the end / next)."""
        album = self._selected_album()
        if album is None:
            return False
        self._queue(album, front)
        return True

    def cycle_view(self, step: int):
        """Ctrl+Tab / Ctrl+Shift+Tab: the next (or previous) view, round the ends."""
        views = list(browse.VIEWS)
        self.set_view(views[(views.index(self.view) + step) % len(views)])

    def lift(self):
        """Ctrl+L with the Library already open: bring it to the front, search and all as they were."""
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _choose_first(self):
        self._flush_search()
        if self._rows:
            self._choose(next(iter(self._rows.values())))

    def _choose(self, album: Album):
        self.on_choose(album.path)
        self.close()

    def _add_folder(self):
        folder = filedialog.askdirectory(parent=self.win, title="Add a folder of music to the Library",
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
        self._load()

    def _moved(self, event):
        if event.widget is self.win:
            geometry = placement.current(self.win)
            if geometry:
                self.on_moved(geometry)

    def close(self):
        self.win.destroy()

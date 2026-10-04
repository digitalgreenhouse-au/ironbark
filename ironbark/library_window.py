"""The Library window: every album under a music folder, from the saved library index
(library_index.py), so it opens at once and only changed files are read again.

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
needed, and the row under the pointer lit up as in the track list."""

from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from . import browse, controls, library_index, placement, popups, theme
from .library import Album
from .playlist_view import TreeDrag, _fold, fit_columns

NEW_DOT = "●  "   # in front of a new album's name (until it's opened)
HEADINGS = {"album": "Album", "artist": "Artist", "year": "Year", "quality": "Quality", "tracks": "Tracks",
            "added": "Added"}


def added_text(seconds: float) -> str:
    """When an album arrived, Australian style: 04/10/2026."""
    return time.strftime("%d/%m/%Y", time.localtime(seconds)) if seconds > 0 else ""


class LibraryWindow:
    _sorts: dict[str, tuple[str, bool]] = {}      # per view: (column, reversed), kept for the session
    _closed: dict[str, set[str]] = {}             # per view: groups folded shut

    def __init__(self, parent: tk.Misc, root_folder: Path, current: Path | None,
                 on_choose: Callable[[Path], None], on_root_changed: Callable[[Path], None],
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
        self.root_folder = root_folder
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
        self.index = library_index.shared(root_folder)
        self._refresh: dict | None = None      # a refresh running in the background

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
        self.search_var.trace_add("write", lambda *_: self._fill())
        controls.FlatButton(top, "Rescan", self.rescan, label="Look for added or changed music now").pack(
            side="right", padx=theme.pad(theme.GAP, 0))
        controls.FlatButton(top, "Change folder…", self._change_root,
                            label="Choose the folder your music is in").pack(side="right", padx=theme.pad(theme.GAP_MEDIUM, 0))
        self.search.pack(side="left", fill="x", expand=True)
        search = self.search.entry
        search.bind("<Down>", lambda e: self._focus_list())
        search.bind("<Return>", lambda e: self._choose_first())

        # The browse views: a row of words, the one on show in green with a green bar under it.
        self.view_var = tk.StringVar(value=self.view)
        self.views = controls.ChoiceRow(frame, [(key, browse.VIEW_TITLES[key]) for key in browse.VIEWS],
                                        self.view_var, command=lambda: self.set_view(self.view_var.get()))
        self.views.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
        self._view_labels: dict[str, tk.Label] = self.views.labels
        self._paint_views()

        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True, pady=theme.pad(theme.GAP, 0))
        self.tree = ttk.Treeview(table, columns=browse.COLUMNS, show="headings", selectmode="browse")
        widths = {"album": 220, "artist": 140, "year": 48, "quality": 64, "tracks": 52, "added": 82}
        for column in browse.COLUMNS:
            anchor = "e" if column in ("tracks", "year") else "w"
            self.tree.heading(column, text=HEADINGS[column], anchor=anchor,
                              command=lambda c=column: self.sort_by(c))
            self.tree.column(column, anchor=anchor, width=theme.px(widths[column]),
                             stretch=column in ("album", "artist"))
        self.tree.column("#0", width=theme.px(22), stretch=False)  # the fold-open arrow of a group
        self._fit = fit_columns(self.tree, {"album": 3, "artist": 2}, minimum=theme.px(60))
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self._bold = bold
        self._colour_tags()
        theme.on_change(self._palette_changed)
        # (0.22.0) The slim scrollbar, only there when the list is longer than its window.
        self.scrollbar = controls.AutoScrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        self.scrollbar.auto_pack(side="right", fill="y", padx=(theme.px(theme.GAP_HAIR), 0), before=self.tree)
        # The row under the pointer lights up (as in the track list).
        self.hovered: str | None = None
        self.drag: TreeDrag | None = None
        self.tree.bind("<Motion>", self._hover_row, add="+")
        self.tree.bind("<Leave>", lambda e: self._set_hover(None), add="+")
        self.tree.bind("<Double-Button-1>", self._double_click)
        self.tree.bind("<Return>", lambda e: self._choose_selected())
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

        search.focus_set()
        self._load()

    # -- the index -------------------------------------------------------------------------------
    def _load(self):
        """Show what the index already knows at once; bring it up to date in the background
        (only changed files are read). The first time ever, every file is read once."""
        self.index = library_index.shared(self.root_folder)
        self.albums = self.index.albums() if self.index.entries else []
        self.win.title(f"Library — {self.root_folder.name or self.root_folder}")
        self._fill()
        if not self.index.refreshed:  # not yet checked this session
            self._start_refresh()

    def _start_refresh(self):
        if self._refresh is not None:
            return
        job: dict = {"folder": self.root_folder, "index": self.index}
        self._refresh = job

        def work():
            try:
                job["diff"] = job["index"].refresh()
            except Exception as error:  # noqa: BLE001 — shown, never fatal: the index is only a speed-up
                job["error"] = str(error)
            job["done"] = True

        threading.Thread(target=work, name="library-index", daemon=True).start()
        self.win.after(100, self._wait_for_refresh, job)
        self._paint_status()

    def _wait_for_refresh(self, job: dict):
        if not self.win.winfo_exists() or job is not self._refresh:
            return
        if not job.get("done"):
            self._paint_status()
            self.win.after(150, self._wait_for_refresh, job)
            return
        self._refresh = None
        if job["folder"] == self.root_folder:
            self.albums = self.index.albums()
            self._fill()

    def rescan(self):
        self._start_refresh()

    def reload(self):
        """Show the index again (after a tag edit updated it), keeping the view, sort and search."""
        if self.win.winfo_exists() and self._refresh is None:
            self.albums = self.index.albums()
            self._fill()

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
        self.tree.tag_configure("hover", background=theme.SURFACE_HI, foreground=theme.TEXT)
        self.tree.tag_configure("group", foreground=theme.ACCENT_TEXT, font=self._bold)
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
            if words and grouped and all(w in _fold(group.name) for w in words):
                albums = group.albums  # the artist / genre / year itself matches: all of it
            else:
                albums = [a for a in group.albums if not words or browse.matches(a, words, _fold)]
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
        if job is not None and not total:
            done, of = self.index.progress
            text = (f"Building the library index (first time only)… {done} of {of} files" if of
                    else f"Looking through {self.root_folder}…")
        elif not total:
            text = f"No music found in {self.root_folder}"
        else:
            words = self.search_var.get().strip()
            text = f"{len(self._shown)} of {total} albums" if words else f"{total} albums in {self.root_folder}"
            if job is not None:
                done, of = self.index.progress
                text += f" · checking for changes… {done} of {of}" if of else " · checking for changes…"
            elif self.index.last_diff is not None and not self.index.last_diff.nothing:
                text += f" · updated: {self.index.last_diff.note()}"
            new = len(self._new)
            if new:  # first, so a long folder name can't push it out of sight
                text = f"{new} new album{'' if new == 1 else 's'} · {text}"
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
            self.status.configure(text=f"Adding “{album.name}”…")
            self.on_add(album.path, self._added)

    def _added(self, message: str):
        if self.win.winfo_exists():
            self.status.configure(text=message)
            self._paint_buttons()

    def _queue(self, album: Album, front: bool):
        if self.on_queue is not None:
            self.status.configure(text=f"Adding “{album.name}” to Up next…")
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
        menu.add("Open in new tab", lambda: self._choose(album), "Enter")
        target = self.add_target()
        if self.on_add is not None and target is not None:
            short = target if len(target) <= 28 else target[:27] + "…"
            menu.add(f"Add to “{short}”", self._add_selected)
        if self.on_queue is not None:
            menu.add_separator()
            menu.add("Play next", lambda: self._queue(album, True))
            menu.add("Add to queue", lambda: self._queue(album, False))
        if self.on_edit_tags is not None and self.can_edit_tags():
            menu.add_separator()
            menu.add("Edit tags…", lambda: self.on_edit_tags(album.path))
        menu.popup(event.x_root, event.y_root)

    def _load_all(self):
        """Every album shown (all of them, or just the search results), in this order."""
        self.on_load_all(self.root_folder, self.search_var.get().strip())
        self.close()

    def _focus_list(self):
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

    def _choose_selected(self):
        """Open the selected album. (On a group row, Return / double-click fold it open or shut:
        Tk's own Treeview bindings do that.)"""
        album = self._selected_album()
        if album is not None:
            self._choose(album)

    def _choose_first(self):
        if self._rows:
            self._choose(next(iter(self._rows.values())))

    def _choose(self, album: Album):
        self.on_choose(album.path)
        self.close()

    def _change_root(self):
        folder = filedialog.askdirectory(parent=self.win, title="Choose your music library folder",
                                         initialdir=str(self.root_folder), mustexist=True)
        if folder:
            self.root_folder = Path(folder)
            self.on_root_changed(self.root_folder)
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

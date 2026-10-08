"""Up next docked in a wide window: the right pane's view switch (Tracks · Up next · Lyrics) shows the
songs waiting to play as a list the pane's height, with the same actions as the Up next pop-over
under the track list — play a song now, take it out, Clear (Ctrl+Z puts them back), Stop after the
current song and the Sleep timer.

The list looks like the track list (the same row styles, the slim scrollbar in the window's margin):
# · Title · Artist · Time, and a column of download marks while a song from a network drive is waiting
(● in the cache, ◐ 72% downloaded, ○ waiting, ! couldn't download, that song's row in the warning
colour — hover a row's mark for its words).
Double-click or Enter plays a song now, Delete takes it out, right-click (or the menu key) for both.
Several songs can be selected (Shift+click, Ctrl+click, Shift+↑ / ↓, Ctrl+A): Delete and the menu
take them all out at once (the window's Ctrl+Z puts them back). The selection stays on the same songs
as the queue moves on (a song starts and leaves the list). A very long queue lists its first `SHOWN`
songs and counts the rest under them, so Tk's thread is never held up filling rows.

Nothing here talks to the player: the window hands the list what to show (`show`) and does what it's
asked (the `on_…` callbacks), as it does for the pop-over.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import controls, keys, popups, theme
from .playlist_view import TREE_STYLES, WIDE_CAPS, fit_columns
from .tabbar import Tooltip

COLUMNS = ("mark", "no", "title", "artist", "time")
MARK_W = 56     # px at 100%: the download-mark column ("◐ 72%"), shown only for network songs
SHOWN = 500     # songs listed at most (the rest are counted under the list)


def _clock(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


class UpNextView(ttk.Frame):
    """`show(queue, marks, stopping, sleep_label)` with the queue (the player's own list of Up next
    entries, each with its `.track`: replaced whole on every change, never changed in place) and, per
    song from the top, its (mark, tone, meaning) from the network cache. The callbacks: `on_play(entry)`,
    `on_remove(entries)` (the entries chosen: the queue may have moved on since they were shown, so the
    window finds where they are now), `on_clear()`, `on_stop_after()` (flip "Stop after the current
    song"), `on_sleep_menu(widget)` (the sleep timer's menu, at the button). `scroll_parent`: the padded
    frame the slim scrollbar sits in the margin of (the window's body)."""

    def __init__(self, parent, *, scroll_parent: tk.Misc, on_play: Callable[[object], None],
                 on_remove: Callable[[list], None], on_clear: Callable[[], None],
                 on_stop_after: Callable[[], None], on_sleep_menu: Callable[[tk.Misc], None]):
        super().__init__(parent)
        self.on_play, self.on_remove = on_play, on_remove
        # What the list holds now, so a poll only redraws on a change: the queue it was given, the
        # marks given with it, the entries listed (row i is entries[i]) and each row's words and mark.
        self._queue = None
        self._given: list = []
        self._entries: list = []
        self._shown: tuple = ()
        self._removed_at: int | None = None   # the first row just taken out from here (see show)
        self._mark_column = False   # the download-mark column is on show
        self._end: str | None = None  # where Shift+↑ / ↓ has taken the selection to (it runs from the focus row)
        self.count = 0

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=theme.pad(0, theme.GAP))
        self.actions = actions
        self.clear_button = controls.PillButton(actions, "Clear", on_clear, kind="plain", small=True,
                                                label="Take every song out of Up next (Ctrl+Z puts them back)")
        self.clear_button.pack(side="left")
        self.stopping = tk.BooleanVar(self, value=False)
        self.stop_switch = controls.Switch(actions, "Stop after the current song", self.stopping,
                                           command=on_stop_after)
        self.stop_switch.pack(side="left", padx=theme.pad(theme.GAP_WIDE, 0))
        self.sleep_button = controls.PillButton(actions, "Sleep timer…", lambda: on_sleep_menu(self.sleep_button),
                                                kind="plain", small=True,
                                                label="Stop the music after a while, or at the end of the album")
        self.sleep_button.pack(side="right")
        self.bind("<Destroy>", self._let_go, add="+")

        # Nothing waiting: a quiet note in the list's place.
        self.empty = ttk.Frame(self)
        ttk.Label(self.empty, text="Nothing in Up next", style="Title.TLabel", anchor="w").pack(fill="x")
        ttk.Label(self.empty, text="Right-click songs (or an album in the Library) and choose Play next or Add to "
                                   "queue: they play before the tab carries on.", style="Muted.TLabel",
                  anchor="w", justify="left", wraplength=theme.px(420)).pack(fill="x",
                                                                              pady=theme.pad(theme.GAP_SMALL, 0))

        table = ttk.Frame(self)
        self.table = table
        self.tree = ttk.Treeview(table, columns=COLUMNS, show="headings", selectmode="extended", height=4,
                                 displaycolumns=("no", "title", "artist", "time"))
        tree = self.tree
        for column, heading, anchor in (("mark", "", "w"), ("no", "#", "e"), ("title", "Title", "w"),
                                        ("artist", "Artist", "w"), ("time", "Time", "e")):
            tree.heading(column, text=heading, anchor=anchor)
            tree.column(column, anchor=anchor)
        tree.column("mark", width=theme.px(MARK_W), stretch=False)
        tree.column("no", width=theme.px(38), stretch=False)
        tree.column("time", width=theme.px(52), stretch=False)
        self._fit = fit_columns(tree, {"title": 3, "artist": 2}, minimum=theme.px(50),
                                caps={"artist": theme.px(WIDE_CAPS["artist"])})
        self.scrollbar = controls.AutoScrollbar(scroll_parent, command=tree.yview)
        tree.configure(yscrollcommand=self.scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        self.more = ttk.Label(table, style="Muted.TLabel", anchor="w")   # "and 1200 more", under the list
        self.scrollbar.in_margin(table)
        self._colour_tags()
        theme.on_change(self._palette_changed)
        tree.bind("<Double-Button-1>", lambda e: self.play_selected())
        for key in ("<Return>", "<KP_Enter>"):
            tree.bind(key, lambda e: self.play_selected() or "break")
        tree.bind("<Delete>", lambda e: self.remove_selected() or "break")
        # Several songs: Shift+click and Ctrl+click are the list's own (extended); Shift+↑ / ↓ grow the
        # selection from the focus row, which a click or a plain arrow moves.
        for name, rows in (("Up", -1), ("Down", 1)):
            tree.bind(f"<Shift-Key-{name}>", lambda e, r=rows: self._extend(r))
        for sequence in ("<Button-1>", "<Key-Up>", "<Key-Down>"):
            tree.bind(sequence, lambda e: setattr(self, "_end", None), add="+")
        for sequence in keys.sequences("select_all"):
            tree.bind(sequence, lambda e: self.select_all() or "break")
        tree.bind("<Button-3>", self._context)
        for key in ("<App>", "<Shift-F10>"):
            tree.bind(key, lambda e: self._context(popups.row_event(tree)) or "break")
        self._marks: list[str] = []   # each row's mark's meaning, for the hover
        self._tip_row = None
        self._tip = Tooltip(tree, self._mark_tip)
        tree.bind("<Motion>", self._hover, add="+")
        tree.bind("<Leave>", lambda e: self._hover(None), add="+")
        self._paint_empty(True)

    def _let_go(self, event):
        if event.widget is self:   # (closed: let go of the window's methods — controls.ChoiceRow._untrace)
            self.on_play = self.on_remove = lambda chosen: None
            self._queue, self._entries = None, []

    def _colour_tags(self):
        # (A row takes one colour: a song that couldn't download is in the warning colour. Green would
        # read as the song playing, as in the track list, so a song in the cache keeps the text colour.)
        self.tree.tag_configure("hover", background=theme.HOVER, foreground=theme.TEXT)
        self.tree.tag_configure("warn", foreground=theme.WARN)

    def _palette_changed(self, _palette):
        if self.winfo_exists():
            self._colour_tags()

    def set_tier(self, tier: str):
        """The window's layout: the rows as tall as the track list's."""
        self.tree.configure(style=TREE_STYLES.get(tier, ""))

    # -- what it shows ---------------------------------------------------------------------------------
    def show(self, queue, marks: list[tuple[str, str, str]], stopping: bool, sleep_label: str):
        """The songs waiting (the first SHOWN of them: the list scrolls, the rest are counted), their
        download marks, and the state of Stop after / the sleep timer."""
        if self.stopping.get() != stopping:
            self.stopping.set(stopping)
        self.sleep_button.set_text(sleep_label)
        self.clear_button.set_enabled(bool(queue))
        if queue is self._queue and marks == self._given:
            return   # (polled several times a second: nothing has changed)
        self._given = marks
        entries = self._entries if queue is self._queue else list(queue[:SHOWN])
        marks = list(marks[:len(entries)]) + [("", "", "")] * (len(entries) - len(marks))
        rows = tuple((e.track.title, e.track.artist or "", e.track.duration, marks[i]) for i, e in enumerate(entries))
        tree = self.tree
        # The selection belongs to the songs, not the rows: a song that started has left the top, and
        # the ones under it have moved up. (Read while the rows are still the old list's.)
        chosen = {id(entry) for entry in (self._entry(iid) for iid in tree.selection()) if entry is not None}
        focused = self._entry(tree.focus())
        before, listed = self._shown, self._entries
        self._queue, self._entries, self._shown = queue, entries, rows
        self.count = len(queue)
        self._paint_more(len(queue) - len(entries))
        column = any(mark for _t, _a, _d, (mark, _tone, _meaning) in rows)
        if (len(entries) == len(listed) and column == self._mark_column
                and all(new is old for new, old in zip(entries, listed))):
            # The same songs, a download moved on (polled several times a second): only the rows
            # that changed are written, and the row under the pointer stays lit.
            for i, (row, old) in enumerate(zip(rows, before)):
                if row != old:
                    tree.item(str(i), values=self._values(i, row), tags=self._tags(i, row))
                    self._marks[i] = row[3][2]
            return
        self._mark_column = column
        tree.delete(*tree.get_children())
        tree.configure(displaycolumns=(("mark",) if column else ()) + ("no", "title", "artist", "time"))
        self._marks = []
        for i, row in enumerate(rows):
            tree.insert("", "end", iid=str(i), values=self._values(i, row), tags=self._tags(i, row))
            self._marks.append(row[3][2])
        keep = [str(i) for i, entry in enumerate(entries) if id(entry) in chosen]
        if not keep and self._removed_at is not None and entries:
            # Songs just taken out from here: the one now in their place is selected, so Delete goes on.
            keep = [str(min(self._removed_at, len(entries) - 1))]
        if keep:
            tree.selection_set(keep)
            again = next((str(i) for i, entry in enumerate(entries) if entry is focused), None)
            tree.focus(again if again in keep else keep[0])
        self._end = None
        self._fit()
        self._paint_empty(not rows)

    def _paint_more(self, more: int):
        """Under the list: how many more songs wait after the ones listed."""
        if more > 0:
            self.more.configure(text=f"and {more} more")
            if not self.more.winfo_manager():
                self.more.pack(side="bottom", fill="x", before=self.tree, pady=theme.pad(theme.GAP_SMALL, 0))
        elif self.more.winfo_manager():
            self.more.pack_forget()

    @staticmethod
    def _values(i: int, row) -> tuple:
        title, artist, duration, (mark, _tone, _meaning) = row
        return mark, i + 1, title, artist, _clock(duration)

    def _tags(self, i: int, row) -> tuple[str, ...]:
        tags = ("warn",) if row[3][1] == "warn" else ()
        return ("hover",) + tags if str(i) == self._tip_row else tags

    def _paint_empty(self, empty: bool):
        if empty and not self.empty.winfo_manager():
            self.table.pack_forget()
            self.empty.pack(fill="x", pady=theme.pad(theme.GAP_LARGE, 0))
        elif not empty and not self.table.winfo_manager():
            self.empty.pack_forget()
            self.table.pack(fill="both", expand=True)

    # -- acting --------------------------------------------------------------------------------------
    def _entry(self, iid: str):
        """The queued song a row shows (None: no such row)."""
        return self._entries[int(iid)] if iid.isdigit() and int(iid) < len(self._entries) else None

    def selected_entries(self) -> list:
        """Every song selected, top to bottom (the focus row when none is): the player's entries."""
        chosen = self.tree.selection() or ((self.tree.focus(),) if self.tree.focus() else ())
        rows = sorted(int(iid) for iid in chosen if iid.isdigit() and int(iid) < len(self._entries))
        return [self._entries[row] for row in rows]

    def selected(self):
        """The first song selected (the focus row when none is): its entry, or None."""
        chosen = self.selected_entries()
        return chosen[0] if chosen else None

    def play_selected(self):
        chosen = self.selected()
        if chosen is not None:
            self.on_play(chosen)

    def remove_selected(self):
        self._remove(self.selected_entries())

    def _remove(self, entries: list):
        if not entries:
            return
        wanted = {id(entry) for entry in entries}
        rows = [i for i, entry in enumerate(self._entries) if id(entry) in wanted]
        # (The window shows the list afresh as it takes them out: the row now in their place is selected.)
        self._removed_at = min(rows) if rows else None
        try:
            self.on_remove(entries)
        finally:
            self._removed_at = None

    def select_all(self):
        rows = self.tree.get_children()
        if rows:
            self.tree.selection_set(rows)
            if not self.tree.focus():
                self.tree.focus(rows[0])
            self._end = None

    def _extend(self, rows: int):
        """Shift+↑ / ↓: the selection runs from the focus row to one row further up or down."""
        tree = self.tree
        children = tree.get_children()
        if not children:
            return "break"
        anchor = tree.focus() if tree.focus() in children else children[0]
        end = self._end if self._end in children else anchor
        to = children[max(0, min(len(children) - 1, children.index(end) + rows))]
        low, high = sorted((children.index(anchor), children.index(to)))
        tree.selection_set(children[low:high + 1])
        tree.focus(anchor)
        tree.see(to)
        self._end = to
        return "break"

    def _context(self, event):
        row = self.tree.identify_row(event.y)
        entry = self._entry(row)
        if entry is None:
            return
        if row not in self.tree.selection():   # (a right-click inside a selection keeps it)
            self.tree.selection_set(row)
            self.tree.focus(row)
        # (The songs themselves, not their rows: the list may move on while the menu is open.)
        chosen = self.selected_entries()
        menu = popups.PopupMenu(self.winfo_toplevel())
        menu.add("Play now", lambda: self.on_play(entry), keys.hint("list_play"))
        if len(chosen) > 1:
            menu.add(f"Take these {len(chosen)} songs out of Up next", lambda: self._remove(chosen),
                     keys.hint("remove"))
        else:
            menu.add("Take it out of Up next", lambda: self._remove([entry]), keys.hint("remove"))
        menu.popup(event.x_root, event.y_root)

    def _hover(self, event):
        """The row under the pointer lights up, and a network song's mark says what it means."""
        row = self.tree.identify_row(event.y) if event is not None else ""
        if row == self._tip_row:
            return
        for iid, on in ((self._tip_row, False), (row, True)):
            if iid and self.tree.exists(iid):
                tags = [t for t in self.tree.item(iid, "tags") if t != "hover"]
                self.tree.item(iid, tags=(["hover"] + tags) if on else tags)
        self._tip_row = row or None
        if event is not None:   # (the tip says the new row's mark: shown again after the usual pause)
            self._tip._hide()
            self._tip._schedule()

    def _mark_tip(self) -> str:
        row = self._tip_row
        return self.mark_meaning(int(row)) if row and row.isdigit() else ""

    def mark_meaning(self, position: int) -> str:
        return self._marks[position] if 0 <= position < len(self._marks) else ""


def count_text(count: int, seconds: float) -> str:
    """The pane's count over the list: "5 songs · 21:30", or "Nothing waiting"."""
    if not count:
        return "Nothing waiting"
    minutes, rest = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    length = f"{hours}:{minutes:02d}:{rest:02d}" if hours else f"{minutes}:{rest:02d}"
    return f"Up next · {count} song{'' if count == 1 else 's'} · {length}"


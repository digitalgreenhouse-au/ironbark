"""Up next docked in a wide window: the right pane's view switch (Tracks · Up next · Lyrics) shows the
songs waiting to play as a list the pane's height, with the same actions as the Up next pop-over
under the track list — play a song now, take it out, Clear (Ctrl+Z puts them back), Stop after the
current song and the Sleep timer.

The list looks like the track list (the same row styles, the slim scrollbar in the window's margin):
# · Title · Artist · Time, and a column of download marks while a song from a network drive is waiting
(● in the cache, ◐ 72% downloaded, ○ waiting, ! couldn't download, that song's row in the warning
colour — hover a row's mark for its words).
Double-click or Enter plays a song now, Delete takes it out, right-click (or the menu key) for both.

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


def _clock(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


class UpNextView(ttk.Frame):
    """`show(queue, marks, stopping, sleep_label)` with the queue (engine Queued entries: `.title`,
    `.artist`, `.duration`) and, per song, its (mark, tone, meaning) from the network cache. The
    callbacks: `on_play(position)`, `on_remove(position)`, `on_clear()`, `on_stop_after()` (flip
    "Stop after the current song"), `on_sleep_menu(widget)` (the sleep timer's menu, at the button).
    `scroll_parent`: the padded frame the slim scrollbar sits in the margin of (the window's body)."""

    def __init__(self, parent, *, scroll_parent: tk.Misc, on_play: Callable[[int], None],
                 on_remove: Callable[[int], None], on_clear: Callable[[], None],
                 on_stop_after: Callable[[], None], on_sleep_menu: Callable[[tk.Misc], None]):
        super().__init__(parent)
        self.on_play, self.on_remove = on_play, on_remove
        # What the list holds now (titles and marks), so a poll only redraws on a change.
        self._shown: tuple | None = None
        self._mark_column = False   # the download-mark column is on show
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
        self.tree = ttk.Treeview(table, columns=COLUMNS, show="headings", selectmode="browse", height=4,
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
        self.scrollbar.in_margin(table)
        self._colour_tags()
        theme.on_change(self._palette_changed)
        tree.bind("<Double-Button-1>", lambda e: self.play_selected())
        for key in ("<Return>", "<KP_Enter>"):
            tree.bind(key, lambda e: self.play_selected() or "break")
        tree.bind("<Delete>", lambda e: self.remove_selected() or "break")
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
            self.on_play = self.on_remove = lambda position: None

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
        """The songs waiting (all of them: the list scrolls), their download marks, and the state of
        Stop after / the sleep timer."""
        if self.stopping.get() != stopping:
            self.stopping.set(stopping)
        self.sleep_button.set_text(sleep_label)
        self.clear_button.set_enabled(bool(queue))
        marks = list(marks) + [("", "", "")] * (len(queue) - len(marks))
        rows = tuple((track.title, track.artist or "", track.duration, marks[i]) for i, track in enumerate(queue))
        if rows == self._shown:
            return
        before, self._shown = self._shown or (), rows
        self.count = len(rows)
        tree = self.tree
        column = any(mark for _t, _a, _d, (mark, _tone, _meaning) in rows)
        if len(rows) == len(before) and column == self._mark_column:
            # The same songs, a download moved on (polled several times a second): only the rows
            # that changed are written, and the row under the pointer stays lit.
            for i, (row, old) in enumerate(zip(rows, before)):
                if row != old:
                    tree.item(str(i), values=self._values(i, row), tags=self._tags(i, row))
                    self._marks[i] = row[3][2]
            return
        self._mark_column = column
        selected = tree.selection()
        keep = int(selected[0]) if selected else None
        tree.delete(*tree.get_children())
        tree.configure(displaycolumns=(("mark",) if column else ()) + ("no", "title", "artist", "time"))
        self._marks = []
        for i, row in enumerate(rows):
            tree.insert("", "end", iid=str(i), values=self._values(i, row), tags=self._tags(i, row))
            self._marks.append(row[3][2])
        if keep is not None and rows:
            row = str(min(keep, len(rows) - 1))
            tree.selection_set(row)
            tree.focus(row)
        self._fit()
        self._paint_empty(not rows)

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
    def selected(self) -> int | None:
        chosen = self.tree.selection() or ((self.tree.focus(),) if self.tree.focus() else ())
        return int(chosen[0]) if chosen and chosen[0].isdigit() else None

    def play_selected(self):
        position = self.selected()
        if position is not None:
            self.on_play(position)

    def remove_selected(self):
        position = self.selected()
        if position is not None:
            self.on_remove(position)

    def _context(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        self.tree.focus(row)
        position = int(row)
        menu = popups.PopupMenu(self.winfo_toplevel())
        menu.add("Play now", lambda: self.on_play(position), keys.hint("list_play"))
        menu.add("Take it out of Up next", lambda: self.on_remove(position), keys.hint("remove"))
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


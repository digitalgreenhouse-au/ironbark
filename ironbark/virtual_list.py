"""Big lists in Tk: a ttk.Treeview that only ever holds the rows on show.

A Treeview holding every row of a big library is slow to fill (seconds for 200,000 songs, and a
quarter of a gigabyte) and slower still to search. `VirtualTree` keeps just the rows that fit in
its window and fills them again from a `RowSource` as the list moves. Its own scrollbar maps to
positions in the source, and the mouse wheel, the arrow keys, Page Up / Down and Home / End move
through the source, not through Tk's rows. The selection, the row under the pointer, the playing
mark and folded groups all live in source positions, so they survive scrolling, and the memory
used doesn't grow with the list.

The selection is one row, or — `multiple=True` — any number of rows, chosen as in Explorer:
Shift+click or Shift+↑ / ↓ / PgUp / PgDn / Home / End from the row clicked first, Ctrl+click to add
or take out one row, and `select_all` (Ctrl+A, bound by the owner). It's kept as runs of rows
(`Ranges`), so selecting all of 200,000 songs costs no more than selecting one. Pressing on a row
that's already part of a selection of several keeps them all (so they can be dragged together);
let go without dragging and just that row is selected. Tk itself never selects anything here
(its selectmode is "none"): the list paints the selected rows on show.

A source is anything with `len()` and `row(i)` → (key, values, tags) or (key, values, tags, text).
The key is the row's id in the Treeview while it's on show (so a right-click, a drag — TreeDrag —
or a test can name it); `text` goes in the tree column (#0: a group's fold arrow). A row tagged
"group" is a group heading: clicking its arrow, double-clicking it or ← / → asks the owner to fold
it (`on_toggle`) — the source decides what folding means, as rows are never Tk children. A row
tagged "note" (a message such as "No matches") doesn't light up under the pointer. A source may also
have `key(i)` (a row's key without making the whole row) and `index_of(key)`.

Text that doesn't fit its column ends with "…" (Tk itself just cuts it off). Only the rows on show
are measured, so a big list costs no more than a small one — and a text is measured as the sum of
its characters' widths, each asked of Tk once (asking Tk for a whole text takes a fifth of a
millisecond, and a page of long names would need hundreds of asks). The sum is never less than
the width Tk draws (measured on Windows: kerning — "AV", "LT" — and a fallback font's "♥" only
draw a pixel or two narrower), so a text the sum says fits really does.

No Tk call is made off Tk's thread: a source whose rows come from the library database fills a
cache from its own worker and calls `refresh()` (from Tk's thread) when a page has arrived.
"""

from __future__ import annotations

import bisect
import time
import tkinter as tk
from collections.abc import Callable, Iterable, Iterator, Sequence
from tkinter import font as tkfont
from tkinter import ttk
from typing import Protocol

from . import controls, popups, theme

Row = tuple   # (key, values, tags) or (key, values, tags, text)

WHEEL_ROWS = 3          # rows a notch of the mouse wheel moves
JUMP_PAUSE_S = 1.0      # type-to-jump: letters typed within this of each other make one word
CELL_ROOM = 10          # px at 100%: a cell's own padding, left out of the room its text has
UNSIZED_PAGE = 60       # rows put in a list not laid out yet (its first <Configure> sets the real page)
KEEP_REMAP = 20_000     # a selection of more rows than this isn't carried over to a changed list (just its row)
DRAG_SLOP = 6           # px at 100%: a press that moves less than this before its release is a click
ELLIPSIS = "…"
OPEN_MARK, CLOSED_MARK = "▾", "▸"   # a group heading's arrow (in the tree column)
CLASS_TAG = "IronbarkVirtualTree"
# The Treeview class bindings the list leaves out: they would scroll, select or fold Tk's own rows
# (the list moves through its source instead, and folds through its owner).
_LEFT_OUT = {"<MouseWheel>", "<Shift-MouseWheel>", "<<ToggleSelection>>", "<Shift-Button-1>", "<Key-space>",
             "<Key-Return>", "<Key-Next>", "<Key-Prior>", "<Key-Left>", "<Key-Right>", "<Key-Down>", "<Key-Up>",
             "<Shift-Key-Up>", "<Shift-Key-Down>", "<Shift-Key-Next>", "<Shift-Key-Prior>", "<Shift-Key-Home>",
             "<Shift-Key-End>", "<Control-Button-1>"}
_MEASURED_KEEP = 5_000    # characters' widths remembered per font before the memory of them starts afresh
_SHIFT, _CONTROL, _ALT = 0x1, 0x4, 0x20000


class RowSource(Protocol):
    def __len__(self) -> int: ...

    def row(self, index: int) -> Row: ...


class ListSource:
    """A RowSource over rows made in advance (search results, a few thousand albums). `index_of`
    finds a row by its key (the list keeps its selection across a new source with it)."""

    def __init__(self, rows: Sequence[Row] = ()):
        self.rows = list(rows)
        self._index: dict[str, int] | None = None

    def __len__(self) -> int:
        return len(self.rows)

    def row(self, index: int) -> Row:
        return self.rows[index]

    def key(self, index: int) -> str:
        return self.rows[index][0]

    def index_of(self, key: str) -> int | None:
        if self._index is None:
            self._index = {row[0]: i for i, row in enumerate(self.rows)}
        return self._index.get(key)


class Ranges:
    """Row positions held as sorted runs [start, end) that neither overlap nor touch: every row of a
    200,000-row list selected is one run."""

    __slots__ = ("runs",)

    def __init__(self, runs: Iterable[tuple[int, int]] = ()):
        self.runs: list[tuple[int, int]] = []
        for start, end in runs:
            self.add(start, end)

    @classmethod
    def of(cls, indices: Iterable[int]) -> Ranges:
        """The runs these positions make (in any order)."""
        runs: list[tuple[int, int]] = []
        for i in sorted(set(indices)):
            if runs and runs[-1][1] == i:
                runs[-1] = (runs[-1][0], i + 1)
            else:
                runs.append((i, i + 1))
        found = cls()
        found.runs = runs
        return found

    def __len__(self) -> int:
        return sum(end - start for start, end in self.runs)

    def __bool__(self) -> bool:
        return bool(self.runs)

    def __contains__(self, index: int) -> bool:
        at = bisect.bisect_right(self.runs, (index, float("inf"))) - 1
        return at >= 0 and self.runs[at][0] <= index < self.runs[at][1]

    def __iter__(self) -> Iterator[int]:
        for start, end in self.runs:
            yield from range(start, end)

    def __eq__(self, other) -> bool:
        return isinstance(other, Ranges) and self.runs == other.runs

    def first(self) -> int | None:
        return self.runs[0][0] if self.runs else None

    def add(self, start: int, end: int) -> None:
        if end <= start:
            return
        kept = []
        for s, e in self.runs:
            if e < start or s > end:   # (apart, not even touching)
                kept.append((s, e))
            else:
                start, end = min(s, start), max(e, end)
        kept.append((start, end))
        kept.sort()
        self.runs = kept

    def discard(self, start: int, end: int) -> None:
        kept = []
        for s, e in self.runs:
            if e <= start or s >= end:
                kept.append((s, e))
                continue
            if s < start:
                kept.append((s, start))
            if e > end:
                kept.append((end, e))
        self.runs = kept

    def clipped(self, count: int) -> Ranges:
        """The runs inside a list of `count` rows."""
        found = Ranges()
        found.runs = [(s, min(e, count)) for s, e in self.runs if s < count]
        return found


def _use_class_bindings(tree: ttk.Treeview) -> None:
    """Swap the Treeview class tag in `tree`'s bindtags for a copy of its bindings without the
    ones that scroll or select inside Tk (made once per Tk interpreter)."""
    root = tree._root()
    if not getattr(root, "_dg_virtual_tree_class", False):
        root._dg_virtual_tree_class = True
        for sequence in root.bind_class("Treeview"):
            if sequence not in _LEFT_OUT:
                root.tk.call("bind", CLASS_TAG, sequence, root.bind_class("Treeview", sequence))
    tree.bindtags(tuple(CLASS_TAG if tag == "Treeview" else tag for tag in tree.bindtags()))


class VirtualTree:
    """See the module notes. `frame` holds the list and its slim scrollbar (pack or grid it);
    `tree` is the ttk.Treeview (headings, columns and tag colours are the owner's to set — "hover"
    is configured here first, so it wins over the owner's tags). `margin=True`: the scrollbar sits
    in the margin right of the list instead (controls.AutoScrollbar.in_margin), so the list keeps its
    full width and its words line up with what's above it; `parent` must then be the frame whose
    padding is that margin (or `scroll_parent` names that frame: a list deeper inside it).
    `multiple=True`: several rows can be selected (see the module notes).

    Callbacks: on_select(index | None) when the selection moves (the row the keyboard is on);
    on_activate(index) on a double-click of an ordinary row; on_toggle(index) to fold or open a
    group heading; on_jump(text) → the index to jump to as letters are typed (None: no
    type-to-jump); on_top_edge() when ↑ is pressed on the first row (a search panel takes the
    keyboard back to its box)."""

    def __init__(self, parent: tk.Misc, columns: Sequence[str], *, show: str = "headings",
                 on_select: Callable[[int | None], None] | None = None,
                 on_activate: Callable[[int], None] | None = None,
                 on_toggle: Callable[[int], None] | None = None,
                 on_jump: Callable[[str], int | None] | None = None,
                 on_top_edge: Callable[[], None] | None = None,
                 fonts: dict[str, object] | None = None, playing_column: str | None = None,
                 playing_mark: str = "♪", margin: bool = False, multiple: bool = False,
                 scroll_parent: tk.Misc | None = None):
        self.on_select, self.on_activate, self.on_toggle = on_select, on_activate, on_toggle
        self.on_jump, self.on_top_edge = on_jump, on_top_edge
        self.fonts = dict(fonts or {})       # tag -> the font its rows are drawn in (measured for "…")
        self.playing_column, self.playing_mark = playing_column, playing_mark
        self.multiple = multiple
        self.source: RowSource = ListSource()
        self.top = 0                         # the first source row on show
        self.selected: int | None = None     # the row the keyboard is on (Enter acts on it)
        self.anchor: int | None = None       # where a Shift+click or Shift+arrow selection runs from
        self.chosen = Ranges()               # every row selected (the keyboard's row among them)
        self.hovered: int | None = None
        self.playing: int | None = None
        self._shown: dict[str, int] = {}     # key on show -> its source index
        self._drawn: list[tuple] = []        # the rows the Treeview holds, as last put there

        self._render_job = None
        self._metrics: tuple[int, int] | None = None   # (the first row's top, a row's height), measured
        self._widths: dict[str, dict[str, int]] = {}   # font -> {character: its width}
        self._style_fonts: dict[str, tkfont.Font] = {}  # a style's font (as Tk names it) -> it, for measuring
        self._typed, self._typed_at = "", 0.0
        self._held: tuple[int, int, int] | None = None   # (row, x, y): pressed on a selection of several

        self.frame = ttk.Frame(parent)
        # height=4: ask for little, then grow into whatever room the window gives the list.
        self.tree = ttk.Treeview(self.frame, columns=tuple(columns), show=show, selectmode="none", height=4)
        self.scrollbar = controls.AutoScrollbar((scroll_parent or parent) if margin else self.frame,
                                                command=self._scrolled)
        self.tree.configure(yscrollcommand=self._tk_scrolled)
        self.tree.pack(side="left", fill="both", expand=True)
        if margin:
            self.scrollbar.in_margin(self.frame)
        else:
            self.scrollbar.auto_pack(side="right", fill="y", padx=(theme.px(theme.GAP_HAIR), 0), before=self.tree)
        _use_class_bindings(self.tree)
        self._font = tkfont.nametofont("TkDefaultFont")
        self._paint_tags()
        theme.on_change(self._palette_changed)

        tree = self.tree
        tree.bind("<Configure>", lambda e: self._schedule(), add="+")
        tree.bind("<MouseWheel>", self._wheel)
        tree.bind("<<TreeviewSelect>>", self._tk_selected, add="+")
        tree.bind("<Motion>", self._motion, add="+")
        tree.bind("<Leave>", lambda e: self.set_hover(None), add="+")
        tree.bind("<ButtonPress-1>", self._pressed, add="+")
        tree.bind("<Double-Button-1>", self._double_click, add="+")
        tree.bind("<ButtonRelease-1>", self._released, add="+")
        moves = (("Up", lambda: -1), ("Down", lambda: 1), ("Prior", lambda: -self.page()),
                 ("Next", lambda: self.page()))
        for name, rows in moves:
            tree.bind(f"<Key-{name}>", lambda e, r=rows: self._navigate(e, lambda: self.step(r())))
            tree.bind(f"<Shift-Key-{name}>", lambda e, r=rows: self._navigate(e, lambda: self.extend(r())))
        tree.bind("<Key-Home>", lambda e: self._navigate(e, lambda: self.select(0)))
        tree.bind("<Key-End>", lambda e: self._navigate(e, lambda: self.select(len(self.source) - 1)))
        tree.bind("<Shift-Key-Home>", lambda e: self._navigate(e, lambda: self.extend_to(0)))
        tree.bind("<Shift-Key-End>", lambda e: self._navigate(e, lambda: self.extend_to(len(self.source) - 1)))
        tree.bind("<Key-Left>", lambda e: self._sideways(False))
        tree.bind("<Key-Right>", lambda e: self._sideways(True))
        tree.bind("<KeyPress>", self._typed_key)
        tree.bind("<Destroy>", self._destroyed, add="+")

    def _destroyed(self, event):
        """Gone: let go of the owner's methods (see ChoiceRow._untrace) and of its fonts here, on Tk's
        thread — a Tk font left to Python's cycle collector may be freed on any thread, and a Tk
        object freed off Tk's thread breaks the interpreter."""
        if event.widget is self.tree:
            self.on_select = self.on_activate = self.on_toggle = self.on_jump = self.on_top_edge = None
            self.source = ListSource()
            self.fonts = {}
            self._style_fonts = {}
            self._drawn = []
            self._widths.clear()
            if self._render_job is not None:   # (a drawing waiting for Tk to be idle: its command goes now)
                try:
                    self.tree.after_cancel(self._render_job)
                except tk.TclError:
                    pass
            self._render_job = None

    # -- the source ------------------------------------------------------------------------------
    def set_source(self, source: RowSource, keep: bool = False, select: int | None = None):
        """Show another source (rows from the top, nothing selected). `keep`: the same list
        changed (a group folded, a search typed, a row's text): stay where it was, and keep the
        selected rows the source can find again by their keys (`index_of`). `select`: select this
        row."""
        before = self.selected
        old_cursor = self._key_of(self.selected) if keep and self.selected is not None else None
        old_keys: list[str] = []
        if keep and self.multiple and 1 < len(self.chosen) <= KEEP_REMAP:
            old_keys = [key for key in (self._key_of(i) for i in self.chosen) if key is not None]
        self.set_hover(None)   # (Tk's row lit now may be drawn again unchanged: it mustn't stay lit)
        self.source = source
        self.chosen = Ranges()
        self.selected = self.anchor = None
        if keep:
            finder = getattr(source, "index_of", None)
            if finder is not None:
                if old_cursor is not None:
                    self.selected = self.anchor = finder(old_cursor)
                found = [i for i in (finder(key) for key in old_keys) if i is not None]
                self.chosen = Ranges.of(found)
                if self.selected is not None:
                    self.chosen.add(self.selected, self.selected + 1)
                elif found:
                    self.selected = self.anchor = found[0]
        else:
            self.top = 0
        if select is not None and 0 <= select < len(source):
            self.selected = self.anchor = select
            self.chosen = Ranges([(select, select + 1)])
        self.render()
        if select is not None:
            self.see(select)
        if keep and self.selected != before and self.on_select is not None:
            self.on_select(self.selected)

    def refresh(self):
        """The rows on show again (their text or tags changed, or a page of them has arrived)."""
        self.render()

    def fonts_changed(self):
        """The list's style (its font, its rows' height) or a font of `fonts` changed size: measured
        afresh, and the rows on show drawn again once Tk is idle."""
        self._widths.clear()
        self._metrics = None
        self._schedule()

    def _row_font(self):
        """The font the rows are drawn in: the list's style's own (a larger one in a roomy style), else
        Tk's default."""
        try:
            name = str(ttk.Style(self.tree).lookup(str(self.tree.cget("style")) or "Treeview", "font"))
        except tk.TclError:
            name = ""
        if not name:
            return self._font
        found = self._style_fonts.get(name)
        if found is None:
            try:
                if name == "TkDefaultFont":
                    found = self._font
                elif name in tkfont.names(self.tree._root()):   # (a named font: itself, as it changes)
                    found = tkfont.nametofont(name, root=self.tree._root())
                else:
                    found = tkfont.Font(root=self.tree._root(), font=name)
            except tk.TclError:
                found = self._font
            self._style_fonts[name] = found
        return found

    def _key_of(self, index: int | None) -> str | None:
        if index is None or not 0 <= index < len(self.source):
            return None
        key = getattr(self.source, "key", None)
        return key(index) if key is not None else self.source.row(index)[0]

    def index_of_iid(self, iid: str) -> int | None:
        """The source index of a row on show, by its Treeview id (None: not on show)."""
        return self._shown.get(iid)

    def index_at(self, y: int) -> int | None:
        """The source index of the row at this height in the list (None: no row there)."""
        return self._shown.get(self.tree.identify_row(y))

    def iid_of(self, index: int) -> str | None:
        """A source row's Treeview id while it's on show, else None."""
        key = self._key_of(index)
        return key if key in self._shown else None

    # -- size -----------------------------------------------------------------------------------
    def _measure(self) -> tuple[int, int]:
        """(where the first row starts, a row's height) in the list: measured from a row on show
        when there is one, else worked out from the style."""
        if self._metrics is not None:
            return self._metrics
        tree = self.tree
        shown = tree.get_children()
        box = tree.bbox(shown[0]) if shown else ""
        if box:
            self._metrics = (int(box[1]), max(1, int(box[3])))
            return self._metrics
        try:
            height = int(ttk.Style(tree).lookup(str(tree.cget("style")) or "Treeview", "rowheight") or 0)
        except (tk.TclError, ValueError):
            height = 0
        height = height or self._font.metrics("linespace") + theme.px(theme.GAP)
        headings = "headings" in [str(part) for part in tree.cget("show")]
        return (height + theme.px(theme.GAP_SMALL) if headings else theme.px(theme.GAP_HAIR)), height

    def page(self) -> int:
        """Rows that fit in the list (whole rows only: the list never holds a cut-off one, so Tk
        never scrolls inside it). A list not laid out yet (no height) holds UNSIZED_PAGE rows until
        it is."""
        height = self.tree.winfo_height()
        if height <= 1:
            return UNSIZED_PAGE
        first, row = self._measure()
        return max(1, (height - first - theme.px(theme.GAP_HAIR)) // row)

    # -- drawing -------------------------------------------------------------------------------
    def _schedule(self):
        """Draw again once Tk is idle (a resize: column widths settle first)."""
        if self._render_job is None:
            self._render_job = self.tree.after_idle(self._idle_render)

    def _idle_render(self):
        self._render_job = None
        if self.tree.winfo_exists():
            self._metrics = None
            self.render()

    def render(self):
        """Put the source rows from `top` that fit into the Treeview (and only those)."""
        tree = self.tree
        count, page = len(self.source), self.page()
        self.top = max(0, min(self.top, count - page))
        end = min(count, self.top + page)
        columns = self._displayed()
        rooms = {c: max(0, int(tree.column(c, "width")) - theme.px(CELL_ROOM)) for c in columns}
        positions = {c: i for i, c in enumerate(tree.cget("columns"))}
        playing_at = positions.get(self.playing_column) if self.playing_column else None
        plain = self._row_font()
        rows = []
        for index in range(self.top, end):
            key, values, tags, *text = self.source.row(index)
            values, tags = list(values), list(tags)
            if index == self.playing:
                tags.append("playing")
                if playing_at is not None and playing_at < len(values):
                    values[playing_at] = self.playing_mark
            if index == self.hovered:
                tags.insert(0, "hover")
            font = next((self.fonts[t] for t in tags if t in self.fonts), plain)

            for column in columns:
                at = positions[column]
                if at < len(values) and isinstance(values[at], str):
                    values[at] = self.fit(values[at], rooms[column], font)
            rows.append((key, values, tags, text[0] if text else ""))
        self._put(rows)
        self._shown = {row[0]: index for index, row in zip(range(self.top, end), rows)}
        if rows and self._metrics is None and tree.winfo_height() > 1:
            self._measure()
            if self.page() != page:   # (the page was an estimate until a row could be measured)
                self.render()
                return
        self._paint_selection()
        self._paint_scrollbar()

    def _put(self, rows: list[tuple]):
        """Make the Treeview hold these rows (key, values, tags, text), top to bottom. Rows already
        there in the same places are only changed where they differ (a window being resized draws the
        same rows again and again); otherwise they're all made afresh."""
        tree, drawn = self.tree, self._drawn
        common = min(len(rows), len(drawn))
        if not drawn or [row[0] for row in rows[:common]] != [row[0] for row in drawn[:common]]:
            if drawn:
                tree.delete(*[row[0] for row in drawn])
            for key, values, tags, text in rows:
                tree.insert("", "end", iid=key, values=values, tags=tags, text=text)
        else:
            for row, old in zip(rows[:common], drawn[:common]):
                if row[1:] != old[1:]:
                    tree.item(row[0], values=row[1], tags=row[2], text=row[3])
            if len(drawn) > common:
                tree.delete(*[row[0] for row in drawn[common:]])
            for key, values, tags, text in rows[common:]:
                tree.insert("", "end", iid=key, values=values, tags=tags, text=text)
        self._drawn = rows

    def _displayed(self) -> list[str]:
        shown = [str(c) for c in self.tree.cget("displaycolumns")]
        return [str(c) for c in self.tree.cget("columns")] if shown == ["#all"] else shown

    def fit(self, text: str, room: int, font=None) -> str:
        """`text` as it fits in `room` pixels: whole, or cut with "…" on the end (the longest start
        of it that fits with "…" after it)."""
        font = font or self._font
        if not text:
            return text
        width = self._char_width(font)
        whole = 0
        for ch in text:
            whole += width(ch)
        if whole <= room:
            return text
        room -= width(ELLIPSIS)
        used = cut = 0
        for ch in text:
            used += width(ch)
            if used > room:
                break
            cut += 1
        return text[:cut].rstrip() + ELLIPSIS

    def _char_width(self, font) -> Callable[[str], int]:
        """A character's width in `font` (asked of Tk once, then remembered)."""
        widths = self._widths.get(str(font))
        if widths is None or len(widths) >= _MEASURED_KEEP:
            widths = self._widths[str(font)] = {}

        def width(ch: str) -> int:
            found = widths.get(ch)
            if found is None:
                found = widths[ch] = font.measure(ch)
            return found

        return width

    def _paint_scrollbar(self):
        count, page = len(self.source), self.page()
        if count <= page:
            self.scrollbar.set(0.0, 1.0)
        else:
            self.scrollbar.set(self.top / count, min(1.0, (self.top + page) / count))

    def _paint_tags(self):
        # Configured first, so it wins over the owner's tags (the first-configured tag has the say).
        self.tree.tag_configure("hover", background=theme.HOVER, foreground=theme.TEXT)

    def _palette_changed(self, _palette):
        if self.tree.winfo_exists():
            self._paint_tags()

    # -- moving --------------------------------------------------------------------------------
    def scroll_to(self, top: int):
        top = max(0, min(top, len(self.source) - self.page()))
        if top != self.top:
            self.top = top
            self.render()

    def see(self, index: int):
        """Scroll so that source row `index` is on show."""
        page = self.page()
        if index < self.top:
            self.scroll_to(index)
        elif index >= self.top + page:
            self.scroll_to(index - page + 1)

    def _scrolled(self, *args):
        """The scrollbar was dragged or clicked: ("moveto", fraction) or ("scroll", n, "units" / "pages")."""
        if not args:
            return
        if args[0] == "moveto":
            self.scroll_to(round(float(args[1]) * len(self.source)))
        elif args[0] == "scroll":
            steps = int(float(args[1]))
            self.scroll_to(self.top + steps * (self.page() if args[2] == "pages" else 1))

    def _tk_scrolled(self, first, last):
        """Tk's own idea of the view: it never needs to scroll (every row on show fits), but if it
        has (a row clicked that it thought cut off), the list moves there instead."""
        moved = round(float(first) * len(self.tree.get_children()))
        if moved > 0:
            self.tree.yview_moveto(0)
            self.top += moved
            self._schedule()

    def _wheel(self, event):
        notches = event.delta / 120
        rows = -round(notches * WHEEL_ROWS) or (-1 if event.delta > 0 else 1 if event.delta < 0 else 0)
        self.scroll_to(self.top + rows)
        return "break"

    # -- selection -------------------------------------------------------------------------------
    def _expected(self) -> list[str]:
        """The rows on show that are selected (their Treeview ids)."""
        if not self.chosen:
            return []
        return [iid for iid, index in self._shown.items() if index in self.chosen]

    def _paint_selection(self):
        """Show the selection on the rows on show (Tk's own selection is only ever this)."""
        tree = self.tree
        wanted = self._expected()
        current = tree.selection()
        if set(current) != set(wanted):
            if wanted:
                tree.selection_set(wanted)
            else:
                tree.selection_remove(*current)
        cursor = self.iid_of(self.selected) if self.selected is not None else None
        if cursor is not None:
            tree.focus(cursor)

    def _changed(self, before: tuple, notify: bool):
        self._paint_selection()
        if notify and self.on_select is not None and (self.selected, self.chosen.runs) != before:
            self.on_select(self.selected)

    def _state(self) -> tuple:
        return self.selected, list(self.chosen.runs)

    def select(self, index: int | None, see: bool = True, notify: bool = True):
        """Select source row `index` alone (None: nothing), scrolled into view."""
        if index is not None:
            if not len(self.source):
                index = None
            else:
                index = max(0, min(index, len(self.source) - 1))
        before = self._state()
        self.selected = self.anchor = index
        self.chosen = Ranges([(index, index + 1)]) if index is not None else Ranges()
        if index is not None and see:
            self.see(index)
        self._changed(before, notify)

    def extend_to(self, index: int, see: bool = True, notify: bool = True):
        """Select every row from the anchor (the row picked first) to `index` (Shift+click,
        Shift+arrows); a list of single selection just moves there."""
        count = len(self.source)
        if not count:
            return
        index = max(0, min(index, count - 1))
        if not self.multiple:
            self.select(index, see, notify)
            return
        if self.anchor is None or not 0 <= self.anchor < count:
            self.anchor = self.selected if self.selected is not None and self.selected < count else index
        before = self._state()
        self.selected = index
        self.chosen = Ranges([(min(self.anchor, index), max(self.anchor, index) + 1)])
        if see:
            self.see(index)
        self._changed(before, notify)

    def extend(self, rows: int):
        """Shift+↑ / ↓ / PgUp / PgDn: the selection grows (or shrinks) from the anchor."""
        if not len(self.source):
            return
        if self.selected is None:
            self.step(rows)
            return
        self.extend_to(self.selected + rows)

    def toggle(self, index: int, notify: bool = True):
        """Ctrl+click: this row into the selection, or out of it (the keyboard moves to it)."""
        if not self.multiple:
            self.select(index, see=False, notify=notify)
            return
        before = self._state()
        if index in self.chosen:
            self.chosen.discard(index, index + 1)
        else:
            self.chosen.add(index, index + 1)
        self.selected = self.anchor = index
        self._changed(before, notify)

    def select_all(self, notify: bool = True) -> bool:
        """Every row selected (Ctrl+A). False when there's nothing to select."""
        count = len(self.source)
        if not count or not self.multiple:
            return False
        before = self._state()
        self.chosen = Ranges([(0, count)])
        if self.selected is None:
            self.selected = 0
        self.anchor = 0
        self._changed(before, notify)
        return True

    def select_indices(self, indices: Iterable[int], cursor: int | None = None, notify: bool = True):
        """Select these rows (several only in a list of multiple selection); the keyboard goes to
        `cursor` (default: the first of them)."""
        count = len(self.source)
        found = [i for i in indices if 0 <= i < count]
        if not self.multiple:
            self.select(cursor if cursor is not None else (found[0] if found else None), notify=notify)
            return
        before = self._state()
        self.chosen = Ranges.of(found)
        self.selected = self.anchor = cursor if cursor is not None else (found[0] if found else None)
        if self.selected is not None:
            self.see(self.selected)
        self._changed(before, notify)

    def selection(self) -> int | None:
        """The row the keyboard is on (the one selected, in a list of single selection)."""
        return self.selected

    def menu_event(self):
        """A stand-in for a right-click on the row the keyboard is on (the menu key, Shift+F10): that
        row is scrolled into view first — only the rows on show are Tk's, so a selected row the list
        has scrolled away from has no place to open its menu at."""
        if self.selected is not None:
            self.see(self.selected)
        return popups.row_event(self.tree)

    def sync(self):
        """Take in a selection set on Tk's rows directly since (by a script or a test), before
        acting on the selection — Tk tells the list only once events are handled."""
        self._tk_selected()

    def selected_indices(self) -> list[int]:
        """Every selected row, top to bottom."""
        return list(self.chosen)

    def selected_count(self) -> int:
        return len(self.chosen)

    def is_selected(self, index: int) -> bool:
        return index in self.chosen

    def step(self, rows: int):
        """Move the selection `rows` down (up when negative); with nothing selected, the first row
        on show (or the last, going up)."""
        if not len(self.source):
            return
        if self.selected is None:
            self.select(self.top if rows > 0 else min(len(self.source), self.top + self.page()) - 1)
        elif rows < 0 and self.selected == 0 and self.on_top_edge is not None:
            self.on_top_edge()
        else:
            self.select(self.selected + rows)

    def _navigate(self, event, move):
        if event.state & (_CONTROL | _ALT):   # (Ctrl / Alt + an arrow: the keyboard table's, not the list's)
            return None
        move()
        return "break"

    def _tk_selected(self, _event=None):
        """Tk says its selection changed. The list sets it itself (`_paint_selection`), so that's
        usually just the list's own painting coming back; otherwise something set Tk's selection
        directly (a script, a test): the rows on show it names become the selection."""
        chosen = self.tree.selection()
        if set(chosen) == set(self._expected()):
            return
        before = self._state()
        indices = [self._shown[iid] for iid in chosen if iid in self._shown]
        if not self.multiple:
            indices = indices[:1]
        focus = self._shown.get(self.tree.focus())
        self.chosen = Ranges.of(indices)
        self.selected = self.anchor = focus if focus in indices else (indices[0] if indices else None)
        self._changed(before, True)

    # -- the pointer and groups ------------------------------------------------------------------
    def _pressed(self, event):
        """A click picks rows (Tk itself selects nothing here): alone; Shift — from the anchor to
        here; Ctrl — this one in or out. Pressed on a row of a selection of several, they stay
        selected for a drag; let go without dragging and just that row is selected."""
        self._held = None
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        index = self.index_at(event.y)
        if index is None:
            return
        if self.multiple and event.state & _SHIFT:
            self.extend_to(index, see=False)
        elif self.multiple and event.state & _CONTROL:
            self.toggle(index)
        elif self.multiple and index in self.chosen and len(self.chosen) > 1:
            self._held = (index, event.x, event.y)
            before = self._state()
            self.selected = index
            self._changed(before, True)
        else:
            self.select(index, see=False)

    def set_hover(self, index: int | None):
        """Light up the row under the pointer (None: none)."""
        if index == self.hovered:
            return
        for row, on in ((self.hovered, False), (index, True)):
            iid = self.iid_of(row) if row is not None else None
            if iid is not None and self.tree.exists(iid):
                tags = [t for t in self.tree.item(iid, "tags") if t != "hover"]
                self.tree.item(iid, tags=(["hover"] + tags) if on else tags)
        self.hovered = index

    def _motion(self, event):
        if event.state & 0x100:   # (the button held down: a drag, not a hover)
            return
        index = self.index_at(event.y)
        if index is not None and "note" in self.source.row(index)[2]:
            index = None   # (a message row, such as "No matches", isn't something to pick)
        self.set_hover(index)

    def set_playing(self, index: int | None):
        """Mark the playing row (tag "playing", and the mark in `playing_column`)."""
        if index != self.playing:
            self.playing = index
            self.render()

    def is_group(self, index: int | None) -> bool:
        return index is not None and 0 <= index < len(self.source) and "group" in self.source.row(index)[2]

    def _released(self, event):
        """A click on a group heading's arrow folds or opens it. A column's edge dragged along the
        headings: the rows on show are fitted ("…") to its new width. A click (not a drag) on a row
        of a selection of several: just that row."""
        held, self._held = self._held, None
        if held is not None:
            index, x, y = held
            if max(abs(event.x - x), abs(event.y - y)) < theme.px(DRAG_SLOP) and 0 <= index < len(self.source):
                self.select(index, see=False)
        if event.y < self._measure()[0]:
            self._schedule()
            return
        index = self.index_at(event.y)
        if (self.is_group(index) and self.tree.identify_column(event.x) == "#0"
                and self.on_toggle is not None):
            self.on_toggle(index)

    def _double_click(self, event):
        index = self.index_at(event.y)
        if index is None or self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        if self.is_group(index):
            if self.on_toggle is not None and self.tree.identify_column(event.x) != "#0":
                self.on_toggle(index)
        elif self.on_activate is not None:
            self.on_activate(index)

    def _sideways(self, right: bool):
        """← / → on a group heading folds / opens it (← on a row inside a group goes to its heading).
        Elsewhere the key goes on (the keyboard table's seek)."""
        index = self.selected
        if index is None or "tree" not in [str(part) for part in self.tree.cget("show")]:
            return None
        if self.is_group(index):
            row = self.source.row(index)
            folded = len(row) > 3 and row[3] == CLOSED_MARK
            if folded == right and self.on_toggle is not None:
                self.on_toggle(index)
            return "break"
        if right:
            return None
        heading = next((i for i in range(index - 1, -1, -1) if self.is_group(i)), None)
        if heading is None:
            return None
        self.select(heading)
        return "break"

    # -- type to jump ------------------------------------------------------------------------------
    def _typed_key(self, event):
        """Letters and digits typed in the list jump to the first row starting with them (typed
        within a second of each other, they make one word). Other keys go on."""
        if self.on_jump is None or event.state & (_CONTROL | _ALT) or not event.char or not event.char.isalnum():
            return None
        now = time.monotonic()
        self._typed = (self._typed if now - self._typed_at < JUMP_PAUSE_S else "") + event.char
        self._typed_at = now
        index = self.on_jump(self._typed)
        if index is not None:
            self.select(index)
        return "break"

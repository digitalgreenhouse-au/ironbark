"""VirtualTree (virtual_list.py): a Treeview that holds only the rows on show, over a source of any
size — keys, selection, see(), the scrollbar, type-to-jump, folding and "…" all work in source rows."""
import gc
import tkinter as tk
import tracemalloc
from types import SimpleNamespace

import pytest

from ironbark import theme
from ironbark.playlist_view import TreeDrag
from ironbark.virtual_list import CLOSED_MARK, ELLIPSIS, OPEN_MARK, ListSource, VirtualTree


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    theme.apply(root)
    root.geometry(f"{theme.px(420)}x{theme.px(360)}")
    yield root
    root.destroy()
    del root
    gc.collect()  # on this thread (Tk objects freed on another thread abort Python)


class Numbers:
    """200,000 rows made as they're asked for: nothing is kept."""

    def __init__(self, count: int = 200_000):
        self.count = count
        self.asked = 0

    def __len__(self):
        return self.count

    def row(self, i):
        self.asked += 1
        return (f"r{i}", (f"Song {i:06d}", f"Artist {i % 97}"), ())


def make(root, source=None, **options) -> VirtualTree:
    vt = VirtualTree(root, ("title", "artist"), **options)
    vt.tree.heading("title", text="Title")
    vt.tree.heading("artist", text="Artist")
    vt.frame.pack(fill="both", expand=True)
    root.update()
    if source is not None:
        vt.set_source(source)
        root.update()
    return vt


def key(vt, event, **kw):
    vt.tree.focus_force()       # (key presses go to the widget with the keyboard)
    vt.tree.update()
    vt.tree.event_generate(event, **kw)


def test_holds_only_the_rows_on_show_and_moves_through_the_source(root):
    source = Numbers()
    vt = make(root, source)
    page = vt.page()
    shown = vt.tree.get_children()
    assert 3 <= page and len(shown) == page <= page + 2 and shown[0] == "r0"
    assert vt.tree.yview() == (0.0, 1.0)                 # Tk itself never scrolls: every row fits
    first, last = vt.scrollbar.get()
    assert first == 0.0 and last == pytest.approx(page / 200_000) and vt.scrollbar.needed
    vt.see(150_000)
    shown = vt.tree.get_children()
    assert "r150000" in shown and len(shown) == page and vt.top == 150_000 - page + 1
    vt.select(199_999)
    assert vt.tree.selection() == ("r199999",) and vt.tree.get_children()[-1] == "r199999"
    vt._scrolled("moveto", "0.5")
    assert vt.top == 100_000 and vt.tree.selection() == ()      # scrolled away: still selected in the source
    assert vt.selected == 199_999
    vt._scrolled("scroll", "1", "pages")
    assert vt.top == 100_000 + page
    vt._scrolled("scroll", "-2", "units")
    assert vt.top == 100_000 + page - 2
    vt.scroll_to(10 ** 9)                                # clamped to the end
    assert vt.tree.get_children()[-1] == "r199999" and vt.top == 200_000 - page
    assert source.asked < 10 * page                      # rows are only made as they're shown


def test_memory_stays_flat_however_far_the_list_moves(root):
    vt = make(root, Numbers())
    page = vt.page()
    for top in range(0, 2_000, page):                    # warm up (Tk's own caches, the width memory)
        vt.scroll_to(top)
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    for top in range(2_000, 200_000, 997):
        vt.scroll_to(top)
        assert len(vt.tree.get_children()) <= page + 2
    grown = tracemalloc.get_traced_memory()[0] - before
    tracemalloc.stop()
    assert grown < 3_000_000, grown   # (the width memory is bounded; nothing else is kept per row)


def test_keys_select_in_the_source(root):
    picked = []
    edge = []
    vt = make(root, Numbers(1000), on_select=picked.append, on_top_edge=lambda: edge.append(1))
    page = vt.page()
    key(vt, "<KeyPress-Down>")
    assert vt.selected == 0 and picked == [0] and vt.tree.selection() == ("r0",)
    key(vt, "<KeyPress-Next>")
    assert vt.selected == page and vt.tree.selection() == (f"r{page}",)
    key(vt, "<KeyPress-End>")
    assert vt.selected == 999 and vt.tree.get_children()[-1] == "r999"
    key(vt, "<KeyPress-Prior>")
    assert vt.selected == 999 - page
    key(vt, "<KeyPress-Home>")
    assert vt.selected == 0 and vt.top == 0
    key(vt, "<KeyPress-Up>")
    assert edge == [1] and vt.selected == 0             # ↑ on the first row: handed to the owner
    vt.tree.event_generate("<MouseWheel>", delta=-120)
    assert vt.top == 3 and vt.tree.yview() == (0.0, 1.0)


def test_a_click_selects_and_a_double_click_activates(root):
    chosen = []
    vt = make(root, Numbers(50), on_activate=chosen.append)
    root.update()
    x, y, _w, h = vt.tree.bbox("r2")
    vt.tree.event_generate("<Button-1>", x=x + 5, y=y + h // 2)
    vt.tree.event_generate("<ButtonRelease-1>", x=x + 5, y=y + h // 2)
    root.update()
    assert vt.selected == 2
    last = vt.tree.get_children()[-1]                    # the bottom row: Tk mustn't scroll inside the list
    x, y, _w, h = vt.tree.bbox(last)
    vt.tree.event_generate("<Button-1>", x=x + 5, y=y + h // 2)
    vt.tree.event_generate("<ButtonRelease-1>", x=x + 5, y=y + h // 2)
    root.update()
    assert vt.tree.yview()[0] == 0.0 and vt.selected == vt.index_of_iid(last)
    vt._double_click(SimpleNamespace(x=x + 5, y=y + h // 2))   # (Tk won't make a double-click up)
    assert chosen == [vt.index_of_iid(last)]


def test_hover_and_playing_marks_follow_source_rows(root):
    vt = make(root, Numbers(500), playing_column="title")
    vt.set_hover(1)
    assert vt.tree.item("r1", "tags")[0] == "hover"
    vt.set_playing(2)
    assert "playing" in vt.tree.item("r2", "tags") and vt.tree.set("r2", "title") == "♪"
    vt.scroll_to(200)
    assert not vt.tree.exists("r2")
    vt.scroll_to(0)
    assert vt.tree.set("r2", "title") == "♪" and vt.tree.item("r1", "tags")[0] == "hover"
    vt.set_hover(None)
    assert "hover" not in vt.tree.item("r1", "tags")


def test_type_to_jump(root):
    names = ["Abba", "Adele", "Beck", "Björk", "Blur", "Cream"] + [f"Zed {n}" for n in range(200)]
    source = ListSource([(f"a{i}", (name, ""), ()) for i, name in enumerate(names)])

    def jump(typed):
        return next((i for i, name in enumerate(names) if name.lower().startswith(typed.lower())), None)

    vt = make(root, source, on_jump=jump)
    key(vt, "<KeyPress-b>")
    key(vt, "<KeyPress-l>")
    assert vt.selected == 4                              # "bl": Blur
    vt._typed_at -= 5                                    # (a pause: a new word)
    key(vt, "<KeyPress-z>")
    assert vt.selected == 6 and vt.tree.selection() == ("a6",)


def test_groups_fold_through_the_owner(root):
    closed = set()
    groups = {"Pop": ["21", "Hits"], "Rock": ["Tommy"]}

    def rows():
        found = []
        for g, (name, albums) in enumerate(groups.items()):
            found.append((f"g{g}", (name, ""), ("group",), CLOSED_MARK if name in closed else OPEN_MARK))
            if name not in closed:
                found += [(f"g{g}a{n}", (album, ""), ()) for n, album in enumerate(albums)]
        return found

    def toggle(index):
        name = vt.source.row(index)[1][0]
        (closed.discard if name in closed else closed.add)(name)
        vt.set_source(ListSource(rows()), keep=True)

    vt = make(root, ListSource(rows()), show="tree headings", on_toggle=toggle)
    vt.tree.column("#0", width=theme.px(22), stretch=False)   # (the arrow's column, as the Library's)
    assert vt.tree.get_children() == ("g0", "g0a0", "g0a1", "g1", "g1a0")
    vt.select(0)
    key(vt, "<KeyPress-Left>")                           # ← on an open group: fold it
    assert vt.tree.get_children() == ("g0", "g1", "g1a0") and vt.selected == 0
    key(vt, "<KeyPress-Right>")
    assert vt.tree.get_children() == ("g0", "g0a0", "g0a1", "g1", "g1a0")
    vt.select(2)
    key(vt, "<KeyPress-Left>")                           # ← inside a group: to its heading
    assert vt.selected == 0
    x, y, _w, h = vt.tree.bbox("g1")
    vt._double_click(SimpleNamespace(x=theme.px(60), y=y + h // 2))
    assert vt.tree.get_children() == ("g0", "g0a0", "g0a1", "g1")


def test_text_that_does_not_fit_ends_with_an_ellipsis(root):
    long = "A very long album name that will never fit in a narrow column at all"
    vt = make(root, ListSource([("a0", (long, "Artist"), ())]))
    vt.tree.column("title", width=theme.px(120), stretch=False)
    vt.refresh()
    shown = vt.tree.set("a0", "title")
    assert shown.endswith(ELLIPSIS) and long.startswith(shown[:-1].rstrip())
    assert vt._font.measure(shown) <= theme.px(120)
    assert vt.tree.set("a0", "artist") == "Artist"       # a short one is left whole


def test_rows_drag_by_key_and_keep_their_selection_across_a_new_source(root):
    rows = [(f"k{i}", (f"Song {i}", ""), ()) for i in range(30)]
    vt = make(root, ListSource(rows))
    dragged = []
    TreeDrag(vt.tree, lambda iid: vt.index_of_iid(iid), lambda carried, x, y, phase: dragged.append((carried, phase)))
    x, y, _w, h = vt.tree.bbox("k3")
    vt.tree.event_generate("<ButtonPress-1>", x=x + 5, y=y + 2, rootx=0, rooty=0)
    vt.tree.event_generate("<B1-Motion>", x=x + 5, y=y + 60, rootx=0, rooty=60)
    vt.tree.event_generate("<ButtonRelease-1>", x=x + 5, y=y + 60, rootx=0, rooty=60)
    assert dragged and dragged[0] == (3, "move") and dragged[-1] == (3, "drop")
    vt.select(5)
    vt.set_source(ListSource(rows[2:]), keep=True)       # two rows went above it: the same row stays selected
    assert vt.selected == 3 and vt.tree.selection() == ("k5",)
    vt.set_source(ListSource(rows))                      # a new list: from the top, nothing selected
    assert vt.selected is None and vt.top == 0


@pytest.mark.perf
def test_a_step_through_a_big_list_is_quick(root):
    """(perf) Moving one row through 200,000, a page at a time and by the scrollbar: each refill
    of the rows on show well under 5 ms (the median), the window never waiting on the list."""
    import statistics
    import time

    vt = make(root, Numbers())
    times = []
    for top in list(range(0, 400)) + list(range(100_000, 100_400, 7)) + list(range(0, 200_000, 4_999)):
        started = time.perf_counter()
        vt.scroll_to(top)
        times.append(time.perf_counter() - started)
    median = statistics.median(times)
    print(f"\nrefill median {median * 1000:.2f} ms, worst {max(times) * 1000:.2f} ms, {vt.page()} rows")
    assert median < 0.005


def test_fitted_text_really_fits_and_is_measured_without_asking_tk_for_every_cell(root):
    """"…" is fitted from each character's width (asked of Tk once): a page of long names used to
    take hundreds of whole-text measures — tens of milliseconds a scroll step. What's shown must
    still fit by Tk's own measure (kerning and fallback fonts only make the real text narrower),
    and not be cut much shorter than it need be."""
    import random

    words = ("Ångström Motörhead WAVE LTA AVA Tea ♥ café naïve Œuvre Guðmundsdóttir 東京 Привет — · "
             "24/96 ffi fl Yes To Wa Łódź Straße AC/DC Guns N' Roses Beyoncé Sigur Rós jalapeño").split()
    rng = random.Random(7)
    texts = [" ".join(rng.choice(words) for _ in range(rng.randint(1, 14))) for _ in range(600)]
    vt = make(root, ListSource([(f"r{i}", (text, text[::-1]), ()) for i, text in enumerate(texts)]))
    vt.tree.column("title", width=theme.px(150), stretch=False)
    vt.tree.column("artist", width=theme.px(90), stretch=False)
    vt.refresh()
    font = vt._font
    measured = []
    real_measure = font.measure
    font.measure = lambda text, *args: measured.append(text) or real_measure(text, *args)
    shown = []
    try:
        for top in range(0, len(texts), vt.page()):
            vt.scroll_to(top)
            for iid in vt.tree.get_children():
                index = vt.index_of_iid(iid)
                shown += [(texts[index], vt.tree.set(iid, "title"), "title"),
                          (texts[index][::-1], vt.tree.set(iid, "artist"), "artist")]
    finally:
        del font.measure
    distinct = set("".join(texts)) | {ELLIPSIS}
    assert all(len(text) == 1 for text in measured) and len(measured) <= len(distinct)   # (each character once)
    cut = 0
    for text, fitted, column in shown:
        room = int(vt.tree.column(column, "width")) - theme.px(10)
        assert real_measure(fitted) <= room, (fitted, room)
        if fitted != text:
            cut += 1
            kept = fitted[:-1]
            assert fitted.endswith(ELLIPSIS) and text.startswith(kept)
            more = len(kept) + len(text[len(kept):]) - len(text[len(kept):].lstrip()) + 1
            assert real_measure(text[:more] + ELLIPSIS) > room - theme.px(6)   # (one more letter wouldn't fit)
    assert cut > 100


def test_right_on_an_ordinary_row_goes_on_to_the_window(root):
    """→ folds open a group heading; on any other row it's the window's key (the keyboard table's
    seek), as with Tk's own list."""
    rows = [("g0", ("Pop", ""), ("group",), OPEN_MARK), ("g0a0", ("21", ""), ()), ("g0a1", ("Hits", ""), ())]
    vt = make(root, ListSource(rows), show="tree headings", on_toggle=lambda index: None)
    went_on = []
    root.bind("<Key-Right>", lambda e: went_on.append("right"))
    root.bind("<Key-Left>", lambda e: went_on.append("left"))
    vt.select(1)
    key(vt, "<KeyPress-Right>")
    assert went_on == ["right"] and vt.selected == 1
    key(vt, "<KeyPress-Left>")                           # ← inside a group: to its heading (the list's own)
    assert went_on == ["right"] and vt.selected == 0


def test_a_column_dragged_wider_is_fitted_again(root):
    long = "A very long album name that will never fit in a narrow column at all"
    vt = make(root, ListSource([("a0", (long, "Artist"), ())]))
    vt.tree.column("title", width=theme.px(120), stretch=False)
    vt.refresh()
    assert vt.tree.set("a0", "title").endswith(ELLIPSIS)
    vt.tree.column("title", width=theme.px(600))         # (the heading's edge dragged, then let go)
    vt.tree.event_generate("<ButtonRelease-1>", x=theme.px(120), y=2)
    root.update()
    assert vt.tree.set("a0", "title") == long


# -- several rows selected -------------------------------------------------------------------------------
def test_ranges_hold_a_selection_as_runs():
    from ironbark.virtual_list import Ranges

    chosen = Ranges([(0, 200_000)])
    assert len(chosen) == 200_000 and 199_999 in chosen and 200_000 not in chosen and chosen.runs == [(0, 200_000)]
    chosen.discard(10, 20)
    assert chosen.runs == [(0, 10), (20, 200_000)] and 15 not in chosen and 20 in chosen
    chosen.add(5, 25)
    assert chosen.runs == [(0, 200_000)]
    picked = Ranges.of([7, 3, 4, 5, 9])
    assert picked.runs == [(3, 6), (7, 8), (9, 10)] and list(picked) == [3, 4, 5, 7, 9]
    assert picked.clipped(8).runs == [(3, 6), (7, 8)] and Ranges().first() is None


def _click(vt, iid, **modifiers):
    """A press and release on a row (state: Shift 0x1, Ctrl 0x4)."""
    state = (0x1 if modifiers.get("shift") else 0) | (0x4 if modifiers.get("ctrl") else 0)
    x, y, _w, h = vt.tree.bbox(iid)
    vt.tree.event_generate("<ButtonPress-1>", x=x + 5, y=y + h // 2, state=state)
    vt.tree.event_generate("<ButtonRelease-1>", x=x + 5, y=y + h // 2, state=state | 0x100)
    vt.tree.update()


def test_shift_and_ctrl_clicks_and_keys_select_several_rows(root):
    picked = []
    vt = make(root, Numbers(200_000), multiple=True, on_select=picked.append)
    _click(vt, "r2")
    assert vt.selected_indices() == [2] and vt.tree.selection() == ("r2",)
    _click(vt, "r5", shift=True)                         # Shift: from the row clicked first
    assert vt.selected_indices() == [2, 3, 4, 5] and set(vt.tree.selection()) == {"r2", "r3", "r4", "r5"}
    _click(vt, "r3", ctrl=True)                          # Ctrl: one row out…
    assert vt.selected_indices() == [2, 4, 5] and vt.selected == 3
    _click(vt, "r7", ctrl=True)                          # …and one in
    assert vt.selected_indices() == [2, 4, 5, 7]
    _click(vt, "r4")                                     # a plain click inside: just that row (no drag)
    assert vt.selected_indices() == [4]
    key(vt, "<Shift-KeyPress-Down>")
    key(vt, "<Shift-KeyPress-Down>")
    assert vt.selected_indices() == [4, 5, 6] and vt.selected == 6
    key(vt, "<Shift-KeyPress-Up>")
    assert vt.selected_indices() == [4, 5]
    key(vt, "<Shift-KeyPress-End>")                      # to the end of 200,000: one run, not a row each
    assert vt.selected_count() == 200_000 - 4 and vt.chosen.runs == [(4, 200_000)] and vt.top > 100_000
    key(vt, "<KeyPress-Home>")                           # a plain key: one row again
    assert vt.selected_indices() == [0] and vt.top == 0
    assert vt.select_all() and vt.selected_count() == 200_000 and len(vt.tree.selection()) == vt.page()
    vt.scroll_to(150_000)                                # every row on show is painted selected
    assert len(vt.tree.selection()) == len(vt.tree.get_children()) and picked


def test_pressing_on_a_selection_keeps_it_for_a_drag(root):
    rows = [(f"k{i}", (f"Song {i}", ""), ()) for i in range(30)]
    vt = make(root, ListSource(rows), multiple=True)
    dragged = []
    TreeDrag(vt.tree, lambda iid: vt.selected_indices() if vt.is_selected(vt.index_of_iid(iid)) else None,
             lambda carried, x, y, phase: dragged.append((list(carried), phase)))
    vt.select_indices([2, 3, 4])
    x, y, _w, h = vt.tree.bbox("k3")
    vt.tree.event_generate("<ButtonPress-1>", x=x + 5, y=y + 2, rootx=0, rooty=0)
    vt.tree.event_generate("<B1-Motion>", x=x + 5, y=y + 60, rootx=0, rooty=60, state=0x100)
    vt.tree.event_generate("<ButtonRelease-1>", x=x + 5, y=y + 60, rootx=0, rooty=60, state=0x100)
    assert dragged[-1] == ([2, 3, 4], "drop") and vt.selected_indices() == [2, 3, 4]   # dragged: still all three


def test_a_selection_follows_its_rows_into_a_changed_list_and_tk_changes_are_taken_in(root):
    rows = [(f"k{i}", (f"Song {i}", ""), ()) for i in range(30)]
    vt = make(root, ListSource(rows), multiple=True)
    vt.select_indices([5, 6, 9], cursor=6)
    vt.set_source(ListSource(rows[3:]), keep=True)       # three rows went above them
    assert vt.selected_indices() == [2, 3, 6] and vt.selected == 3
    vt.tree.selection_set(("k8",))                       # a script sets Tk's selection itself
    vt.sync()
    assert vt.selected_indices() == [5]
    vt.tree.selection_remove(*vt.tree.selection())
    vt.sync()
    assert vt.selected_indices() == [] and vt.selected is None


def test_a_list_not_laid_out_yet_still_holds_a_screenful(root):
    vt = VirtualTree(root, ("title", "artist"))          # (never packed: no height)
    vt.set_source(Numbers(1000))
    assert len(vt.tree.get_children()) == vt.page() > 10


def test_a_new_source_lets_go_of_the_row_lit_under_the_pointer(root):
    rows = [(f"k{i}", (f"Song {i}", ""), ()) for i in range(30)]
    vt = make(root, ListSource(rows))
    vt.set_hover(2)
    assert "hover" in vt.tree.item("k2", "tags")
    vt.set_source(ListSource(rows), keep=True)            # the same rows again (drawn unchanged)
    assert "hover" not in vt.tree.item("k2", "tags") and vt.hovered is None
    vt.set_hover(5)
    assert [iid for iid in vt.tree.get_children() if "hover" in vt.tree.item(iid, "tags")] == ["k5"]

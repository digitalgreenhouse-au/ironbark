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

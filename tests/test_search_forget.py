"""(0.54.0) The search box forgets an old search: words left in the track list's or the Library's box
for two minutes without being used — typed in, or the results clicked, scrolled or keyed in — are
emptied, and the list shows everything again. Tk smoke tests, skipped without a display."""
import gc
import time
import tkinter as tk
from pathlib import Path

import pytest
from test_library_view import music, wait_for_albums  # noqa: F401  (the Library's fixture)

from ironbark import controls, theme
from ironbark.library import Track
from ironbark.library_view import LibraryView
from ironbark.playlist_view import PlaylistView


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    root.withdraw()
    theme.apply(root)
    yield root
    root.destroy()
    del root
    gc.collect()  # on this thread (Tk objects freed on another thread abort Python)


def wait_until(root, done, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not done() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    return done()


def test_two_minutes_is_the_time(root):
    assert controls.FORGET_SEARCH_MS == 120_000
    box = controls.SearchBox(root, "Search")
    assert box.forget_after is None   # (off unless asked for: the Keys window's filter keeps its words)


def test_words_left_alone_are_emptied(root):
    box = controls.SearchBox(root, "Search", forget_after=60)
    box.pack()
    box.query.set("adele")
    assert box._forget_job is not None
    assert wait_until(root, lambda: box.query.get() == "")
    assert box._forget_job is None


def test_an_empty_box_has_nothing_to_forget(root):
    box = controls.SearchBox(root, "Search", forget_after=60)
    assert box._forget_job is None
    box.query.set("a")
    box.query.set("")
    assert box._forget_job is None


def test_typing_and_using_the_results_start_the_time_again(root):
    root.deiconify()   # (Tk drops made-up clicks on a window that isn't shown)
    box = controls.SearchBox(root, "Search", forget_after=60_000)
    box.pack()
    results = tk.Listbox(root)
    results.pack()
    results.bind("<Button-1>", lambda e: "break")   # (the list's own click: must not hide the tag's)
    box.keep_alive(results)
    root.update()
    box.query.set("ad")
    first = box._forget_job
    box.query.set("adele")                          # typing
    second = box._forget_job
    assert second is not None and second != first
    results.event_generate("<ButtonPress-1>", x=5, y=5)   # a click in the results
    assert box._forget_job is not None and box._forget_job != second
    third = box._forget_job
    results.event_generate("<MouseWheel>", delta=-120, x=5, y=5)   # the wheel
    assert box._forget_job != third
    assert box.query.get() == "adele"


def test_the_focus_stays_where_it_is(root):
    """Forgetting empties the words only: unlike the ×, it doesn't move the focus."""
    root.deiconify()
    view = PlaylistView(root, on_play=lambda i: None)
    view.search.forget_after = 60
    other = tk.Entry(root)
    other.pack()
    view.set_tracks([Track(Path("A") / "01 Accordion.flac", "Accordion", artist="Madvillain")])
    view.query.set("madv")
    other.focus_force()
    root.update()
    assert wait_until(root, lambda: view.query.get() == "")
    assert root.focus_get() is other


def test_the_track_list_lists_every_song_again(root):
    view = PlaylistView(root, on_play=lambda i: None)
    assert view.search.forget_after == controls.FORGET_SEARCH_MS
    view.search.forget_after = 60
    tracks = [Track(Path("A") / "01 Accordion.flac", "Accordion", artist="Madvillain", track_no=1),
              Track(Path("B") / "01 Rolling.flac", "Rolling in the Deep", artist="Adele", track_no=1)]
    view.set_tracks(tracks)
    view.query.set("adele")
    view._render()
    assert [r for r in view.tree.get_children() if r.isdigit()] == ["1"]
    assert wait_until(root, lambda: view.query.get() == "")
    view._render()
    assert [r for r in view.tree.get_children() if r.isdigit()] == ["0", "1"]


def test_the_library_forgets_too(root, music):  # noqa: F811
    w = LibraryView(root, music, None, lambda p: None, lambda p: None, lambda r, q: None)
    try:
        wait_for_albums(w, root)
        assert w.search.forget_after == controls.FORGET_SEARCH_MS
        assert w.search._alive_tag in w.tree.bindtags()   # (going through its albums keeps the search)
        w.search.forget_after = 60
        w.search_var.set("tommy")
        assert wait_until(root, lambda: w.search_var.get() == "")
    finally:
        w.close()


def test_closing_lets_go_of_the_timer_and_its_tag(root):
    box = controls.SearchBox(root, "Search", forget_after=60_000)
    box.keep_alive(tk.Listbox(root))
    tag = box._alive_tag
    box.query.set("adele")
    assert box._forget_job is not None and box.bind_class(tag)
    box.destroy()
    assert box._forget_job is None and not root.bind_class(tag)

"""The pieces of 0.33.0 working together: several songs selected in a wide window's docked Up next and
in the library search's results, the Cached column on the virtual track list (a whole-library tab
included, without reading every song), and the Whole library choice with a big tab's search on its
way. Tk tests run on the hidden desktop (conftest) and skip without a display."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skip(reason="the wide window is unreachable since 0.34.0 (the window is a side"
                              " player, its width capped under layout.SPLIT_MIN); these tests go with the"
                              " wide layout's removal")

from ironbark import keys, library_tracks, popups
from ironbark.library import Track
from ironbark.library_db import LibraryDB
from ironbark.playlist_view import PlaylistView
from test_big_lists import _library, _wait, root, synth_db  # noqa: F401  (root, synth_db are fixtures)
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)
from test_quick_search import panel, press
from test_wide_window import _open


def test_a_widget_of_its_own_binds_an_actions_keys_from_the_table():
    assert keys.sequences("select_all") == ["<Control-Key-a>", "<Control-Key-A>"]
    assert keys.sequences("remove") == ["<Key-Delete>"]
    assert keys.sequences("list_play_next") == ["<Control-Shift-Key-Return>"]


# -- the docked Up next ------------------------------------------------------------------------------------
def _songs(*titles: str) -> list[Track]:
    return [Track(Path(f"C:/Music/Up/{n:02d} {title}.flac"), title, artist="Up", duration=100.0)
            for n, title in enumerate(titles, 1)]


def test_docked_up_next_selects_several_and_takes_them_out_with_one_undo(make_window, monkeypatch):
    window, root, player, errors = _open(make_window)
    window._queue_tracks(_songs("Alpha", "Bravo", "Charlie", "Delta"), False)
    window.playlist.view_switch.choose("up_next")
    _pump(root, 0.4)
    dock = window._up_next_view
    tree = dock.tree
    assert str(tree.cget("selectmode")) == "extended"
    # Shift+↓ grows the selection from the focus row, Shift+↑ shrinks it; Ctrl+A takes them all.
    tree.selection_set("0")
    tree.focus("0")
    press(tree, "<Shift-Key-Down>")
    press(tree, "<Shift-Key-Down>")
    assert tree.selection() == ("0", "1", "2")
    press(tree, "<Shift-Key-Up>")
    assert tree.selection() == ("0", "1")
    press(tree, "<Control-Key-a>")
    assert tree.selection() == ("0", "1", "2", "3")
    # A right-click inside a selection of several keeps it, and its menu takes them all out.
    shown = []
    monkeypatch.setattr(popups.PopupMenu, "popup", lambda self, x, y: shown.append(self))
    tree.selection_set(("1", "3"))
    x, y, _w, h = tree.bbox("3")
    dock._context(SimpleNamespace(x=x + 5, y=y + h // 2, x_root=0, y_root=0))
    assert tree.selection() == ("1", "3")
    assert shown[-1].labels() == ["Play now", "Take these 2 songs out of Up next"]
    dock.remove_selected()                                        # Delete
    _pump(root, 0.3)
    assert [t.title for t in player.up_next()] == ["Alpha", "Charlie"]
    assert window._notice == "Took 2 songs out of Up next"
    assert tree.get_children() == ("0", "1")
    window.undo()                                                 # Ctrl+Z: back where they were
    _pump(root, 0.3)
    assert [t.title for t in player.up_next()] == ["Alpha", "Bravo", "Charlie", "Delta"]
    assert window._notice == "Put 2 songs back in Up next"
    tree.selection_set("2")
    dock.remove_selected()
    _pump(root, 0.3)
    assert window._notice == "Took “Charlie” out of Up next"
    window._queue_tracks(_songs("Echo"), False)                   # Up next changed since: no undo
    window.undo()
    assert [t.title for t in player.up_next()] == ["Alpha", "Bravo", "Delta", "Echo"]
    assert errors == []


# -- the library search's results ---------------------------------------------------------------------
def test_search_results_take_several_selected_songs(root, monkeypatch):
    shown = []
    monkeypatch.setattr(popups.PopupMenu, "popup", lambda self, x, y: shown.append(self))
    calls = []
    qs, _db = panel(root, calls, playlists=lambda: [])
    qs.open("adele")
    assert qs.wait()
    root.update()
    assert qs.tree.get_children()[:3] == ("s:songs", "t1", "t2")
    qs.list.select_indices([1, 2], cursor=2)                      # Shift+click: both songs
    qs._act("queue")                                              # Ctrl+Enter
    assert calls[-1] == ("queue", ["Rolling in the Deep", "Rumour Has It"], False)
    assert "Added 2 songs to Up next" in qs.status.cget("text")
    assert [t.title for t in qs._carried("t1")] == ["Rolling in the Deep", "Rumour Has It"]
    x, y, _w, h = qs.tree.bbox("t1")
    qs._context(SimpleNamespace(x=x + 5, y=y + h // 2, x_root=0, y_root=0))
    assert qs.list.selected_count() == 2 and "Add these 2 songs to playlist" in shown[-1].labels()
    shown[-1].menu.find("Play next").command()
    assert calls[-1] == ("queue", ["Rolling in the Deep", "Rumour Has It"], True)
    qs.list.select(2)                                             # one song again: just it
    press(qs.tree, "<Shift-KeyPress-Return>")
    assert calls[-1] == ("queue", ["Rumour Has It"], True)
    assert qs._carried("t1") == [qs.items[1][1].track]
    qs.close()


# -- the Cached column on the virtual track list ------------------------------------------------------
def test_a_whole_library_tabs_cached_column_reads_no_more_songs(root, synth_db):
    from ironbark.ui import _paths_to_classify

    path, _files = synth_db
    with LibraryDB.open(path) as db:
        tracks = library_tracks.build(db, db.albums())
        view = PlaylistView(root, on_play=lambda i: None)
        view.set_tracks(tracks)
        view.set_cache_column(True)
        root.update()
        assert _wait(root, lambda: all(not view.tree.set(iid, "title").startswith("…")
                                       for iid in view.tree.get_children()))
        assert [str(c) for c in view.tree.cget("displaycolumns")][-2:] == ["cache", "time"]
        shown = view.visible_tracks()
        assert shown and all(tracks.peek(i) is track for i, track in shown)
        misses = tracks.misses
        view.set_marks({i: "○" for i, _track in shown})
        root.update()
        assert view.tree.set(str(shown[0][0]), "cache") == "○"
        paths = list(_paths_to_classify(tracks))                  # whether the tab has network songs
        assert len(paths) <= len(tracks.groups) + len(tracks.loaded()) < len(tracks)
        assert str(paths[0]).endswith(("\\", "/"))                 # (an album's folder)
        assert tracks.misses == misses
        view.set_tracks([])                                       # a new list: its marks start afresh
        assert view._marks == {}
        view.tree.destroy()
        view._worker.join(5)
        assert not tracks.waiting() or _wait(root, lambda: not tracks.waiting())


def test_a_plain_lists_paths_are_all_classified():
    from ironbark.ui import _paths_to_classify

    songs = _songs("One", "Two")
    assert list(_paths_to_classify(songs)) == [song.path for song in songs]


def test_whole_library_drops_a_big_tabs_search_on_its_way(root):
    view = PlaylistView(root, on_play=lambda i: None)
    view.set_tracks(_library(400, 50))                            # 20,000 songs: searched on the list's thread
    view.query.set("song 3")
    view._search_now()
    assert view._asked["search"] is not None
    view.set_scope("library")
    assert view._asked["search"] is None
    _pump(root, 1.0)
    assert view._found is None and view._words == []              # the tab isn't filtered behind the results
    view.set_scope("tab")                                         # back: the tab is filtered by the box
    assert _wait(root, lambda: view._words == ["song", "3"])
    view.tree.destroy()
    view._worker.join(5)

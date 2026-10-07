"""Big lists: the track list on a VirtualTree (only the rows on show are Tk's), several songs selected
(Shift / Ctrl+click, Shift+arrows, Ctrl+A) and every song action taking them all, the '/' key, the
Plays column read for the rows on show off Tk's thread, and the Library's song views selecting several
songs. Tk tests run on the hidden desktop (conftest) and skip without a display."""
from __future__ import annotations

import gc
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ironbark import keys, playlist_view, playlists, theme, virtual_list
from ironbark.library import Track
from ironbark.library_db import LibraryDB

from test_keys import _focus, _resolved, _run
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.geometry("520x620+40+40")
    theme.apply(r)
    errors = []
    r.report_callback_exception = lambda *info: errors.append(info)
    r.errors = errors
    r.update()
    yield r
    r.destroy()
    del r
    gc.collect()   # on this thread (Tk objects freed on another thread abort Python)


def _library(albums: int, songs: int, root: str = "C:/Music") -> list[Track]:
    return [Track(Path(f"{root}/Artist {a % 13}/Album {a}/{t:02d}.flac"), f"Song {a} {t}",
                  artist=f"Artist {a % 13}", album=f"Album {a}", track_no=t + 1, duration=180.0,
                  sample_rate=44100, bits=16) for a in range(albums) for t in range(songs)]


def _wait(root, until, seconds: float = 10.0):
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    return until()


# -- the track list as a virtual list -----------------------------------------------------------------
def test_a_whole_library_tab_holds_only_the_rows_on_show(root):
    tracks = _library(2_000, 10)                          # 20,000 songs, 2,000 album headers
    view = playlist_view.PlaylistView(root, on_play=lambda i: None)
    started = time.perf_counter()
    view.set_tracks(tracks)
    root.update()
    took = time.perf_counter() - started
    page = view.list.page()
    shown = view.tree.get_children()
    assert len(shown) <= page and shown[:3] == ("a0", "0", "1") and len(view.list.source) == 22_000
    assert took < 2.0
    view.show_current(15_005)                            # the playing song, far down: scrolled to, marked
    assert view.tree.set("15005", "no") == playlist_view.PLAYING_MARK and view.list.top > 15_000
    assert view.selected_track() == 15_005
    view.list.scroll_to(0)
    view.refresh_titles()                                # (a ♥ came or went: only the rows on show are drawn)
    assert len(view.tree.get_children()) <= page


def test_a_big_list_is_searched_off_tk_and_enter_straight_away_waits_for_nothing(root):
    tracks = _library(2_500, 10)                          # 25,000: searched on the list's own thread
    played = []
    view = playlist_view.PlaylistView(root, on_play=played.append)
    view.set_tracks(tracks)
    view.query.set("song 1234 5")
    view._search_now()                                   # (the pause in typing came)
    assert view._asked["search"] is not None              # on its way: the list is still the old one
    assert _wait(root, lambda: view._asked["search"] is None)
    assert view._songs == ["12345"]
    view.query.set("song 1")
    view._play_first_result()                            # Enter before the pause: searched here and now
    assert played == [1]                                  # (Song 0 1: the first song "song 1" finds)


def test_ctrl_a_shift_and_ctrl_select_songs_and_headers_stand_for_their_albums(root):
    tracks = _library(3, 4)
    view = playlist_view.PlaylistView(root, on_play=lambda i: None)
    view.set_tracks(tracks)
    root.update()
    assert view.select_all() and view.selected_rows() == list(range(12)) and view.selected_count() == 15
    view.select(1)
    view.list.extend_to(view._row_of(5))                 # Shift: songs 1 – 5 and album 1's header between
    assert view.selected_rows() == [1, 2, 3, 4, 5, 6, 7]  # (a header row brings its whole album)
    view.select(1)
    view.list.toggle(view._row_of(9))                    # Ctrl
    assert view.selected_rows() == [1, 9] and view.selected_track() == 9
    view.query.set("album 2")
    view._render()                                        # a search keeps the selected songs it still lists
    assert view._songs == ["2", "6", "8", "9", "10", "11"] and view.selected_rows() == [9]

    view.query.set("")
    view._render()
    assert view.select_all() and view.selected_rows() == list(range(12))


def test_a_drag_from_a_selection_carries_every_song_selected(root):
    tracks = _library(1, 8)
    dragged = []
    view = playlist_view.PlaylistView(root, on_play=lambda i: None,
                                      on_drag=lambda rows, x, y, phase: dragged.append((list(rows), phase)))
    view.set_tracks(tracks, flat=True)
    root.update()
    view.select_tracks([1, 2, 5])
    x, y, _w, h = view.tree.bbox("2")
    view.tree.event_generate("<ButtonPress-1>", x=x + 40, y=y + 2, rootx=0, rooty=0)
    view.tree.event_generate("<B1-Motion>", x=x + 40, y=y + 60, rootx=0, rooty=60, state=0x100)
    view.tree.event_generate("<ButtonRelease-1>", x=x + 40, y=y + 60, rootx=0, rooty=60, state=0x100)
    assert dragged[-1] == ([1, 2, 5], "drop")


def test_the_plays_column_reads_counts_for_the_rows_on_show_off_tk(root, tmp_path, tk_thread_rule):
    tracks = _library(40, 10)
    with LibraryDB.open(tmp_path / "library.db") as db:
        for _ in range(3):
            db.record_play(str(tracks[1].path), 100.0, 180.0)
        db.record_play(str(tracks[2].path), 100.0, 180.0)
        asked = []
        real = playlist_view._play_counts
        view = playlist_view.PlaylistView(root, on_play=lambda i: None, plays_db=lambda: db)
        playlist_view._play_counts = lambda db, keys: asked.append(len(keys)) or real(db, keys)
        try:
            view.set_columns(("plays",))
            view.set_tracks(tracks)
            root.update()
            assert _wait(root, lambda: view.tree.set("1", "plays") == "3")
            assert view.tree.set("2", "plays") == "1" and view.tree.set("0", "plays") == ""
            assert sum(asked) <= virtual_list.UNSIZED_PAGE + 2 * view.list.page()   # only rows drawn were asked for
            db.record_play(str(tracks[2].path), 100.0, 180.0)
            view.plays_changed()
            assert _wait(root, lambda: view.tree.set("2", "plays") == "2")
        finally:
            playlist_view._play_counts = real
        view.tree.destroy()
        view._worker.join(5)


# -- the main window -----------------------------------------------------------------------------------
def _playlist_window(make_window, tmp_path, count: int = 6):
    from ironbark.ui import Tab

    window, root, player, errors = make_window()
    songs = [Track(tmp_path / f"{n}.flac", f"Song {n}", duration=60.0) for n in range(1, count + 1)]
    path = tmp_path / "Mix.m3u8"
    playlists.write(path, songs)
    tab = Tab("playlist", str(path), tracks=list(songs))
    window.tabs.append(tab)
    window.select_tab(len(window.tabs) - 1)
    _pump(root, 0.1)
    return window, root, player, errors, tab, songs


def test_ctrl_a_delete_and_one_undo_puts_them_all_back(make_window, tmp_path):
    window, root, player, errors, tab, songs = _playlist_window(make_window, tmp_path)
    tree = window.playlist.tree
    assert _resolved(window, tree, "Ctrl+A") == "select_all"
    window.playlist.select_tracks([1, 3, 4])
    assert _run(window, tree, "Delete")
    assert tab.tracks == [songs[0], songs[2], songs[5]] and window._notice == "Removed 3 songs from “Mix”"
    assert _run(window, root, "Ctrl+Z")
    assert tab.tracks == songs and window._notice == "Put 3 songs back"
    assert window.playlist.selected_rows() == [1, 3, 4]   # (selected again, as they were)
    assert _run(window, tree, "Ctrl+A") and window.playlist.selected_rows() == list(range(6))
    assert _run(window, tree, "Ctrl+Enter")               # every one of them into Up next
    assert player.up_next() == songs
    window.playlist.select_tracks([0, 1])
    assert _run(window, tree, "Alt+Down")                 # moving is for one song: nothing moves
    assert tab.tracks == songs
    assert not errors


def test_the_song_menu_offers_the_selection(make_window, tmp_path, monkeypatch):
    from ironbark import popups

    window, root, player, errors, tab, songs = _playlist_window(make_window, tmp_path)
    shown = []
    original = popups.PopupMenu.popup
    monkeypatch.setattr(popups.PopupMenu, "popup", lambda self, x, y: shown.append(self) or original(self, x, y))
    window.playlist.select_tracks([0, 2])
    tree = window.playlist.tree
    x, y, _w, h = tree.bbox("2")
    event = SimpleNamespace(x=x + 30, y=y + h // 2, x_root=root.winfo_rootx() + 40, y_root=root.winfo_rooty() + 200)
    window.playlist._context(event)                       # (a right-click inside the selection keeps it)
    labels = shown[-1].labels()
    assert labels[0] == "Play" and "Add these 2 songs to playlist" in labels
    assert "Remove these 2 songs from playlist" in labels
    shown[-1].invoke("Add to favourites")
    assert songs[0] in window.favourites and songs[2] in window.favourites and songs[1] not in window.favourites
    window.playlist._context(event)
    shown[-1].invoke("Remove these 2 songs from playlist")

    assert tab.tracks == [songs[1], songs[3], songs[4], songs[5]]
    assert not errors


def test_slash_goes_to_the_search_box(make_window):
    window, root, player, errors = make_window()
    tree = window.playlist.tree
    assert _resolved(window, tree, "/") == "search_tab"
    _focus(root, tree)
    tree.event_generate("<KeyPress-slash>")
    _pump(root, 0.1)
    assert root.focus_get() is window.playlist.entry
    window.playlist.entry.event_generate("<KeyPress-slash>")   # typing in the box: a /, nothing else
    _pump(root, 0.05)
    assert window.playlist.query.get() == "/" and keys.BY_ID["search_tab"].keys == ("Ctrl+F", "/")
    assert not errors


# -- the Library's song views --------------------------------------------------------------------------
def test_library_song_views_select_several_and_act_on_them_all(root, tmp_path, monkeypatch, tk_thread_rule):
    from ironbark import library_index, settings
    from ironbark.library_window import LibraryWindow
    from ironbark.quick_search import Actions

    from test_library_window import flac, wait_for_index

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    music = tmp_path / "Music"
    for a in ("A", "B"):
        for n in (1, 2, 3):
            flac(music / a / f"0{n}.flac", title=f"{a}{n}", artist="X", album=a, tracknumber=str(n))
    queued, added = [], []
    actions = Actions(queue_tracks=lambda tracks, front: queued.append(([t.title for t in tracks], front)))
    w = LibraryWindow(root, music, None, lambda p: None, lambda p: None, lambda r, q: None, view="songs",
                      actions=actions, on_add_tracks=lambda tracks: added.append([t.title for t in tracks]) or "",
                      song_columns=("plays", "last"))
    wait_for_index(w, root)
    assert _wait(root, lambda: len(w.list.source) == 6 and not w.tree.set(w.tree.get_children()[0], "title")
                 .startswith("…"))                        # (a song view opened before the first look ended)


    assert "plays" in w.tree.cget("displaycolumns") and "last" in w.tree.cget("displaycolumns")
    assert w.select_all() and w.list.selected_count() == 6
    assert w.queue_selected(front=True)
    assert _wait(root, lambda: bool(queued)) and sorted(queued[-1][0]) == ["A1", "A2", "A3", "B1", "B2", "B3"]
    w.list.select(0)
    w.list.toggle(2)
    w._add_selected()
    assert _wait(root, lambda: bool(added)) and len(added[-1]) == 2
    w.set_view("albums")
    assert not w.list.multiple and not w.select_all()      # albums: one at a time
    w.close()
    library_index.close_shared()

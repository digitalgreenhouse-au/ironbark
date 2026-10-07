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
    assert view.selected_rows() == [1, 2, 3, 4, 5]        # (just the songs lit: not the rest of album 1)
    view.list.select(view._header_row_of(2))              # a header on its own: its album
    assert view.selected_rows() == [8, 9, 10, 11]
    view.list.toggle(view._row_of(1))                     # …and a song of another, Ctrl+clicked
    assert view.selected_rows() == [1, 8, 9, 10, 11]
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


# -- a whole-library tab without a Track held for every song --------------------------------------------
LIB_ROOT = Path("C:/Music")


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory):
    import synthlib

    folder = tmp_path_factory.mktemp("bigtab")
    files = synthlib.make_library(3_000, LIB_ROOT, seed=7)
    with LibraryDB.open(folder / "library.db", [LIB_ROOT]) as db:
        synthlib.load(db, LIB_ROOT, files)
    return folder / "library.db", files


def test_album_orders_list_songs_as_the_full_read_does(synth_db):
    from ironbark import library_tracks

    path, _files = synth_db
    with LibraryDB.open(path) as db:
        albums = db.albums()
        full = db.albums_tracks([a.path for a in albums])
        light = library_tracks.build(db, albums)
        expected = [t for a in albums for t in full.get(a.path, [])]
        assert len(light) == len(expected) and len(light.groups) == len(albums)
        assert [t.path for t in light] == [t.path for t in expected]            # play order, CUE images too
        assert light.seconds == pytest.approx(sum(t.duration for t in expected))
        assert light.varies == {"artist", "album", "quality"}


def test_a_light_tab_reads_albums_as_they_show_and_holds_the_playing_window(synth_db, monkeypatch):
    from ironbark import library_tracks, searchfold

    path, _files = synth_db
    monkeypatch.setattr(library_tracks, "KEEP", 200)
    with LibraryDB.open(path) as db:
        tracks = library_tracks.build(db, db.albums())
        assert tracks.peek(100) is None and tracks.misses == 0
        tracks.want(100)
        deadline = time.monotonic() + 10
        while tracks.peek(100) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        start = tracks.groups[tracks._group_of(100)][1]
        assert tracks.peek(100) is not None and tracks.peek(start) is not None and tracks.serial >= 1
        tracks.ready(2_000, [1_999, 2_001, 2_002])                    # the song to play, read at once
        assert tracks.peek(2_000) is not None and tracks.misses == 0
        for index in range(0, len(tracks), 97):                        # read far and wide: the LRU lets go…
            tracks[index]
        assert tracks.misses > 0 and len(tracks._shared.cache) <= library_tracks.KEEP + 60
        assert all(tracks.peek(i) is not None for i in (1_999, 2_000, 2_001, 2_002))   # …never of the held
        song = tracks[2_000]
        assert tracks.index_of_path(song.path) == 2_000
        assert tracks.index_of_key(str(song.path) if song.cue is None else f"{song.path}#{song.cue.number}") == 2_000
        words = searchfold.words("love ni")
        everything = list(tracks)
        assert tracks.matching(words) == [i for i, t in enumerate(everything)
                                          if all(w in searchfold.haystack(t) for w in words)]
        assert [t.path for t in tracks.many([5, 2_999, 7])] == [everything[5].path, everything[2_999].path,
                                                                 everything[7].path]
        extra = Track(Path("D:/Elsewhere/x.flac"), "Extra")
        more = tracks + [extra]
        assert len(more) == len(tracks) + 1 and more[len(tracks)] is extra
        assert more.index_of_path(extra.path) == len(tracks)
        assert more.keys_in([str(extra.path), "D:/nowhere.flac"]) == {str(extra.path)}
        assert len(more.groups) == len(tracks.groups) + 1


def test_the_engine_neither_copies_nor_reads_a_light_tab(synth_db):
    from ironbark import library_tracks
    from ironbark.engine import Player

    path, _files = synth_db
    with LibraryDB.open(path) as db:
        tracks = library_tracks.build(db, db.albums())
        player = Player()
        player.set_tracks(tracks)
        assert player.tracks is tracks and isinstance(player._order, range)   # no copy, no per-song order
        tracks.ready(500, player.order_around(500))
        player.index = 500
        before = tracks.misses
        for _ in range(8):                                     # the next songs, as the decoder lines them up
            following = player._next_after(player.index)
            assert player._track_at(following) is not None
            player.index = following
            tracks.hold(player.order_around(player.index))   # (the window's poll)
            tracks.ready(player.index)
        assert tracks.misses == before                         # every song the engine asked for was read
        song = tracks[player.index]
        assert player._find(tracks, song.path) == player.index
        player.shuffle = True
        player._rebuild_order(first=player.index)              # (as a song picked by hand starts a shuffle)
        assert sorted(player._order) == list(range(len(tracks))) and player._order[0] == player.index
        assert player.order_around(player.index, before=0, after=2)[0] == player.index
        plain = list(tracks)[:50]
        player.set_tracks(plain)
        assert player.tracks is plain and player._find(plain, plain[30].path) == 30
        player.set_tracks(plain[:10])                      # another list: the old one's paths go with it
        assert player._paths is None


def test_the_track_list_shows_a_light_tab_and_searches_it_in_the_database(root, synth_db):
    from ironbark import library_tracks

    path, _files = synth_db
    with LibraryDB.open(path) as db:
        tracks = library_tracks.build(db, db.albums())
        view = playlist_view.PlaylistView(root, on_play=lambda i: None)
        view.set_tracks(tracks)
        root.update()
        assert _wait(root, lambda: all(not view.tree.set(iid, "title").startswith("…")
                                       for iid in view.tree.get_children()))
        assert view.tree.get_children()[0] == "a0" and "artist" in view.varies
        view.query.set("love")
        view._render()
        everything = list(tracks)
        expected = [str(i) for i, t in enumerate(everything) if "love" in playlist_view._haystack(t)]
        assert view._songs == expected
        view.tree.destroy()
        view._worker.join(5)
        assert not tracks.waiting() or _wait(root, lambda: not tracks.waiting())


def test_open_all_albums_makes_a_light_tab_the_window_plays_and_counts(make_window, tmp_path):
    from ironbark import library_index, library_tracks

    from test_library_window import flac

    window, root, player, errors = make_window()
    music = tmp_path / "Music"
    for a in ("Bees", "Cats"):
        for n in (1, 2, 3):
            flac(music / a / f"0{n}.flac", title=f"{a} {n}", artist=a, album=a, tracknumber=str(n))
    index = window._index()
    index.refresh()
    window.open_tab("library", music)
    tab = window.tabs[-1]
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading)
    assert isinstance(tab.tracks, library_tracks.LibraryTrackList) and len(tab.tracks) == 7   # (and the test song)
    assert window.playlist._tracks is tab.tracks
    window.playlist.select(1)                             # (Bees 1, the second song: scrolled into view)
    assert _wait(root, lambda: "Bees 1" in {window.playlist.tree.set(i, "title")
                                            for i in window.playlist.tree.get_children()})
    assert library_tracks_count_text(window, tab) == "Library · 7 songs · 3 albums"
    window._play_tab(len(window.tabs) - 1, 2)
    assert player.tracks is tab.tracks                     # handed to the engine as it is, not copied
    window._hold_playing()
    assert tab.tracks._shared.held and tab.tracks.peek(2) is not None
    assert window.playlist.select_all() and window._queue_selected(front=False)
    assert [t.title for t in player.up_next()][-6:] == ["Bees 1", "Bees 2", "Bees 3", "Cats 1", "Cats 2", "Cats 3"]
    assert not errors
    library_index.close_shared()


def library_tracks_count_text(window, tab) -> str:
    from ironbark.ui import tab_numbers

    songs, albums, _seconds = tab_numbers(tab)
    return f"Library · {songs} songs · {albums} albums"


# -- the album list shared with the search panel, searched off Tk's thread when big ------------------------
def test_library_albums_are_the_search_panels_and_a_big_list_is_searched_off_tk(root, tmp_path, monkeypatch,
                                                                                 tk_thread_rule):
    from ironbark import library_index, library_window, quick_search, settings
    from ironbark.library_window import LibraryWindow

    from test_library_window import flac, wait_for_index

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    monkeypatch.setattr(library_window, "ALBUM_THREAD_AT", 1)      # (as a library of 25,000 albums would)
    music = tmp_path / "Music"
    for name, artist in (("Tommy", "The Who"), ("21", "Adele"), ("25", "Adele")):
        flac(music / name / "01.flac", title=f"{name} one", artist=artist, album=name)
    chosen = []
    w = LibraryWindow(root, music, None, on_choose=chosen.append, on_root_changed=lambda p: None,
                      on_load_all=lambda r, q: None)
    wait_for_index(w, root)
    assert _wait(root, lambda: len(w._items) == 3 and w._album_rows_asked is None)
    shared = quick_search.shared_catalogue(w.index.db)
    assert {id(a) for a in w.albums} <= {id(a) for a in shared.albums}   # one copy of the albums
    w.search_var.set("adele")
    w._search_now()                                       # (the pause in typing came: off Tk's thread)
    assert w._album_rows_asked is not None and len(w._items) == 3        # the old list stays meanwhile
    assert _wait(root, lambda: w._album_rows_asked is None)
    assert sorted(a.title for k, a in w._items if k == "album") == ["21", "25"]
    w.search_var.set("tommy")
    w._choose_first()                                     # Enter straight away: searched here and now
    assert [p.name for p in chosen] == ["Tommy"]
    library_index.close_shared()


# -- review: Tk's thread, what's acted on, what's lit -------------------------------------------------------
def test_a_big_selection_of_a_library_tab_is_read_off_tks_thread(make_window, tmp_path, monkeypatch):
    import threading

    from ironbark import library_index, library_tracks, popups, ui

    from test_library_window import flac

    monkeypatch.setattr(ui, "READ_HERE_MOST", 2)          # (as a Ctrl+A over 200,000 songs would be)
    reads = []
    many = library_tracks.LibraryTrackList.many
    monkeypatch.setattr(library_tracks.LibraryTrackList, "many",
                        lambda self, rows: reads.append(threading.current_thread() is threading.main_thread())
                        or many(self, rows))
    shown = []
    popup = popups.PopupMenu.popup
    monkeypatch.setattr(popups.PopupMenu, "popup", lambda self, x, y: shown.append(self) or popup(self, x, y))
    window, root, player, errors = make_window()
    music = tmp_path / "Music"
    for a in ("Bees", "Cats"):
        for n in (1, 2, 3):
            flac(music / a / f"0{n}.flac", title=f"{a} {n}", artist=a, album=a, tracknumber=str(n))
    window._index().refresh()
    window.open_tab("library", music)
    tab = window.tabs[-1]
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading)
    assert isinstance(tab.tracks, library_tracks.LibraryTrackList)
    _pump(root, 0.4)                                      # (the window has marked the tab's playing song)
    assert window.playlist.select_all() and window._queue_selected(front=False)
    assert player.up_next() == [] and window._notice == "Reading 7 songs…"    # read on a worker meanwhile
    assert _wait(root, lambda: len(player.up_next()) == 7)
    assert reads == [False] and window._notice.startswith("Added 7 songs")

    reads.clear()
    assert window.playlist.select_all()
    rows = playlist_view.Rows(window.playlist.selected_rows())
    window._track_menu(rows, SimpleNamespace(x_root=root.winfo_rootx() + 40, y_root=root.winfo_rooty() + 80))
    assert reads == [] and "Add these 7 songs to playlist" in shown[-1].labels()   # nothing read to show it
    shown[-1].invoke("Play next")
    assert _wait(root, lambda: len(player.up_next()) == 14) and reads == [False]

    reads.clear()
    monkeypatch.setattr(window, "_drop_target", lambda x, y: ("tab", 0))
    window._song_drag(rows, 10, 10, "drop")                # onto the first tab (it has the seventh)
    assert _wait(root, lambda: len(window.tabs[0].tracks) == 7) and reads == [False]
    assert not errors
    library_index.close_shared()


def test_a_few_albums_tab_is_searched_by_its_own_songs(root, synth_db, monkeypatch):
    from ironbark import library_tracks, searchfold

    path, _files = synth_db
    with LibraryDB.open(path) as db:
        tab = library_tracks.build(db, db.albums()[:5])
        asked = []
        real = db.ids_matching
        monkeypatch.setattr(db, "ids_matching", lambda words, among=None: asked.append(among) or real(words, among))
        view = playlist_view.PlaylistView(root, on_play=lambda i: None)
        view.set_tracks(tab)
        view.query.set("a")
        view._render()                                    # (a short list: searched on Tk's thread…)
        assert asked and asked[-1] is not None            # …by its own songs' ids, not the whole library's
        everything = list(tab)
        assert view._songs == [str(i) for i, t in enumerate(everything) if "a" in searchfold.haystack(t)]
        monkeypatch.setattr(library_tracks, "BY_ID_MOST", 3)
        assert tab.matching(["a"]) == [int(i) for i in view._songs] and asked[-1] is None
        view.tree.destroy()


def test_a_long_playlist_drop_under_the_last_row_on_show_lands_there(root):
    tracks = _library(1, 200)
    view = playlist_view.PlaylistView(root, on_play=lambda i: None)
    view.set_tracks(tracks, flat=True)
    root.update()
    rows = view._shown_songs()
    last = view.tree.bbox(rows[-1])
    under = view.tree.winfo_rooty() + last[1] + last[3] + 1   # (the gap under the last whole row)
    assert int(rows[-1]) < 199 and view.drop_index(under) == int(rows[-1]) + 1   # not the end of the playlist
    view.show_drop_line(view.drop_index(under))
    root.update()
    assert view._drop_line.winfo_ismapped()
    view.list.scroll_to(len(tracks))
    root.update()
    rows = view._shown_songs()
    last = view.tree.bbox(rows[-1])
    assert view.drop_index(view.tree.winfo_rooty() + last[1] + last[3] + 1) == 200   # the end, when it's on show


def test_no_matches_doesnt_light_up_and_a_new_list_lets_go_of_the_lit_row(root):
    view = playlist_view.PlaylistView(root, on_play=lambda i: None, on_search_library=lambda q: None)
    view.set_tracks(_library(1, 5), flat=True)
    root.update()
    view.query.set("zzz")
    view._render()
    root.update()
    x, y, _w, h = view.tree.bbox("none")
    view.tree.event_generate("<Motion>", x=x + 30, y=y + h // 2)
    assert view.list.hovered is None
    x, y, _w, h = view.tree.bbox("library")
    view.tree.event_generate("<Motion>", x=x + 30, y=y + h // 2)
    assert view.hovered == "library"                      # (the link to the whole library does)
    view.query.set("")
    view._render()
    root.update()

    def lit():
        return [iid for iid in view.tree.get_children() if "hover" in view.tree.item(iid, "tags")]

    x, y, _w, h = view.tree.bbox("2")
    view.tree.event_generate("<Motion>", x=x + 30, y=y + h // 2)
    assert lit() == ["2"]
    view.set_tracks(view._tracks, flat=True)              # (the same rows again: a ♥ or a tag changed)
    assert lit() == [] and view.hovered is None
    x, y, _w, h = view.tree.bbox("4")
    view.tree.event_generate("<Motion>", x=x + 30, y=y + h // 2)
    assert lit() == ["4"]                                 # (one row lit, never two)


def test_plays_read_before_a_play_was_counted_are_not_shown(root):
    from ironbark.library_window import LibraryWindow

    refreshed = []
    w = SimpleNamespace(_counts={"k": (1, 5.0)}, _counts_wanted={"k": None}, _counts_asked=7, view="songs",
                        song_columns=("plays",), win=SimpleNamespace(winfo_exists=lambda: True),
                        list=SimpleNamespace(refresh=lambda: refreshed.append(1)))
    LibraryWindow.plays_changed(w)                        # a play was counted while a read was on its way
    assert w._counts == {} and w._counts_wanted == {} and w._counts_asked is None and refreshed
    old = SimpleNamespace(ask=7, error=None, value=({"k": 1}, {"k": 5.0}))
    assert not LibraryWindow._take_counts(w, old) and w._counts == {}     # the old counts aren't taken
    w._counts_asked = 8
    w._counts_wanted = {"k": None}
    assert LibraryWindow._take_counts(w, SimpleNamespace(ask=8, error=None, value=({"k": 2}, {"k": 9.0})))
    assert w._counts == {"k": (2, 9.0)} and w._counts_wanted == {}
    w._counts_asked, w._counts_wanted = 9, {"j": None}
    assert not LibraryWindow._take_counts(w, SimpleNamespace(ask=9, error=OSError("busy"), value=None))
    assert w._counts_wanted == {}                         # (asked again when next drawn)


def test_the_menu_key_opens_the_selected_songs_menu_after_the_list_scrolled_away(root):
    menus = []
    view = playlist_view.PlaylistView(root, on_play=lambda i: None, on_menu=lambda rows, event: menus.append(rows),
                                      on_heading_menu=lambda event: menus.append("headings"))
    view.set_tracks(_library(1, 200), flat=True)
    root.update()
    view.select(5)
    view.list.scroll_to(150)                              # (the wheel: row 5 is no longer Tk's)
    assert not view.tree.exists("5")
    view.tree.focus_force()
    root.update()
    view.tree.event_generate("<Shift-F10>")
    root.update()
    assert menus == [[5]] and view.tree.exists("5")       # its own menu, scrolled back to it


def test_a_library_tab_keeps_an_albums_songs_read_again_since_it_was_made(tmp_path, synth_db):
    import shutil

    from ironbark import library_tracks

    path, _files = synth_db
    copy = tmp_path / "library.db"
    shutil.copy(path, copy)                               # (the module's database stays as it was)
    with LibraryDB.open(copy) as db:
        tab = library_tracks.build(db, db.albums())
        folder, start, end = tab.groups[10]
        before = [t.path for t in tab.many(range(start, end))]
        old = list(tab.ids[start:end])
        with db._writing() as con:                        # its files read again (as a tag edit has it): new ids
            fields = [row[1] for row in con.execute("PRAGMA table_info(tracks)") if row[1] != "id"]
            marks = ", ".join("?" * len(old))
            con.execute(f"INSERT INTO tracks({', '.join(fields)}) SELECT {', '.join(fields)} FROM tracks"
                        f" WHERE id IN ({marks}) ORDER BY id", old)
            con.execute(f"DELETE FROM tracks WHERE id IN ({marks})", old)
        songs = [tab[i] for i in range(start, end)]
        assert [t.path for t in songs] == before and not any(t.missing for t in songs)   # not "file not found"
        assert set(tab.ids[start:end]).isdisjoint(old)
        assert tab.index_of_path(before[0]) == start


def test_a_library_tabs_columns_vary_by_what_they_show_and_with_songs_added():
    import os

    from ironbark import library_tracks
    from ironbark.library_db import AlbumOrder

    one, two = Path("C:/M/One"), Path("C:/M/Two")
    orders = {os.fspath(one): AlbumOrder([1, 2], 400.0, frozenset({"X"}), frozenset({"One"}),
                                         frozenset({("MP3", 16, 44100), ("MP3", 0, 48000)})),   # both "MP3"
              os.fspath(two): AlbumOrder([3], 200.0, frozenset({"X"}), frozenset({"Two"}),
                                         frozenset({("FLAC", 16, 44100)}))}
    db = SimpleNamespace(album_orders=lambda folders: orders)
    tab = library_tracks.build(db, [SimpleNamespace(path=one), SimpleNamespace(path=two)])
    assert tab.varies == {"album", "quality"}                 # (MP3, then 16/44.1: the Format column varies)
    more = tab + [Track(Path("D:/Else/Three/01.flac"), "Y", artist="Z", album="Two", codec="FLAC", bits=16,
                        sample_rate=44100)]
    assert more.varies == {"artist", "album", "quality"}      # a song by another artist added: Artist varies
    assert tab.varies == {"album", "quality"}


def test_a_click_doesnt_work_out_what_a_drag_would_carry(root):
    asked, dragged = [], []
    view = playlist_view.PlaylistView(root, on_play=lambda i: None,
                                      on_drag=lambda rows, x, y, phase: dragged.append((list(rows), phase)))
    view.set_tracks(_library(1, 8), flat=True)
    root.update()
    carried = view._carried
    view.drag.what = lambda iid: asked.append(iid) or carried(iid) or None
    x, y, _w, h = view.tree.bbox("5")
    view.tree.event_generate("<ButtonPress-1>", x=x + 40, y=y + 2, rootx=0, rooty=0)
    view.tree.event_generate("<ButtonRelease-1>", x=x + 40, y=y + 2, rootx=0, rooty=0)
    assert asked == [] and view.selected_rows() == [5]   # a click (of a big selection's row: not every song)
    x, y, _w, h = view.tree.bbox("2")                    # (elsewhere on the screen: not a double-click)
    view.tree.event_generate("<ButtonPress-1>", x=x + 40, y=y + 2, rootx=300, rooty=300)
    view.tree.event_generate("<B1-Motion>", x=x + 40, y=y + 60, rootx=300, rooty=360, state=0x100)
    view.tree.event_generate("<ButtonRelease-1>", x=x + 40, y=y + 60, rootx=300, rooty=360, state=0x100)
    assert asked == ["2"] and dragged[-1] == ([2], "drop")

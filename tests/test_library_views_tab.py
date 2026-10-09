"""0.41.0: the Library's views inside the Library tab (library_view.py in place of the track list:
Ctrl+L, the "Songs ▾" button, Esc back), an album chosen there opens a tab and the list returns,
Ctrl+K is the Library tab's search box, "Add songs from the Library…" picks songs for a playlist in
the Library tab, and the views' narrow columns."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import soundfile as sf

from ironbark import library_index, library_view, playlists, theme
from ironbark.library import read_track
from ironbark.tabbar import Popover
from ironbark.ui import Tab
from tagging import write_vorbis
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)


def _wait(root, ready, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if ready():
            return True
        _pump(root, 0.05)
    return ready()


def _flac(path: Path, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros((4410, 2), dtype=np.int16), 44100, subtype="PCM_16")
    if tags:
        write_vorbis(path, **tags)
    return path


def _library(window, root, tmp_path):
    """A small library in the window's folders, read into the database; the main tab first."""
    music = Path(window._library_roots()[0])
    songs = []
    for album, artist, titles in (("Blue Train", "John Coltrane", ("Blue Train", "Locomotion")),
                                  ("Kind of Blue", "Miles Davis", ("So What", "Blue in Green"))):
        for n, title in enumerate(titles, 1):
            songs.append(_flac(music / album / f"{n:02d} {title}.flac", TITLE=title, ARTIST=artist, ALBUM=album,
                               TRACKNUMBER=str(n)))
    library_index.shared([music]).refresh()
    if not window.tabs[0].main:
        window.tabs = [Tab("library", ""), *window.tabs]
    window.select_tab(1)   # (the album tab; the Library tab is first)
    _pump(root, 0.2)
    return music, songs


def test_ctrl_l_shows_the_views_in_the_library_tab_and_esc_puts_the_list_back(make_window, tmp_path):
    window, root, player, errors = make_window()
    music, songs = _library(window, root, tmp_path)
    assert not window.playlist.views_button.winfo_manager()          # (the album tab: no views button)
    window.toggle_library()                                            # Ctrl+L
    _pump(root, 0.3)
    view = window._library
    assert window.current == 0 and window._library_shown and view is not None
    assert view.frame.winfo_manager() == "pack" and not window.playlist.frame.winfo_manager()
    assert not window.head.winfo_manager() and window.transport.winfo_manager()   # the cover goes, the transport stays
    assert str(root.tk.call("focus")) == str(view.search.entry)
    assert _wait(root, lambda: len(view.albums) >= 2), (view.albums, view._loading, view._problem, view.status.cget("text"), errors)
    assert view.view_button.cget("text") == "Albums ▾"
    window._escape_settings()                                          # Esc: the track list back
    _pump(root, 0.2)
    assert not window._library_shown and window.playlist.frame.winfo_manager() == "pack"
    assert window.head.winfo_manager() == "pack" and not view.frame.winfo_manager()
    assert window.playlist.views_button.winfo_manager() == "pack"      # (the Library tab: the "Songs ▾" button)
    window.toggle_library()
    _pump(root, 0.2)
    assert window._library_shown
    window.toggle_library()                                            # Ctrl+L again: back
    assert not window._library_shown
    window.select_tab(1)
    _pump(root, 0.2)
    assert not window.playlist.views_button.winfo_manager()
    assert errors == []


def test_the_views_drop_down_lists_songs_first_and_a_view_picked_shows_it(make_window, tmp_path):
    window, root, player, errors = make_window()
    _library(window, root, tmp_path)
    window.select_tab(0)
    _pump(root, 0.2)
    window._views_menu()                                               # the "Songs ▾" button over the tab's list
    menu = Popover.open_one
    labels = menu.labels()
    assert labels[0] == "Songs" and "Artists" in labels and "Needs a look" in labels and "Songs" not in labels[1:]
    row = next(r for r in menu.rows() if r.item.label == "Artists")
    row.command()
    _pump(root, 0.3)
    assert window._library_shown and window._library.view == "artists" and window.prefs["library_view"] == "artists"
    assert window._library.view_button.cget("text") == "Artists ▾"
    window._library._view_menu()                                       # the view's own drop-down: Songs goes back
    menu = Popover.open_one
    assert menu.labels()[0] == "Songs"
    next(r for r in menu.rows() if r.item.label == "Songs").command()
    _pump(root, 0.2)
    assert not window._library_shown and window.playlist.frame.winfo_manager() == "pack"
    assert errors == []


def test_an_album_chosen_in_the_views_opens_a_tab_and_the_list_comes_back(make_window, tmp_path):
    window, root, player, errors = make_window()
    _library(window, root, tmp_path)
    window.open_library(view="albums")
    view = window._library
    assert _wait(root, lambda: len(view._items) >= 2), (view.albums, view._loading, view._problem, view.status.cget("text"), errors)
    view.list.select(next(i for i, (kind, album) in enumerate(view._items) if kind == "album" and album.path.name == "Blue Train"))
    assert view._choose_selected()
    _pump(root, 0.3)
    assert not window._library_shown and window.tabs[window.current].kind == "album"
    assert window.tabs[window.current].path.endswith("Blue Train") and window.playlist.frame.winfo_manager() == "pack"
    # "Show in Library" from anywhere: the views come back on Albums with that album selected
    window.open_library(album=Path(window.tabs[window.current].path))
    _pump(root, 0.3)
    assert window._library_shown and window.current == 0 and view.view == "albums"
    assert view._selected_album() is not None and view._selected_album().path.name == "Blue Train"
    assert errors == []


def test_add_songs_from_the_library_picks_them_in_the_library_tab(make_window, tmp_path):
    window, root, player, errors = make_window()
    music, songs = _library(window, root, tmp_path)
    playlist = tmp_path / "Road trip.m3u8"
    playlists.write(playlist, [read_track(songs[0])])
    window.tabs.append(Tab("playlist", str(playlist)))
    window.select_tab(2)
    assert _wait(root, lambda: window.tabs[2].tracks is not None)
    window.start_picking(2)                                            # the tab menu's "Add songs from the Library…"
    _pump(root, 0.2)
    assert window.current == 0 and window.playlist.picking == "Road trip"
    assert _wait(root, lambda: window.tabs[0].tracks is not None and len(window.tabs[0].tracks) >= 4), (window.tabs[0].tracks, errors)
    _pump(root, 0.3)
    assert window.playlist.pick_button.cget("text") == "Add" and not window.playlist.pick_button.enabled
    tracks = window.tabs[0].tracks
    picked = [i for i, t in enumerate(tracks) if t.title in ("Locomotion", "So What")]
    window.playlist.select_tracks(picked)                              # two songs picked
    _pump(root, 0.2)
    assert window.playlist.pick_button.cget("text") == "Add 2 songs" and window.playlist.pick_button.enabled
    window._pick_add()
    assert _wait(root, lambda: window.playlist.picking is None)
    _pump(root, 0.3)
    assert window.current == 2 and len(playlists.read(playlist)) == 3 and "Added 2 songs" in window._notice
    # Cancel (Esc) comes back with nothing added
    window.start_picking(2)
    _pump(root, 0.2)
    assert window.current == 0 and window.playlist.picking == "Road trip"
    window._escape_settings()
    _pump(root, 0.2)
    assert window.playlist.picking is None and window.current == 2 and len(playlists.read(playlist)) == 3
    assert errors == []


def test_the_views_show_fewer_columns_when_narrow(make_window, tmp_path):
    window, root, player, errors = make_window(geometry=f"{theme.px(420)}x{theme.px(760)}+20+20")
    _library(window, root, tmp_path)
    window.open_library(view="albums")
    view = window._library
    assert _wait(root, lambda: len(view._items) >= 2 and view._narrow), (view._items, view._narrow, view.frame.winfo_width(), errors)
    _pump(root, 0.3)
    assert tuple(view.tree.cget("displaycolumns")) == library_view.NARROW_ALBUM_COLUMNS
    names = [view.list.source.row(i)[1][0] for i in range(len(view.list.source))]
    assert "John Coltrane – Blue Train" in names and "Miles Davis – Kind of Blue" in names
    view.set_view("artists")
    assert _wait(root, lambda: any(kind == "group" for kind, _thing in view._items))
    rows = [view.list.source.row(i)[1][0] for i in range(len(view.list.source))]
    assert "Blue Train" in rows and "John Coltrane" in rows and not any(" – " in row for row in rows)   # grouped: the album's name alone
    view.set_view("most_played")
    _pump(root, 0.3)
    assert tuple(view.tree.cget("displaycolumns")) == library_view.NARROW_DISPLAYED["most_played"]
    assert errors == []

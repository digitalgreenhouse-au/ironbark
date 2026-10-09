"""0.40.0: the main Library tab (every library folder; can't be closed, can be moved, its own plate;
an open-album mark on each row), Rename… on any tab, and the search box's Whole library toggle (this
tab's matches, then "From your library" rows: a song outside this tab plays from the Library tab; in
a playlist tab a "+" adds it)."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import soundfile as sf
from tagging import write_vorbis
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

from ironbark import library_index, playlists
from ironbark.ui import Tab


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


def test_the_library_tab_is_always_there_cant_be_closed_and_can_be_moved(make_window):
    window, root, player, errors = make_window()
    album = window.tabs[0]
    window.tabs = [Tab("library", ""), album]
    window.current = 1
    window._refresh_tabs()
    _pump(root, 0.2)
    main = window.tabs[0]
    assert main.main and main.title == "Library" and window.bar._locked == {0} and window.bar._tinted == {0}
    window.close_tab(0)                                   # never closed…
    assert window.tabs[0] is main and "Library tab stays open" in window._notice
    window.move_tab(0, 1)                                 # …but it can be moved like any other
    _pump(root, 0.1)
    assert window.tabs[1] is main and window.bar._locked == {1} and window.current == 0
    assert "Close tab" not in _tab_menu_labels(window, 1) and "Rename tab…" in _tab_menu_labels(window, 1)
    assert errors == []


def _tab_menu_labels(window, index: int) -> list[str]:
    from types import SimpleNamespace

    shown = []
    real = window._menu

    def menu():
        m = real()
        shown.append(m)
        return m

    window._menu = menu
    try:
        window._tab_menu(index, SimpleNamespace(x_root=0, y_root=0))
    finally:
        window._menu = real
    menu = shown[-1]
    labels = menu.labels()
    menu.hide()
    return labels


def test_rename_gives_a_tab_a_name_of_its_own_and_the_albums_name_comes_back(make_window):
    window, root, player, errors = make_window()
    window.rename_tab(0, "Road trip")
    _pump(root, 0.1)
    assert window.tabs[0].name == "Road trip" and window.tabs[0].title == "Road trip"
    assert window.bar._titles == ["Road trip"]
    assert window.tabs[0].source()["name"] == "Road trip"
    window.rename_tab(0, "")                               # Use the album's name
    _pump(root, 0.1)
    assert window.tabs[0].name == "" and window.tabs[0].title == "Album" and "name" not in window.tabs[0].source()
    assert errors == []


def test_whole_library_search_lists_the_librarys_songs_and_a_plus_adds_one_to_the_playlist(make_window, tmp_path):
    window, root, player, errors = make_window()
    music = tmp_path / "Music"
    other = _flac(music / "Other" / "01 Other.flac", TITLE="Other Song", ARTIST="Someone", ALBUM="Other")
    index = library_index.shared([music])
    index.refresh()                                        # (the library knows both albums)
    playlist = tmp_path / "Mix.m3u8"
    playlists.write(playlist, [player.track])
    window.open_tab("playlist", playlist)
    assert _wait(root, lambda: window.tabs[-1].tracks is not None)
    assert window.playlist.whole_library and window.playlist.adds
    window.playlist.query.set("other")
    assert _wait(root, lambda: window.playlist.library_hit("lib0") is not None, 15)
    hit = window.playlist.library_hit("lib0")
    assert hit.track.title == "Other Song" and "libhead" in window.playlist._listing.tail
    assert window.playlist.library_hit("lib1") is None     # (the playlist's own song isn't listed again)
    window.playlist.on_library_hit(hit, "add")             # the "+": one click adds it to this playlist
    assert _wait(root, lambda: other in playlists.read(playlist), 10)
    assert "Added" in window._notice
    # The toggle off: the box filters the tab only, and the library rows go.
    window.playlist.set_whole_library(False)
    _pump(root, 0.2)
    assert window.playlist.library_hit("lib0") is None and window.prefs.get("search_whole_library") is not None
    assert errors == []


def test_a_library_song_found_from_another_tab_plays_from_the_library_tab(make_window, tmp_path):
    window, root, player, errors = make_window()
    music = tmp_path / "Music"
    _flac(music / "Other" / "01 Other.flac", TITLE="Elsewhere", ARTIST="Someone", ALBUM="Other")
    index = library_index.shared([music])
    index.refresh()
    window.playlist.query.set("elsewhere")
    assert _wait(root, lambda: window.playlist.library_hit("lib0") is not None, 15)
    hit = window.playlist.library_hit("lib0")
    window.playlist.on_library_hit(hit, "play")           # Enter / a double-click on the library row
    assert _wait(root, lambda: any(t.main for t in window.tabs) and window.tabs[window.current].main, 10)
    main = window.current
    assert _wait(root, lambda: window.tabs[main].tracks is not None and window.playing_tab == main, 15)
    assert window.playlist.open_column                      # the Library tab's rows carry the open-album mark
    assert errors == []


def test_the_open_album_mark_opens_the_songs_album_in_a_new_tab(make_window, tmp_path):
    window, root, player, errors = make_window()
    music = tmp_path / "Music"
    index = library_index.shared([music])
    index.refresh()
    window.tabs.insert(0, Tab("library", ""))
    window.select_tab(0)
    assert _wait(root, lambda: window.tabs[0].tracks is not None and len(window.tabs[0].tracks) > 0, 15)
    assert window.playlist.open_column
    before = len(window.tabs)
    window._open_album_of(0)
    _pump(root, 0.2)
    assert len(window.tabs) == before + 1 or window.current != 0   # (the album was already open: it's selected)
    assert window.tabs[window.current].kind == "album"
    assert window.playing_tab in (None, 0, 1) and errors == []

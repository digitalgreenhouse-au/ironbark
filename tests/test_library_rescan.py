"""The ↻ button beside the Library tab's search box (and F5): look through the library folders for
music added, changed or gone, and take it in without restarting — the Library tab follows, and the
status line says how it went."""

from __future__ import annotations

import os
import time

from test_keys import _run
from test_library_tab import _flac, _wait
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

from ironbark import library_rescan, library_view


def _on_the_library_tab(window, root):
    main = window._main_tab(make=True)
    window.select_tab(main)
    tab = window.tabs[main]
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading, 10)
    return tab


def test_the_button_shows_on_the_library_tab_only(make_window):
    window, root, player, errors = make_window()
    button = window.playlist.rescan_button
    _on_the_library_tab(window, root)
    window.select_tab(next(i for i, t in enumerate(window.tabs) if not t.main))
    _pump(root, 0.1)
    assert button is not None and not button.winfo_manager()   # (an album tab: no button)
    window.select_tab(window._main_tab())
    _pump(root, 0.1)
    assert button.winfo_manager() and button.winfo_ismapped()
    window.select_tab(next(i for i, t in enumerate(window.tabs) if not t.main))
    _pump(root, 0.1)
    assert not button.winfo_manager()
    assert not errors


def test_music_added_since_start_up_comes_in_and_the_library_tab_lists_it(make_window, tmp_path):
    window, root, player, errors = make_window()
    music = tmp_path / "Music"
    index = window._index()
    index.refresh()
    tab = _on_the_library_tab(window, root)
    before = len(tab.tracks)
    song = _flac(music / "New Album" / "01 Arrived.flac", title="Arrived", artist="Someone", album="New Album")
    old = time.time() - 60   # (a minute old: not "still being copied")
    os.utime(song, (old, old))
    window.playlist.rescan_button.command()
    assert window._notice == library_rescan.LOOKING
    assert not window.playlist.rescan_button.enabled           # greyed while it looks
    assert _wait(root, lambda: not library_rescan.busy(window), 15)
    assert window.playlist.rescan_button.enabled
    assert window._notice.startswith(("Library updated: 1 new", "New music"))
    assert _wait(root, lambda: tab.tracks is not None and len(tab.tracks) == before + 1, 15)   # the tab follows
    assert "Arrived" in [tab.tracks[i].title for i in range(len(tab.tracks))]
    assert not errors


def test_nothing_changed_says_the_library_is_up_to_date(make_window):
    window, root, player, errors = make_window()
    window._index().refresh()
    _on_the_library_tab(window, root)
    window.playlist.rescan_button.command()
    assert _wait(root, lambda: not library_rescan.busy(window), 15)
    assert window._notice == "The library is up to date: nothing added, changed or gone"
    assert not errors


def test_f5_looks_from_any_tab_and_a_second_press_waits_for_the_first(make_window, monkeypatch):
    window, root, player, errors = make_window()
    window._index().refresh()
    looks = []
    real = window.watcher.check

    def slow_check(*args, **kwargs):
        looks.append(1)
        time.sleep(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(window.watcher, "check", slow_check)
    assert not window.tabs[window.current].main                # (an album tab on show)
    _run(window, root, "F5")
    assert window._notice == library_rescan.LOOKING
    _run(window, root, "F5")
    assert window._notice == "Already looking through the library…"
    assert _wait(root, lambda: not library_rescan.busy(window), 15)
    assert looks == [1]
    assert not errors


def test_with_no_library_folder_it_says_how_to_add_one(make_window):
    window, root, player, errors = make_window()
    window._set_library_folders([])                          # (the library cleared)
    _pump(root, 0.3)
    library_rescan.rescan(window)
    assert window._notice == library_view.NO_FOLDERS_TEXT and not library_rescan.busy(window)
    assert not errors


def test_the_library_views_have_a_button_of_their_own_for_their_rescan(make_window):
    window, root, player, errors = make_window()
    window.open_library()
    _pump(root, 0.3)
    views = window._library
    rescans = []
    views.rescan = lambda: rescans.append(1)
    views.rescan_button.command()                            # (bound to the method as built: call it the same way)
    library_rescan.rescan(window)                            # the window's ↻ / F5 with the views on show
    assert rescans == [1]
    assert not errors

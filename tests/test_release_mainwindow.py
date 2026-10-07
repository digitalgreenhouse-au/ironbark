"""The main window before release: a song asked for by name from the album playing after that album's
folder changed, the docked Up next keeping its selection on the same songs (and coping with a very
long queue), full screen with Keep on top, the queue with no tabs open, and the wording of messages.
The real window on temp settings with a fake player (test_light_mini.make_window): no device is opened
and nothing of the user's is touched."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import chrome, popups
from ironbark.library import Track
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)
from test_wide_window import _open

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the borderless full screen is Windows' own")


def _flac(path):
    sf.write(path, np.zeros((4410, 2), dtype=np.int16), 44100, subtype="PCM_16")
    return path


def _wait(root, ready, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if ready():
            return True
        _pump(root, 0.05)
    return ready()


# -- a song asked for by name from the album playing ---------------------------------------------------
def test_a_song_new_to_the_playing_album_plays_when_asked_for_by_name(make_window, tmp_path, monkeypatch):
    window, root, player, errors = make_window()
    tab = window.tabs[0]
    player.set_tracks(tab.tracks)                   # (as when the tab was played: the player holds its list)
    album = tmp_path / "Music" / "Album"
    intro = _flac(album / "00 Intro.flac")          # added to the folder while the album plays
    played = []
    monkeypatch.setattr(player, "play", lambda index, start_at=0.0: played.append(index))
    window.play_song(Track(path=intro, title="Intro"), album)
    assert _wait(root, lambda: played and not tab.loading)
    assert [t.path.name for t in tab.tracks] == ["00 Intro.flac", "01 Song.flac"]
    assert player.tracks[played[-1]].path == intro         # the song asked for plays …
    assert player.tracks is tab.tracks                     # … from the list as read again
    assert window.playing_tab == 0 and not errors


# -- the docked Up next keeps its selection on the same songs -------------------------------------------
def _songs(*titles: str):
    return [Track(Path(f"C:/Music/Up/{n:02d} {title}.flac"), title, artist="Up", duration=100.0)
            for n, title in enumerate(titles, 1)]


def _docked(make_window, *titles):
    window, root, player, errors = _open(make_window)
    window._queue_tracks(_songs(*titles), False)
    window.set_view("up_next")
    _pump(root, 0.4)
    return window, root, player, errors, window._up_next_view


def _selected_titles(dock):
    return [dock.tree.item(iid, "values")[2] for iid in dock.tree.selection()]


def _row_of(dock, title):
    return next(iid for iid in dock.tree.get_children() if dock.tree.item(iid, "values")[2] == title)


def test_docked_up_next_keeps_the_selection_on_its_song_as_the_queue_moves_on(make_window):
    window, root, player, errors, dock = _docked(make_window, "A", "B", "C")
    dock.tree.selection_set(_row_of(dock, "B"))
    dock.tree.focus(_row_of(dock, "B"))
    player.remove_from_queue(0)                 # (as when A starts playing: it leaves Up next)
    _pump(root, 0.4)
    assert _selected_titles(dock) == ["B"] and dock.tree.item(dock.tree.focus(), "values")[2] == "B"
    dock.remove_selected()                      # Delete takes out the song selected, not the one now in its row
    _pump(root, 0.3)
    assert [t.title for t in player.up_next()] == ["C"] and window._notice == "Took “B” out of Up next"
    assert _selected_titles(dock) == ["C"]      # (the song now in its place: Delete goes on)
    assert not errors


def test_docked_up_next_keeps_the_selection_when_the_queue_keeps_its_length(make_window):
    window, root, player, errors, dock = _docked(make_window, "A", "B", "C")
    dock.tree.selection_set(_row_of(dock, "B"))
    player.remove_from_queue(0)                 # A starts playing …
    player.add_to_queue(_songs("D"))            # … and a song is added: three songs again
    _pump(root, 0.4)
    assert [dock.tree.item(i, "values")[2] for i in dock.tree.get_children()] == ["B", "C", "D"]
    assert _selected_titles(dock) == ["B"]
    assert not errors


def test_docked_up_next_acts_on_the_songs_chosen_even_before_the_list_catches_up(make_window, monkeypatch):
    window, root, player, errors, dock = _docked(make_window, "A", "B", "C", "D")
    shown = []
    monkeypatch.setattr(popups.PopupMenu, "popup", lambda self, x, y: shown.append(self))
    row = _row_of(dock, "C")
    x, y, _w, h = dock.tree.bbox(row)
    dock._context(SimpleNamespace(x=x + 5, y=y + h // 2, x_root=0, y_root=0))   # a menu on C …
    player.remove_from_queue(0)                 # … left open while A starts playing
    _pump(root, 0.4)
    shown[-1].menu.find("Take it out of Up next").command()
    _pump(root, 0.3)
    assert [t.title for t in player.up_next()] == ["B", "D"]
    dock.tree.selection_set(_row_of(dock, "D"))
    player.remove_from_queue(0)                 # B starts playing, and Enter comes before the next poll
    played = []
    monkeypatch.setattr(player, "play_queued", lambda position: played.append(player.up_next()[position].title))
    dock.play_selected()
    assert played == ["D"] and not errors


# -- a very long Up next, docked ----------------------------------------------------------------------
def test_a_very_long_up_next_docked_shows_its_head_and_keeps_the_window_quick(make_window):
    window, root, player, errors, dock = _docked(make_window, "A")
    songs = _songs("Song") * 100_000
    player.add_to_queue(songs)
    _pump(root, 0.4)
    rows = dock.tree.get_children()
    assert 50 <= len(rows) <= 1000                                  # the head of the queue, not every song
    assert window.playlist.count_label.cget("text").startswith("Up next · 100001 songs")
    assert dock.more.winfo_ismapped() and "more" in dock.more.cget("text")
    started = time.perf_counter()
    for _ in range(5):
        window._paint_docks()                                       # a poll with nothing changed
        window._view_count(None)
        window._paint_up_next()
    assert (time.perf_counter() - started) / 5 < 0.01
    dock.tree.selection_set(rows[3])
    chosen = dock.tree.item(rows[3], "values")[2]
    started = time.perf_counter()
    player.remove_from_queue(0)                                     # a song change: the head leaves
    window._paint_docks()
    assert time.perf_counter() - started < 0.25
    assert [dock.tree.item(i, "values")[2] for i in dock.tree.selection()] == [chosen]
    assert len(dock.tree.selection()) == 1 and not errors


# -- full screen with Keep on top ----------------------------------------------------------------------
def _topmost(root) -> bool:
    return bool(int(root.attributes("-topmost")))


@windows_only
def test_full_screen_with_keep_on_top_leaves_room_for_the_players_other_windows(make_window):
    window, root, player, errors = _open(make_window, size=(1280, 800), always_on_top=True)
    assert _topmost(root)
    window.toggle_full_screen()
    _pump(root, 0.4)
    assert chrome.is_fullscreen(root) and not _topmost(root)        # (it covers the screen anyway)
    window.show_help()                                               # F1: in front of it, not hidden behind
    _pump(root, 0.6)
    win = window._help.win
    x, y = win.winfo_rootx() + win.winfo_width() // 2, win.winfo_rooty() + win.winfo_height() // 2
    assert root.winfo_containing(x, y) is not None and root.winfo_containing(x, y).winfo_toplevel() is win
    window._toggle_on_top()                                          # the switch, flicked while full screen
    assert not _topmost(root) and window.on_top.get()
    window.bring_to_front()                                          # (a second launch, say)
    _pump(root, 0.3)
    assert not _topmost(root)
    window.toggle_full_screen()                                      # back: on top again
    _pump(root, 0.4)
    assert not chrome.is_fullscreen(root) and _topmost(root) and window.prefs["always_on_top"] is True
    assert not errors


# -- the queue with no tabs open, in a wide window ------------------------------------------------------
def test_up_next_queued_with_no_tabs_open_shows_in_a_wide_window_too(make_window):
    window, root, player, errors = _open(make_window)
    window.set_view("up_next")
    _pump(root, 0.3)
    window.close_tab(0)                                  # every tab closed: the empty view
    _pump(root, 0.3)
    assert not window.tabs and window.empty_view.winfo_ismapped()
    window._queue_tracks(_songs("One", "Two"), False)   # e.g. Add to queue from the Library
    _pump(root, 0.4)
    label = window.up_next_label
    assert label.winfo_ismapped() and label.cget("text") == "Up next (2) · One — Up"
    assert window.empty_view.winfo_rooty() < label.winfo_rooty() < window.status_row.winfo_rooty()
    window._show_up_next()                              # its click: the list, to start a song
    _pump(root, 0.3)
    assert window.up_next_pop.is_open
    window._show_up_next()
    window.reopen_tab()                                  # a tab again: the docked list is back in its pane
    _pump(root, 0.5)
    assert window._up_next_view.winfo_ismapped() and not label.winfo_ismapped()
    assert not errors


# -- "Use current" (make this spot home) when the window has no spot of its own ------------------------
@windows_only
def test_use_current_says_why_it_cant_while_full_screen_maximised_or_mini(make_window):
    window, root, player, errors = _open(make_window, size=(1280, 800))
    window.toggle_full_screen()
    _pump(root, 0.4)
    window.make_home()
    assert window.prefs["home_geometry"] == "" and "full screen" in window._notice
    window.toggle_full_screen()
    root.state("zoomed")
    _pump(root, 0.4)
    window.make_home()
    assert window.prefs["home_geometry"] == "" and "maximised" in window._notice
    root.state("normal")
    _pump(root, 0.4)
    window.toggle_mini()                         # the mini strip: home is a spot for the full window
    _pump(root, 0.4)
    window.make_home()
    assert window.prefs["home_geometry"] == "" and "full window" in window._notice
    window.toggle_mini()
    _pump(root, 0.4)
    window.make_home()
    assert window.prefs["home_geometry"] and window._notice.startswith("Home set")
    assert not errors


# -- the words when an undo can't be done -----------------------------------------------------------------
def test_an_up_next_edit_that_cant_be_undone_says_up_next(make_window):
    window, root, player, errors, dock = _docked(make_window, "A", "B", "C")
    dock.tree.selection_set(_row_of(dock, "B"))
    dock.remove_selected()
    player.remove_from_queue(0)                 # A starts playing: Up next has moved on
    window.undo()
    assert window._notice == "Can't undo: Up next has changed since"
    assert [t.title for t in player.up_next()] == ["C"] and not errors

"""A wide window's right pane: the view switch (Tracks · Up next · Lyrics), the whole library's
results under the track list's own search box (This tab | Whole library), and the status line that
never moves the list. (The narrow window staying exactly as it was is test_narrow_golden.py.)"""

from __future__ import annotations

import time

import pytest

from ironbark import keys
from ironbark.ui import split_status
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)
from test_wide_window import _open, _resize


def _wait(root, ready, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if ready():
            return True
        _pump(root, 0.05)
    return ready()


# -- the view switch ----------------------------------------------------------------------------------
def test_the_view_switch_and_search_choice_show_only_in_a_wide_window(make_window):
    window, root, player, errors = _open(make_window)
    pv = window.playlist
    assert pv.view_switch.winfo_ismapped() and pv.scope_choice.winfo_ismapped()
    assert [label.cget("text") for label in pv.view_switch.labels.values()] == ["Tracks", "Up next", "Lyrics"]
    assert [label.cget("text") for label in pv.scope_choice.labels.values()] == ["This tab", "Whole library"]
    # The choice sits right beside the box, the count on the header row's right.
    gap = pv.scope_choice.winfo_rootx() - (pv.search.winfo_rootx() + pv.search.winfo_width())
    assert 0 <= gap <= 30
    assert pv.count_label.winfo_ismapped() and pv.count_label.winfo_rooty() < pv.search.winfo_rooty()
    _resize(root, 420, 900)
    assert not pv.view_switch.winfo_ismapped() and not pv.scope_choice.winfo_ismapped()
    assert pv.table.winfo_ismapped() and not errors


def test_up_next_docks_with_the_pop_overs_actions(make_window, monkeypatch):
    window, root, player, errors = _open(make_window)
    pv = window.playlist
    window._queue_tracks([player.track, player.track], False)
    _pump(root, 0.3)
    assert window.up_next_label.winfo_ismapped()
    pv.view_switch.choose("up_next")
    _pump(root, 0.4)
    dock = window._up_next_view
    assert dock.winfo_ismapped() and not pv.table.winfo_ismapped() and not pv.search_row.winfo_ismapped()
    assert not window.up_next_label.winfo_manager()           # (the list itself is on show)
    assert dock.tree.get_children() == ("0", "1")
    assert dock.tree.item("0", "values")[2] == "Song One"
    assert pv.count_label.cget("text").startswith("Up next · 2 songs")
    played = []
    monkeypatch.setattr(player, "play_queued", lambda entry: played.append(entry))
    dock.tree.selection_set("1")
    dock.play_selected()                                       # double-click / Enter: play it now
    assert played == [player.queue[1]]                         # (that entry: the same song is queued twice)
    dock.tree.selection_set("0")
    dock.remove_selected()                                     # Delete: take it out
    _pump(root, 0.3)
    assert len(player.up_next()) == 1 and dock.tree.get_children() == ("0",)
    dock.clear_button.activate()                               # Clear …
    _pump(root, 0.3)
    assert player.up_next() == [] and window._notice.startswith("Cleared Up next")
    assert dock.empty.winfo_ismapped() and pv.count_label.cget("text") == "Nothing waiting"
    window.undo()                                              # … and Ctrl+Z puts them back
    _pump(root, 0.3)
    assert len(player.up_next()) == 1 and dock.tree.get_children() == ("0",)
    dock.stop_switch.toggle.toggle()                           # Stop after the current song
    _pump(root, 0.3)
    assert player.stop_after == "song" and dock.stopping.get()
    # Remembered for the session: the narrow window shows the list, and wide again, Up next.
    _resize(root, 420, 900)
    assert not dock.winfo_ismapped() and pv.table.winfo_ismapped() and window.view == "up_next"
    assert window.up_next_label.winfo_ismapped()
    _resize(root, 1920, 1080)
    assert dock.winfo_ismapped() and not pv.table.winfo_ismapped()
    window.search_this_tab()                                   # Ctrl+F: this tab's list again
    _pump(root, 0.3)
    assert window.view == "tracks" and pv.table.winfo_ismapped() and not dock.winfo_ismapped()
    assert not errors


def test_lyrics_dock_and_follow_the_song_playing(make_window):
    window, root, player, errors = _open(make_window)
    lrc = player.track.path.with_suffix(".lrc")
    lrc.write_text("[00:01.00]First line\n[00:40.00]The line at forty\n[01:00.00]A later line\n", encoding="utf-8")
    window.playlist.view_switch.choose("lyrics")
    view = window._lyrics_view
    assert _wait(root, lambda: view.lyrics is not None)
    assert view.winfo_ismapped() and not window.playlist.table.winfo_ismapped()
    assert "The line at forty" in view.text.get("1.0", "end")
    assert view.current == 1                                   # (the song is 0:42 in: the line from 0:40)
    assert window.playlist.count_label.cget("text").startswith("From ")
    assert not errors


def test_whole_library_shows_its_results_in_the_lists_place(make_window, monkeypatch):
    window, root, player, errors = _open(make_window)
    pv = window.playlist
    rendered = []
    monkeypatch.setattr(pv, "_render", lambda: rendered.append(pv.query.get()))
    pv.scope_choice.choose("library")
    _pump(root, 0.3)
    inline = window._inline
    assert inline.is_open and inline.panel.winfo_ismapped() and not pv.table.winfo_ismapped()
    assert inline.panel.master is pv.frame and inline.search is None   # (docked: the track list's box is its box)
    assert pv.hint.cget("text") == "Search every song, album and artist"
    searched = []
    monkeypatch.setattr(inline, "_search_now", lambda: searched.append(inline.query.get()))
    pv.query.set("so")
    _pump(root, 0.4)
    assert searched and searched[-1] == "so" and rendered == []   # the tab isn't filtered meanwhile
    acted = []
    monkeypatch.setattr(inline, "_act", lambda how, first=False: acted.append((how, first)))
    pv._play_first_result()                                    # Enter in the box: the first result
    pv._library_key("queue")                                   # Ctrl+Enter: add it to Up next
    assert acted == [("play", True), ("queue", True)]
    pv.scope_choice.choose("tab")                              # back to this tab: searched for the same words
    _pump(root, 0.3)
    assert not inline.is_open and pv.table.winfo_ismapped() and not inline.panel.winfo_ismapped()
    assert rendered[-1] == "so" and pv.hint.cget("text") == "Search songs, artists, albums"
    # A song played from the results: the tab's list again, the box emptied.
    pv.scope_choice.choose("library")
    _pump(root, 0.2)
    inline.close()
    _pump(root, 0.2)
    assert window.search_scope == "tab" and pv.query.get() == "" and pv.table.winfo_ismapped()
    # The narrow window always searches the tab.
    pv.scope_choice.choose("library")
    _resize(root, 420, 900)
    assert pv.scope == "tab" and pv.table.winfo_ismapped() and not inline.panel.winfo_ismapped()
    assert not errors


def _keyboard(root) -> str:
    return str(root.tk.call("focus", "-lastfor", root))


def test_the_keyboard_never_stays_in_a_part_of_the_pane_put_away(make_window):
    """A result played with the keyboard, a click on the view switch, the window made narrow: the
    keys go to what's on show, never to a hidden list (Delete there would take a song out unseen)."""
    window, root, player, errors = _open(make_window)
    pv = window.playlist
    pv.scope_choice.choose("library")
    _pump(root, 0.3)
    inline = window._inline
    inline.tree.focus_set()
    inline.close()                                             # (what playing a result does)
    _pump(root, 0.2)
    assert not inline.panel.winfo_ismapped() and _keyboard(root) == str(pv.tree)
    window._queue_tracks([player.track, player.track], False)
    pv.view_switch.choose("up_next")
    _pump(root, 0.3)
    dock = window._up_next_view
    assert _keyboard(root) == str(dock.tree)                   # (it was in the track list, now put away)
    pv.view_switch.choose("tracks")                            # a click: the switch never takes the keyboard
    _pump(root, 0.3)
    assert _keyboard(root) == str(pv.tree)
    pv.view_switch.choose("up_next")
    _pump(root, 0.3)
    _resize(root, 420, 900)
    assert _keyboard(root) == str(pv.tree) and not dock.winfo_ismapped()
    _resize(root, 1920, 1080)
    pv.view_switch.choose("lyrics")
    _pump(root, 0.3)
    assert _keyboard(root) == str(root)                        # (nothing in the lyrics takes it: the window)
    assert not errors


def test_a_tab_opened_while_searching_the_whole_library_shows_its_songs(make_window, tmp_path):
    window, root, player, errors = _open(make_window)
    pv = window.playlist
    pv.scope_choice.choose("library")
    pv.query.set("abba")
    _pump(root, 0.3)
    other = tmp_path / "Other"
    other.mkdir()
    window.open_tab("folder", other)                           # e.g. from the Library window
    _pump(root, 0.4)
    assert window.search_scope == "tab" and pv.scope == "tab" and pv.query.get() == ""
    assert pv.table.winfo_ismapped() and not window._inline.panel.winfo_ismapped()
    # Switching tabs while the box searches the whole library keeps the words: they aren't the tab's.
    pv.scope_choice.choose("library")
    pv.query.set("abba")
    _pump(root, 0.3)
    window.select_tab(0)
    _pump(root, 0.3)
    assert pv.query.get() == "abba" and window._inline.panel.winfo_ismapped()
    # Searching the tab, a switch empties the box as before.
    pv.scope_choice.choose("tab")
    _pump(root, 0.2)
    window.select_tab(1)
    _pump(root, 0.3)
    assert pv.query.get() == "" and not errors


def test_docked_up_next_rows_follow_downloads_without_redrawing_the_list(make_window, monkeypatch):
    window, root, player, errors = _open(make_window)
    window._queue_tracks([player.track] * 3, False)
    marks = [("◐ 10%", "plain", "10% downloaded"), ("●", "accent", "In the cache"), ("!", "warn", "Couldn't")]
    monkeypatch.setattr(window, "_up_next_marks", lambda queue, shown=10: list(marks))
    window.set_view("up_next")
    _pump(root, 0.4)
    dock = window._up_next_view
    tree = dock.tree
    # A song in the cache isn't green (that reads as the song playing); one that couldn't download warns.
    assert not tree.item("1", "tags") and tuple(tree.item("2", "tags")) == ("warn",)
    dock._tip_row = "0"                                        # (the pointer over the first row)
    tree.item("0", tags=("hover",))
    rebuilt = []
    real_delete = tree.delete
    monkeypatch.setattr(tree, "delete", lambda *items: rebuilt.append(items) or real_delete(*items))
    marks[0] = ("◐ 55%", "plain", "55% downloaded")
    _pump(root, 0.4)
    assert rebuilt == [] and tree.item("0", "values")[0] == "◐ 55%" and "hover" in tree.item("0", "tags")
    assert dock.mark_meaning(0) == "55% downloaded"
    window._unqueue(2)                                         # a song taken out: written afresh
    _pump(root, 0.4)
    assert rebuilt and tree.get_children() == ("0", "1") and not errors


def test_docked_up_next_keeps_the_songs_selected_as_up_next_moves_on(make_window, monkeypatch):
    """The selection is songs, not rows: when the first song in Up next starts playing (it leaves the
    list), the songs chosen stay chosen one row up, and Delete or Play acts on them — never on a
    neighbour that moved into their place."""
    from ironbark.library import Track

    window, root, player, errors = _open(make_window)
    songs = [Track(player.track.path, f"Song {letter}", duration=60.0) for letter in "ABCD"]
    window._queue_tracks(songs, False)
    window.set_view("up_next")
    _pump(root, 0.4)
    dock, tree = window._up_next_view, window._up_next_view.tree
    c = player.queue[2]
    tree.selection_set("2")                                    # Song C
    tree.focus("2")
    with player._lock:
        player.queue = player.queue[1:]                        # Song A starts playing: it leaves Up next
    _pump(root, 0.4)
    assert tree.selection() == ("1",) and dock.selected() is c
    played = []
    monkeypatch.setattr(player, "play_queued", lambda entry: played.append(entry))
    dock.play_selected()
    assert played == [c]                                       # Song C, not Song D
    dock.remove_selected()                                     # Delete: Song C goes, and nothing else
    _pump(root, 0.4)
    assert [t.title for t in player.up_next()] == ["Song B", "Song D"]
    tree.selection_set("1")                                    # Song D …
    with player._lock:
        player.queue = player.queue[:1]                        # … taken out elsewhere (the pop-over's ×)
    _pump(root, 0.4)
    assert tree.selection() == () and dock.selected_entries() == []   # no neighbour chosen in its place
    dock.remove_selected()
    assert [t.title for t in player.up_next()] == ["Song B"] and not errors


def test_the_right_pane_keeps_the_keyboard_order(make_window):
    window, root, player, errors = _open(make_window)
    pv = window.playlist
    order = [str(w) for w in pv.frame.winfo_children()]
    # The view switch (in the header), then the box, then This tab | Whole library, then the list.
    assert order.index(str(pv.header)) < order.index(str(pv.search)) < order.index(str(pv.scope_choice)) \
        < order.index(str(pv.table))
    assert not errors


# -- the status line -------------------------------------------------------------------------------------
def test_split_status_never_cuts_inside_a_word():
    measure = lambda text: 7 * len(text)  # noqa: E731
    assert split_status("Shuffle on", 300, measure) == ("", "Shuffle on")
    text = "The network is slower than the music · paused at 2:14 · building up 45 s"
    upper, lower = split_status(text, 300, measure)
    assert upper and lower and f"{upper} {lower}" == text
    assert measure(upper) <= 300 and measure(lower) <= 300
    words = text.split(" ")
    assert all(word in words for word in f"{upper} {lower}".split(" "))
    upper, lower = split_status(" ".join(["word"] * 40), 100, measure)
    assert lower.endswith("…") and set(lower[:-1].split(" ")) == {"word"} and measure(lower) <= 100
    path = "\\\\server\\share\\" + "x" * 30 + " gone"            # a word wider than a line: cut at a slash
    upper, lower = split_status(path, 140, measure)
    assert upper == "\\\\server\\share\\" and lower.startswith("x")


def test_a_long_status_message_takes_a_second_line_and_the_list_never_moves(make_window):
    window, root, player, errors = make_window(geometry="300x640+20+20")
    _pump(root, 0.4)
    tree = window.playlist.tree
    before = (tree.winfo_rooty(), tree.winfo_height(), window.status_row.winfo_height(),
              window.status_label.winfo_rooty())
    message = ("Sleep timer: 30 minutes · bit-perfect: stops after that song, no fade, and then the "
               "window carries on")
    window._set_notice(message, 0)
    _pump(root, 0.6)
    upper, lower = window.status_more.cget("text"), window.status_label.cget("text")
    assert window.status_more.winfo_ismapped() and upper and lower
    assert message.startswith(upper + " ")                    # the first line: whole words from the start
    rest = message[len(upper) + 1:]
    assert lower == rest or (lower.endswith("…") and rest.startswith(lower[:-1])
                             and rest[len(lower) - 1] in " ·:,;")   # (cut after a whole word)
    assert (tree.winfo_rooty(), tree.winfo_height(), window.status_row.winfo_height(),
            window.status_label.winfo_rooty()) == before      # nothing moved
    assert window.status_more.winfo_rooty() + window.status_more.winfo_height() <= window.status_label.winfo_rooty() + 1
    assert window._status_cut and window._status_text == message   # (the tooltip's whole message)
    window._set_notice("Shuffle on", 0)
    _pump(root, 0.4)
    assert not window.status_more.winfo_ismapped() and window.status_label.cget("text") == "Shuffle on"
    assert not window._status_cut and not errors


def test_the_longest_status_messages_fit_the_narrow_window_on_one_line(make_window):
    from ironbark.details import network_line
    from ironbark.engine import NetworkView

    from tkinter import font as tkfont

    from ironbark import theme

    window, root, player, errors = make_window(geometry="420x640+20+20")
    _pump(root, 0.4)
    if tkfont.nametofont("TkDefaultFont", root=root).actual()["size"] != theme.SIZE_BODY:
        pytest.skip("Tk's default font isn't the system's own size here (an earlier test changed the process)")
    messages = [
        network_line(NetworkView("waiting", True, None, "", at=134.0))[0],
        network_line(NetworkView("waiting", True, None, "the download is slower than the music: building up 45 s",
                                 at=134.0))[0],
        network_line(NetworkView("released", True))[0],
        network_line(NetworkView("preparing", True, None,
                                 "waiting for \\\\192.168.80.173 (not answering) · starts by itself when it's back"))[0],
        "Sleep timer: time's up · stops after this song (bit-perfect: no fade)",
        "Sleep timer: 30 minutes · bit-perfect: stops after that song, no fade",
    ]
    for message in messages:
        window._set_notice(message, 0)
        _pump(root, 0.3)
        assert window.status_label.cget("text") == message and not window.status_more.winfo_ismapped(), message
    assert not errors


def test_the_mini_strips_tooltips_name_their_keys(make_window):
    window, root, player, errors = make_window()
    mini = window.mini
    assert mini.next_button.tip.text == f"Next ({keys.hint('next')})"
    assert mini.previous_button.tip.text == f"Previous ({keys.hint('previous')})"
    assert mini.play_button.tip.text == f"Play / pause ({keys.hint('play')})"
    assert mini.full_button.tip.text == f"Full player ({keys.hint('mini')})"
    assert mini.settings_button.tip.text == f"Settings ({keys.hint('settings')})"
    assert mini.close_button.tip.text == f"Close ({keys.hint('quit')})"
    assert not errors


def test_search_the_whole_library_row_uses_the_wide_windows_own_box(make_window, monkeypatch):
    window, root, player, errors = _open(make_window)
    asked = []
    monkeypatch.setattr(window, "find_in_library", lambda query=None: asked.append(query))
    window.playlist.query.set("nothing like it")
    window._search_whole_library("nothing like it")             # the "no matches" row, in a wide window
    _pump(root, 0.3)
    assert window.search_scope == "library" and window._inline.is_open and asked == []
    assert window.playlist.query.get() == "nothing like it"     # (the same words, searched at once)
    window.set_search_scope("tab")
    _resize(root, 420, 900)
    window._search_whole_library("nothing like it")             # the narrow window: the search panel
    assert asked == ["nothing like it"] and not errors

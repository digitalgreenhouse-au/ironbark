"""The shared widgets before the first release: the Settings volume slider asks before a jump up where
bit-perfect leaves no other volume; mini mode and in-app prompts never leave the keyboard on something
hidden or gone; a song change doesn't throw away songs the user picked; an album row shows its whole
sample rate; Enter plays what's lit; the mini strip's Tab order runs left to right. Tk tests run on
the hidden desktop (conftest) and skip without a display."""

from __future__ import annotations

import gc
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ironbark import playlist_view, popups, theme
from ironbark.library import Track

from test_big_lists import _playlist_window
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)


def _new_root(scaling: float | None = None):
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    native = r.tk.call("tk", "scaling")
    if scaling:
        r.tk.call("tk", "scaling", scaling * 96 / 72)
    r.geometry("420x520+60+60")
    theme.apply(r)
    errors = []
    r.report_callback_exception = lambda *info: errors.append(info)
    r.errors = errors
    _pump(r, 0.1)
    return r, native


def _close_root(r, native, scale_before):
    for prompt in list(popups.Prompt.stack):
        prompt.answer(None)
    theme.use(theme.DARK)
    r.tk.call("tk", "scaling", native)   # (Tk's next root starts from the last scaling set)
    theme._scale = scale_before
    r.destroy()


@pytest.fixture
def root():
    scale_before = theme._scale
    r, native = _new_root()
    yield r
    _close_root(r, native, scale_before)
    del r
    gc.collect()   # on this thread (Tk objects freed on another thread abort Python)


def _has_keyboard(root, widget) -> bool:
    return str(root.tk.call("focus", "-lastfor", root)) == str(widget)


def _songs(count: int, rate: int = 44100, bits: int = 16, album: str = "A") -> list[Track]:
    return [Track(Path(f"C:/M/{album}/{n:02d}.flac"), f"Song {n}", artist="X", album=album, track_no=n,
                  duration=180.0, sample_rate=rate, bits=bits) for n in range(1, count + 1)]


# -- the Settings volume slider in bit-perfect without a device volume ------------------------------------
def test_the_volume_slider_asks_before_a_jump_up_where_nothing_else_turns_it_down(make_window, monkeypatch):
    from ironbark import ui

    window, root, player, errors = make_window()
    asked = []
    answer = {"yes": False}
    monkeypatch.setattr(popups, "confirm", lambda *a, **k: asked.append(a[1]) or answer["yes"])
    window.exclusive.set(True)
    player.hardware_volume = False
    window._set_volume(ui.SAFE_VOLUME)
    window._open_settings()
    _pump(root, 0.2)
    slider = window.volume_slider

    def press(value: float):
        """A click on the slider's track at this level (no drag)."""
        inset = slider._radii[1]
        x = inset + (slider.winfo_width() - 2 * inset) * value / 100
        slider._press(SimpleNamespace(x=x))
        _pump(root, 0.05)
        slider._release(SimpleNamespace(x=x))
        _pump(root, 0.05)

    slider.nudge("end")                                       # End: straight to 100% …
    _pump(root, 0.05)
    assert asked == ["Turn the volume up to 100%?"] and player.volume == pytest.approx(ui.SAFE_VOLUME)
    assert slider.value == pytest.approx(ui.SAFE_VOLUME * 100)   # … asked first, and refused: nothing moved
    slider.nudge(4)                                           # Page Up: 20% in one press
    _pump(root, 0.05)
    assert asked[-1] == "Turn the volume up to 50%?" and player.volume == pytest.approx(ui.SAFE_VOLUME)
    press(100)                                                # a click at the far end
    assert asked[-1] == "Turn the volume up to 100%?" and player.volume == pytest.approx(ui.SAFE_VOLUME)
    assert not slider.dragging
    count = len(asked)
    slider.nudge(1)                                           # a step at a time: no question
    _pump(root, 0.05)
    assert player.volume == pytest.approx(ui.SAFE_VOLUME + ui.VOLUME_STEP) and len(asked) == count
    slider.nudge("start")                                     # down is always fine
    _pump(root, 0.05)
    assert player.volume == 0.0 and len(asked) == count
    slider.nudge(4)                                           # up to the safe level: no question
    _pump(root, 0.05)
    assert player.volume == pytest.approx(0.2) and len(asked) == count
    answer["yes"] = True
    slider.nudge("end")                                       # asked, and agreed: 100%
    _pump(root, 0.05)
    assert asked[-1] == "Turn the volume up to 100%?" and player.volume == 1.0
    assert slider.value == pytest.approx(100.0)
    # The Windows volume in charge (not bit-perfect): no question.
    window.exclusive.set(False)
    window._set_volume(ui.SAFE_VOLUME)
    count = len(asked)
    slider.nudge("end")
    _pump(root, 0.05)
    assert player.volume == 1.0 and len(asked) == count
    window._set_volume(ui.SAFE_VOLUME)
    press(90)
    assert player.volume == pytest.approx(0.9, abs=0.01) and len(asked) == count
    assert not errors


# -- mini mode and the keyboard ----------------------------------------------------------------------
def test_mini_mode_takes_the_keyboard_off_the_hidden_list_and_gives_it_back(make_window, tmp_path):
    window, root, player, errors, tab, songs = _playlist_window(make_window, tmp_path)
    tree = window.playlist.tree
    window.playlist.select(1)
    tree.focus_set()
    _pump(root, 0.05)
    assert _has_keyboard(root, tree)
    window.toggle_mini()
    _pump(root, 0.2)
    at = str(root.tk.call("focus", "-lastfor", root))
    assert not any(at == str(w) or at.startswith(f"{w}.") for w in (window.bar, window.body)), at
    window.toggle_mini()                                      # back to the full window: back to the list
    _pump(root, 0.2)
    assert _has_keyboard(root, tree)
    assert not errors


def test_leaving_mini_mode_takes_the_keyboard_off_the_hidden_strip(make_window, tmp_path):
    window, root, player, errors, tab, songs = _playlist_window(make_window, tmp_path)
    tree = window.playlist.tree

    def keys_in_strip() -> bool:
        at = str(root.tk.call("focus", "-lastfor", root))
        return at == str(window.mini) or at.startswith(f"{window.mini}.")

    # Into the strip with the keyboard on nothing of the full window's, then Tab to "Full player" and
    # Space: the strip is put away under the keyboard (Space again would press the hidden button).
    root.focus_set()
    _pump(root, 0.05)
    window.toggle_mini()
    _pump(root, 0.2)
    window.mini.full_button.focus_set()
    _pump(root, 0.05)
    window.mini.full_button.activate()
    _pump(root, 0.2)
    assert not window.mini_mode and not keys_in_strip()
    # From the list, Tab into the strip and back: the keyboard returns to the list, not the strip.
    window.playlist.select(1)
    tree.focus_set()
    _pump(root, 0.05)
    window.toggle_mini()
    _pump(root, 0.2)
    window.mini.play_button.focus_set()
    _pump(root, 0.05)
    window.toggle_mini()
    _pump(root, 0.2)
    assert not keys_in_strip() and _has_keyboard(root, tree)
    assert not errors


def test_the_mini_strips_tab_order_runs_left_to_right_and_close_comes_last(make_window):
    window, root, player, errors = make_window()
    window.toggle_mini()
    _pump(root, 0.2)
    mini = window.mini
    order, widget = [], root
    for _ in range(12):
        widget = widget.tk_focusNext()
        if widget is None or widget in order:
            break
        order.append(widget)
    assert order == [mini.seek, mini.previous_button, mini.play_button, mini.next_button, mini.settings_button,
                     mini.full_button, mini.close_button]
    assert not errors


# -- prompts give the keyboard back ------------------------------------------------------------------------
def test_a_prompt_gives_the_keyboard_back_where_it_was(root):
    from tkinter import ttk

    tree = ttk.Treeview(root)
    tree.pack()
    other = ttk.Entry(root)
    other.pack()
    _pump(root, 0.05)
    def click_cancel(prompt):
        prompt.buttons[1].pressed = True                      # (pressed, then let go over it)
        prompt.buttons[1]._release(SimpleNamespace(x=2, y=2))

    for close in (popups.Prompt.press_default, popups.Prompt.press_cancel, click_cancel):
        tree.focus_set()
        _pump(root, 0.05)
        prompt = popups.Prompt(root, "A question", buttons=(("OK", True), ("Cancel", False)), cancel=False)
        _pump(root, 0.05)
        assert not _has_keyboard(root, tree)
        close(prompt)
        _pump(root, 0.05)
        assert prompt.done and _has_keyboard(root, tree)
    # One box over another: the second gives the keyboard to the first, the first back to the list.
    tree.focus_set()
    first = popups.Prompt(root, "First", buttons=(("OK", True),))
    second = popups.Prompt(root, "Second", buttons=(("OK", True),))
    _pump(root, 0.05)
    second.press_default()
    _pump(root, 0.05)
    at = str(root.tk.call("focus", "-lastfor", root))
    assert at == str(first) or at.startswith(f"{first}.")
    first.press_default()
    _pump(root, 0.05)
    assert _has_keyboard(root, tree)
    # The box underneath answered first (its question became moot): the one left still gives it back.
    first = popups.Prompt(root, "First", buttons=(("OK", True),))
    second = popups.Prompt(root, "Second", buttons=(("OK", True),))
    _pump(root, 0.05)
    first.press_default()
    _pump(root, 0.05)
    second.press_default()
    _pump(root, 0.05)
    assert _has_keyboard(root, tree)
    # Where the keyboard was isn't on show any more: it isn't sent there.
    tree.focus_set()
    prompt = popups.Prompt(root, "Hidden", buttons=(("OK", True),))
    _pump(root, 0.05)
    tree.pack_forget()
    prompt.press_default()
    _pump(root, 0.05)
    assert not _has_keyboard(root, tree)
    tree.pack()
    # Where the keyboard was has gone meanwhile: nothing to give back to, no error.
    other.focus_set()
    prompt = popups.Prompt(root, "Gone", buttons=(("OK", True),))
    _pump(root, 0.05)
    other.destroy()
    prompt.press_default()
    _pump(root, 0.05)
    assert not root.errors


def test_renaming_a_playlist_from_the_list_leaves_the_keyboard_on_the_list(make_window, tmp_path):
    from test_keys import _run

    window, root, player, errors, tab, songs = _playlist_window(make_window, tmp_path)
    tree = window.playlist.tree
    window.playlist.select(1)
    tree.focus_set()
    _pump(root, 0.05)
    root.after(150, lambda: popups.Prompt.stack[-1].press_cancel())   # (Esc, while the box waits)
    assert _run(window, tree, "F2")
    _pump(root, 0.05)
    assert not popups.Prompt.stack and _has_keyboard(root, tree)
    assert not errors


# -- the track list ----------------------------------------------------------------------------------------
def test_a_song_change_keeps_the_songs_the_user_picked(root):
    view = playlist_view.PlaylistView(root, on_play=lambda i: None)
    view.set_tracks(_songs(200), flat=True)
    _pump(root, 0.05)
    view.show_current(0)                                      # nothing selected: the selection follows
    assert view.selected_rows() == [0]
    view.show_current(1)                                      # the last playing song alone: follows again
    assert view.selected_rows() == [1]
    view.select_tracks([3, 4, 6, 7])                          # several picked …
    top = view.list.top
    view.show_current(150)                                    # … the next song starts far down
    assert view.selected_rows() == [3, 4, 6, 7] and view.list.top == top
    assert view.playing == 150 and view.tree.set("3", "no") != playlist_view.PLAYING_MARK
    view.select(5)                                            # one song picked that isn't playing: kept too
    view.show_current(151)
    assert view.selected_rows() == [5] and view.list.top == top
    view.select(151)                                          # the playing song picked: follows
    view.show_current(152)
    assert view.selected_rows() == [152]
    view.clear_current()                                      # (stopped: nothing selected)
    view.show_current(10)
    assert view.selected_rows() == [10]
    assert not root.errors


def test_enter_plays_what_is_lit_after_a_ctrl_click_took_the_keyboards_row_out(root):
    played = []
    view = playlist_view.PlaylistView(root, on_play=played.append)
    view.set_tracks(_songs(8), flat=True)
    _pump(root, 0.05)
    view.select(1)
    view.list.toggle(view._row_of(4))                         # Ctrl+click song 5 in …
    view.list.toggle(view._row_of(4))                         # … and out again
    assert view.selected_rows() == [1] and view.list.selected == view._row_of(4)
    assert view.activate() and played == [1]                  # Enter: the lit song, as Ctrl+Enter and Delete
    view.list.toggle(view._row_of(1))                         # nothing lit: nothing plays
    assert view.selected_rows() == [] and not view.activate() and played == [1]
    assert not root.errors


@pytest.mark.parametrize("scaling", [1.0, 1.25, 1.5])
def test_an_album_rows_sample_rate_is_never_cut_short(scaling):
    scale_before = theme._scale
    r, native = _new_root(scaling)
    try:
        theme._scale = theme.scale(r)
        theme.apply(r)
        view = playlist_view.PlaylistView(r, on_play=lambda i: None)
        rates = (44100, 88200, 96000, 176400, 192000, 352800)
        tracks = [t for rate in rates for t in _songs(1, rate, 24, album=f"R{rate}")]
        view.set_tracks(tracks)
        _pump(r, 0.2)
        assert "quality" not in view.shown                       # (the narrow window: no Format column)
        for g, rate in enumerate(rates):
            view.list.see(view._header_row_of(g))                # (in a whole run the rows can be taller)
            assert view.tree.set(f"a{g}", "time") == f"24/{rate / 1000:g}"
        view.list.see(view._row_of(0))
        assert view.tree.set("0", "time") == "3:00"
        assert not r.errors
    finally:
        _close_root(r, native, scale_before)
        del r
        gc.collect()

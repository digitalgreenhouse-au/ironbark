"""The window is a side player (0.34.0): a tall strip whose width stays in a band and whose height is
free, with no maximise and no full screen. The real window on temp settings with a fake player
(test_light_mini.make_window): nothing of the user's is touched. Tk tests skip without a display."""
from __future__ import annotations

import sys

import pytest

from ironbark import chrome, keys, layout, placement, settings, theme
from ironbark.ui import FULL_MAX_WIDTH, FULL_MIN_SIZE, NO_LIMIT
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)


def _about(got: int, want: int) -> bool:
    """Without the standard title bar, Windows and Tk disagree by a pixel or two on the frame."""
    return abs(got - want) <= 4


def test_the_cap_keeps_the_window_a_column():
    """The widest the window can be is under the width where the split layouts would start."""
    assert FULL_MIN_SIZE[0] < 420 < FULL_MAX_WIDTH < layout.SPLIT_MIN
    assert "full_screen" not in keys.BY_ID and "maximised" not in settings.DEFAULTS
    assert settings._checked({"maximised": True}) == {"maximised": True}   # an older file: kept, ignored
    for name in ("enter_fullscreen", "exit_fullscreen", "is_fullscreen", "toggle_maximise",
                 "keep_maximise_in_work_area"):
        assert not hasattr(chrome, name)


def test_the_full_window_keeps_to_its_width_band_and_any_height(make_window):
    window, root, player, errors = make_window(geometry="420x640+40+40")
    _pump(root, 0.4)
    assert root.minsize() == (theme.px(FULL_MIN_SIZE[0]), theme.px(FULL_MIN_SIZE[1]))
    assert root.maxsize() == (theme.px(FULL_MAX_WIDTH), NO_LIMIT)
    root.geometry("1400x640+0+0")          # dragged or snapped wider: it stops at the cap, still a column
    _pump(root, 0.5)
    assert _about(root.winfo_width(), theme.px(FULL_MAX_WIDTH)) and window._tier == layout.COLUMN
    root.geometry("420x900+0+0")           # taller is fine
    _pump(root, 0.5)
    assert root.winfo_height() >= 850 and _about(root.winfo_width(), 420)
    root.geometry("200x640+0+0")           # narrower stops at the minimum
    _pump(root, 0.5)
    assert _about(root.winfo_width(), theme.px(FULL_MIN_SIZE[0]))
    assert not errors


def test_a_saved_size_wider_than_the_cap_opens_at_the_cap_in_the_column(make_window):
    window, root, player, errors = make_window(geometry="1200x700+0+0")   # left wide by an older version
    _pump(root, 0.6)
    assert _about(root.winfo_width(), theme.px(FULL_MAX_WIDTH)) and window._tier == layout.COLUMN
    assert window.bar.winfo_ismapped() and window.playlist.frame.winfo_ismapped()
    assert window.head.winfo_ismapped() and not window.split.winfo_ismapped()
    assert not errors


def test_the_mini_strip_isnt_capped_and_the_cap_returns_with_the_full_window(make_window):
    window, root, player, errors = make_window()
    _pump(root, 0.4)
    window.toggle_mini()
    _pump(root, 0.5)
    assert window.mini_mode and root.maxsize() == (NO_LIMIT, NO_LIMIT)
    window.toggle_mini()
    _pump(root, 0.5)
    assert not window.mini_mode and root.maxsize() == (theme.px(FULL_MAX_WIDTH), NO_LIMIT)
    assert not errors


def test_the_strip_has_minimise_and_close_only_and_a_double_click_goes_home(make_window):
    window, root, player, errors = make_window(geometry="420x640+40+40")
    _pump(root, 0.4)
    assert not hasattr(window, "toggle_full_screen") and not hasattr(window.bar, "max_btn")
    assert window.bar._caption_room() == (2 * theme.px(theme.CAPTION_W) if chrome.CUSTOM else 0)
    window._bar_double_click()                     # no home yet: it says how to set one, and stays put
    assert window._notice.startswith("No home yet") and _about(root.winfo_width(), 420)
    window.make_home()
    home = placement.parse(window.prefs["home_geometry"])
    assert home and window._notice.startswith("Home set")
    root.geometry("+160+160")
    _pump(root, 0.4)
    window._bar_double_click()                     # a home set: back to it
    _pump(root, 0.5)
    here = placement.parse(root.geometry())
    assert (here[2], here[3]) == (home[2], home[3])
    assert not errors


@pytest.mark.skipif(sys.platform != "win32", reason="the window style is Windows' own")
def test_windows_is_told_the_window_cant_be_maximised(make_window):
    import ctypes

    window, root, player, errors = make_window()
    _pump(root, 0.4)
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongPtrW(chrome._hwnd(root), chrome.GWL_STYLE)
    assert not style & chrome.WS_MAXIMIZEBOX          # so Windows+Up and Snap's maximise do nothing
    assert style & chrome.WS_MINIMIZEBOX and style & chrome.WS_THICKFRAME and not style & chrome.WS_CAPTION
    assert not errors

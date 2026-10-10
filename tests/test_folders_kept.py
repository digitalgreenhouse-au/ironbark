"""0.41.1: a library folder on the network stays listed while Local network access is off — Add
folder… adds to the list instead of replacing it, Remove takes out only the one asked — and a
whole-library search says, once, which folders it can't look through. Also the mini strip's glyph
weights."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

from ironbark import mini_view, places, tabbar, theme
from ironbark.library_view import unsearched_note

NET = Path("Y:/Net Music")


@pytest.fixture
def network_drive(monkeypatch):
    """Y: counts as a network drive (whatever this PC has), everything else as local."""
    monkeypatch.setattr(places, "is_remote", lambda path: str(path).upper().startswith("Y:"))


def test_unsearched_note_names_the_network_folders_left_out(network_drive, tmp_path):
    local = tmp_path / "Music"
    assert unsearched_note([local, NET], {"local_network": False}) == (
        f"Not searched: “{NET}” — on your network, and Local network access is off (Settings → Library)")
    assert unsearched_note([local, NET], {"local_network": True}) == ""
    assert unsearched_note([local], {"local_network": False}) == ""


def test_add_folder_keeps_a_network_folder_that_is_greyed(network_drive, make_window, monkeypatch, tmp_path):
    from ironbark import ui

    local = tmp_path / "Music"
    more = tmp_path / "More Music"
    more.mkdir()
    window, root, _player, errors = make_window(library_folders=[str(local), str(NET)], local_network=False)
    assert window._library_roots() == [local]            # the network folder isn't read…
    monkeypatch.setattr(ui.filedialog, "askdirectory", lambda **kwargs: str(more))
    window._add_library_folder()
    _pump(root, 0.3)
    assert [Path(f) for f in window.prefs["library_folders"]] == [local, NET, more]   # …but it stays listed
    assert window._library_roots() == [local, more]
    assert not errors


def test_remove_folder_keeps_the_greyed_network_folder(network_drive, make_window, monkeypatch, tmp_path):
    from ironbark import ui

    local = tmp_path / "Music"
    more = tmp_path / "More Music"
    more.mkdir()
    window, root, _player, errors = make_window(library_folders=[str(local), str(NET), str(more)],
                                                local_network=False)

    class Removes:
        def __init__(self, *args, **kwargs):
            pass

        def wait(self):
            return True

    monkeypatch.setattr(ui.popups, "Prompt", Removes)
    window._remove_library_folder(more)
    _pump(root, 0.3)
    assert [Path(f) for f in window.prefs["library_folders"]] == [local, NET]
    assert not errors


def test_a_whole_library_search_says_once_what_it_leaves_out(network_drive, make_window, tmp_path):
    local = tmp_path / "Music"
    window, root, _player, errors = make_window(library_folders=[str(local), str(NET)], local_network=False)
    window._search_library_for_list(["adele"])
    _pump(root, 0.5)
    assert str(NET) in window._notice and "Local network access" in window._notice
    window._set_notice("", 0)
    window._search_library_for_list(["adele"])   # a second search: no nagging
    _pump(root, 0.5)
    assert window._notice == ""
    assert not errors


def test_a_search_with_every_folder_read_says_nothing(make_window, tmp_path):
    window, root, _player, errors = make_window()
    window._search_library_for_list(["adele"])
    _pump(root, 0.5)
    assert "Not searched" not in (window._notice or "")
    assert not errors


def test_mini_strip_glyphs_are_weighed_against_each_other(make_window):
    import tkinter.font as tkfont

    window, root, _player, _errors = make_window()
    window.toggle_mini()
    _pump(root, 0.3)
    mini = window.mini
    for button in (mini.previous_button, mini.next_button):
        assert button._glyph == theme.px(mini_view.SKIP_GLYPH) < theme.px(button.SKIP_SIZE)
    # (Without Segoe's icon font — a Mac — the plain glyphs are set a point bigger to weigh the same.)
    icon_size = theme.ICON if tabbar.icon_font(root) else theme.ICON + 1
    for button in (mini.settings_button, mini.full_button, mini.close_button):
        assert tkfont.Font(root=root, font=button.cget("font")).cget("size") == icon_size

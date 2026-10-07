"""The main window's wide layouts (layout.py) on real windows: the parts move into the two panes and
back, nothing is left behind, the cover is never enlarged, the list's columns, the strip's maximise
button, F11 full screen on the same window, and the honest count of a Library tab. (The narrow
window staying exactly as it was is test_narrow_golden.py.)"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path
from tkinter import font as tkfont
from types import SimpleNamespace

import pytest
from PIL import Image

from ironbark import chrome, layout, playlist_view, settings, tabbar, theme
from ironbark.engine import State, Status
from ironbark.library import Track
from ironbark.tabbar import Popover
from test_light_mini import _colours_in_use, _only_in, _pump, make_window  # noqa: F401  (make_window: a fixture)


def _open(make_window, size=(1920, 1080), scaling=1.0, **prefs):
    w, h = size
    window, root, player, errors = make_window(scaling=scaling, volume=0.8,
                                               geometry=f"{round(w * scaling)}x{round(h * scaling)}+0+0", **prefs)
    _pump(root, 0.5)
    return window, root, player, errors


def _resize(root, w, h, scaling=1.0, seconds=0.5):
    root.geometry(f"{round(w * scaling)}x{round(h * scaling)}+0+0")
    _pump(root, seconds)


def _container(widget) -> str:
    return str(widget.pack_info()["in"]) if widget.winfo_manager() == "pack" else ""


def _centre(widget) -> tuple[int, int]:
    return widget.winfo_rootx() + widget.winfo_width() // 2, widget.winfo_rooty() + widget.winfo_height() // 2


def _tracks(folder: Path, song: Path, n: int, artist="Artist", album="Album", bits=16, rate=44100) -> list[Track]:
    return [Track(path=song if i == 0 else folder / f"{i + 1:02d} Song.flac", title=f"Song {i + 1}", artist=artist,
                  album=album, duration=200.0, sample_rate=rate, bits=bits, channels=2, track_no=i + 1)
            for i in range(n)]


def _playlist_tracks(song: Path) -> list[Track]:
    """A playlist: songs from two albums, two artists, two qualities."""
    a = _tracks(song.parent, song, 3, artist="First Artist", album="First Album")
    b = _tracks(song.parent.parent / "Other", song, 3, artist="Second Artist", album="Second Album", bits=24,
                rate=96000)
    return a + b


# -- layouts ---------------------------------------------------------------------------------------
def test_the_tier_follows_the_window(make_window):
    window, root, player, errors = _open(make_window)
    assert window._tier == layout.SPLIT_L
    for size, tier in (((1280, 800), layout.SPLIT_M), ((1000, 800), layout.SPLIT), ((420, 900), layout.COLUMN),
                       ((1920, 1080), layout.SPLIT_L)):
        _resize(root, *size)
        assert window._tier == tier, size
    assert not errors


def test_the_parts_move_into_the_two_panes(make_window):
    window, root, player, errors = _open(make_window)
    for part in (window.head, window.seek_row, window.transport):
        assert _container(part) == str(window.stack)
    assert _container(window.playlist.frame) == str(window.right)
    assert window.split.winfo_ismapped() and window.status_row.winfo_ismapped()
    # Each part shows (none is hidden behind the frame it's packed in).
    for widget in (window.title_label, window.art_label, window.playlist.tree, window.play_button):
        assert root.winfo_containing(*_centre(widget)) is widget, widget
    rows = window.playlist.tree.winfo_height() // int(window.playlist.tree.tk.call(
        "ttk::style", "lookup", window.playlist.tree.cget("style"), "-rowheight"))
    assert rows >= 13
    assert window.kind_label.winfo_ismapped() and window.kind_label.cget("text") == "NOW PLAYING"
    assert window.title_label.cget("style") == "DisplayLarge.TLabel"
    assert window.playlist.header.winfo_ismapped() and window.playlist.count_label.cget("text").startswith("Album · 1 song")
    # Back to the narrow window: the panes go, the parts come home, the caption and header row hide.
    _resize(root, 420, 900)
    assert not window.split.winfo_manager() and _container(window.head) == str(window.body)
    assert not window.kind_label.winfo_manager() and not window.playlist.header.winfo_manager()
    assert window.title_label.cget("style") == "Display.TLabel"
    assert not errors


def test_the_layout_matches_the_plan_at_150_percent(make_window):
    window, root, player, errors = _open(make_window, size=(1280, 720), scaling=1.5)
    assert window._tier == layout.SPLIT_M and theme.scale() == pytest.approx(1.5, abs=0.01)
    plan = window._plan
    assert abs(window.left.winfo_width() - theme.px(plan.left_w)) <= 2
    assert abs(window.stack.winfo_width() - theme.px(plan.stack_w)) <= 2
    assert window.art_size == theme.px(plan.slot) and plan.slot % layout.SLOT_STEP == 0
    assert not errors


def test_twenty_switches_leave_nothing_behind(make_window):
    window, root, player, errors = _open(make_window, size=(420, 900))
    before = [str(w) for w in window.body.pack_slaves()]
    for i in range(20):
        _resize(root, *((1500, 900) if i % 2 == 0 else (420, 900)), seconds=0.15)
    _pump(root, 0.4)
    assert window._tier == layout.COLUMN
    assert [str(w) for w in window.body.pack_slaves()] == before
    assert window.stack.pack_slaves() == [] and window.right.pack_slaves() == []
    _resize(root, 1500, 900)
    assert [str(w) for w in window.stack.pack_slaves()] == [str(window.head), str(window.seek_row),
                                                            str(window.transport)]
    assert window.right.pack_slaves() == [window.playlist.frame]
    assert not errors


def test_the_column_chrome_constant_matches_the_narrow_window(make_window):
    """layout.COLUMN_CHROME is the narrow window's height less its cover and list, in logical px (a
    constant, so the 780–879 px rule never depends on what's on show). Measured at the display's own
    scaling, it's within a few px."""
    window, root, player, errors = make_window(geometry="420x900+0+0")
    _pump(root, 0.5)
    if tkfont.nametofont("TkDefaultFont", root=root).actual()["size"] != theme.SIZE_BODY:
        pytest.skip("Tk's default font isn't the system's own size here (an earlier test changed the process)")
    measured = sum(w.winfo_reqheight() for w in (window.bar, window.head_text, window.seek_row, window.transport,
                                                 window.status_row)) + theme.px(48)
    assert abs(measured / theme.scale() - layout.COLUMN_CHROME) <= 8
    assert not errors


# -- the cover -------------------------------------------------------------------------------------
def _with_cover(window, root, side: int):
    folder = Path(window.tabs[0].path)
    Image.new("RGB", (side, side), (200, 40, 40)).save(folder / "cover.jpg")
    window.art_size = 0
    window._art_key = None
    window._fit_art()
    _pump(root, 1.0)


def test_a_small_cover_is_shown_at_its_own_size_on_a_mat(make_window):
    window, root, player, errors = _open(make_window)
    _with_cover(window, root, 300)
    slot = window.art_size
    assert slot == theme.px(layout.COVER_MAX)
    assert window.art_label.winfo_width() == slot            # the mat fills the slot…
    assert "300 × 300 px" in window._cover_tip and "not enlarged" in window._cover_tip
    picture = window._art_image
    assert picture.size == (300, 300) and picture.info["source_size"] == (300, 300)   # …the cover isn't enlarged
    assert not errors


def test_a_big_cover_fills_the_slot_and_the_narrow_window_still_enlarges(make_window):
    window, root, player, errors = _open(make_window)
    _with_cover(window, root, 1200)
    assert window._art_image.size == (window.art_size,) * 2
    assert f"shown at {window.art_size}" in window._cover_tip
    _resize(root, 420, 900)
    _with_cover(window, root, 300)
    assert window._art_image.size == (window.art_size,) * 2 and window.art_size > 300   # as it always did
    assert window._cover_tip == ""
    assert not errors


def test_the_volume_badge_clears_the_buttons_at_the_narrowest_stack(make_window):
    window, root, player, errors = _open(make_window, size=(900, 400))
    assert window._plan.stack_w == layout.STACK_MIN
    _pump(root, 0.3)
    badge, repeat = window.volume_badge, window.repeat_button
    assert badge.winfo_ismapped()
    assert badge.winfo_rootx() >= repeat.winfo_rootx() + repeat.winfo_width()
    assert not errors


def test_the_cover_hides_in_a_short_wide_window_and_with_the_switch(make_window):
    window, root, player, errors = _open(make_window, size=(1600, 330))
    assert window._plan.slot == 0 and not window.art_box.winfo_ismapped()
    _resize(root, 1600, 900)
    assert window.art_box.winfo_ismapped() and window.art_size > 0
    window.show_art.set(False)
    window._toggle_art()
    _pump(root, 0.3)
    assert not window.art_box.winfo_ismapped() and window._plan.slot == 0
    window.show_art.set(True)
    window._toggle_art()
    _pump(root, 0.5)
    assert window.art_box.winfo_ismapped()
    assert not errors


# -- status line, Up next, empty window, strip -------------------------------------------------------
def test_up_next_and_the_status_line_in_a_wide_window(make_window):
    window, root, player, errors = _open(make_window)
    window._queue_tracks([player.track, player.track], False)
    _pump(root, 0.4)
    assert _container(window.up_next_label) == str(window.right) and window.up_next_label.winfo_ismapped()
    assert window.status_row.winfo_ismapped()
    _resize(root, 420, 900)
    assert _container(window.up_next_label) == str(window.body) and window.up_next_label.winfo_ismapped()
    assert not errors


def test_the_empty_window_and_the_plus_button(make_window):
    window, root, player, errors = _open(make_window)
    window.close_tab(0)
    _pump(root, 0.4)
    assert window.empty_view.winfo_ismapped() and not window.split.winfo_manager()
    add = window.bar.add_btn
    assert add.winfo_ismapped() and add.winfo_x() == window.bar._add_x(0)
    assert window.bar.max_btn is not None and window.bar.max_btn.winfo_ismapped()   # still a wide window
    _resize(root, 420, 900)
    assert window.empty_view.winfo_ismapped() and add.winfo_x() == window.bar._add_x(0)
    assert not window.bar.max_btn.winfo_ismapped()
    window.reopen_tab()
    _pump(root, 1.0)
    _resize(root, 1920, 1080)
    assert window.split.winfo_ismapped() and not window.empty_view.winfo_ismapped()
    assert not errors


def test_the_maximise_button_shows_only_in_a_wide_window(make_window):
    if not chrome.CUSTOM:
        pytest.skip("the strip draws its own window buttons on Windows only")
    window, root, player, errors = _open(make_window, size=(420, 900))
    caption = theme.px(theme.CAPTION_W)
    assert window.bar.max_btn is None and window.bar.min_btn.place_info()["x"] == str(-caption)
    _resize(root, 1920, 1080)
    assert window.bar.max_btn.winfo_ismapped()
    assert window.bar.min_btn.place_info()["x"] == str(-2 * caption)
    assert window.bar.tab_max == layout.TAB_MAX[layout.SPLIT_L]
    window.bar.max_btn.command()
    _pump(root, 0.4)
    assert root.state() == "zoomed"
    window._save_if_changed()
    assert window.prefs["maximised"] is True
    window.bar.max_btn.command()
    _pump(root, 0.4)
    assert root.state() == "normal"
    window._save_if_changed()
    assert window.prefs["maximised"] is False
    assert not errors


def test_a_window_left_maximised_opens_maximised(make_window):
    window, root, player, errors = _open(make_window, size=(1000, 700), maximised=True)
    assert root.state() == "zoomed"
    assert not errors


# -- what survives a switch ------------------------------------------------------------------------
def test_search_selection_scroll_and_keyboard_survive_a_switch(make_window):
    window, root, player, errors = _open(make_window, size=(420, 900))
    song = player.track.path
    tracks = _tracks(song.parent, song, 60)
    window.tabs[0].tracks = tracks
    window._show_current_tab()
    _pump(root, 0.3)
    view = window.playlist
    view.select(40)
    view.list.scroll_to(30)                              # (the list scrolls through its rows itself)
    _pump(root, 0.2)
    top = view.list.top
    assert top > 0
    view.entry.focus_set()
    _pump(root, 0.1)
    focus = root.focus_get()
    _resize(root, 1920, 1080)
    assert view.selected_track() == 40                   # (Tk holds only the rows on show: the list knows)
    assert root.focus_get() is focus
    view.query.set("Song 4")
    view._search_now()
    _resize(root, 1300, 900)
    _resize(root, 420, 900)
    assert view.query.get() == "Song 4" and view.selected_track() == 40

    view.query.set("")
    view._search_now()
    view.list.scroll_to(top)
    _resize(root, 1920, 1080)
    assert abs(view.list.top - top) <= 3

    assert not errors


def test_keys_still_work_after_a_switch(make_window):
    window, root, player, errors = _open(make_window, size=(420, 900))
    _resize(root, 1920, 1080)
    _resize(root, 420, 900)
    _resize(root, 1920, 1080)
    window.tabs.append(window.tabs[0].__class__("album", window.tabs[0].path, tracks=list(window.tabs[0].tracks)))
    window._refresh_tabs()
    tree = window.playlist.tree
    tree.focus_force()
    _pump(root, 0.1)
    if root.focus_get() is not tree:
        pytest.skip("this session can't give a window the keyboard")
    for sequence in ("<KeyPress-space>", "<KeyPress-n>", "<KeyPress-p>"):
        tree.event_generate(sequence)
    _pump(root, 0.05)
    assert player.calls[-3:] == ["toggle_pause", "next", "previous"]
    shuffle = player.shuffle
    tree.event_generate("<KeyPress-s>")
    tree.event_generate("<Control-Tab>")
    _pump(root, 0.1)
    assert player.shuffle != shuffle and window.current == 1
    tree.event_generate("<Control-f>")
    _pump(root, 0.1)
    assert root.focus_get() is window.playlist.entry
    assert not errors


def test_mini_strip_round_trip_from_a_wide_window(make_window):
    window, root, player, errors = _open(make_window)
    window.toggle_mini()
    _pump(root, 0.4)
    assert window.mini_mode and window._tier == layout.SPLIT_L   # (the strip doesn't count as a size)
    window.toggle_mini()
    _pump(root, 0.6)
    assert not window.mini_mode and window._tier == layout.SPLIT_L
    assert _container(window.head) == str(window.stack) and window.split.winfo_ismapped()
    assert not errors


def test_settings_opens_in_a_wide_window(make_window):
    window, root, player, errors = _open(make_window)
    window._open_settings()
    _pump(root, 0.3)
    assert window.settings_pop.is_open
    Popover.close_open()
    assert not errors


def test_light_theme_leaves_no_dark_colours(make_window):
    window, root, player, errors = _open(make_window)
    window.prefs["theme"] = "light"
    window._apply_theme()
    _pump(root, 0.6)
    leftover = _colours_in_use(root) & _only_in(theme.DARK, theme.LIGHT)
    assert not leftover
    window.prefs["theme"] = "dark"
    window._apply_theme()
    _pump(root, 0.3)
    assert not errors


# -- the list's columns ----------------------------------------------------------------------------
def _shown(window) -> tuple:
    return tuple(str(c) for c in window.playlist.tree.cget("displaycolumns"))


def test_columns_show_by_themselves_where_values_vary(make_window):
    window, root, player, errors = _open(make_window)
    assert _shown(window) == ("no", "title", "time")              # one album: nothing repeated
    window.tabs[0].tracks = _playlist_tracks(player.track.path)
    window.tabs[0].kind = "playlist"
    window._show_current_tab()
    _pump(root, 0.3)
    assert _shown(window) == ("no", "title", "artist", "album", "quality", "time")
    tree = window.playlist.tree
    assert tree.set("3", "quality") == "24/96"
    width = sum(int(tree.column(c, "width")) for c in _shown(window))
    assert width <= tree.winfo_width()                             # Time is never cut off
    assert int(tree.column("artist", "width")) <= theme.px(playlist_view.WIDE_CAPS["artist"])
    _resize(root, 1000, 800)                                       # SPLIT: room for Artist only
    assert _shown(window) == ("no", "title", "artist", "time")
    _resize(root, 420, 900)                                        # the narrow window's own setting
    assert _shown(window) == ("no", "title", "time")
    assert tree.set("0", "title").endswith("·  First Artist")      # (the artist with the title there)
    assert not errors


def test_unticking_a_wide_column_keeps_it_off(make_window):
    window, root, player, errors = _open(make_window)
    window.tabs[0].tracks = _playlist_tracks(player.track.path)
    window._show_current_tab()
    _pump(root, 0.3)
    window._set_track_column("album", False)
    assert "album" not in _shown(window) and window.prefs["wide_track_columns"] == ["artist", "quality"]
    assert window.prefs["track_columns"] == []                    # the narrow window's setting untouched
    _resize(root, 1300, 900)
    assert "album" not in _shown(window)
    window._set_track_column("album", True)
    assert "album" in _shown(window)
    assert not errors


def test_the_heading_menu_says_when_a_column_is_hidden_for_repeating(make_window):
    window, root, player, errors = _open(make_window)
    seen = []
    window._columns_menu(SimpleNamespace(add_check=lambda label, on, command: seen.append((label, on))))
    assert ("Artist (same on every row)", True) in seen and ("Format (same on every row)", True) in seen
    _resize(root, 420, 900)
    seen.clear()
    window._columns_menu(SimpleNamespace(add_check=lambda label, on, command: seen.append((label, on))))
    assert seen == [("Artist", False), ("Album", False), ("Plays", False)]
    assert not errors


def test_album_rows_put_the_quality_in_the_format_column(make_window):
    window, root, player, errors = _open(make_window)
    song = player.track.path
    tracks = _tracks(song.parent, song, 3) + _tracks(song.parent.parent / "Other", song, 3, album="Other",
                                                     bits=24, rate=96000)
    window.tabs[0].kind = "library"
    window.tabs[0].tracks = tracks
    window._show_current_tab()
    _pump(root, 0.3)
    tree = window.playlist.tree
    header = tree.get_children()[0]
    assert "quality" in _shown(window)
    assert tree.set(header, "quality") == "16/44.1" and tree.set(header, "time") == ""
    _resize(root, 420, 900)
    assert tree.set(header, "time") == "16/44.1"                  # the narrow window: under Time, as before
    assert not errors


# -- the count -------------------------------------------------------------------------------------
def test_a_library_tab_counts_honestly_in_both_layouts(make_window):
    window, root, player, errors = _open(make_window, size=(420, 900))
    song = player.track.path
    tracks = _tracks(song.parent, song, 3) + _tracks(song.parent.parent / "Other", song, 4, album="Other")
    window.tabs[0].kind = "library"
    window.tabs[0].tracks = tracks
    window.playing_tab = None
    player.status = lambda: Status(State.STOPPED, -1, None, 0.0, 0.0)   # nothing playing…
    window.tabs[0].last_index, window.tabs[0].resume_at = -1, 0.0            # …nor played in this tab
    window._show_current_tab()
    _pump(root, 0.4)
    assert window.format_label.cget("text") == "Library · 7 songs · 2 albums · 23:20"
    assert window.sub_label.cget("text") == "Press play to start"
    _resize(root, 1920, 1080)
    assert window.playlist.count_label.cget("text") == "Library · 7 songs · 2 albums · 23:20"
    assert window.kind_label.cget("text") == "LIBRARY"
    assert not errors


def test_a_long_title_is_shortened_with_its_whole_text_in_a_tooltip(make_window):
    window, root, player, errors = _open(make_window, size=(1000, 800))
    long = "A Very Long Song Title That Goes On And On Past The Width Of Any Stack " * 2
    player.track.title = long
    _pump(root, 0.6)
    shown = window.title_label.cget("text")
    assert shown.endswith("…") and len(shown) < len(long)
    assert window.title_label.winfo_reqwidth() <= theme.px(window._plan.stack_w)
    assert window._head_full[0] == long and window._head_cut[0]
    assert window._mini_state["title"] == long                    # the mini strip gets the whole title
    assert not errors


# -- full screen -----------------------------------------------------------------------------------
def _frame_rect(root) -> tuple[int, int, int, int]:
    import ctypes.wintypes as wt

    rect = wt.RECT()
    ctypes.windll.user32.GetWindowRect(int(root.wm_frame(), 16), ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


@pytest.mark.skipif(sys.platform != "win32", reason="the borderless full screen is Windows' own")
def test_f11_full_screen_keeps_the_window_and_comes_back_exactly(make_window):
    window, root, player, errors = _open(make_window, size=(1280, 800))
    window._save_if_changed()
    geometry, handle, rect = window.prefs["geometry"], root.wm_frame(), _frame_rect(root)
    window.toggle_full_screen()
    _pump(root, 0.5)
    assert chrome.is_fullscreen(root) and root.wm_frame() == handle      # the same window handle
    assert _frame_rect(root) == chrome.monitor_rect(root)                # covers its monitor exactly
    assert placement_current(root) is None
    window._save_if_changed()
    assert window.prefs["geometry"] == geometry and window.prefs["maximised"] is False   # never saved
    # Esc with a pop-over open only closes the pop-over; then it leaves full screen.
    window._open_settings()
    _pump(root, 0.2)
    Popover._escape(root, SimpleNamespace(widget=root))
    assert not window.settings_pop.is_open and chrome.is_fullscreen(root)
    Popover._escape(root, SimpleNamespace(widget=window.playlist.entry))   # typing in the search: no
    assert chrome.is_fullscreen(root)
    Popover._escape(root, SimpleNamespace(widget=window.playlist.tree))
    _pump(root, 0.5)
    assert not chrome.is_fullscreen(root) and root.wm_frame() == handle
    assert _frame_rect(root) == rect                                     # exactly where it was
    assert not errors


def placement_current(root):
    from ironbark import placement

    return placement.current(root)


@pytest.mark.skipif(sys.platform != "win32", reason="the borderless full screen is Windows' own")
def test_full_screen_from_maximised_goes_back_to_maximised_and_z_leaves_it(make_window):
    window, root, player, errors = _open(make_window, size=(1280, 800))
    root.state("zoomed")
    _pump(root, 0.4)
    window.toggle_full_screen()
    _pump(root, 0.4)
    assert chrome.is_fullscreen(root)
    window._save_if_changed()
    assert window.prefs["maximised"] is True                        # as it was before full screen
    window._bar_double_click()                                      # double-clicking the strip leaves it
    _pump(root, 0.4)
    assert not chrome.is_fullscreen(root) and root.state() == "zoomed"
    window.toggle_full_screen()
    _pump(root, 0.3)
    window.toggle_mini()                                            # so does the mini strip
    _pump(root, 0.3)
    assert not chrome.is_fullscreen(root) and window.mini_mode
    window.toggle_full_screen()                                     # (not from the mini strip)
    assert not chrome.is_fullscreen(root)
    window.toggle_mini()
    _pump(root, 0.3)
    assert not errors


# -- the keyboard's order, and what a switch paints -------------------------------------------------
def _tab_order(window) -> list[str]:
    """The widgets Tab goes through from the strip's + button, in order (by name on the window)."""
    names = {str(window.playlist.entry): "search", str(window.playlist.tree): "list",
             str(window.volume_badge): "badge", str(window.play_button): "play", str(window.seek): "seek"}
    order, widget, seen = [], window.bar.add_btn, set()
    while widget is not None and str(widget) not in seen:
        seen.add(str(widget))
        if str(widget) in names:
            order.append(names[str(widget)])
        widget = widget.tk_focusNext()
    return order


def test_tab_goes_from_the_search_box_to_the_list_in_every_layout(make_window):
    window, root, player, errors = _open(make_window, size=(420, 900))
    narrow = _tab_order(window)
    assert narrow == ["seek", "play", "badge", "search", "list"]
    _resize(root, 1920, 1080)
    assert _tab_order(window) == narrow          # the search box sits above the list in the wide window too
    _resize(root, 420, 900)
    assert _tab_order(window) == narrow          # and a trip to a wide size leaves the order as it was
    assert not errors


def _layout_state(window) -> dict:
    return dict(left=window.left.winfo_width(), stack=window.stack.winfo_ismapped(),
                stack_w=window.stack.winfo_width(), art=window.art_box.winfo_height())


def test_a_switch_paints_the_panes_already_in_place(make_window):
    """Measuring a new layout makes Tk lay out and paint it: by then the panes must already have
    (about) their final sizes, or the window flashes a list across its whole width, with no cover or
    transport, as it crosses a breakpoint."""
    window, root, player, errors = _open(make_window, size=(420, 900))
    painted = []
    measure = window._measure_heights

    def measuring():
        root.update_idletasks()
        painted.append(_layout_state(window))
        return measure()

    window._measure_heights = measuring
    for size in ((1920, 1080), (420, 900), (1300, 900), (1920, 1080)):
        painted.clear()
        _resize(root, *size)
        final = _layout_state(window)
        for state in painted:
            if window._tier == layout.COLUMN:
                continue
            assert state["stack"], size
            for key in ("left", "stack_w", "art"):
                assert abs(state[key] - final[key]) <= theme.px(24), (size, key, state, final)
    assert not errors


def test_a_wide_window_is_planned_for_its_own_size_before_it_shows(make_window, monkeypatch):
    """Before it's first shown Tk calls a window 200 px square: a wide window opening is planned for
    the size it opens at, so its cover is fetched for the right slot from the start."""
    from ironbark.ui import PlayerWindow

    planned = []
    settle = PlayerWindow._settle_size

    def settling(self, geometry):
        planned.append(self._plan)
        settle(self, geometry)

    monkeypatch.setattr(PlayerWindow, "_settle_size", settling)
    window, root, player, errors = _open(make_window)
    assert window._tier == layout.SPLIT_L
    assert planned[0] == window._plan and planned[0].slot == layout.COVER_MAX
    assert not errors


@pytest.mark.skipif(not chrome.CUSTOM, reason="the strip moves the window itself on Windows only")
def test_pressing_the_strip_in_full_screen_leaves_the_window_where_it_is(make_window, monkeypatch):
    window, root, player, errors = _open(make_window, size=(1280, 800))
    moves = []
    monkeypatch.setattr(chrome, "start_move", lambda w: moves.append(w))
    canvas = window.bar.canvas
    empty = (canvas.winfo_width() // 2, theme.px(theme.ROW_Y))
    assert window.bar._slot_at(*empty) is None
    window.bar._pressed(SimpleNamespace(x=empty[0], y=empty[1], x_root=0, y_root=0))
    assert len(moves) == 1                       # a window drag, as ever…
    window.toggle_full_screen()
    _pump(root, 0.3)
    window.bar._pressed(SimpleNamespace(x=empty[0], y=empty[1], x_root=0, y_root=0))
    assert len(moves) == 1                       # …but not out of full screen
    window._leave_full_screen()
    _pump(root, 0.3)
    assert not errors


@pytest.mark.skipif(not chrome.CUSTOM, reason="the strip draws its own window buttons on Windows only")
def test_the_maximise_button_made_while_maximised_says_restore(make_window):
    window, root, player, errors = _open(make_window, size=(420, 900), maximised=True)
    if root.state() != "zoomed" or window._tier == layout.COLUMN:
        pytest.skip("this screen is too narrow for a wide window when maximised")
    button = window.bar.max_btn
    assert button.cget("text") == tabbar.glyph(root, "restore") and button.tip.text == "Restore"
    button.command()
    _pump(root, 0.4)
    assert root.state() == "normal"
    assert not errors


# -- speed -----------------------------------------------------------------------------------------
def test_a_switch_with_a_big_library_tab_is_quick(make_window):
    window, root, player, errors = _open(make_window, size=(420, 900))
    song = player.track.path
    tracks = []
    for album in range(50):
        tracks += _tracks(song.parent.parent / f"Album {album}", song, 13, artist=f"Artist {album % 7}",
                          album=f"Album {album}")
    window.tabs[0].kind = "library"
    window.tabs[0].tracks = tracks
    window._show_current_tab()
    _pump(root, 0.5)
    times = []
    for size in ((1920, 1080), (420, 900), (1920, 1080), (420, 900)):
        root.geometry(f"{size[0]}x{size[1]}+0+0")
        root.update_idletasks()
        start = time.perf_counter()
        window._apply_layout()
        root.update_idletasks()
        times.append(time.perf_counter() - start)
        _pump(root, 0.3)
    assert sorted(times)[len(times) // 2] < 0.5, times
    assert not errors


def test_settings_know_the_new_preferences():
    assert settings.DEFAULTS["wide_track_columns"] == ["artist", "album", "quality"]
    assert settings.DEFAULTS["maximised"] is False
    assert settings._checked({"wide_track_columns": "artist", "maximised": "yes"}) == {}
    assert settings._checked({"wide_track_columns": ["album", 3]}) == {"wide_track_columns": ["album"]}

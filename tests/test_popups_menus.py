"""0.21.0: in-app prompts instead of Windows' boxes, right-click menus in the app's
colours, and Settings that scroll. The prompts answer right (OK / Cancel / Enter / Esc, name
checks), are modal, and the hearing-safety warning can't be missed; the menus run the right
command, move with the keys, grey out what can't be done and show submenus in place; Settings
reaches its last line in a short window. Tk smoke tests (skipped without a display)."""

from __future__ import annotations

import gc
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import conftest
import pytest
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

from ironbark import playlists, popups, recycle, tabbar, theme
from ironbark.popups import PopupMenu, Prompt
from ironbark.tabbar import glyph

PACKAGE = Path(__file__).resolve().parents[1] / "ironbark"


# -- no display needed ---------------------------------------------------------------------------
def test_no_windows_boxes_or_menus_are_left_in_the_app():
    """Every simpledialog / messagebox / tk.Menu is gone; Windows' own file and folder pickers stay."""
    found = []
    for path in PACKAGE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in (r"\bsimpledialog\b", r"\bmessagebox\b", r"tk\.Menu\b", r"\.tk_popup\(", r"Pop\.TButton"):
            if re.search(pattern, text):
                found.append(f"{path.name}: {pattern}")
    assert found == []


def test_menu_items_build_a_tree():
    items = popups.MenuItems()
    items.add_separator()                         # never at the top…
    items.add("Play", lambda: None, "Enter")
    items.add_separator()
    items.add_separator()                         # …nor two in a row
    sub = items.add_submenu("Columns")
    sub.add_check("Artist", True, lambda: None)
    assert items.labels() == ["Play", "Columns"]
    assert [i.separator for i in items.items] == [False, True, False]
    assert items.find("Columns").submenu is sub and items.find("Columns").shortcut == "›"
    assert sub.items[0].checked is True
    long = items.add("x" * 80, lambda: None)
    assert len(long.label) == popups.MENU_TEXT and long.label.endswith("…")


def test_new_colour_pairs_pass_wcag():
    """The tick on a highlighted menu row, the warning edge and the red ring are graphics (3:1)."""
    for palette in (theme.DARK, theme.LIGHT):
        roles = palette.roles()
        for fg, bg in (("ACCENT_TEXT", "HOVER"), ("WARN", "SURFACE"), ("WARN", "BG"), ("DANGER", "SURFACE")):
            assert (fg, bg) in theme.GRAPHIC_PAIRS
            assert theme.contrast_ratio(roles[fg], roles[bg]) >= theme.AA_GRAPHICS, (palette.name, fg, bg)


# -- Tk ------------------------------------------------------------------------------------------
@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.geometry("420x520+60+60")
    r.title("DG test — popups")
    theme.apply(r)
    errors = []
    r.report_callback_exception = lambda *info: errors.append(info)
    r.errors = errors
    _pump(r, 0.2)
    yield r
    theme.use(theme.DARK)
    for prompt in list(Prompt.stack):
        prompt.answer(None)
    r.destroy()
    del r
    gc.collect()   # on this thread (Tk objects freed on another thread abort Python)


def answer_later(root, action, ms=60):
    """Answer the next prompt while a helper waits for it: `action(prompt)` once it's up."""
    seen = []

    def go():
        if not Prompt.stack:
            root.after(20, go)
            return
        seen.append(Prompt.stack[-1])
        action(Prompt.stack[-1])
    root.after(ms, go)
    return seen


def test_ask_text_enter_gives_the_text_esc_gives_none(root):
    seen = answer_later(root, lambda p: (p.text.set("  Road trip  "), p.press_default()))
    assert popups.ask_text(root, "New playlist", "Name:", initial="Playlist") == "Road trip"
    assert seen[0].entry is not None and seen[0].done
    answer_later(root, lambda p: p.press_cancel())
    assert popups.ask_text(root, "New playlist", initial="Playlist") is None
    answer_later(root, lambda p: p._chosen(1))   # the Cancel button
    assert popups.ask_text(root, "New playlist", initial="Playlist") is None
    assert Prompt.stack == [] and not root.errors


def test_ask_text_keeps_the_box_open_until_the_name_is_fine(root):
    taken = {"rock"}
    steps = []

    def act(p):
        p.text.set("Rock")
        p.press_default()                                         # refused: still open, says why
        steps.append((p.done, p.problem_label.cget("text")))
        p.text.set("Rock 2")                                      # typing clears the note
        steps.append(p.problem_label.cget("text"))
        p.press_default()

    answer_later(root, act)
    name = popups.ask_text(root, "New playlist", initial="x",
                           validate=lambda t: "There's already one called that." if t.lower() in taken else None)
    assert name == "Rock 2"
    assert steps == [(False, "There's already one called that."), ""]


def test_confirm_and_tell(root):
    answer_later(root, lambda p: p._chosen(0))                # a click on Delete (Enter answers No)
    assert popups.confirm(root, "Delete it?", ok="Delete", danger=True) is True
    answer_later(root, lambda p: p.press_cancel())
    assert popups.confirm(root, "Delete it?", ok="Delete", danger=True) is False
    seen = answer_later(root, lambda p: p.press_default())
    assert popups.tell(root, "Couldn't save", "Disk full") is None
    assert [b.text_value for b in seen[0].buttons] == ["OK"]


def test_a_prompt_is_modal_and_the_window_keys_dont_reach_the_player(root):
    from ironbark import keys

    keys.Keys(root)   # (as the player's window has: on X11 it takes Ctrl+W out of Cut, too)
    fired = []
    root.bind("<Control-w>", lambda e: fired.append("close tab"))
    prompt = Prompt(root, "Rename playlist", "New name:", entry="Mix", buttons=(("Rename", True), ("Cancel", None)))
    _pump(root, 0.1)
    assert root.grab_current() is prompt                          # nothing else takes the mouse
    top = str(root)
    for widget in (prompt, prompt.entry, *prompt.buttons):
        assert top not in widget.bindtags() and prompt.tag in widget.bindtags()
    prompt.entry.focus_force()
    _pump(root, 0.05)
    prompt.entry.event_generate("<Control-w>")
    prompt.entry.event_generate("<Return>")
    _pump(root, 0.05)
    assert fired == [] and prompt.done and prompt.result == "Mix"
    assert root.grab_current() is None and not root.errors


def test_esc_key_and_a_closed_window_cancel(root):
    import tkinter as tk

    prompt = Prompt(root, "Delete?", buttons=(("Delete", True), ("No", False)), cancel=False)
    _pump(root, 0.05)
    prompt.focus_force()
    _pump(root, 0.05)
    prompt.event_generate("<Escape>")
    _pump(root, 0.05)
    assert prompt.done and prompt.result is False
    win = tk.Toplevel(root)
    win.geometry("300x300")
    _pump(root, 0.1)
    other = Prompt(win, "Play a test tone?", buttons=(("Play", True), ("Cancel", False)), cancel=False)
    win.destroy()                                                 # the window went: a cancel, not a hang
    assert other.done and other.wait() is False


def test_a_click_outside_flashes_the_box(root):
    prompt = Prompt(root, "Delete?", buttons=(("Delete", True), ("Cancel", False)), cancel=False)
    _pump(root, 0.1)
    flashes = []
    prompt.flash = lambda times=2: flashes.append(times)
    prompt._pressed(SimpleNamespace(widget=prompt, x_root=root.winfo_rootx() + 2, y_root=root.winfo_rooty() + 2))
    inside = SimpleNamespace(widget=prompt, x_root=prompt.winfo_rootx() + 5, y_root=prompt.winfo_rooty() + 5)
    prompt._pressed(inside)
    assert flashes == [2] and not prompt.done
    prompt.press_cancel()


def test_prompts_fit_a_narrow_window_and_follow_the_palette(root):
    root.geometry("260x400")
    _pump(root, 0.2)
    prompt = Prompt(root, "Name for the new playlist, which is quite a long question", "Message " * 20,
                    entry="", buttons=(("OK", True), ("Cancel", None)))
    _pump(root, 0.1)
    assert prompt.winfo_width() <= root.winfo_width()
    assert prompt.winfo_x() >= 0 and prompt.winfo_x() + prompt.winfo_width() <= root.winfo_width()
    assert prompt.cget("bg") == theme.DARK.SURFACE
    theme.use(theme.LIGHT, root)
    assert prompt.cget("bg") == theme.LIGHT.SURFACE and prompt.title_label.cget("fg") == theme.LIGHT.TEXT
    assert prompt.buttons[0].fill_colour() == theme.LIGHT.SELECT     # the default button: filled green
    prompt.press_cancel()


def test_prompt_buttons_keep_a_gap_between_them(root):
    prompt = Prompt(root, "Delete the playlist?", buttons=(("Keep", False), ("Maybe", None), ("Delete", True, "danger")),
                    cancel=False)
    _pump(root, 0.1)
    boxes = [(b.winfo_x(), b.winfo_x() + b.winfo_width()) for b in prompt.buttons]
    for (_left, right), (left, _right) in zip(boxes, boxes[1:]):
        assert left - right >= theme.px(theme.GAP_MEDIUM)
    assert boxes[-1][1] == prompt.buttons[-1].master.winfo_width()       # the last one ends on the edge
    prompt.press_cancel()


def test_a_warning_title_wraps_beside_its_sign(root):
    title = "Bit-perfect plays at the file's own level, so turn the volume down first to protect your ears:"
    prompt = Prompt(root, title, "Message", tone="warn", cover=True, buttons=(("I understand", True),))
    _pump(root, 0.1)
    wrap = prompt._width() - 2 * theme.px(theme.GAP_WIDE) - 2 * theme.px(popups.WARN_EDGE)
    sign = prompt.sign.winfo_reqwidth() + theme.px(theme.GAP_MEDIUM)
    assert prompt.title_wrap == wrap - prompt.sign.winfo_reqwidth() - theme.px(theme.GAP_MEDIUM)   # the room beside it
    assert int(prompt.title_label.cget("wraplength")) <= prompt.title_wrap
    head = prompt.title_label.master
    assert sign + prompt.title_label.winfo_reqwidth() <= head.winfo_width()            # nothing cut off
    assert prompt.title_label.winfo_x() + prompt.title_label.winfo_width() <= head.winfo_width()
    prompt.press_default()


def test_a_prompts_texts_fit_their_room_whatever_their_lines_come_to(root):
    # A wrapped line can come out within a character of the wrap (word by word, some length always
    # does: the Mac's fonts met it with the warning above); the label's border and padding come on
    # top, and wrapped at the full room its end was cut off.
    for words in range(20, 60):
        text = "Ab " * words
        prompt = Prompt(root, text, text, detail=text, tone="warn", cover=True, buttons=(("OK", True),))
        _pump(root, 0.05)
        head = prompt.title_label.master
        assert prompt.sign.winfo_reqwidth() + theme.px(theme.GAP_MEDIUM) + prompt.title_label.winfo_reqwidth() \
            <= head.winfo_width(), words
        for label in (prompt.message_label, prompt.detail_label):
            assert label.winfo_reqwidth() <= label.master.winfo_width(), words
        prompt.press_default()


def test_the_tag_editor_and_test_signal_questions_are_in_app(root):
    from ironbark.tag_window import ask
    from ironbark.tools.testsignals_window import confirm_play

    seen = answer_later(root, lambda p: p.press_default())
    assert ask(root, "Save")("Save changes to 12 files?", "Changing: Album.") is True
    assert seen[0].heading == "Save changes to 12 files?" and seen[0].buttons[0].text_value == "Save"
    seen = answer_later(root, lambda p: (seen.append((p.cover is not None, p.tone, root.grab_current() is p)),
                                         p.press_cancel()))
    signal = SimpleNamespace(name="1 kHz tone at −20 dBFS")
    assert confirm_play(root, signal, "Monitor speakers") is False        # Esc = don't play
    assert seen[1] == (True, "warn", True)                              # alone, amber, modal


# -- menus -----------------------------------------------------------------------------------------
def test_menu_runs_commands_and_moves_with_the_keys(root):
    ran = []
    menu = PopupMenu(root)
    menu.add("Play", lambda: ran.append("play"), "Enter")
    menu.add("Move up", lambda: ran.append("up"), "Ctrl+Up", enabled=False)
    menu.add_separator()
    menu.add("Close tab", lambda: ran.append("close"), "Ctrl+W")
    menu.popup(root.winfo_rootx() + 30, root.winfo_rooty() + 30)
    _pump(root, 0.1)
    assert menu.is_open and root.focus_lastfor() is menu     # the keyboard goes to the menu
    rows = menu.rows()
    assert [r.text for r in rows] == ["Play", "Move up", "Close tab"]
    assert [r.keys.cget("text") for r in rows] == ["Enter", "Ctrl+Up", "Ctrl+W"]   # shortcuts shown
    assert rows[1].label.cget("fg") == rows[1].disabled_colour()                  # greyed out
    menu.press("Down")
    assert menu.active_row is rows[0] and rows[0].plate.cget("bg") == theme.HOVER   # its rounded plate lights
    assert rows[0].cget("bg") == theme.SURFACE and len(rows[0]._corners) == 4
    assert all(corner.winfo_manager() == "place" for corner in rows[0]._corners)
    menu.press("Down")                                                            # skips the greyed one
    assert not any(corner.winfo_manager() for corner in rows[0]._corners)         # the old row's plate goes
    assert menu.active_row is rows[2]
    menu.press("Down")                                                            # round to the top
    assert menu.active_row is rows[0]
    menu.press("Up")
    menu.press("Return")
    assert ran == ["close"] and not menu.is_open
    _pump(root, 0.1)
    assert not menu.winfo_exists()                                                # made for one right-click
    assert not root.errors


def test_menu_keys_and_mouse_share_one_highlight_and_esc_closes(root):
    fired = []
    root.bind("n", lambda e: fired.append("next track"))
    menu = PopupMenu(root)
    menu.add("Play", lambda: fired.append("play"))
    menu.add("Play next", lambda: fired.append("play next"))
    menu.popup(root.winfo_rootx() + 30, root.winfo_rooty() + 30)
    _pump(root, 0.1)
    rows = menu.rows()
    rows[1]._entered()                       # the pointer onto "Play next"
    assert menu.active_row is rows[1]
    menu.press("Up")
    assert menu.active_row is rows[0] and rows[1].cget("bg") == theme.SURFACE
    menu.focus_force()
    _pump(root, 0.05)
    menu.event_generate("<KeyPress-n>")      # a shortcut key while the menu is open: swallowed
    menu.event_generate("<Escape>")
    _pump(root, 0.05)
    assert fired == [] and not menu.is_open


def test_submenus_open_in_place_and_come_back(root):
    picked = []
    menu = PopupMenu(root)
    menu.add("Play", lambda: None)
    sub = menu.add_submenu("Add to playlist")
    sub.add("Road trip", lambda: picked.append("Road trip"))
    sub.add_separator()
    sub.add("New playlist…", lambda: picked.append("new"), "Ctrl+N")
    columns = menu.add_submenu("Columns")
    columns.add_check("Artist", True, lambda: picked.append("artist off"))
    menu.popup(root.winfo_rootx() + 30, root.winfo_rooty() + 30)
    _pump(root, 0.1)
    menu.press("Down")
    menu.press("Down")                               # onto "Add to playlist ›"
    menu.press("Right")
    assert menu.is_open and [r.text for r in menu.rows()] == ["Add to playlist", "Road trip", "New playlist…"]
    assert menu.rows()[0].back and menu.rows()[0].chevron == "left"   # the way back: a chevron and the title
    assert menu.active_row.text == "Road trip"
    menu.press("Left")                               # back, with the highlight on where we came from
    assert menu.active_row.text == "Add to playlist"
    menu.invoke("Columns")
    tick = menu.rows()[1]
    assert tick.text == "Artist" and tick.tick.cget("text") == glyph(root, "check")
    menu.invoke("Columns")                           # (the back row is titled with the submenu's name)
    menu.invoke("Add to playlist")
    menu.invoke("New playlist…")
    assert picked == ["new"] and not menu.is_open


def test_menu_stays_inside_the_window(root):
    menu = PopupMenu(root)
    for n in range(6):
        menu.add(f"Item number {n} with a longer name", lambda: None)
    corner = (root.winfo_rootx() + root.winfo_width() - 3, root.winfo_rooty() + root.winfo_height() - 3)
    menu.popup(*corner)
    _pump(root, 0.1)
    x, y, w, h = menu.winfo_x(), menu.winfo_y(), menu.winfo_width(), menu.winfo_height()
    assert 0 <= x and x + w <= root.winfo_width() and 0 <= y and y + h <= root.winfo_height()
    assert y + h <= corner[1] - root.winfo_rooty()        # opened upwards: no room below the pointer
    tabbar.Popover.close_open()


def test_menu_too_tall_for_its_window_scrolls(root):
    root.geometry("300x160")
    _pump(root, 0.2)
    menu = PopupMenu(root)
    for n in range(14):
        menu.add(f"Song {n}", lambda: None)
    menu.popup(root.winfo_rootx() + 10, root.winfo_rooty() + 10)
    _pump(root, 0.1)
    assert menu.winfo_height() <= root.winfo_height() and menu.area.needed()
    menu.press("End")                                       # the keyboard scrolls the last one into view
    assert menu.area.visible(menu.active_row) and menu.active_row.text == "Song 13"
    tabbar.Popover.close_open()


# -- the player's own menus, prompts and Settings -----------------------------------------------------
@pytest.fixture
def shown_menus(monkeypatch):
    shown = []
    original = PopupMenu.popup

    def record(self, x, y):
        shown.append(self)
        original(self, x, y)
    monkeypatch.setattr(PopupMenu, "popup", record)
    return shown


def test_track_and_tab_menus(make_window, shown_menus, tmp_path, monkeypatch):
    window, root, player, errors = make_window()
    event = SimpleNamespace(x_root=root.winfo_rootx() + 40, y_root=root.winfo_rooty() + 200)
    window._track_menu([0], event)
    menu = shown_menus[-1]
    assert menu.labels() == ["Play", "Play next", "Add to Up next", "Add to playlist", "Add to favourites",
                             "Edit tags…", "Tag history (undo)…", "Stop after the current song", "Sleep timer", "Columns"]
    menu.invoke("Columns")
    menu.invoke("Artist")                                    # a tick item: switches the column on
    assert "artist" in window.playlist.columns and window.prefs["track_columns"] == ["artist"]
    window._heading_menu(event)
    heading = shown_menus[-1]
    assert [r.tick.cget("text") for r in heading.rows()] == [glyph(window.root, "check"), "", ""]   # Artist, Album, Plays
    heading.invoke("Artist")
    assert "artist" not in window.playlist.columns
    # The keyboard's menu key opens the same menu under the focused row.
    tree = window.playlist.tree
    window.playlist.set_tracks(window.tabs[window.current].tracks, flat=True)
    tree.focus("0")
    window.playlist._context(popups.row_event(tree))
    assert shown_menus[-1].labels()[0] == "Play" and shown_menus[-1].is_open
    tabbar.Popover.close_open()

    # A playlist tab: Move up is greyed out on its first song; the tab menu offers rename / delete.
    monkeypatch.setattr(playlists, "folder", lambda: tmp_path / "Playlists")
    path = tmp_path / "Playlists" / "Mix.m3u8"
    playlists.write(path, [window.tabs[0].tracks[0]] * 2)
    window.open_tab("playlist", path)
    _pump(root, 0.5)
    window._track_menu([0], event)
    rows = {r.text: r for r in shown_menus[-1].rows()}
    assert not rows["Move up"].enabled and rows["Move down"].enabled and rows["Remove from playlist"].enabled
    tabbar.Popover.close_open()
    window._tab_menu(window.current, event)
    assert shown_menus[-1].labels() == ["Add songs from the Library…", "Rename playlist…", "Delete playlist…", "Move left", "Move right",
                                        "Close tab"]
    assert not {r.text: r for r in shown_menus[-1].rows()}["Move right"].enabled   # it's the last tab
    tabbar.Popover.close_open()
    assert not errors


@pytest.mark.skipif(os.name != "nt", reason="Windows' file names ignore case")
def test_playlist_name_prompt_checks_for_a_clash(make_window, tmp_path, monkeypatch):
    window, root, player, errors = make_window()
    monkeypatch.setattr(playlists, "folder", lambda: tmp_path / "Playlists")
    playlists.write(playlists.path_for("Rock"), [])
    assert window.playlist_name_problem("rock") == "There's already a playlist called “Rock”."
    assert window.playlist_name_problem("   ") == "Type a name for the playlist."
    assert window.playlist_name_problem("Jazz") is None
    current = playlists.path_for("Rock")
    assert window.playlist_name_problem("Rock", current) is None          # its own name is fine
    notes = []

    def act(p):
        p.text.set("Rock")
        p.press_default()
        notes.append(p.problem_label.cget("text"))
        p.text.set("Jazz")
        p.press_default()

    answer_later(root, act)
    window.new_playlist()
    assert notes == ["There's already a playlist called “Rock”."]
    assert playlists.path_for("Jazz").exists() and window.tabs[window.current].title == "Jazz"
    # Delete it: the in-app confirm; Esc and Enter keep it, a click on Delete removes it.
    answer_later(root, lambda p: p.press_cancel())
    window.delete_playlist(window.current)
    assert playlists.path_for("Jazz").exists()
    answer_later(root, lambda p: p.press_default())
    window.delete_playlist(window.current)
    assert playlists.path_for("Jazz").exists()
    answer_later(root, lambda p: p._chosen(0))
    window.delete_playlist(window.current)          # (to the Recycle Bin, on a worker thread)
    gone = playlists.path_for("Jazz")
    deadline = time.monotonic() + 10
    while (gone.exists() or recycle.BIN_NAME not in (window._notice or "")) and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    assert not gone.exists() and not errors and str(gone) in conftest.binned
    assert window._notice.startswith(f"Moved the playlist “Jazz” to the {recycle.BIN_NAME}")


def test_a_playlist_on_a_drive_with_no_recycle_bin_is_deleted_for_good_only_when_said(make_window, tmp_path,
                                                                                       monkeypatch):
    from ironbark import recycle

    window, root, player, errors = make_window()
    monkeypatch.setattr(playlists, "folder", lambda: tmp_path / "Playlists")
    monkeypatch.setattr(recycle, "can_recycle", lambda path: False)   # (a network drive, a USB stick)
    path = playlists.path_for("Mix")
    playlists.write(path, [])
    window.open_tab("playlist", path)
    asked = []
    answer_later(root, lambda p: (asked.append((p.heading, [b.text_value for b in p.buttons])), p._chosen(0)))
    window.delete_playlist(window.current)
    assert asked == [("Delete the playlist “Mix” for good?", ["Delete for good", "Cancel"])]
    assert not path.exists() and str(path) not in conftest.binned and not errors
    assert window._notice == "Deleted the playlist “Mix”"


def test_protect_ears_is_an_unmissable_modal_box(make_window):
    from ironbark.ui import SAFE_VOLUME

    window, root, player, errors = make_window()
    player.set_volume(1.0)
    window._protect_ears(True)                                   # the device turns itself down: nothing
    assert Prompt.stack == [] and player.volume == 1.0
    seen = []

    def check(p):
        # While it's up: the volume is already down, the box is modal, alone and amber.
        seen.append((player.volume, root.grab_current() is p, p.cover is not None, p.tone,
                     p.cover.winfo_ismapped(), p.winfo_ismapped()))
        p.press_default()

    answer_later(root, check)
    window._protect_ears(False)
    assert seen == [(SAFE_VOLUME, True, True, "warn", True, True)]
    assert player.volume == SAFE_VOLUME and window.volume_slider.value == pytest.approx(SAFE_VOLUME * 100)
    assert Prompt.stack == [] and root.grab_current() is None and not errors


def test_volume_on_my_amplifier_stands_the_ear_checks_down(make_window, monkeypatch):
    """Settings → Volume on my amplifier, for the output in use: kept by name, and on it the
    protect-your-ears box doesn't come up; switched off in Bit-perfect, it does."""
    from ironbark import engine, settings_view
    from ironbark.ui import SAFE_VOLUME

    window, root, player, errors = make_window()
    if getattr(window, "amp_volume", None) is None:
        pytest.skip("no Bit-perfect on this system")
    monkeypatch.setattr(engine.devices, "has_hardware_volume", lambda name: False)
    player.set_output_device("USB DAC")
    window.exclusive.set(True)
    player.set_volume(1.0)
    settings_view.show_amp_volume(window)
    assert not window.amp_volume.get() and "For USB DAC only." in window.amp_volume_row.about.cget("text")
    window.amp_volume_row.toggle.toggle()                              # on
    assert window.prefs["amp_volume_outputs"] == ["USB DAC"] and player.amp_outputs == {"USB DAC"}
    window._protect_ears(player.refresh_hardware_volume())
    assert Prompt.stack == [] and player.volume == 1.0 and not window._ears_at_risk()
    player.set_output_device("Headphones")                             # another output: its own setting
    settings_view.show_amp_volume(window)
    assert not window.amp_volume.get()
    player.set_output_device("USB DAC")
    settings_view.show_amp_volume(window)
    assert window.amp_volume.get()
    player.refresh_hardware_volume()
    seen = []
    answer_later(root, lambda p: (seen.append(p.tone), p.press_default()))
    window.amp_volume_row.toggle.toggle()                              # off, in Bit-perfect: the ear check
    assert window.prefs["amp_volume_outputs"] == [] and not player.amp_volume
    assert seen == ["warn"] and player.volume == SAFE_VOLUME and window._ears_at_risk()
    assert Prompt.stack == [] and not errors


def test_settings_scroll_to_the_bottom_in_a_short_window(make_window):
    window, root, player, errors = make_window(geometry="420x460+120+120")
    window._open_settings()
    _pump(root, 0.3)
    view, area = window.settings, window.settings_area
    pop = view.frame
    assert view.is_open
    assert pop.winfo_y() + pop.winfo_height() <= root.winfo_height()       # never past the window's bottom
    assert area.needed() and area.scrollbar.winfo_ismapped()               # …so it scrolls, scrollbar shown
    assert not area.visible(window.version_label)
    # Every section of both pages can be brought into view, the version line included.
    found = set()
    for page, body in (("advanced", view.advanced), ("simple", view.simple)):
        view.show_page(page)
        _pump(root, 0.1)
        captions = [w for w in _all(body) if getattr(w, "cget", None) and _text(w).isupper() and len(_text(w)) > 3]
        found |= {_text(c) for c in captions}
        for widget in captions + ([window.version_label] if page == "simple" else []):
            area.see(widget)
            assert area.visible(widget), _text(widget)
    assert found >= {"SOUND", "WINDOW", "LIBRARY", "EXPERIMENTAL", "HELP"}
    # The mouse wheel over a setting scrolls the panel (and stops there: the Output box doesn't change).
    area.scroll_to(0)
    _pump(root, 0.05)
    choice = window.output_box.current()
    window.output_box.event_generate("<MouseWheel>", delta=-120)
    _pump(root, 0.05)
    assert area.canvas.yview()[0] > 0 and window.output_box.current() == choice
    for _ in range(40):
        window.version_label.event_generate("<MouseWheel>", delta=-120)
    _pump(root, 0.05)
    assert area.canvas.yview()[1] == pytest.approx(1.0) and area.visible(window.version_label)
    # A taller window: the panel grows with it.
    before = pop.winfo_height()
    root.geometry("420x700")
    _pump(root, 0.3)
    assert pop.winfo_height() > before and pop.winfo_y() + pop.winfo_height() <= root.winfo_height()
    assert not errors


def test_settings_volume_slider_and_flat_buttons(make_window):
    window, root, player, errors = make_window(home_geometry="")
    window._open_settings()
    _pump(root, 0.2)
    window._volume_slid(50.0)
    assert player.volume == pytest.approx(0.5) and window.volume_slider.value == pytest.approx(50.0)
    assert "50%" in window.volume_text.cget("text")
    window.volume_var.set(80.0)                                      # e.g. the mouse wheel over the transport
    assert window.volume_slider.value == pytest.approx(80.0)
    assert not window.snap_button.enabled                            # no home yet: greyed, does nothing
    snapped = []
    window.snap_home = lambda: snapped.append(True)
    window.snap_button._release(SimpleNamespace(x=2, y=2))
    assert snapped == []
    window.prefs["home_geometry"] = "420x640+120+120"
    window._update_home_text()
    assert window.snap_button.enabled
    assert not errors


def test_new_tab_menu_and_up_next_take_the_keys(make_window):
    window, root, player, errors = make_window()
    opened = []
    window.open_library = lambda: opened.append("library")
    rows = window.new_menu.menu_rows()
    rows[0].command = window.open_library
    window.new_menu.toggle(window.bar.add_btn, window.bar.height)
    _pump(root, 0.1)
    window.new_menu.move(1)
    assert window.new_menu.active_row is rows[0]
    window.new_menu.run_active()
    assert opened == ["library"] and not window.new_menu.is_open
    assert not errors


def test_tooltips_share_one_delay_and_wrap_to_the_window(root):
    import tkinter as tk

    label = tk.Label(root, text="?")
    label.pack()
    tip = tabbar.Tooltip(label, "A long explanation " * 30)
    _pump(root, 0.1)
    assert tip.wrap() <= theme.px(tabbar.TOOLTIP_WRAP) and tip.wrap() <= root.winfo_width()
    root.geometry("150x300")
    _pump(root, 0.2)
    assert tip.wrap() == theme.px(tabbar.TOOLTIP_MIN_WRAP)
    jobs = []
    label.after = lambda ms, fn: jobs.append(ms) or "job"
    tip._schedule()
    assert jobs == [tabbar.TOOLTIP_DELAY_MS]


def _all(widget):
    found, stack = [], [widget]
    while stack:
        w = stack.pop()
        found.append(w)
        stack.extend(w.winfo_children())
    return found


def _text(widget) -> str:
    try:
        return str(widget.cget("text"))
    except Exception:
        return ""

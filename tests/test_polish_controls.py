"""The rounded look, second part: fields and drop-downs drawn as ttk image elements, one pill family,
frameless lists with a rounded selected row and a scrollbar in the margin, menus with a rounded
highlight plate, prompts and pop-overs with an EDGE rim, Windows 11 tooltips, the mini strip's drawn
buttons — and keyboard access for every drawn control (keyfocus: Tab, Enter / Space, the arrows, a
DG-green focus ring, Settings' own Tab order)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ironbark import controls, keyfocus, shapes, theme
from ironbark.library import Track
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)
from test_main_window_polish import _near, _pixel, root  # noqa: F401  (root is a fixture)


# -- the style pictures (no display needed) ---------------------------------------------------------
def test_every_style_picture_is_at_least_its_borders_plus_two():
    """Tk hangs drawing an image element whose picture is smaller than its 9-slice borders."""
    pictures = shapes.element_pictures()
    assert {"field BG", "field SURFACE focus", "chevron", "heading", "selection", "clear", "thumb v",
            "thumb h hot"} <= set(pictures)
    for name, (picture, (left, top, right, bottom)) in pictures.items():
        assert picture.width >= left + right + 2 and picture.height >= top + bottom + 2, name


def test_style_pictures_draw_the_palette():
    pictures = shapes.element_pictures()
    field, _border = pictures["field BG"]
    middle = (field.width // 2, field.height // 2)
    assert field.getpixel(middle)[:3] == theme.rgb("SURFACE")          # one step "into" the window
    assert field.getpixel((0, 0))[:3] == theme.rgb("BG")               # the corner: the window shows
    assert _near("#%02x%02x%02x" % field.getpixel((middle[0], 0))[:3], "EDGE")   # the 1 px rim
    card, _border = pictures["field SURFACE"]
    assert card.getpixel(middle)[:3] == theme.rgb("BG")                 # on a card: BG inside
    selection, _border = pictures["selection"]
    assert selection.getpixel((selection.width // 2, selection.height // 2)) == theme.rgb("SELECT") + (255,)
    assert selection.getpixel((0, 0))[3] < 255                          # rounded: see-through corner
    heading, (_l, _t, _r, bottom) = pictures["heading"]
    assert heading.getpixel((1, heading.height - 1)) == theme.rgb("BORDER")   # the line under the headings
    assert heading.getpixel((1, 0)) == theme.rgb("BG")
    thumb, _border = pictures["thumb v"]
    hot, _border = pictures["thumb v hot"]
    opaque = [x for x in range(thumb.width) if thumb.getpixel((x, thumb.height // 2))[3] > 128]
    wider = [x for x in range(hot.width) if hot.getpixel((x, hot.height // 2))[3] > 128]
    assert len(opaque) < len(wider) <= thumb.width                     # wider under the pointer
    assert _near("#%02x%02x%02x" % thumb.getpixel((thumb.width // 2, thumb.height // 2))[:3], "THUMB")


# -- ttk styles ---------------------------------------------------------------------------------------
def test_elements_are_made_once_and_repainted_on_a_palette_change(root):
    images = root._dg_style_images
    names = {key: str(image) for key, image in images.items()}
    field = images["field BG"]
    middle = (field.width() // 2, field.height() // 2)
    assert _pixel_of(field, *middle) == theme.DARK.SURFACE
    theme.use(theme.LIGHT, root)
    assert root._dg_style_images is images and {k: str(v) for k, v in images.items()} == names
    assert _pixel_of(field, *middle) == theme.LIGHT.SURFACE            # the same picture, new pixels
    theme.use(theme.DARK, root)
    assert _pixel_of(field, *middle) == theme.DARK.SURFACE
    assert not root.errors


def _pixel_of(photo, x, y) -> str:
    r, g, b = (int(v) for v in photo.tk.splitlist(photo.tk.call(str(photo), "get", x, y)))
    return f"#{r:02x}{g:02x}{b:02x}"


def test_lists_are_frameless_with_a_rounded_selected_row(root):
    from tkinter import ttk

    style = ttk.Style(root)
    assert style.lookup("Treeview", "background") == theme.BG == style.lookup("Treeview", "fieldbackground")
    assert style.lookup("Treeview", "background", ["selected"]) == theme.BG   # the plate is drawn over it
    assert "Round.selection" in str(style.layout("Treeview.Row"))
    assert "Round.heading" in str(style.layout("Treeview.Heading"))
    assert "Scrollbar.thumb" in str(style.layout(theme.SLIM_SCROLLBAR))      # (found by its name's ending)
    assert style.lookup(theme.CARD_SCROLLBAR, "troughcolor") == theme.SURFACE


def test_fields_and_drop_downs_are_control_height(root):
    from tkinter import ttk

    entry = ttk.Entry(root)
    card = ttk.Entry(root, style=theme.CARD_ENTRY)
    box = ttk.Combobox(root, values=["Dark", "Light"], state="readonly")
    pop = ttk.Combobox(root, values=["Dark"], state="readonly", style=theme.CARD_COMBOBOX)
    for widget in (entry, card, box, pop):
        widget.pack()
    _pump(root, 0.1)
    for widget in (entry, card, box, pop):
        assert abs(widget.winfo_reqheight() - theme.px(theme.CONTROL_H)) <= 1
    style = ttk.Style(root)
    assert "Round.field" in str(style.layout("TEntry")) and "Card.field" in str(style.layout(theme.CARD_ENTRY))
    assert "Round.chevron" in str(style.layout("TCombobox"))
    assert "downarrow" not in str(style.layout("TCombobox"))            # no separate arrow box
    assert not root.errors


# -- the pill family ------------------------------------------------------------------------------------
def test_one_pill_family(root):
    plain = controls.PillButton(root, "Cancel", lambda: None, kind="plain")
    small = controls.PillButton(root, "Mini player", lambda: None, kind="plain", small=True, bg="SURFACE")
    primary = controls.PillButton(root, "Save", lambda: None)
    danger = controls.PillButton(root, "Delete", lambda: None, kind="danger")
    for pill in (plain, small, primary, danger):
        pill.pack()
    _pump(root, 0.1)
    assert plain.winfo_reqheight() == primary.winfo_reqheight() == theme.px(theme.CONTROL_H)
    assert small.winfo_reqheight() == theme.px(theme.CONTROL_H_SMALL)
    assert plain.fill_colour() == theme.SURFACE_HI and plain._outline == "EDGE"
    assert primary.fill_colour() == theme.SELECT and danger.fill_colour() is None and danger._outline == "DANGER"
    plain._state(hover=True)
    assert plain.fill_colour() == theme.mix("SURFACE_HI", "TEXT", 0.06)
    plain._state(pressed=True)
    assert plain.fill_colour() == theme.SURFACE_DOWN
    # Words still readable on every plain shade (WCAG AA, both palettes).
    for palette in (theme.DARK, theme.LIGHT):
        theme.use(palette)
        for fill in (theme.SURFACE_HI, theme.mix("SURFACE_HI", "TEXT", 0.06), theme.SURFACE_DOWN):
            assert theme.contrast_ratio(theme.TEXT, fill) >= theme.AA_TEXT
    theme.use(theme.DARK)
    assert not root.errors


# -- the keyboard ----------------------------------------------------------------------------------------
def _give_keyboard(widget):
    """Move the keyboard to `widget` as Tab would. (On the tests' hidden desktop the app is never the
    active one, so Windows sends no focus events of its own: the one Tk would send is sent here.)"""
    widget.focus_set()
    widget.event_generate("<FocusIn>")
    widget.update_idletasks()


def test_tab_reaches_a_pill_and_enter_or_space_presses_it(root):
    pressed = []
    pill = controls.FlatButton(root, "Rescan", lambda: pressed.append(1))
    pill.pack()
    _pump(root, 0.1)
    assert str(pill.cget("takefocus")) == "1"
    assert {"<Key-Return>", "<Key-space>"} <= set(pill.bind())
    resting = str(pill.cget("image"))
    _give_keyboard(pill)
    assert pill.focused and str(pill.cget("image")) != resting           # the DG-green ring
    assert _near(_pixel(pill, pill.winfo_reqwidth() // 2, 0), "ACCENT")
    assert pill._key_pressed(None) == "break" and pressed == [1]          # (the window's own key doesn't act)
    pill.set_enabled(False)
    assert str(pill.cget("takefocus")) == "0" and not pill.can_focus()   # greyed out: Tab passes it by
    pill._key_pressed(None)
    assert pressed == [1]
    root.focus_set()
    pill.event_generate("<FocusOut>")
    assert not pill.focused
    assert not root.errors


def test_round_buttons_switches_sliders_and_choices_take_the_keyboard(root):
    import tkinter as tk

    pressed = []
    button = controls.RoundIconButton(root, root, "shuffle", lambda: pressed.append(1), bg="BG")
    variable = tk.BooleanVar(value=False)
    switch = controls.Switch(root, "Match levels", variable)
    released = []
    slider = controls.Slider(root, on_release=released.append)
    choice = tk.StringVar(value="a")
    row = controls.ChoiceRow(root, (("a", "One"), ("b", "Two"), ("c", "Three")), choice)
    for widget in (button, switch, slider, row):
        widget.pack(fill="x")
    _pump(root, 0.1)
    assert all(str(w.cget("takefocus")) == "1" for w in (button, switch.toggle, slider, row))
    _give_keyboard(button)
    middle = button._diameter // 2
    assert _near(_pixel(button, middle, 0), "ACCENT")                     # a ring round the disc
    button._key_pressed(None)
    assert pressed == [1]
    switch.toggle._key_pressed(None)                                      # Space flips a switch
    assert variable.get() is True
    switch.set_enabled(False)
    assert str(switch.toggle.cget("takefocus")) == "0"
    slider.set(30.0, 120.0)
    assert slider.nudge(1) == "break" and released == [36.0]              # a twentieth of the way
    slider.nudge("start")
    slider.nudge(-1)                                                      # never below the start
    assert released[-2:] == [0.0, 0.0]
    slider.step = 5.0
    slider.nudge(4)                                                       # Page Up: four steps
    assert released[-1] == 20.0
    row.step(1)
    assert choice.get() == "b"
    row.step(1)
    row.step(1)                                                           # stops at the last
    assert choice.get() == "c" and row.bars["c"].lit and not row.bars["a"].lit
    resting = str(row.labels["c"].cget("image"))
    _give_keyboard(row)
    assert str(row.labels["c"].cget("image")) != resting                  # the ring round the chosen word
    assert not root.errors


def test_settings_takes_the_keyboard_tabs_round_it_and_gives_it_back(make_window):
    window, root, _player, errors = make_window()
    _pump(root, 0.3)
    window.playlist.tree.focus_set()
    _pump(root, 0.1)
    window._open_settings()
    _pump(root, 0.3)
    pop = window.settings.frame                                           # (0.39.0: a view, not a pop-over)
    assert root.focus_lastfor() is pop                                    # the view has the keyboard
    widgets = keyfocus.focusable_in(pop)
    assert widgets[0] is window.mini_button                               # top to bottom
    tops = [(w.winfo_rooty(), w.winfo_rootx()) for w in widgets]
    assert tops == sorted(tops)
    assert window.preamp_box not in widgets                               # the Advanced page: not on show
    assert {window.output_box, window.theme_box, window.volume_slider} <= set(widgets)
    keyfocus._step(SimpleNamespace(widget=pop), 1)                        # Tab
    assert root.focus_lastfor() is widgets[0]
    _pump(root, 0.2)
    widgets = keyfocus.focusable_in(pop)                                  # (the Done bar laid out by now)
    keyfocus._step(SimpleNamespace(widget=widgets[0]), -1)                # Shift+Tab: round to the last
    assert root.focus_lastfor() is widgets[-1]
    _pump(root, 0.2)
    assert window.settings_area.visible(widgets[-1])                      # scrolled into view
    # The order follows the controls' places on screen, so it changes as the page scrolls: from here
    # Tab goes to whatever is next now, and Tab from the last of all rounds to the first.
    last = widgets[-1]
    order = keyfocus.focusable_in(pop)
    keyfocus._step(SimpleNamespace(widget=last), 1)
    assert root.focus_lastfor() is order[(order.index(last) + 1) % len(order)]
    order = keyfocus.focusable_in(pop)
    keyfocus._step(SimpleNamespace(widget=order[-1]), 1)                   # Tab from the last: the first
    assert root.focus_lastfor() is order[0]
    root.event_generate("<Escape>")                                       # Esc closes it…
    _pump(root, 0.1)
    from ironbark.tabbar import Popover

    assert Popover.open_one is None and root.focus_lastfor() is window.playlist.tree   # …and gives it back
    assert errors == []


def _click(widget, x: int = 20, y: int = 10):
    widget.event_generate("<ButtonPress-1>", x=x, y=y, rootx=widget.winfo_rootx() + x,
                          rooty=widget.winfo_rooty() + y)
    widget.event_generate("<ButtonRelease-1>", x=x, y=y, rootx=widget.winfo_rootx() + x,
                          rooty=widget.winfo_rooty() + y)


def test_a_click_that_closes_a_panel_keeps_the_keyboard_where_it_clicked(make_window):
    """Settings or a right-click menu closed by a click on the list or the search box: the keyboard
    stays there (Space then plays, letters type) rather than jumping back to where it was before."""
    from ironbark.tabbar import Popover

    window, root, _player, errors = make_window()
    _pump(root, 0.3)
    entry, tree = window.playlist.entry, window.playlist.tree
    entry.focus_set()
    window._open_settings()
    _pump(root, 0.2)
    assert root.focus_lastfor() is window.settings.frame
    window.select_tab(0)                                                  # a tab clicked: back to the player
    _pump(root, 0.2)
    assert not window.settings.is_open and root.focus_lastfor() is entry
    window._track_menu([0], SimpleNamespace(x_root=tree.winfo_rootx() + 40, y_root=tree.winfo_rooty() + 40))
    _pump(root, 0.2)
    assert Popover.open_one is not None and root.focus_lastfor() is Popover.open_one
    _click(entry)
    _pump(root, 0.2)
    assert Popover.open_one is None and root.focus_lastfor() is entry
    window._open_settings()                                               # closed from the keyboard:
    _pump(root, 0.2)
    root.event_generate("<Escape>")
    _pump(root, 0.1)
    assert root.focus_lastfor() is entry                                  # back where it was
    assert errors == []


def test_settings_fills_the_window_under_the_strip_with_two_pages(make_window):
    window, root, _player, errors = make_window()
    _pump(root, 0.3)
    window._open_settings()
    _pump(root, 0.3)
    view = window.settings
    assert view.is_open and view.page == "simple"
    assert view.frame.winfo_ismapped() and not window.playlist.frame.winfo_ismapped()
    assert view.frame.winfo_width() == window.body.winfo_width() - 2 * theme.px(theme.GAP_WIDE)   # the column's width
    assert view.frame.winfo_y() >= window.bar.height
    assert view.page_button.text_value == "Advanced settings…" and view.done_button.text_value == "Done"
    view.toggle_page()
    _pump(root, 0.2)
    assert view.page == "advanced" and view.page_button.text_value == "Back"
    # The drop-downs beside their names share one width, so their left edges line up.
    lefts = {box.winfo_rootx() for box in (window.headroom_box, window.levelling_box, window.preamp_box)}
    assert len(lefts) == 1
    view.done_button.command()
    _pump(root, 0.2)
    assert not view.is_open and window.playlist.frame.winfo_ismapped()
    assert window.mini_button.winfo_reqheight() == theme.px(theme.CONTROL_H_SMALL)
    assert errors == []


# -- menus, prompts, tooltips ------------------------------------------------------------------------------
def test_menu_rows_tick_column_chevrons_and_separators(root):
    from ironbark.popups import PopupMenu

    menu = PopupMenu(root)
    menu.add("Play", lambda: None, "Enter")
    menu.add_separator()
    menu.add_check("Artist", True, lambda: None)
    sub = menu.add_submenu("Add to playlist")
    sub.add("Road trip", lambda: None)
    menu.popup(root.winfo_rootx() + 20, root.winfo_rooty() + 20)
    _pump(root, 0.1)
    rows = menu.rows()
    assert all(r.tick is not None for r in rows)                          # a menu with ticks: every row
    assert rows[2].chevron == "right" and rows[2].keys.cget("text") == ""  # drawn chevron, not "›"
    line = next(w for w in menu.body.winfo_children() if not hasattr(w, "plate"))
    assert int(line.pack_info()["padx"]) == theme.px(theme.MENU_INSET)
    assert all(abs(r.winfo_reqheight() - theme.px(theme.MENU_ROW_H)) <= 1 for r in rows)
    assert menu.cget("highlightbackground") == theme.EDGE
    menu.escape()
    plain = PopupMenu(root)
    plain.add("Play", lambda: None)
    plain.popup(root.winfo_rootx() + 20, root.winfo_rooty() + 20)
    _pump(root, 0.1)
    assert plain.rows()[0].tick is None                                   # no ticks: no tick column
    plain.escape()
    assert not root.errors


def test_prompts_have_an_edge_rim_and_a_card_field(root):
    from ironbark.popups import Prompt

    prompt = Prompt(root, "New playlist", entry="Road trip", buttons=(("Create", True), ("Cancel", None)))
    _pump(root, 0.1)
    assert prompt.cget("highlightbackground") == theme.EDGE
    assert str(prompt.entry.cget("style")) == theme.CARD_ENTRY
    assert [b.kind for b in prompt.buttons] == ["primary", "plain"]
    prompt.answer(None)
    assert not root.errors


def test_tooltips_are_rounded_by_windows_11_or_keep_their_rim(root):
    from ironbark.tabbar import Tooltip, round_corners

    label = controls.FlatButton(root, "Rescan")
    label.pack()
    _pump(root, 0.1)
    tip = Tooltip(label, "Look for added music now")
    tip._show()
    _pump(root, 0.1)
    rounded = round_corners(tip._tip, theme.EDGE)
    assert tip.label.cget("highlightbackground") == theme.EDGE
    assert int(tip.label.cget("highlightthickness")) == (0 if rounded else 1)
    tip._hide()
    assert not root.errors


# -- the main window and the mini strip ----------------------------------------------------------------------
def test_the_track_list_scrollbar_sits_in_the_margin(make_window):
    window, root, _player, errors = make_window()
    song = window.player.track
    many = [Track(path=song.path, title=f"Song {n}", artist="Artist", album="Album", duration=200.0,
                  sample_rate=44100, bits=16, channels=2, track_no=n) for n in range(1, 60)]
    window.playlist.set_tracks(many)
    _pump(root, 0.4)
    tree, bar = window.playlist.tree, window.playlist.scrollbar
    assert bar.needed and bar.winfo_manager() == "place"
    assert bar.winfo_rootx() >= tree.winfo_rootx() + tree.winfo_width()  # outside the list: in the margin
    search = window.playlist.search_row   # the box and the Whole library switch share the row
    assert tree.winfo_rootx() + tree.winfo_width() == search.winfo_rootx() + search.winfo_width()
    assert errors == []


def test_search_box_hint_and_its_key(make_window):
    window, root, _player, errors = make_window()
    _pump(root, 0.2)
    box = window.playlist.search
    assert "Ctrl+F" not in box.hint.cget("text") and box.keys_hint.cget("text") == "Ctrl+F"
    root.focus_set()
    box.update_hint()
    assert box.keys_hint.winfo_manager() == "place" and not box.clear_button.winfo_manager()
    box.query.set("gfy")
    assert not box.keys_hint.winfo_manager() and box.clear_button.winfo_manager() == "place"
    assert isinstance(box.clear_button, controls.RoundIconButton)
    box.clear_button.command()
    assert box.query.get() == ""
    assert errors == []


def test_the_mini_strip_uses_the_drawn_buttons(make_window):
    window, root, _player, errors = make_window()
    window.toggle_mini()
    _pump(root, 0.3)
    mini = window.mini
    assert isinstance(mini.play_button, controls.PlayButton)
    assert mini.previous_button._drawn == "prev" and mini.next_button._drawn == "next"
    for button in (mini.settings_button, mini.full_button, mini.close_button):
        assert isinstance(button, controls.RoundIconButton)
    mini.show(title="t", artist="a", position=1.0, duration=2.0, playing=True, status_colour="GOOD",
              status_detail="All good")
    middle = theme.px(8) // 2
    assert _near(_pixel(mini.status_dot, middle, middle), "GOOD")       # a drawn dot, not a square
    assert mini.play_button.playing
    theme.use(theme.LIGHT, root)
    assert _near(_pixel(mini.status_dot, middle, middle), "GOOD")       # repainted in the new palette
    theme.use(theme.DARK, root)
    assert errors == []


@pytest.mark.parametrize("kind", ["plain", "primary"])
def test_flat_buttons_fit_their_words(root, kind):
    button = controls.FlatButton(root, "Freeze", kind=kind, fit="Resume")
    button.pack()
    _pump(root, 0.05)
    width = button.winfo_reqwidth()
    button.configure(text="Resume")
    _pump(root, 0.05)
    assert button.winfo_reqwidth() == width                                # its neighbours don't move

"""The shared widgets, made sturdier: the track list and the Library act on what was just typed and
stay quick on a whole library; closed windows, menus and prompts are freed (and their Tk objects let
go of on Tk's thread); controls ignore a press let go elsewhere; a destructive question answers
"no" to Enter; the seek bar copes with a song change or a lost release mid-drag; the tab strip's +
comes home when the last tab closes. Tk tests (skipped without a display)."""

from __future__ import annotations

import gc
import time
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from ironbark import controls, playlist_view, popups, theme
from ironbark.library import Track

from test_library_window import flac, wait_for_index


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.geometry("520x520+60+60")
    r.title("DG test — widgets")
    theme.apply(r)
    errors = []
    r.report_callback_exception = lambda *info: errors.append(info)
    r.errors = errors
    r.update()
    yield r
    theme.use(theme.DARK)
    for prompt in list(popups.Prompt.stack):
        prompt.answer(None)
    r.destroy()
    del r
    gc.collect()   # on this thread (Tk objects freed on another thread abort Python)


def _pump(root, seconds: float):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def _songs(*titles: str, album: str = "A") -> list[Track]:
    return [Track(Path(f"C:/M/{album}/{n:02d}.flac"), title, artist="X", album=album, track_no=n)
            for n, title in enumerate(titles, 1)]


def _release(widget, x: int, y: int):
    """A left-button release at (x, y) in `widget` (Tk works out the screen position)."""
    widget.event_generate("<ButtonRelease-1>", x=x, y=y)


# -- the track list's search -----------------------------------------------------------------------
def test_enter_and_down_straight_after_typing_act_on_what_was_typed(root):
    played = []
    view = playlist_view.PlaylistView(root, on_play=played.append)
    view.set_tracks(_songs("Intro", "Blue", "Green", "Yellow"), flat=True)
    view.query.set("yellow")
    view._play_first_result()                 # Enter within the pause: no events pumped
    assert played == [3]
    view.query.set("blue")
    view._focus_results()                     # Down within the pause
    assert view.tree.selection() == ("1",)
    _pump(root, 0.3)                          # nothing waiting would rebuild the list and lose it
    assert view.tree.selection() == ("1",) and view.tree.focus() == "1" and view._search_job is None


def test_a_search_lists_rows_instead_of_remaking_them(root, monkeypatch):
    tracks = [Track(Path(f"C:/M/Album {a}/{t:02d}.flac"), f"Song {a} {t}", artist=f"Artist {a % 7}",
                    album=f"Album {a}", track_no=t + 1) for a in range(60) for t in range(10)]
    view = playlist_view.PlaylistView(root, on_play=lambda i: None)
    view.set_tracks(tracks)
    folded = []
    real = playlist_view._haystack
    monkeypatch.setattr(playlist_view, "_haystack", lambda track: folded.append(1) or real(track))
    made = []
    insert = view.tree.insert
    monkeypatch.setattr(view.tree, "insert", lambda *a, **k: made.append(1) or insert(*a, **k))
    monkeypatch.setattr(view.tree, "delete", lambda *a: pytest.fail("a search deleted rows"))
    for query in ("song 1", "song 12", "song 12 3", "artist 3", "", "song 12"):
        view.query.set(query)
        view._render()
        words = query.split()
        expected = [str(i) for i, t in enumerate(tracks) if all(w in real(t) for w in words)]
        assert view._songs == expected, query
        assert [r for r in view.tree.get_children() if r.isdigit()] == expected
    assert made == [] and len(folded) == len(tracks)     # every row made once; each song folded once
    assert view.tree.set("123", "title") == "Song 12 3  ·  Artist 5"   # searching: the artist with the title
    view.query.set("")
    view._render()
    assert view.tree.set("123", "title") == "Song 12 3"
    assert view.tree.set(view.tree.get_children()[0], "title").startswith("Album 0")


def test_a_search_keeps_marks_and_lets_go_of_hidden_rows(root):
    view = playlist_view.PlaylistView(root, on_play=lambda i: None)
    view.set_tracks(_songs("Intro", "Blue", "Green", "Yellow"), flat=True)
    view.show_current(2)
    view.select(0)
    view.query.set("green")
    view._render()
    assert view.tree.get_children() == ("2",) and view.tree.selection() == ()
    assert view.tree.focus() == ""                          # Enter can't play a hidden song
    assert view.tree.set("2", "no") == playlist_view.PLAYING_MARK
    view.select(0)                                          # hidden: not selected
    assert view.tree.selection() == ()
    view.query.set("zzz")
    view._render()
    assert view.tree.get_children() == ("none",) and view._songs == []
    view.query.set("")
    view._render()
    assert view.tree.get_children() == ("0", "1", "2", "3") and view.tree.selection() == ()


def test_a_new_list_drops_a_search_waiting_to_run(root):
    view = playlist_view.PlaylistView(root, on_play=lambda i: None)
    view.set_tracks(_songs("One", "Two"), flat=True)
    view.query.set("one")
    view.query.set("")                                      # as a tab switch clears the search…
    view.set_tracks(_songs("Three", "Four", album="B"), flat=True)   # …and shows the new tab
    assert view._search_job is None and view.tree.get_children() == ("0", "1")


# -- the Library -------------------------------------------------------------------------------------
@pytest.fixture
def library(tmp_path, monkeypatch):
    from ironbark import settings

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    music = tmp_path / "Music"
    flac(music / "21" / "01.flac", title="Rolling", artist="Adele", album="21", date="2011")
    flac(music / "Tommy" / "01.flac", title="Overture", artist="The Who", album="Tommy", date="1969")
    return music


def test_library_search_waits_for_a_pause_and_enter_uses_the_words(root, library):
    from ironbark.library_window import LibraryWindow

    chosen = []
    w = LibraryWindow(root, library, None, on_choose=chosen.append, on_root_changed=lambda p: None,
                      on_load_all=lambda r, q: None)
    wait_for_index(w, root)
    fills = []
    real = w._fill
    w._fill = lambda: fills.append(1) or real()
    for letter in "tommy":                     # typing: no refill per key
        w.search_var.set(w.search_var.get() + letter)
    assert fills == [] and len(w._rows) == 2
    w._choose_first()                          # Enter straight away: the album typed, not the first listed
    assert [p.name for p in chosen] == ["Tommy"] and fills == [1]


def test_a_closed_library_window_is_freed(root, library):
    from ironbark.library_window import LibraryWindow
    from ironbark.tabbar import Popover

    w = LibraryWindow(root, library, None, lambda p: None, lambda p: None, lambda r, q: None,
                      on_queue=lambda path, front: None)
    wait_for_index(w, root)
    _pump(root, 0.1)
    first = next(iter(w._rows))
    w.tree.focus(first)
    w._context(popups.row_event(w.tree))       # a right-click menu, then closed
    menu = weakref.ref(Popover.open_one)
    assert menu() is not None
    Popover.close_open()
    _pump(root, 0.1)
    window, var = weakref.ref(w), w.search_var
    traces = root.tk.call("trace", "info", "variable", str(var))
    assert len(root.tk.splitlist(traces)) == 2        # the box's own and the window's
    w.win.destroy()                            # the title bar's ×, as well as Esc / a choice (close)
    assert len(root.tk.splitlist(root.tk.call("trace", "info", "variable", str(var)))) == 0
    assert w.search_var is None and w.view_var is None
    del w, var
    gc.collect()
    assert window() is None and menu() is None


# -- freed by reference counting (no cycles left for the collector to free on another thread) -----
def test_buttons_with_tooltips_are_freed_with_their_window(root):
    import tkinter as tk

    from ironbark.tabbar import IconButton

    gc.collect()
    gc.disable()
    try:
        top = tk.Toplevel(root)
        flat = controls.FlatButton(top, "Rescan", lambda: None, label="Look for added music now")
        icon = IconButton(top, root, "settings", lambda: None, label="Settings")
        flat.pack()
        icon.pack()
        root.update()
        refs = [weakref.ref(flat), weakref.ref(icon)]
        del flat, icon
        top.destroy()
        del top
        assert [r() for r in refs] == [None, None]
    finally:
        gc.enable()


def test_pill_buttons_share_one_measuring_font(root):
    a = controls.PillButton(root, "A", lambda: None)
    b = controls.FlatButton(root, "B")
    c = controls.FlatButton(root, "C")
    assert b._measure is c._measure and a._measure is not None


def test_answered_prompts_and_closed_menus_are_freed(root):
    gc.collect()
    commands = None
    for n in range(4):
        menu = popups.PopupMenu(root)
        menu.add("Play", lambda big=list(range(1000)): None)
        menu.popup(root.winfo_rootx() + 20, root.winfo_rooty() + 20)
        root.update()
        menu.hide()
        root.update()
        prompt = popups.Prompt(root, "Name?", entry="Mix", buttons=(("OK", True), ("Cancel", None)))
        prompt.press_cancel()
        assert prompt.text is None and prompt._done_var is None    # Tk variables let go of here
        refs = [weakref.ref(menu), weakref.ref(prompt)]
        del menu, prompt
        gc.collect()
        assert [r() for r in refs] == [None, None]
        if n == 0:
            commands = len(root._tclCommands)
    assert len(root._tclCommands) == commands       # nothing new held by the root per menu or prompt
    assert list(root.tk.splitlist(root.tk.call("bind", "DGScroll"))) == ["<MouseWheel>"]


def test_the_wheel_still_scrolls_a_long_menu_and_keys_reach_the_right_prompt(root):
    import tkinter as tk

    area = controls.ScrollArea(root)
    area.pack(fill="both", expand=True)
    rows = [tk.Label(area.inner, text=f"Row {n}") for n in range(80)]
    for row in rows:
        row.pack()
    area.bind_wheel()
    _pump(root, 0.1)
    assert area.wheel_tag in rows[5].bindtags()
    rows[5].event_generate("<MouseWheel>", delta=-120)
    assert area.canvas.yview()[0] > 0
    win = tk.Toplevel(root)
    win.geometry("300x300")
    _pump(root, 0.1)
    under = popups.Prompt(root, "First?", entry="a", buttons=(("OK", True), ("Cancel", None)))
    over = popups.Prompt(win, "Second?", buttons=(("OK", True), ("Cancel", None)))
    under.entry.focus_force()                               # (the first box's window made active again)
    _pump(root, 0.05)
    under.entry.event_generate("<Return>")                 # the key went to the first box: it answers
    assert under.done and under.result == "a" and not over.done
    over.press_cancel()
    win.destroy()


# -- a press let go somewhere else isn't a click --------------------------------------------------
def test_controls_ignore_a_release_away_from_them(root):
    import tkinter as tk

    from ironbark.tabbar import MenuRow, SettingRow, TabBar

    root.deiconify()
    root.geometry("520x520")
    closed, ran, chosen = [], [], []
    bar = TabBar(root, root, on_select=lambda i: None, on_close=closed.append, on_new=lambda: None,
                 on_settings=lambda: None, on_double=lambda: None, on_quit=lambda: None)
    bar.pack(fill="x")
    flag, switch_var, choice = tk.BooleanVar(value=False), tk.BooleanVar(value=False), tk.StringVar(value="a")
    setting = SettingRow(root, root, "Bit-perfect", "Exclusive mode", flag)
    setting.pack(fill="x")
    row = MenuRow(root, "Delete playlist", lambda: ran.append(1))
    row.pack(fill="x")
    choices = controls.ChoiceRow(root, (("a", "Live"), ("b", "Whole track")), choice, command=lambda: chosen.append(1))
    choices.pack()
    switch = controls.Switch(root, "Match levels", switch_var)
    switch.pack()
    bar.set_tabs(["One", "Two"], 0, None)
    _pump(root, 0.2)
    bar._pointer(bar.slot_x(1) + 5, theme.px(theme.ROW_Y))   # rest on tab 2: its close glyph shows
    bar._reveal_x()
    _pump(root, 0.05)
    x, y = bar._x_centre(1)
    far = 300
    bar.canvas.event_generate("<ButtonPress-1>", x=x, y=y)
    _release(bar.canvas, x + far, y)
    for widget in (setting, row, choices.labels["b"], switch.label):
        _release(widget, widget.winfo_width() + far, 2)
    assert (closed, flag.get(), ran, choice.get(), switch_var.get()) == ([], False, [], "a", False)
    bar.canvas.event_generate("<ButtonPress-1>", x=x, y=y)
    _release(bar.canvas, x, y)
    for widget in (setting, row, choices.labels["b"], switch.label):
        _release(widget, 2, 2)
    assert (closed, flag.get(), ran, choice.get(), switch_var.get()) == ([1], True, [1], "b", True)


# -- the tab strip -----------------------------------------------------------------------------------
def test_new_tab_button_comes_home_when_the_last_tab_closes(root):
    from ironbark.tabbar import TabBar

    root.deiconify()
    bar = TabBar(root, root, on_select=lambda i: None, on_close=lambda i: None, on_new=lambda: None,
                 on_settings=lambda: None, on_double=lambda: None, on_quit=lambda: None)
    bar.pack(fill="x")
    root.update()
    bar.set_tabs(["One", "Two", "Three"], 0, None)
    _pump(root, 0.1)
    home = theme.px(TabBar.DRAG_GAP) + 2
    assert bar.add_btn.winfo_x() > home
    bar.set_tabs([], -1, None)
    _pump(root, 0.1)
    assert bar.add_btn.winfo_x() <= home and bar.add_btn.winfo_ismapped()
    bar.set_tabs(["Again"], 0, None)
    _pump(root, 0.1)
    assert bar.add_btn.winfo_x() > home


# -- pictures, focus, the seek bar ----------------------------------------------------------------
def test_a_greyed_out_switch_keeps_its_picture_when_the_cache_is_cleared(root):
    import tkinter as tk

    from PIL import Image

    switch = controls.Switch(root, "Bit-perfect", tk.BooleanVar(value=True))
    switch.pack()
    switch.set_enabled(False)
    root.update()
    shown = str(switch.toggle.cget("image"))
    for n in range(205):
        controls._photo(root, ("filler", n), lambda: Image.new("RGB", (2, 2)))
    assert shown in [str(name) for name in root.tk.splitlist(root.tk.call("image", "names"))]


def test_search_hint_survives_focus_on_a_part_python_doesnt_know(root):
    box = controls.SearchBox(root, "Search albums")
    box.pack(fill="x")
    root.update()
    root.tk.call("frame", ".tkonly", "-takefocus", 1)       # a widget made by Tk alone (as a drop-down's list)
    root.tk.call("pack", ".tkonly")
    box.entry.focus_force()
    _pump(root, 0.05)
    root.tk.call("focus", "-force", ".tkonly")
    _pump(root, 0.05)
    box.update_hint()                                       # used to raise KeyError and leave the hint off
    assert box.hint.winfo_manager() == "place" and not root.errors


def test_seek_bar_drag_across_a_song_change_does_not_seek_the_new_song(root):
    released = []
    bar = controls.Slider(root, on_release=released.append)
    bar.pack(fill="x")
    _pump(root, 0.05)
    x = int(bar.winfo_width() * 0.95)
    bar.set(0, 600)
    bar._press(SimpleNamespace(x=x))
    bar.set(5, 180)                                         # the next song started mid-drag
    bar._release(SimpleNamespace(x=x))
    assert released == [] and (bar.value, bar.maximum, bar.dragging) == (5, 180, False)
    bar._press(SimpleNamespace(x=x))
    bar.set(6, 180)
    bar._release(SimpleNamespace(x=x))                      # the same song: an ordinary seek
    assert len(released) == 1 and released[0] > 150


def test_seek_bar_recovers_from_a_release_it_never_heard(root):
    bar = controls.Slider(root, on_release=lambda v: None)
    bar.pack(fill="x")
    _pump(root, 0.05)
    bar.set(0, 300)
    bar._press(SimpleNamespace(x=20))
    bar.set(40, 300)                                        # held back while dragged
    assert bar.value != 40
    bar.event_generate("<Motion>", x=30, y=2)               # the pointer moves, the button long up
    assert not bar.dragging and bar.value == 40
    bar.set(41, 300)
    assert bar.value == 41                                  # following again


# -- prompts ---------------------------------------------------------------------------------------
def test_enter_in_a_delete_question_answers_no(root):
    def later(action):
        def go():
            if not popups.Prompt.stack:
                root.after(20, go)
                return
            seen.append(popups.Prompt.stack[-1])
            action(popups.Prompt.stack[-1])
        root.after(50, go)

    seen = []
    later(lambda p: p.press_default())                      # Enter (held, or pressed again)
    assert popups.confirm(root, "Delete the playlist “Mix”?", ok="Delete", danger=True) is False
    box = seen[-1]
    assert box.default == 1 and box.buttons[1].kind == "plain"         # Cancel: plain, no green go-ahead
    assert box.buttons[1].fill_colour() == theme.SURFACE_HI
    later(lambda p: p._chosen(0))                           # a click on Delete
    assert popups.confirm(root, "Delete the playlist “Mix”?", ok="Delete", danger=True) is True
    direct = popups.Prompt(root, "Delete?", buttons=(("Delete", True, "danger"), ("Keep", False)), cancel=False)
    assert direct.default == 1
    direct.press_default()
    assert direct.result is False


def test_a_flash_after_a_palette_change_keeps_the_new_edge(root):
    prompt = popups.Prompt(root, "Turn your amp down first", tone="warn")
    _pump(root, 0.05)
    theme.use(theme.LIGHT, root)
    assert prompt.cget("highlightbackground") == theme.LIGHT.WARN
    prompt.bell = lambda *a: None                           # (no beep from a test)
    prompt.flash()
    _pump(root, 4 * popups.FLASH_MS / 1000 + 0.2)
    assert prompt.cget("highlightbackground") == theme.LIGHT.WARN
    prompt.press_cancel()


# -- palette hooks ---------------------------------------------------------------------------------
def test_dead_palette_hooks_dont_pile_up():
    class Widget:
        def repaint(self, _palette):
            pass

    for _ in range(3000):
        theme.on_change(Widget().repaint)                   # each one gone at once
    dead = sum(ref() is None for ref in theme._hooks)
    assert dead < 3000 and len(theme._hooks) <= 2 * theme._hooks_kept + 64

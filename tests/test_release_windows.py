"""The windows before release: the Library's album list keeping its place when the library
changes in the background; Enter or ↓ straight after typing a song search; Needs a look's status line
while the check runs, its heading menu, and checks of files changed since; the Songs view's sort arrow
over search results; About readable on a small screen; the shortcut sheet's long rows; the floating
search panel dropping a waiting Enter when it's closed. Tk tests skip without a display."""
import gc
import os
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from test_library_songs import song_window, wait_for_songs
from test_library_view import column, flac, music, root, wait_for_index  # noqa: F401 — shared fixtures

from ironbark import help_window, keys, library_check, library_index, library_view, placement, platforms, theme
from ironbark.indexer import Indexer
from ironbark.library_db import LibraryDB
from ironbark.library_view import LibraryView


def pump(root, until, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        if until():
            return
        time.sleep(0.02)
    raise AssertionError("it never happened")


def settle(root, seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


# -- the Library's album list keeps its place ---------------------------------------------------------------
def test_the_album_list_keeps_its_place_and_selection_when_the_library_changes(root, tmp_path, monkeypatch):
    """An album opened (no longer new), a tag edit or the watcher's look reads the albums again: the list
    stays where it was with the same album selected, so adding a few in a row works."""
    from ironbark import settings

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    monkeypatch.setattr(LibraryView, "_sorts", {})
    monkeypatch.setattr(LibraryView, "_closed", {})
    music = tmp_path / "Music"
    for n in range(40):
        flac(music / f"Album {n:02d}" / "01.flac", title=f"Song {n}", artist=f"Artist {n:02d}",
             album=f"Album {n:02d}", date="2001")
    w = LibraryView(root, music, None, on_choose=lambda p: None, on_root_changed=lambda p: None,
                      on_load_all=lambda r, q: None)
    wait_for_index(w, root)
    pump(root, lambda: len(w.list.source) == 40)
    w.list.select(30)
    settle(root, 0.1)
    top, chosen = w.list.top, w._selected_album().path
    assert top > 0 and chosen.name == "Album 30"

    w.refill()                                   # (an album opened: it's no longer new)
    assert w.list.top == top and w._selected_album().path == chosen

    # Something changes in the background: an album arrives that sorts in above the one selected.
    flac(music / "Album 05b" / "01.flac", title="New", artist="Zed", album="Album 05b", date="2002")
    index = library_index.shared([music])
    worker = threading.Thread(target=lambda: index.update([music / "Album 05b" / "01.flac"]))
    worker.start()
    worker.join()
    pump(root, lambda: len(w.list.source) == 41)
    assert w._selected_album().path == chosen and w.list.top == top
    assert str(w.open_button.cget("state")) == "normal"

    # Grouped views keep their place too (rows keyed by group name and album folder, not position).
    w.set_view("artists")
    index_of = next(i for i, (kind, thing) in enumerate(w._items) if kind == "album" and thing.path == chosen)
    w.list.select(index_of)
    w.refill()
    assert w._selected_album().path == chosen
    w.close()


# -- Library → Songs: Enter or ↓ straight after typing ------------------------------------------------------
def test_enter_or_down_straight_after_typing_a_song_search_acts_once_the_songs_are_here(root, music, monkeypatch):
    monkeypatch.setattr(LibraryView, "_sorts", {})
    root.deiconify()                             # (on show: the keyboard focus below is real)
    calls = []
    w = song_window(root, music, calls)
    w.close = lambda: None                       # (kept open to look at it after a play)
    w.set_view("songs")
    wait_for_songs(w, root)

    w.search_var.set("overture")
    w._choose_first()                            # Enter in the box at once: nothing to play yet…
    assert calls == []
    pump(root, lambda: calls)                    # …so it plays once the search has come back
    assert calls == [("play", "Overture", "Tommy")]

    # The results on show stay while the next search is on its way (the list doesn't empty).
    w.search_var.set("rolling")
    w._flush_search()
    assert column(w, "title") == ["Overture"] and w._songs_asked is not None
    w._focus_list()                              # ↓ before the results: done once they're here
    pump(root, lambda: w._songs_asked is None and w.list.selection() == 0)
    assert column(w, "title") == ["Rolling"] and w.list.top == 0
    assert str(w.frame.focus_lastfor()) == str(w.tree)

    # An Enter waiting for one view's songs isn't carried into another view.
    w.search_var.set("hits")
    w._choose_first()
    w.set_view("albums")
    settle(root, 0.4)
    assert len(calls) == 1
    w.frame.destroy()


# -- the Songs view's sort arrow over search results ---------------------------------------------------------
def test_song_search_results_show_no_sort_arrow_until_a_heading_is_clicked(root, music, monkeypatch):
    monkeypatch.setattr(LibraryView, "_sorts", {})
    w = song_window(root, music, [])
    w.set_view("songs")
    wait_for_songs(w, root)
    assert w.tree.heading("title", "text") == "Title ▲"   # the whole list: by title
    w.search_var.set("hits")
    w._flush_search()
    wait_for_songs(w, root)
    assert sorted(column(w, "title")) == ["A", "B"]
    assert not any(w.tree.heading(c, "text").endswith(("▲", "▼")) for c in library_view.COLUMNS)
    w.sort_by("title")                                     # clicked: A to Z (not reversed), and it says so
    wait_for_songs(w, root)
    assert w.tree.heading("title", "text") == "Title ▲" and column(w, "title") == ["A", "B"]
    w.sort_by("title")
    wait_for_songs(w, root)
    assert w.tree.heading("title", "text") == "Title ▼" and column(w, "title") == ["B", "A"]
    w.close()


# -- Needs a look ------------------------------------------------------------------------------------------
class StubChecker:
    def __init__(self):
        self.status = library_check.Status(state="on", running=True, done=5, total=100, seconds_left=600)
        self.changes = 0
        self.workers = 1
        self.state = "on"


def test_needs_look_keeps_its_count_while_the_check_reads_more(root, music, monkeypatch):
    """While the check runs, Needs a look reads its results again every few seconds: the status line keeps
    describing the list on show meanwhile (it used to flash "Reading the songs…")."""
    checker = StubChecker()
    w = LibraryView(root, music, None, on_choose=lambda p: None, on_root_changed=lambda p: None,
                      on_load_all=lambda r, q: None, view="needs_look", checker=checker)
    wait_for_index(w, root)
    db = library_index.shared([music]).db
    song = music / "Tommy" / "01.flac"
    stat = song.stat()
    db.save_check(str(song), stat.st_size, stat.st_mtime_ns, library_check.CHECK_FORMAT, "clipping", 1.0, "{}")
    checker.changes += 1
    monkeypatch.setattr(library_view, "CHECK_REREAD_S", 0.0)
    pump(root, lambda: "1 song needs a look" in w.status.cget("text"))

    checker.changes += 1                        # more stored: read again…
    w._check_read = w._check_took = 0.0
    w._follow_check()
    assert w._songs_asked is not None
    text = w._song_count_text()                  # …the list on show is still described
    assert "1 song needs a look: 1 clipped" in text and "Reading" not in text
    w.close()


def test_needs_look_heading_menu_offers_no_columns_it_cannot_show(root, music, monkeypatch):
    from ironbark import popups

    shown = []
    monkeypatch.setattr(popups.PopupMenu, "popup", lambda self, x, y: shown.append(self))
    w = LibraryView(root, music, None, on_choose=lambda p: None, on_root_changed=lambda p: None,
                      on_load_all=lambda r, q: None, view="needs_look", checker=StubChecker())
    wait_for_index(w, root)
    w.tree.identify_region = lambda x, y: "heading"
    event = SimpleNamespace(x=5, y=5, x_root=5, y_root=5)
    w._context(event)
    assert shown == []                           # Needs a look: no Plays / Last played to tick
    w.set_view("songs")
    w._context(event)
    assert len(shown) == 1                       # the other song views still offer them
    w.close()


def test_needs_look_says_when_a_file_has_changed_since_its_check(tmp_path):
    music = tmp_path / "Music"
    song = music / "A" / "01.flac"
    song.parent.mkdir(parents=True)
    sf.write(song, np.zeros((44100, 2), np.int16), 44100, format="FLAC", subtype="PCM_16")
    db = LibraryDB.open(tmp_path / "library.db", [music])
    try:
        Indexer(db).check([music])
        stat = song.stat()
        db.save_check(os.fspath(song), stat.st_size, stat.st_mtime_ns, library_check.CHECK_FORMAT, "damaged",
                      None, "")
        (hit, look), = library_check.needs_look(db)
        assert not look.changed and look.text == "Damaged: can't be decoded from the start"
        # Re-ripped over the same name: the library sees the new file; the old check says it's out of date.
        sf.write(song, np.zeros((88200, 2), np.int16), 44100, format="FLAC", subtype="PCM_16")
        Indexer(db).update([music], [song])
        (hit, look), = library_check.needs_look(db)
        assert look.changed and look.words == ("damaged",)
        assert look.text == f"Damaged: can't be decoded from the start · {library_check.CHANGED_TEXT}"
    finally:
        db.close()


# -- About on a small screen ---------------------------------------------------------------------------------
def test_about_opens_no_taller_than_the_screen_and_every_part_can_be_read(root, monkeypatch):
    room = theme.px(500)   # (shorter than the page, about 580 px at 100%: 700 was only short with too-big fonts)
    monkeypatch.setattr(placement, "work_height", lambda window: room)
    about = help_window.AboutWindow(root, {}, copy=lambda text: None, open_url=lambda url: None)
    settle(root, 0.4)
    assert about.win.winfo_height() <= room - theme.px(help_window.FIT_MARGIN)
    line = int(about.text.tk.call("font", "metrics", about.text.cget("font"), "-linespace"))
    # A short window: the page scrolls, the privacy text is whole and the licence text keeps its lines.
    assert about.area.needed()
    assert about.privacy.winfo_height() >= about.privacy.winfo_reqheight()
    assert about.text.winfo_height() >= help_window.LICENCE_LINES * line
    about.area.see(about.text)
    assert about.area.visible(about.text) and about.text.winfo_ismapped()
    # The smallest window still reaches everything.
    about.win.geometry(f"{theme.px(360)}x{theme.px(360)}")
    settle(root, 0.4)
    about.area.see(about.text)
    assert about.area.visible(about.text) and about.text.winfo_height() >= help_window.LICENCE_LINES * line
    # A tall one: nothing scrolls, and the licence text takes the room that's left.
    about.win.geometry(f"{theme.px(600)}x{theme.px(1300)}")
    settle(root, 0.4)
    # (Tall enough for the page as it's wrapped here, with room to spare: the fonts and scaling an earlier
    # test in the run left behind can make it taller than 1300 px.)
    chrome = about.win.winfo_height() - about.area.canvas.winfo_height()
    page = about.area.inner.winfo_reqheight() - about.text.winfo_reqheight()
    tall = chrome + page + (help_window.LICENCE_LINES + 4) * line
    if tall > about.win.winfo_height():
        if tall > root.winfo_screenheight() - theme.px(help_window.FIT_MARGIN):
            pytest.skip(f"the screen is too short for the whole About page here ({tall} px)")
        about.win.geometry(f"{theme.px(600)}x{tall}")
        settle(root, 0.4)
        if about.win.winfo_height() < tall and platforms.IS_MAC:   # (macOS keeps a window inside the usable area)
            assert about.win.maxsize()[1] >= tall                  # (not a cap of the window's own)
            pytest.skip(f"the screen's usable area is too short for the whole About page here "
                        f"({tall} px asked, {about.win.winfo_height()} given)")
    sizes = (f"window {about.win.winfo_height()}, panel {about.area.canvas.winfo_height()}, page "
             f"{about.area.inner.winfo_reqheight()}, licence text {about.text.winfo_height()}, line {line}, "
             f"scale {theme.scale():.2f}")
    assert not about.area.needed(), sizes
    assert about.text.winfo_height() > help_window.LICENCE_LINES * line, sizes
    left = about.area.canvas.winfo_height() - about.area.inner.winfo_reqheight()
    assert 0 <= left < line + 2
    about.close()


# -- the shortcut sheet's long rows ----------------------------------------------------------------------------
def test_shortcut_rows_too_long_for_the_list_end_with_an_ellipsis_and_show_whole_when_selected(root):
    sheet = help_window.HelpWindow(root, {})
    settle(root, 0.4)
    width = int(sheet.tree.column("what", "width"))
    font = tkfont.nametofont("TkDefaultFont")
    longest = max(sheet.rows, key=lambda iid: font.measure(sheet.rows[iid][2]))
    assert font.measure(sheet.rows[longest][2]) > width      # (the default size can't show it whole)
    shown = sheet.tree.set(longest, "what")
    assert longest in sheet.cut and shown.endswith("…") and font.measure(shown) <= width
    assert not sheet.full.winfo_manager()
    sheet.tree.selection_set(longest)
    settle(root, 0.1)
    group, keys_text, what = sheet.rows[longest]
    assert sheet.full.cget("text") == f"{keys_text}: {what}" and sheet.full.winfo_manager()
    short = next(iid for iid in sheet.rows if iid not in sheet.cut)
    sheet.tree.selection_set(short)
    settle(root, 0.1)
    assert not sheet.full.winfo_manager()
    # Wide enough, nothing is cut.
    sheet.win.geometry(f"{theme.px(1600)}x{theme.px(640)}")
    settle(root, 0.4)
    assert not sheet.cut and sheet.tree.set(longest, "what") == what
    assert sheet.shown_rows() == keys.sheet_rows()
    sheet.close()


# -- the floating search panel ----------------------------------------------------------------------------
@pytest.fixture
def shown_root():
    """A root on show (the search panel is placed inside it), as the search panel's own tests have it."""
    try:
        shown = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    theme.apply(shown)
    shown.geometry(f"{theme.px(420)}x{theme.px(600)}")
    yield shown
    shown.destroy()
    del shown
    gc.collect()  # on this thread (Tk objects freed on another thread abort Python)

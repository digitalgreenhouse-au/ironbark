"""Library window (0.15.0) on the saved index: browse views, sorting by a heading, search, right-click
Play next; the track list's optional Artist / Album columns; the Up next line. Tk smoke tests,
skipped without a display."""
import gc
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import library_index, settings, theme
from ironbark.engine import Player
from ironbark.library import Track
from ironbark.library_view import LibraryView, added_text
from ironbark.playlist_view import PlaylistView
from ironbark.tabbar import MenuRow, Popover
from ironbark.tagio.flac import FLACFile
from ironbark.ui import PlayerWindow


def flac(path: Path, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros((441, 2), np.int16), 44100, subtype="PCM_16", format="FLAC")
    audio = FLACFile(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    root.withdraw()
    theme.apply(root)
    yield root
    root.destroy()
    del root
    gc.collect()  # on this thread (Tk objects freed on another thread abort Python)


@pytest.fixture
def music(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    music = tmp_path / "Music"
    flac(music / "21" / "01.flac", title="Rolling", artist="Adele", album="21", genre="Pop", date="2011")
    flac(music / "Tommy" / "01.flac", title="Overture", artist="The Who", album="Tommy", date="1969")
    flac(music / "Hits" / "01.flac", title="A", artist="Alpha", album="Hits", genre="Pop", date="1999")
    flac(music / "Hits" / "02.flac", title="B", artist="Beta", album="Hits", genre="Dance", date="1999")
    return music


def wait_for_index(window, root):
    deadline = time.monotonic() + 20
    while window._refresh is not None and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    assert window._refresh is None


def wait_for_albums(window, root):
    """Until the album list has come from the query thread (0.31: never read on Tk's thread)."""
    deadline = time.monotonic() + 20
    while (window._loading or window._asked is not None) and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    assert not window._loading


def column(window, name, parent=""):
    """A column's text down the list (groups are rows of the list, not Tk parents; `parent`, a
    group row's key: the albums under it)."""
    return [str(value) for value in window.column_values(name, parent or None)]


def test_browse_views_sorting_and_search(root, music):
    queued = []
    w = LibraryView(root, music, None, on_choose=lambda p: None, on_root_changed=lambda p: None,
                      on_load_all=lambda r, q: None, on_queue=lambda path, front: queued.append((path.name, front)))
    wait_for_index(w, root)
    assert column(w, "album") == ["21", "Hits", "Tommy"] and "3 albums in" in w.status.cget("text")
    assert column(w, "artist") == ["Adele", "Various Artists", "The Who"]
    assert column(w, "added")[0] == added_text(w.albums[0].added) and "/" in column(w, "added")[0]
    w.sort_by("year")
    assert column(w, "album") == ["Tommy", "Hits", "21"] and w.tree.heading("year", "text") == "Year ▲"
    w.sort_by("year")                                       # again: reversed
    assert column(w, "album") == ["21", "Hits", "Tommy"] and w.tree.heading("year", "text") == "Year ▼"
    w.set_view("artists")
    assert column(w, "album") == ["Adele", "The Who", "Various Artists"]   # groups, "The" ignored
    groups = list(w._groups)
    assert column(w, "album", groups[2]) == ["Hits"] and [str(x) for x in w.tree.cget("show")] == ["tree", "headings"]
    w.set_view("genres")
    assert column(w, "album") == ["Dance", "Pop", "No genre"]
    assert column(w, "album", list(w._groups)[1]) == ["21", "Hits"]
    w.search_var.set("adele")
    w._flush_search()                # (the list follows the box after a pause in typing)
    assert column(w, "album") == ["Pop"] and column(w, "album", list(w._groups)[0]) == ["21"]
    w.search_var.set("dance")      # Dance's own name matches (all of it); Hits is also Pop
    w._flush_search()
    assert column(w, "album") == ["Dance", "Pop"] and column(w, "album", list(w._groups)[1]) == ["Hits"]
    w.search_var.set("")
    w.set_view("recent")
    assert w.sort == ("added", True) and w.tree.heading("added", "text") == "Added ▼"
    # right-click an album → Play next goes to the player with the album's folder
    first = next(iter(w._rows))
    w._queue(w._rows[first], True)
    assert queued and queued[0][1] is True
    w.close()


def test_second_open_shows_the_saved_index_at_once(root, music):
    w = LibraryView(root, music, None, lambda p: None, lambda p: None, lambda r, q: None)
    wait_for_index(w, root)
    w.close()
    library_index._shared = None                 # as after a restart: read back from the database
    look = library_index.shared(music).indexer
    look._lock.acquire()                         # a look under way: the new window's refresh waits for it
    try:
        again = LibraryView(root, music, None, lambda p: None, lambda p: None, lambda r, q: None)
        wait_for_albums(again, root)             # (the list comes from the query thread, a moment later)
        assert column(again, "album") == ["21", "Hits", "Tommy"] and again._refresh is not None   # before any
    finally:                                                                                       # refresh finished
        look._lock.release()
    wait_for_index(again, root)
    again.close()


def test_several_library_folders_and_add_folder(root, music, tmp_path, monkeypatch):
    """(0.31) The Library lists every library folder's albums; "Add folder…" adds one (the player is
    told the new list) and the list follows it once its refresh is done."""
    from tkinter import filedialog

    more = tmp_path / "More"
    flac(more / "Blue" / "01.flac", title="River", artist="Joni Mitchell", album="Blue", date="1971")
    monkeypatch.setattr(LibraryView, "_sorts", {})       # (the sort is kept for the session)
    told = []
    w = LibraryView(root, [music], None, lambda p: None, told.append, lambda r, q: None)
    wait_for_index(w, root)
    assert column(w, "album") == ["21", "Hits", "Tommy"] and w._place() == "Music"
    monkeypatch.setattr(filedialog, "askdirectory", lambda **kwargs: str(more))
    w._add_folder()
    assert told == [[music, more]] and w.roots == [music, more]
    wait_for_index(w, root)
    assert column(w, "album") == ["21", "Blue", "Hits", "Tommy"] and w._place() == "2 folders"
    assert "4 albums in 2 library folders" in w.status.cget("text")
    w.set_roots([more])                                      # (Settings → Library → Remove)
    wait_for_index(w, root)
    assert column(w, "album") == ["Blue"] and library_index.shared([more]).db.roots() == [str(more)]
    told.clear()
    monkeypatch.setattr(filedialog, "askdirectory", lambda **kwargs: str(more / "Blue"))
    w._add_folder()                                          # (a folder already in the library)
    assert told == [] and w.roots == [more] and "already in the Library" in w.status.cget("text")
    w.close()


def test_rebuild_the_library_forgets_a_folder_that_has_gone(root, music, monkeypatch):
    """⋯ → Rebuild the library…: asked first ("Keep it" changes nothing); then everything stored
    is forgotten and the library folders read again, so an album whose folder has gone (one a
    normal look keeps, here the library folder emptied) leaves the list, and plays stay."""
    import shutil

    from ironbark import popups

    monkeypatch.setattr(LibraryView, "_sorts", {})
    w = LibraryView(root, [music], None, lambda p: None, lambda r: None, lambda r, q: None)
    wait_for_index(w, root)
    assert column(w, "album") == ["21", "Hits", "Tommy"]
    shown = []
    monkeypatch.setattr(popups.PopupMenu, "popup", lambda self, x, y: shown.append(self))   # (not shown)
    w._more_menu()
    assert "Rebuild the library…" in shown[-1].labels()
    key = str(music / "Hits" / "01.flac")
    w.index.db.record_play(key, 300, 300)
    for name in ("21", "Hits", "Tommy"):
        shutil.rmtree(music / name)
    flac(music / "Blue" / "01.flac", title="River", artist="Joni Mitchell", album="Blue", date="1971")
    asked = []
    monkeypatch.setattr(popups, "confirm", lambda *a, **k: asked.append((a[1], k.get("danger"))) or False)
    w.rebuild()
    assert asked == [("Rebuild the library?", True)] and w._refresh is None
    assert column(w, "album") == ["21", "Hits", "Tommy"]
    monkeypatch.setattr(popups, "confirm", lambda *a, **k: True)
    w.rebuild()
    assert w._refresh is not None and w._refresh["fresh"] and column(w, "album") == []
    wait_for_index(w, root)
    assert column(w, "album") == ["Blue"]
    assert w.index.db.play_counts([key]) == {key: 1}
    w.close()


def test_a_first_look_fills_the_album_list_as_it_goes(root, music, monkeypatch):
    """A rescan's albums come once at its end; a first look's (or a rebuild's) are asked for again
    every FILL_EVERY_S as albums are stored, so a big library can be browsed while it's read."""
    from ironbark import library_view

    w = LibraryView(root, [music], None, lambda p: None, lambda r: None, lambda r, q: None)
    wait_for_index(w, root)
    wait_for_albums(w, root)
    asked = []
    monkeypatch.setattr(w, "_ask_albums", lambda: asked.append(1))
    w._filling({"first": False})                  # a rescan: nothing asked
    assert asked == []
    job = {"first": True}
    w._filling(job)
    w._filling(job)                               # (again at once: not yet)
    assert asked == [1] and job["filling"]
    job["filled"] -= library_view.FILL_EVERY_S
    w._filling(job)
    assert asked == [1, 1]
    w._filling({"fresh": True})
    assert asked == [1, 1, 1]
    w.close()


def test_the_status_line_wraps_once_per_width_for_each_text(root, music, monkeypatch):
    """(Seen on Windows: a first look's "3 albums so far · Building the library…" set the status line's
    wrap and the window's width chasing each other, and Tk's idle loop never ended.) A width the
    text has been wrapped at already isn't wrapped at again, so a cycle of widths stops; new text is
    wrapped afresh."""
    from ironbark import library_view

    w = LibraryView(root, [music], None, lambda p: None, lambda r: None, lambda r, q: None)
    wait_for_index(w, root)
    wrapped = []
    monkeypatch.setattr(library_view.theme, "wrap_inside", lambda label, width: wrapped.append(width))
    w._say("3 albums so far · Building the library: reading songs… 5 of 8 files (62%) · 0:01")
    for width in (300, 320, 300, 320, 300):       # (the chase: each width once)
        w._wrap_status(width)
    assert wrapped == [300, 320]
    w._say("3 albums so far · Building the library: reading songs… 6 of 8 files (75%) · 0:02")
    w._wrap_status(300)
    assert wrapped == [300, 320, 300]
    w.close()


def test_a_slow_album_list_is_read_again_less_often(root, music, monkeypatch):
    """A very big library's album list takes a while to read: during a first look it's read again
    no more than every FILL_EVERY_S, and less often still when it's slow (at most FILL_SHARE of the time)."""
    from ironbark import library_view

    w = LibraryView(root, [music], None, lambda p: None, lambda r: None, lambda r, q: None)
    wait_for_index(w, root)
    wait_for_albums(w, root)
    asked = []
    monkeypatch.setattr(w, "_ask_albums", lambda: asked.append(1))
    w._albums_took = 2.0                           # (2 s to read: every 8 s at most)
    job = {"first": True}
    w._filling(job)
    job["filled"] -= library_view.FILL_EVERY_S + 0.1
    w._filling(job)
    assert asked == [1]
    job["filled"] -= 2.0 / library_view.FILL_SHARE
    w._filling(job)
    assert asked == [1, 1]
    w.close()


def test_adding_a_folder_inside_or_around_the_library_folders():
    """(0.31) No album is ever listed twice: a folder inside a library folder is already in the
    library; one that holds library folders takes their place."""
    from ironbark.library_view import adding_folder
    from ironbark.ui import library_folders

    music, jazz, other = Path("C:/Music"), Path("C:/Music/Jazz"), Path("D:/Other")
    assert adding_folder([music], other) == ([music, other], "")
    assert adding_folder([music], music) == ([music], f"“{music}” is already in the Library")
    assert adding_folder([music, other], jazz) == ([music, other],
                                                   f"“{jazz}” is already in the Library (it's inside “{music}”)")
    roots, note = adding_folder([jazz, other], music)
    assert roots == [other, music] and "takes in" in note and str(jazz) in note
    assert library_folders({"library_folders": [str(jazz), str(music), str(other)]}) == [music, other]


def test_track_list_artist_and_album_columns(root):
    view = PlaylistView(root, on_play=lambda i: None)
    track = Track(Path("C:/M/A/01.flac"), "Song", artist="Singer", album="Record", track_no=1)
    view.set_tracks([track], flat=True)
    assert view.tree.cget("displaycolumns") == ("no", "title", "time")       # off by default
    assert view.tree.set("0", "title") == "Song  ·  Singer"
    view.set_columns(["album", "artist"])
    assert view.tree.cget("displaycolumns") == ("no", "title", "artist", "album", "time")
    assert (view.tree.set("0", "title"), view.tree.set("0", "artist"), view.tree.set("0", "album")) == (
        "Song", "Singer", "Record")


def test_up_next_line_and_list(root):
    """The Up next line appears with the first queued song, and the pop-over lists them."""
    player = Player()
    tracks = [Track(Path(f"C:/M/A/{n}.flac"), n, artist="X") for n in ("one", "two", "three")]
    body = tk.Frame(root)
    body.pack(fill="both", expand=True)
    status = tk.Label(body)
    status.pack(side="bottom")
    w = SimpleNamespace(player=player, root=root, scale=1.0, _up_next_key=None, prefs={}, _notice="",
                        sleep_timer=None, _netcache=None, _marks_shown=[], settings=SimpleNamespace(is_open=False))
    w.playlist = PlaylistView(body, on_play=lambda i: None)
    w.up_next_label = tk.Label(body)
    w.up_next_pop = Popover(root, width=300)
    w._set_notice = lambda text, ms=0: setattr(w, "_notice", text)
    for name in ("_paint_up_next", "_fill_up_next", "_up_next_marks", "_show_up_next", "_up_next_top", "_unqueue",
                 "_queue_tracks", "stop_after_current", "_sleep_menu", "_menu"):
        setattr(w, name, getattr(PlayerWindow, name).__get__(w))
    root.deiconify()
    root.geometry("320x400")
    w._queue_tracks(tracks[:2], front=False)
    w._queue_tracks(tracks[2:], front=True)
    root.update()
    assert w._notice == "“three” will play next"
    assert w.up_next_label.cget("text") == "Up next (3) · three — X" and w.up_next_label.winfo_ismapped()
    w._show_up_next()
    root.update()
    rows = [c for c in w.up_next_pop.body.winfo_children() if isinstance(c, MenuRow)]
    assert [r.label.cget("text") for r in rows] == ["three — X", "one — X", "two — X"]
    # Below the songs: when to stop (reached by the arrow keys too).
    stops = [r for r in w.up_next_pop.menu_rows() if r not in rows]
    assert [r.label.cget("text") for r in stops] == ["Stop after the current song", "Sleep timer…"]
    stops[0].command()
    assert player.stop_after == "song" and w._notice == "Stops after this song"
    player.set_stop_after(None)
    w._unqueue(1)
    assert player.up_next() == [tracks[2], tracks[1]]
    player.clear_queue()
    w._paint_up_next()
    root.update()
    assert not w.up_next_label.winfo_ismapped() and not w.up_next_pop.is_open

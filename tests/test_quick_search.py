"""The song search panel (quick_search.py, Ctrl+K): searching a real library database, the panel's
debounce and latest-wins asks on its query thread, the sections, the keys (Enter, Shift / Ctrl / Alt
+ Enter, ↓, Esc), the right-click menu, its width tiers, and the track list's "Search the whole
library" row. Favourites (favourites.py) and the database's song lookups for the Library's views."""
import gc
import threading
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from ironbark.tagio.flac import FLACFile

from ironbark import audiofile, library_index, popups, settings, theme
from ironbark.favourites import Favourites
from ironbark.library import Album, Track
from ironbark.library_db import SongHit, SongHits
from ironbark.playlist_view import SEARCH_LIBRARY, PlaylistView
from ironbark.quick_search import Actions, Catalogue, QuickSearch, count_text, search_library, song_values


def flac(path: Path, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros((441, 2), np.int16), 44100, subtype="PCM_16", format="FLAC")
    audio = FLACFile(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A small real library, read into the database."""
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    monkeypatch.setattr(library_index, "_shared", None)
    music = tmp_path / "Music"
    flac(music / "21" / "01.flac", title="Rolling in the Deep", artist="Adele", album="21", date="2011")
    flac(music / "21" / "02.flac", title="Rumour Has It", artist="Adele", album="21", date="2011")
    flac(music / "Goldberg" / "01.flac", title="Aria", artist="Glenn Gould", album="Goldberg Variations",
         composer="Johann Sebastian Bach", date="1981")
    flac(music / "Łódź" / "01.flac", title="Miasto", artist="Łódź Band", album="Łódź", date="1999")
    index = library_index.shared(music)
    index.refresh()
    yield index
    library_index.close_shared()


def test_search_library_finds_songs_albums_and_artists(library):
    db = library.db
    catalogue = Catalogue()
    found = search_library(db, catalogue, "adele")
    assert [hit.track.title for hit in found.songs.rows] == ["Rolling in the Deep", "Rumour Has It"]
    assert found.songs.total == 2 and [a.title for a in found.albums] == ["21"] and found.artists == [("Adele", 1)]
    assert found.songs.rows[0].album.name == "21"
    bach = search_library(db, catalogue, "bach")       # found by its composer: says so
    assert bach.songs.rows[0].where == "composer"
    assert song_values(bach.songs.rows[0])[0] == "Aria  ·  composer: Johann Sebastian Bach"
    assert [a.title for a in search_library(db, catalogue, "lodz").albums] == ["Łódź"]   # accents folded
    generation = catalogue.generation
    search_library(db, catalogue, "gould")
    assert catalogue.generation == generation          # the album list is read again only when it changes
    assert count_text(1234, "song") == "1,234 songs" and count_text(1000, "song", capped=True) == "1,000+ songs"
    assert count_text(1, "album") == "1 album"
    db.release()


def test_the_album_list_is_not_read_for_every_search_while_the_library_keeps_changing():
    """A library's first look changes it with every folder: reading and folding every album (25,000 at
    the most) for each search typed meanwhile would slow every keystroke. Read again once it's had
    REREAD_S to settle."""
    read = []
    db = SimpleNamespace(generation=1, albums=lambda: read.append(1) or [
        Album(Path("C:/M/21"), "21", 2, 44100, 16, ("FLAC",), artist="Adele", year=2011, title="21")])
    catalogue = Catalogue()
    catalogue.update(db)
    for generation in range(2, 30):        # (a folder at a time)
        db.generation = generation
        catalogue.update(db)
    assert len(read) == 1 and catalogue.find(["adele"])[1] == 1
    catalogue.read_at -= Catalogue.REREAD_S
    catalogue.update(db)
    assert len(read) == 2 and catalogue.generation == 29
    catalogue.update(db)                   # (nothing changed since: not read)
    assert len(read) == 2


def test_songs_by_keys_and_sorted_ids(library):
    db = library.db
    found = db.search_songs("rumour").rows[0]
    key = audiofile.key(found.track.path)
    hits = db.songs_by_keys([key, "C:/not/in/the/library.flac"])
    assert [h.track.title for h in hits] == ["Rumour Has It"] and hits[0].album.name == "21"
    titles = [h.track.title for h in db.songs(db.song_ids("title"))]
    assert titles == ["Aria", "Miasto", "Rolling in the Deep", "Rumour Has It"]
    years = [h.track.year for h in db.songs(db.song_ids("year", reverse=True))]
    assert years == [2011, 2011, 1999, 1981]
    db.release()


def other_spelling(key: str) -> str:
    """The same file's key written another way Windows accepts: the drive letter's case swapped."""
    return key[0].swapcase() + key[1:]


def test_a_song_found_by_two_spellings_of_its_path_comes_once(library):
    """Plays, favourites and checks are kept by the path they were made with — a playlist may write
    "c:\\Music" where the library has "C:\\Music". Both find the song, and the key that found it is
    said; the plain list has it once."""
    db = library.db
    hit = db.search_songs("rumour").rows[0]
    key = audiofile.key(hit.track.path)
    assert [h.id for h in db.songs_by_keys([key, other_spelling(key)])] == [hit.id]
    pairs = db.songs_for_keys([other_spelling(key), key, "C:/not/in/the/library.flac"])
    assert [(k, h.id) for k, h in pairs] == [(other_spelling(key), hit.id), (key, hit.id)]
    db.release()


def test_a_favourite_given_just_before_closing_is_stored(library, monkeypatch):
    """The ♥ is stored on a background thread; closing waits for it (the database can be busy a
    moment — another write under way — and the thread would otherwise end with the player)."""
    from ironbark.ui import PlayerWindow

    db = library.db
    track = db.search_songs("aria").rows[0].track
    real = type(db).set_favourite

    def slow(self, key, on):
        time.sleep(0.5)
        return real(self, key, on)

    monkeypatch.setattr(type(db), "set_favourite", slow)
    favourites = Favourites(lambda: library.db)
    favourites.load().join(5)
    favourites.take_loaded()
    favourites.set([track], True)
    PlayerWindow._store_favourites(SimpleNamespace(favourites=favourites))   # (a closing step)
    assert db.favourites() == [audiofile.key(track.path)]
    db.release()


def test_favourites_show_at_once_and_are_stored_in_order(library):
    db = library.db
    track = db.search_songs("aria").rows[0].track
    favourites = Favourites(lambda: library.db)
    told = []
    favourites.changed.append(lambda: told.append(1))
    favourites.load().join(5)
    assert favourites.take_loaded() and favourites.ready and track not in favourites
    assert favourites.toggle([track]) is True and track in favourites and told
    favourites.set([track], False)
    favourites.set([track], True)
    assert favourites.flush()
    assert db.favourites() == [audiofile.key(track.path)]   # on, off, on: stored in that order
    db.release()


# -- the panel, with a stand-in database --------------------------------------------------------------
class FakeDB:
    """Just what the panel's query thread uses: search_songs (recorded), albums, generation."""

    def __init__(self, tracks: list[Track], albums: list[Album]):
        self.tracks, self._albums = tracks, albums
        self.asked: list[str] = []
        self.generation = 1

    def _con(self):
        return None

    def release(self):
        pass

    def interrupt(self, thread):
        pass

    def note_damage(self, error):
        return False

    def albums(self):
        return list(self._albums)

    def search_songs(self, query, limit=200):
        self.asked.append(query)
        words = query.lower().split()
        rows = [SongHit(i + 1, t, t.path.parent) for i, t in enumerate(self.tracks)
                if all(w in f"{t.title} {t.artist} {t.album}".lower() for w in words)]
        return SongHits(rows[:limit], len(rows))


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    theme.apply(root)
    root.geometry(f"{theme.px(420)}x{theme.px(600)}")
    yield root
    root.destroy()
    del root
    gc.collect()  # on this thread (Tk objects freed on another thread abort Python)


def panel(root, calls, **actions):
    tracks = [Track(Path(f"C:/M/21/0{n}.flac"), title, artist="Adele", album="21", track_no=n, duration=200.0,
                    sample_rate=44100, bits=16)
              for n, title in ((1, "Rolling in the Deep"), (2, "Rumour Has It"))]
    albums = [Album(Path("C:/M/21"), "21", 2, 44100, 16, ("FLAC",), artist="Adele", year=2011, title="21")]
    db = FakeDB(tracks, albums)
    index = SimpleNamespace(db=db, indexer=SimpleNamespace(running=False))
    act = Actions(play_song=lambda t, a: calls.append(("play", t.title, a.name)),
                  queue_tracks=lambda ts, front: calls.append(("queue", [t.title for t in ts], front)),
                  open_album=lambda a, play: calls.append(("album", a.name, play)),
                  queue_album=lambda a, front: calls.append(("queue album", a.name, front)),
                  show_in_explorer=lambda path: calls.append(("explorer", Path(path).name)), **actions)
    qs = QuickSearch(root, lambda: index, act, top=lambda: theme.px(30))
    root.update()
    return qs, db


def press(widget, sequence):
    widget.focus_force()
    widget.update()
    widget.event_generate(sequence)


def hold_searches(db: FakeDB) -> threading.Event:
    """The stand-in database's searches wait (on the query thread) until the event is set, so a
    test's Enter is in before the results, however busy the computer is."""
    held = threading.Event()
    search = db.search_songs
    db.search_songs = lambda query, limit=200: (held.wait(10), search(query, limit))[1]
    return held


def test_typing_waits_for_a_pause_and_lists_sections(root, tk_thread_rule):
    calls = []
    qs, db = panel(root, calls)
    qs.open()
    root.update()
    assert qs.is_open and qs.panel.winfo_ismapped() and qs.panel.winfo_y() == theme.px(30)
    qs.query.set("a")
    assert qs._search_job is not None
    qs.flush()
    assert db.asked == [] and "2 or more letters" in qs.status.cget("text")   # one letter: not searched
    for text in ("ad", "ade", "adel", "adele"):          # typing: one search, after the pause
        qs.query.set(text)
    assert db.asked == [] and qs._search_job is not None
    assert qs.wait()
    assert db.asked == ["adele"]
    rows = qs.tree.get_children()
    assert rows == ("s:songs", "t1", "t2", "s:albums", "a0", "s:artists", "r0")
    assert qs.tree.set("s:songs", "title") == "2 songs" and qs.tree.set("a0", "title").startswith("21")
    assert qs.status.cget("text").startswith("2 songs · 1 album · 1 artist")
    qs.list.select(0)
    qs._act("play")                                      # Enter on a section heading folds it
    assert qs.tree.get_children()[:2] == ("s:songs", "s:albums")
    qs._act("play")
    assert "t1" in qs.tree.get_children()


def test_enter_keys_act_on_the_selected_result(root):
    calls = []
    qs, _db = panel(root, calls)
    qs.open("rumour")
    assert qs.wait()
    press(qs.search.entry, "<KeyPress-Down>")            # ↓: into the results, the first song selected
    assert qs.list.selection() == 1 and str(root.focus_get()) == str(qs.tree)
    press(qs.tree, "<Shift-KeyPress-Return>")
    press(qs.tree, "<Control-KeyPress-Return>")
    assert calls == [("queue", ["Rumour Has It"], True), ("queue", ["Rumour Has It"], False)]
    assert qs.is_open and "Up next" in qs.status.cget("text")
    press(qs.tree, "<Alt-KeyPress-Return>")
    assert calls[-1] == ("album", "21", False) and not qs.is_open
    qs.open()
    press(qs.search.entry, "<KeyPress-Return>")          # Enter in the box: the first result plays
    assert calls[-1] == ("play", "Rumour Has It", "21") and not qs.is_open


def test_enter_before_the_results_arrive_acts_once_they_do(root):
    calls = []
    qs, db = panel(root, calls)
    held = hold_searches(db)
    qs.open()
    qs.query.set("rolling")
    press(qs.search.entry, "<KeyPress-Return>")          # pressed before the pause: nothing waits
    assert calls == [] and qs.searching
    held.set()
    assert qs.wait()
    assert calls == [("play", "Rolling in the Deep", "21")]


def test_esc_clears_then_closes_and_a_click_outside_closes(root):
    calls = []
    outside = tk.Frame(root, width=50, height=20)
    outside.place(x=0, y=0)
    qs, _db = panel(root, calls)
    before = tk.Entry(root)
    before.place(x=0, y=0)
    before.focus_force()
    root.update()
    qs.open("adele")
    assert qs.wait()
    assert qs._escape() == "break" and qs.query.get() == "" and qs.is_open
    qs._escape()
    root.update()
    assert not qs.is_open and root.focus_get() is before   # the keyboard goes back where it was
    qs.open()
    qs._click_anywhere(SimpleNamespace(widget=qs.tree))  # inside: stays
    assert qs.is_open
    qs._click_anywhere(SimpleNamespace(widget=outside))
    assert not qs.is_open


def test_right_click_menu_and_drag(root, monkeypatch):
    shown = []
    monkeypatch.setattr(popups.PopupMenu, "popup", lambda self, x, y: shown.append(self))
    calls = []
    favourites = Favourites(lambda: None)
    qs, _db = panel(root, calls, favourites=favourites, playlists=lambda: [Path("C:/P/Road trip.m3u8")])
    qs.open("rolling")
    assert qs.wait()
    root.update()
    x, y, _w, h = qs.tree.bbox("t1")
    qs._context(SimpleNamespace(x=x + 5, y=y + h // 2, x_root=0, y_root=0))
    menu = shown[-1]
    assert menu.labels() == ["Play", "Play next", "Add to Up next", "Open album", "Add to playlist",
                             "Add to favourites", "Show in Library", "Show in Explorer"]
    monkeypatch.setattr(favourites, "_start_writer", lambda: None)   # (nothing to store it in here)
    menu.menu.find("Add to favourites").command()
    assert qs.tree.set("t1", "title").startswith("♥ ")  # marked at once
    menu.menu.find("Play").command()
    assert calls[-1] == ("play", "Rolling in the Deep", "21") and not qs.is_open   # (it takes you there)
    qs.open("adele")
    assert qs.wait()
    assert qs._carried("t1")[0].title == "Rolling in the Deep" and qs._carried("a0") == Path("C:/M/21")
    assert qs._carried("s:songs") is None


def test_columns_follow_the_panel_width(root):
    calls = []
    qs, _db = panel(root, calls)
    qs.open("adele")
    assert qs.wait()
    root.update()
    assert qs.layout == "narrow" and tuple(qs.tree.cget("displaycolumns")) == ("title", "time")
    assert qs.tree.set("t1", "title") == "Rolling in the Deep  ·  Adele"   # the artist rides in the title
    root.geometry(f"{theme.px(1100)}x{theme.px(600)}")
    root.update()
    root.update()
    assert qs.layout == "wide" and "quality" in qs.tree.cget("displaycolumns")
    assert qs.tree.set("t1", "title") == "Rolling in the Deep"
    assert qs.panel.winfo_width() <= theme.px(960) + 2


def test_a_newer_search_replaces_one_on_its_way(root, tk_thread_rule):
    calls = []
    qs, db = panel(root, calls)
    gate = threading.Event()
    search = db.search_songs
    db.search_songs = lambda query, limit=200: (gate.wait(5), search(query, limit))[1]
    qs.open("rolling")
    qs.flush()
    qs.query.set("rumour")
    qs.flush()
    gate.set()
    assert qs.wait()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and qs.results.query != "rumour":
        qs._poll()
    assert qs.results.query == "rumour" and [h.track.title for h in qs.results.songs.rows] == ["Rumour Has It"]


def test_track_list_offers_the_whole_library_when_nothing_matches(root):
    asked = []
    view = PlaylistView(root, on_play=lambda i: None, on_search_library=asked.append)
    view.set_tracks([Track(Path("C:/M/A/01.flac"), "Accordion", artist="Madvillain")])
    view.query.set("rumour")
    view._render()
    assert view.tree.get_children() == ("none", "library")
    shown = view.tree.set("library", "title")              # (in a narrow column: ending with "…")
    assert shown == SEARCH_LIBRARY or (shown.endswith("…") and SEARCH_LIBRARY.startswith(shown[:-1]))
    view.tree.selection_set("library")
    assert view.activate() and asked == ["rumour"]
    view._play_first_result()                            # Enter in the box with no matches: the same
    assert asked == ["rumour", "rumour"]


# -- in the player --------------------------------------------------------------------------------------
from test_light_mini import _pump, make_window  # noqa: E402,F401  (make_window is a fixture)


def test_ctrl_k_opens_the_panel_and_enter_plays_the_song_in_its_album(make_window):
    from ironbark import keys

    window, root, player, errors = make_window()
    library_index.shared(window._library_roots()).refresh()
    assert window.keys.run(window.playlist.tree, "k", keys.CONTROL)
    _pump(root, 0.2)
    quick = window._quick
    assert quick is not None and quick.is_open and str(root.tk.call("focus")) == str(quick.search.entry)
    quick.query.set("song")
    assert quick.wait()
    assert [hit.track.title for hit in quick.results.songs.rows] == ["Song"]
    player.calls.clear()
    quick._act("play", first=True)
    _pump(root, 0.3)
    assert not quick.is_open and window.current == 0 and "play" in player.calls
    window.playlist.query.set("zzz")                   # nothing in the tab: the whole library, with the words
    window.playlist._render()
    window.playlist.tree.selection_set("library")
    assert window.playlist.activate() and quick.is_open and quick.query.get() == "zzz"
    quick.close()
    assert not errors

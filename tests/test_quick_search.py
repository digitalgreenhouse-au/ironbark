"""Searching the whole library (quick_search.py): a real library database searched, the track list's
"Search the whole library" row, Ctrl+K (the Library tab's search box). Favourites (favourites.py) and
the database's song lookups for the Library's views."""
import gc
import os
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import audiofile, library_index, settings, theme
from ironbark.favourites import Favourites
from ironbark.library import Album, Track
from ironbark.playlist_view import SEARCH_LIBRARY, PlaylistView
from ironbark.quick_search import Catalogue, count_text, search_library, song_values
from ironbark.tagio.flac import FLACFile


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


@pytest.mark.skipif(os.name != "nt", reason="Windows' file names ignore case")
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


# -- the track list's whole-library row -------------------------------------------------------------
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


def test_ctrl_k_is_the_library_tabs_search_box_and_the_rows_link_turns_whole_library_on(make_window):
    from ironbark import keys
    from ironbark.ui import Tab

    window, root, player, errors = make_window()
    window.tabs = [Tab("library", ""), *window.tabs]
    window.current = 1
    window._refresh_tabs()
    _pump(root, 0.2)
    window.playlist.set_whole_library(False)
    window.playlist.query.set("zzz")                   # nothing in the tab: the whole-library row, with the words
    window.playlist._render()
    window.playlist.tree.selection_set("library")
    assert window.playlist.activate()
    assert window.current == 1 and window.playlist.scope_var.get() is True and window.playlist.query.get() == "zzz"
    window.playlist.set_whole_library(False)
    assert window.keys.run(window.playlist.tree, "k", keys.CONTROL)   # Ctrl+K: the Library tab's box, emptied
    _pump(root, 0.2)
    assert window.current == 0 and window.tabs[0].main and window.playlist.query.get() == ""
    assert window.playlist.scope_var.get() is True and str(root.tk.call("focus")) == str(window.playlist.entry)
    assert not errors

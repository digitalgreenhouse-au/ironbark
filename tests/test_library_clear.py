"""Settings → Library → Clear the library…: start again with an EMPTY Library — every folder taken
out, not even the system's Music folder in their place, and everything stored about the music
forgotten. The music files stay; plays and favourites come back with the music when it's added back."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from test_library_remove_folder import _flac, _titles, _wait
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)


def _library_with_two_folders(window, root, tmp_path):
    first, other = tmp_path / "Music", tmp_path / "More"
    a = _flac(first / "First" / "01 First.flac", TITLE="First Song", ARTIST="Someone", ALBUM="First")
    b = _flac(other / "Other" / "01 Other.flac", TITLE="Other Song", ARTIST="Someone", ALBUM="Other")
    window._set_library_folders([first, other])
    window._index().refresh()
    main = window._main_tab(make=True)
    window.select_tab(main)
    tab = window.tabs[main]
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and "Other Song" in _titles(tab.tracks))
    return tab, (first, other), (a, b)


def test_clear_the_library_empties_it_and_the_library_tab_follows(make_window, tmp_path):
    from ironbark import library_index
    from ironbark.popups import Prompt
    from ironbark.ui import library_folders

    window, root, _player, errors = make_window()
    tab, folders, files = _library_with_two_folders(window, root, tmp_path)
    window._open_settings()
    _pump(root, 0.4)
    assert window.library_clear_button.enabled and window.library_rebuild_button.enabled

    root.after(100, lambda: Prompt.stack[-1]._chosen(1))       # asked first: Keep it — nothing happens
    window.library_clear_button.command()
    _pump(root, 0.3)
    assert library_folders(window.prefs) == list(folders) and not window.prefs["library_empty"]

    root.after(100, lambda: Prompt.stack[-1]._chosen(0))       # Clear
    window.library_clear_button.command()
    assert _wait(root, lambda: "Cleared the library" in window._notice, 20), window._notice
    # No folders at all — the system's Music folder doesn't stand in — and nothing stored.
    assert window.prefs["library_folders"] == [] and window.prefs["library_folder"] == ""
    assert window.prefs["library_empty"] is True and library_folders(window.prefs) == []
    index = library_index.shared([])
    assert index.roots == [] and index.db.roots() == []
    assert index.db.counts()["tracks"] == 0 and index.db.counts()["folders"] == 0
    # The Library tab follows: empty, and its head says how to fill it.
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and len(tab.tracks) == 0), _titles(tab.tracks)
    # Settings: the rows say it's empty, Clear and Rebuild are greyed, Add folder… is there.
    texts = [str(w.cget("text")) for w in window.library_rows.winfo_children()]
    assert texts == ["No folders: the Library is empty"]
    assert not window.library_clear_button.enabled and not window.library_rebuild_button.enabled
    window.settings.close()
    _pump(root, 0.4)
    assert window._head_full[1] == "Empty: add a folder in Settings → Library"
    assert all(f.exists() for f in files) and not errors         # (the music files are untouched)


def test_an_emptied_library_stays_empty_and_a_folder_added_fills_it_with_its_plays_and_favourites(
        make_window, tmp_path, monkeypatch):
    from ironbark import audiofile, library_clear, library_index, settings
    from ironbark.ui import library_folders

    window, root, _player, errors = make_window()
    tab, (first, _other), (song, _b) = _library_with_two_folders(window, root, tmp_path)
    db = window._index().db
    key = audiofile.key(song)
    db.set_favourite(key)
    db.record_play(key, 0.1, 0.1)
    monkeypatch.setattr(library_clear, "ask", lambda parent: True)
    library_clear.clear(window)
    assert _wait(root, lambda: "Cleared the library" in window._notice, 20), window._notice
    assert db.favourites() == [key] and db.play_counts([key])[key] == 1   # kept, for when it's back
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and len(tab.tracks) == 0), _titles(tab.tracks)

    # The next start: still empty (the settings say so), not the Music folder.
    settings.save(window.prefs)
    assert library_folders(settings.load()) == []
    # An older settings file (no library_empty) with no folders still means the Music folder.
    assert library_folders({"library_folders": [], "library_folder": ""}) != []

    # Clear again: nothing to clear — it says so, and asks nothing.
    monkeypatch.setattr(library_clear, "ask", lambda parent: (_ for _ in ()).throw(AssertionError("asked")))
    library_clear.clear(window)
    assert "empty already" in window._notice

    # A folder added back: the Library holds it again, with the song's plays and favourite.
    window._set_library_folders([first])
    assert window.prefs["library_empty"] is False
    library_index.shared([first]).refresh()
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and "First Song" in _titles(tab.tracks)), \
        _titles(tab.tracks)
    assert "Other Song" not in _titles(tab.tracks)               # (its folder wasn't added back)
    assert window._head_full[1] != "Empty: add a folder in Settings → Library"
    assert db.favourites() == [key] and db.play_counts([key])[key] == 1 and not errors


def test_the_library_views_say_how_to_fill_an_empty_library(make_window, tmp_path, monkeypatch):
    from ironbark import library_clear, library_view

    window, root, _player, errors = make_window()
    _library_with_two_folders(window, root, tmp_path)
    window.open_library()
    _pump(root, 0.5)
    monkeypatch.setattr(library_clear, "ask", lambda parent: True)
    library_clear.clear(window)
    assert _wait(root, lambda: "Cleared the library" in window._notice, 20), window._notice
    window.open_library()
    view = window._library
    assert _wait(root, lambda: view.albums == [] and str(view.status.cget("text")) == library_view.NO_FOLDERS_TEXT), \
        str(view.status.cget("text"))
    assert view.roots == [] and view.root_folder is None and view._place() == ""
    view.rebuild()                                               # (nothing to rebuild: asks nothing)
    assert not errors


def test_a_library_whose_folders_are_all_on_the_network_opens_and_says_why(make_window):
    """Every library folder on the network and Local network access off: none may be read. The
    Library used to fail to open (its views took the first folder, and there was none); it opens,
    empty, and says which folders aren't read and why."""
    share = r"\\192.0.2.10\tunes"
    window, root, _player, errors = make_window(library_folders=[share], library_folder=share, local_network=False)
    assert window._library_roots() == []
    window.open_library()
    view = window._library
    assert _wait(root, lambda: str(view.status.cget("text")).startswith("Not read: “\\\\192.0.2.10\\tunes")), \
        str(view.status.cget("text"))
    assert "Local network access is off" in str(view.status.cget("text"))
    window._open_settings()
    _pump(root, 0.4)
    assert not window.library_rebuild_button.enabled               # (nothing it may read)
    assert window.library_clear_button.enabled                     # (but there's a folder listed to clear)
    assert not errors


def test_a_look_asked_for_before_the_library_was_cleared_does_nothing(tmp_path):
    """A look for the old folders, waiting its turn while the library is cleared, must not put them
    back once it runs."""
    from ironbark import library_db
    from ironbark.indexer import Indexer

    music = tmp_path / "Music"
    _flac(music / "Album" / "01 Song.flac", TITLE="Song", ARTIST="Someone", ALBUM="Album")
    db = library_db.LibraryDB.open(tmp_path / "library.db")
    indexer = Indexer(db)
    indexer.check([music])
    assert [Path(r) for r in db.roots()] == [music]
    found = {}
    indexer._lock.acquire()                                      # (a look under way)
    try:
        waiting = threading.Thread(target=lambda: found.update(diff=indexer.check([music])))
        waiting.start()                                          # asked for now: it waits its turn
        time.sleep(0.3)
        cleared = threading.Thread(target=indexer.empty)
        cleared.start()
        time.sleep(0.3)
    finally:
        indexer._lock.release()
    waiting.join(10)
    cleared.join(10)
    assert db.roots() == [] and db.counts()["tracks"] == 0       # whichever ran first, the library ends empty
    if found["diff"].cancelled:                                  # (the clear went first: the old look did nothing)
        assert not found["diff"].added
    db.close()


def test_settings_library_has_rebuild_and_clear_side_by_side_in_a_narrow_window(make_window):
    window, root, _player, errors = make_window(geometry="300x640+120+120")
    window._open_settings()
    _pump(root, 0.5)
    rebuild, clear = window.library_rebuild_button, window.library_clear_button
    assert rebuild.text_value == "Rebuild the library…" and clear.text_value == "Clear the library…"
    assert rebuild.winfo_parent() == clear.winfo_parent()                     # one row…
    assert clear.winfo_x() > rebuild.winfo_x() + rebuild.winfo_width() - 1   # …side by side
    row = rebuild.nametowidget(rebuild.winfo_parent())
    assert clear.winfo_x() + clear.winfo_width() <= row.winfo_width()        # inside the window
    assert not errors


def test_clearing_doesnt_touch_the_folders_on_disk(make_window, tmp_path, monkeypatch):
    from ironbark import library_clear

    window, root, _player, errors = make_window()
    _tab, folders, files = _library_with_two_folders(window, root, tmp_path)
    before = sorted(p for folder in folders for p in Path(folder).rglob("*"))
    monkeypatch.setattr(library_clear, "ask", lambda parent: True)
    library_clear.clear(window)
    assert _wait(root, lambda: "Cleared the library" in window._notice, 20), window._notice
    assert sorted(p for folder in folders for p in Path(folder).rglob("*")) == before and not errors

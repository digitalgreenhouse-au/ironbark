"""The Library window's song views (Songs, Most played, Recently played, Favourites), the album tag in
the Album column, text cut with "…", the narrow columns keeping their room, type-to-jump, "Open all
N albums in a tab" asking first when it's big, and "Show in Library". Tk tests, skipped without a
display."""
import time

from ironbark import library_index, settings, theme
from ironbark.library_window import LibraryWindow, added_text

from test_library_window import column, flac, music, root, wait_for_index  # noqa: F401 — shared fixtures

def wait_for_songs(window, root):
    """Until the song view's songs (and the rows on show) have come from the query thread."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        root.update()
        source = window.list.source
        shown = [source.row(i) for i in range(min(len(source), 50))]
        if (window._songs_asked is None and window._songs_have == window._wanted()
                and not any("loading" in row[2] for row in shown)):
            return
        time.sleep(0.02)
    raise AssertionError("the songs never came")


def song_window(root, music, calls, favourites=None):
    from ironbark.quick_search import Actions

    actions = Actions(play_song=lambda t, a: calls.append(("play", t.title, a.name)),
                      queue_tracks=lambda ts, front: calls.append(("queue", [t.title for t in ts], front)),
                      favourites=favourites)
    w = LibraryWindow(root, music, None, on_choose=lambda p: None, on_root_changed=lambda p: None,
                      on_load_all=lambda r, q: None, actions=actions)
    wait_for_index(w, root)
    return w


def test_songs_view_lists_sorts_and_searches_every_song(root, music, monkeypatch, tk_thread_rule):
    monkeypatch.setattr(LibraryWindow, "_sorts", {})
    calls = []
    w = song_window(root, music, calls)
    w.set_view("songs")
    wait_for_songs(w, root)
    assert column(w, "title") == ["A", "B", "Overture", "Rolling"] and w.tree.heading("title", "text") == "Title ▲"
    assert tuple(w.tree.cget("displaycolumns")) == ("title", "artist", "album", "year", "quality", "time")
    assert "4 songs in the library" in w.status.cget("text")
    w.sort_by("artist")
    wait_for_songs(w, root)
    assert column(w, "artist") == ["Adele", "Alpha", "Beta", "The Who"]
    w.search_var.set("overture")
    w._flush_search()
    wait_for_songs(w, root)
    assert column(w, "title") == ["Overture"] and w.status.cget("text") == "1 song"
    w.list.select(0)
    assert w.queue_selected(front=True)
    assert calls == [("queue", ["Overture"], True)]
    assert w._choose_selected()                          # Enter: plays it in its album (and the window closes)
    assert calls[-1] == ("play", "Overture", "Tommy") and not w.win.winfo_exists()


def test_favourites_most_and_recently_played(root, music, monkeypatch, tk_thread_rule):
    from ironbark.favourites import Favourites

    monkeypatch.setattr(LibraryWindow, "_sorts", {})
    favourites = Favourites(lambda: library_index.shared(music).db)
    favourites.load().join(5)
    favourites.take_loaded()
    calls = []
    w = song_window(root, music, calls, favourites)
    db = library_index.shared(music).db
    hits = {hit.track.title: hit for hit in db.songs(db.song_ids())}
    w.set_view("favourites")
    wait_for_songs(w, root)
    assert column(w, "title") == [] and "No favourites yet" in w.status.cget("text")
    favourites.set([hits["Overture"].track], True)
    w.favourites_changed()
    wait_for_songs(w, root)
    assert column(w, "title") == ["♥ Overture"] and w.status.cget("text") == "1 favourite"
    db.record_play(hits["A"].track.path, 200, 200, when=1000.0)
    db.record_play(hits["B"].track.path, 200, 200, when=2000.0)
    db.record_play(hits["A"].track.path, 200, 200, when=3000.0)
    w.set_view("most_played")
    wait_for_songs(w, root)
    assert column(w, "title") == ["A", "B"] and column(w, "plays") == ["2", "1"]
    w.set_view("recently_played")
    wait_for_songs(w, root)
    assert column(w, "title") == ["A", "B"] and w.tree.heading("added", "text") == "Played ▼"
    assert column(w, "added")[0] == added_text(3000.0)
    assert favourites.flush()
    db.release()
    w.close()


def test_album_column_shows_the_tag_and_long_names_end_with_an_ellipsis(root, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    monkeypatch.setattr(LibraryWindow, "_sorts", {})
    music = tmp_path / "Music"
    flac(music / "Madvillain-Madvillainy (2004) FLAC" / "01.flac", title="Accordion", artist="Madvillain",
         album="Madvillainy")
    flac(music / "Untagged folder" / "01.flac", title="Song")
    long = "An album name far too long to fit in the column of a narrow Library window at all"
    flac(music / "Long" / "01.flac", title="Song", artist="Band", album=long)
    w = LibraryWindow(root, music, None, lambda p: None, lambda p: None, lambda r, q: None)
    wait_for_index(w, root)
    assert column(w, "album")[1:] == ["Madvillainy", "Untagged folder"]   # the tag; the folder when untagged
    w.win.geometry(f"{theme.px(420)}x{theme.px(500)}")
    root.update()
    root.update()
    shown = w.tree.set(w.tree.get_children()[0], "album")
    assert shown.endswith("…") and long.startswith(shown[:-1].rstrip())
    # The narrow columns keep their room however narrow the window: nothing runs past its right edge.
    displayed = [str(c) for c in w.tree.cget("displaycolumns")]
    widths = sum(int(w.tree.column(c, "width")) for c in displayed)
    assert widths <= w.tree.winfo_width() and int(w.tree.column("added", "width")) == theme.px(96)
    w.close()


def test_type_to_jump_and_open_all_asks_first_when_big(root, music, monkeypatch):
    from ironbark import library_window, popups

    monkeypatch.setattr(LibraryWindow, "_sorts", {})
    loaded = []
    w = LibraryWindow(root, music, None, lambda p: None, lambda p: None, lambda r, q: loaded.append(q))
    wait_for_index(w, root)
    assert w._jump("to") == 2 and w._jump("h") == 1 and w._jump("zz") is None
    w.set_view("artists")
    assert w._items[w._jump("who")] == ("group", "The Who")   # "The" left off, as names sort
    w.set_view("albums")
    assert w.load_all_button.cget("text") == "Open all 3 albums in a tab"
    asked = []
    monkeypatch.setattr(library_window, "LOAD_ALL_WARN", 2)
    monkeypatch.setattr(popups, "confirm", lambda *args, **kwargs: asked.append(args[1]) or False)
    w._load_all()
    assert asked == ["Open 4 songs in one tab?"] and loaded == [] and w.win.winfo_exists()
    monkeypatch.setattr(popups, "confirm", lambda *args, **kwargs: True)
    w._load_all()
    assert loaded == [""] and not w.win.winfo_exists()


def test_show_in_library_selects_the_album(root, music, monkeypatch):
    monkeypatch.setattr(LibraryWindow, "_sorts", {})
    w = LibraryWindow(root, music, None, lambda p: None, lambda p: None, lambda r, q: None)
    wait_for_index(w, root)
    w.set_view("songs")
    w.search_var.set("zzz")
    w.show(album=music / "Tommy")
    assert w.view == "albums" and w.search_var.get() == "" and w._selected_album().path == music / "Tommy"
    w.show(search="adele")
    assert w.view == "albums" and column(w, "album") == ["21"]
    w.close()


def test_a_song_played_under_two_spellings_of_its_path_is_listed_once(root, music, monkeypatch, tk_thread_rule):
    """Plays are kept by the path played from — a playlist may write "C:\…" as "c:\…". The song is
    listed once (two rows with one key used to stop the list), its plays added up."""
    import os

    monkeypatch.setattr(LibraryWindow, "_sorts", {})
    errors = []
    root.report_callback_exception = lambda *info: errors.append(info)
    w = song_window(root, music, [])
    db = library_index.shared(music).db
    hits = {hit.track.title: hit for hit in db.songs(db.song_ids())}
    key = os.fspath(hits["A"].track.path)
    db.record_play(key, 200, 200, when=1000.0)
    db.record_play(key[0].swapcase() + key[1:], 200, 200, when=3000.0)
    db.record_play(hits["B"].track.path, 200, 200, when=2000.0)
    w.set_view("most_played")
    wait_for_songs(w, root)
    assert column(w, "title") == ["A", "B"] and column(w, "plays") == ["2", "1"]
    w.set_view("recently_played")
    wait_for_songs(w, root)
    assert column(w, "title") == ["A", "B"] and column(w, "added")[0] == added_text(3000.0)
    assert errors == []
    db.release()
    w.close()


def test_a_long_song_list_is_sorted_off_tk_and_its_rows_made_as_they_show(root, music, monkeypatch, tk_thread_rule):
    """Needs a look can list tens of thousands of songs (clipping is common in loud masters), and it's
    read again as the check goes on: the query thread sorts it and folds each song's search text, and
    Tk's thread makes only the rows on show."""
    import threading

    from ironbark import library_check, library_window
    from ironbark.library_db import SongHit

    monkeypatch.setattr(LibraryWindow, "_sorts", {})
    w = LibraryWindow(root, music, None, lambda p: None, lambda p: None, lambda r, q: None)
    wait_for_index(w, root)
    db = library_index.shared(music).db
    real = db.songs(db.song_ids())
    many = [(SongHit(n + 1, real[n % 4].track, real[n % 4].album),
             library_check.Look(("clipping",), f"Clipped when mastered ({n:,} runs stuck at full scale)", 5, 0.0))
            for n in range(20_000)]
    monkeypatch.setattr(library_check, "needs_look", lambda db: list(many))
    sorted_on = []
    sort_key = library_window._song_sort_key
    monkeypatch.setattr(library_window, "_song_sort_key",
                        lambda column: sorted_on.append(threading.current_thread().name) or sort_key(column))
    made = []
    w._song_row = lambda hit, extra: made.append(hit.id) or LibraryWindow._song_row(w, hit, extra)
    w.set_view("needs_look")
    wait_for_songs(w, root)
    assert len(w.list.source) == 20_000 and sorted_on and set(sorted_on) == {"library-query"}
    assert len(made) <= 3 * w.list.page() + 50                # (wait_for_songs itself reads 50)
    assert w.status.cget("text").startswith("20,000 songs need a look: 20,000 clipped")
    w.sort_by("artist")
    wait_for_songs(w, root)
    assert set(sorted_on) == {"library-query"} and column(w, "artist")[:2] == ["Adele", "Adele"]
    w.search_var.set("overture")                               # (picked out with the texts folded off Tk)
    w._flush_search()
    assert len(w.list.source) == 5_000 and set(column(w, "title")) == {"Overture"}
    assert w.status.cget("text").startswith("5,000 of 20,000 songs: 20,000 clipped")
    db.release()
    w.close()


def test_a_page_the_songs_list_has_moved_away_from_is_not_read():
    """A scrollbar dragged through a big library asks for every page it passes; the ones it has
    left behind by their turn aren't read (they'd hold up the one it stopped at), and are asked for
    again if they're on show after all."""
    from types import SimpleNamespace

    from ironbark.library_window import PAGE, SongPages, _read_page
    from ironbark.indexer import Answer

    pages = SongPages(list(range(100_000)), lambda hit, extra: ("k", (), ()), lambda page, ids: None)
    db = SimpleNamespace(songs=lambda ids: [f"read {len(ids)}"])
    pages.row(50_000)                                          # (the list is drawn there now)
    assert _read_page(db, pages, 0, list(range(PAGE))) is None
    assert _read_page(db, pages, 50_000 // PAGE, list(range(PAGE))) == ["read 200"]
    pages.asked.add(0)
    window = SimpleNamespace(_pages=pages, _page_serial=3, view="songs")
    assert LibraryWindow._take_page(window, Answer("page:3:0", 9, None)) is True   # (drawn again: asked again)
    assert 0 not in pages.asked and 0 not in pages.pages

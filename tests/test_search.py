import tkinter as tk
from pathlib import Path

import pytest

from ironbark.library import Track
from ironbark.playlist_view import PlaylistView, _fold, matches


def track(folder, title, artist="", album="", n=1):
    return Track(Path(folder) / f"{n:02d} {title}.flac", title, artist=artist, album=album, track_no=n)


def test_fold_ignores_case_and_accents():
    assert _fold("Lïve – SECRET") == "live – secret"


def test_every_word_must_match_somewhere():
    t = track("A", "Accordion", artist="Madvillain", album="Madvillainy")
    assert matches(t, ["accord"])
    assert matches(t, ["madvillain", "accordion"])  # artist + title
    assert not matches(t, ["accordion", "adele"])


@pytest.fixture
def view():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    yield PlaylistView(root, on_play=lambda i: None)
    root.destroy()


def test_search_filters_rows_but_keeps_real_track_numbers(view):
    tracks = [track("A", "Accordion", "Madvillain", "Madvillainy", 1), track("A", "Curls", "Madvillain", "Madvillainy", 2),
              track("B", "Rolling in the Deep", "Adele", "21", 1)]
    view.set_tracks(tracks)
    view.query.set("adele")
    view._render()
    rows = view.tree.get_children()
    assert [r for r in rows if r.isdigit()] == ["2"]   # the index into the full list, so it plays in place
    assert view.tree.set("2", "title") == "Rolling in the Deep  ·  Adele"


def test_no_matches_says_so(view):
    view.set_tracks([track("A", "Accordion")])
    view.query.set("zzz")
    view._render()
    assert view.tree.get_children() == ("none",)

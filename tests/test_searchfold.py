"""searchfold.py: one folding for every search (case, accents and the letters NFKD leaves alone),
search words made safe for SQLite's full-text index, and the wider in-tab filter."""
import sqlite3
from pathlib import Path

import pytest

from ironbark import browse, library_db, playlist_view, searchfold
from ironbark.audiofile import CuePath, CueRange
from ironbark.library import Album, Track


@pytest.mark.parametrize("text, folded", [
    ("Łódź", "lodz"), ("Ørsted", "orsted"), ("Guðmundsdóttir", "gudmundsdottir"), ("Straße", "strasse"),
    ("STRAẞE", "strasse"), ("Æon", "aeon"), ("Œuvre", "oeuvre"), ("Þór", "thor"), ("Đorđe", "dorde"),
    ("Iıİ", "iii"), ("Ágætis byrjun", "agaetis byrjun"), ("Sigur Rós", "sigur ros"), ("Motörhead", "motorhead"),
    ("Dvořák", "dvorak"), ("ｆｕｌｌ ＷＩＤＴＨ １２", "full width 12"), ("ﬁne", "fine"), ("Ⅳ", "iv"),
    ("Lïve – SECRET", "live – secret"), ("e\u0301te\u0301", "ete"), ("Ǿ", "o"), ("AC/DC", "ac/dc"), ("", ""),
])
def test_fold_table(text, folded):
    assert searchfold.fold(text) == folded


def test_fold_of_nothing_is_empty():
    assert searchfold.fold(None) == ""


def test_words_split_at_spaces_and_tokens_split_like_the_index():
    assert searchfold.words("  AC/DC   Live ") == ["ac/dc", "live"]
    assert searchfold.tokens("AC/DC") == ["ac", "dc"]
    assert searchfold.tokens("Guns N' Roses") == ["guns", "n", "roses"]
    assert searchfold.tokens("snake_case — 1997!") == ["snake", "case", "1997"]
    assert searchfold.tokens("Łódź") == ["lodz"]


@pytest.mark.parametrize("query, match", [
    ("AC/DC", '"ac"* AND "dc"*'),
    ("Guns N' Roses", '"guns"* AND "n"* AND "roses"*'),
    ("NEAR(love night)", '"near"* AND "love"* AND "night"*'),
    ("love OR night", '"love"* AND "or"* AND "night"*'),
    ("-live", '"live"*'),
    ('"quoted', '"quoted"*'),
    ("title:love", '"title"* AND "love"*'),
    ("artist : x^y", '"artist"* AND "x"* AND "y"*'),
    ("beet 9", '"beet"* AND "9"*'),
    ("Łódź", '"lodz"*'),
    ("", None), ("  ", None), ('-- " : ^ *', None),
])
def test_fts_match_takes_everything_typed_as_plain_words(query, match):
    assert searchfold.fts_match(query) == match


@pytest.mark.skipif(bool(library_db.fts_problem()), reason="this SQLite has no FTS5")
@pytest.mark.parametrize("query", ["AC/DC", "Guns N' Roses", "NEAR(a b)", "NEAR", "-", "- live", '"', '"love',
                                   "title:", "title:love", "love AND", "OR", "NOT love", "*", "^love", "{title}: x",
                                   "love)", "(love", "col:umn:", "a+b", "\x00x"])
def test_anything_typed_is_safe_to_hand_the_index(query):
    con = sqlite3.connect(":memory:")
    con.execute(library_db._FTS_TABLE)
    con.execute("INSERT INTO song_fts(rowid, title, artist) VALUES (1, 'love song', 'ac dc')")
    match = searchfold.fts_match(query)
    if match is not None:
        con.execute("SELECT rowid FROM song_fts WHERE song_fts MATCH ?", (match,)).fetchall()   # no syntax error
    con.close()


def test_the_index_finds_what_the_words_promise():
    con = sqlite3.connect(":memory:")
    con.execute(library_db._FTS_TABLE)
    rows = [(1, "highway to hell", "ac/dc"), (2, "night prowler", "ac dc"), (3, "lodz", "x"), (4, "near you", "y")]
    con.executemany("INSERT INTO song_fts(rowid, title, artist) VALUES (?, ?, ?)", rows)

    def found(query):
        return sorted(r[0] for r in con.execute("SELECT rowid FROM song_fts WHERE song_fts MATCH ?",
                                                (searchfold.fts_match(query),)))

    assert found("AC/DC") == [1, 2]
    assert found("Łódź") == [3]
    assert found("NEAR") == [4]
    assert found("high hell") == [1]
    assert found("ight") == []      # word starts only: the database's slower search finds the middles
    con.close()


def song(**tags) -> Track:
    tags.setdefault("title", "Svefn-g-englar")
    return Track(path=Path("C:/m/Sigur Rós/Ágætis byrjun/04 Svefn.flac"), **tags)


def test_haystack_has_every_field_the_filter_looks_through():
    t = song(artist="Sigur Rós", album="Ágætis byrjun", album_artist="Sigur Rós Band", composer="Jón Þór Birgisson",
             genre="Post-rock", date="1999-06-12")
    hay = searchfold.haystack(t)
    for word in ("svefn", "sigur", "agaetis", "band", "thor", "post-rock", "1999", "04"):
        assert word in hay
    assert "06-12" not in hay   # the year, not the whole date
    cue = Track(path=CuePath("C:/m/A/Disc.flac", cue=CueRange("C:/m/A/Disc.cue", 3, 0, None)), title="Three")
    assert "disc" in searchfold.haystack(cue)   # a CUE track: its whole file's name


@pytest.mark.parametrize("query", ["agaetis", "birgisson", "post-rock", "1999", "band", "sigur svefn"])
def test_the_in_tab_filter_looks_at_the_new_fields(query):
    t = song(artist="Sigur Rós", album="Ágætis byrjun", album_artist="Sigur Rós Band", composer="Jón Þór Birgisson",
             genre="Post-rock", date="1999")
    assert playlist_view.matches(t, searchfold.words(query))


def test_the_in_tab_filter_still_finds_the_middle_of_a_word():
    assert playlist_view.matches(song(title="Night"), ["ight"])
    assert not playlist_view.matches(song(title="Night"), ["ight", "zzz"])


def test_old_names_stay_for_now():
    assert playlist_view._fold is searchfold.fold
    assert playlist_view._fold("Łódź") == "lodz"


def test_library_search_folds_the_extra_letters_by_default():
    album = Album(path=Path("C:/m/Sigur Rós/Ágætis byrjun"), name="Ágætis byrjun", track_count=10, artist="Sigur Rós",
                  year=1999, genres=("Post-rock",), composers=("Jón Þór Birgisson",), title="Ágætis byrjun")
    for query in ("agaetis", "sigur ros", "1999", "thor", "post"):
        assert browse.matches(album, searchfold.words(query))
    assert not browse.matches(album, searchfold.words("svefn"))

"""Performance of the library database on a synthetic 50,000-song library (marked `perf`: runs only
with --perf, e.g. `uv run pytest -q -p no:cacheprovider --perf -m perf -s`). Checks the targets set
for big libraries: the database opens at once, the whole album list comes back within 150 ms (the
Library window's first rows), and a search answers each keystroke within 100 ms (95% of them).
Run before each release, and on the packaged build too. Timings are printed with -s."""
from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest
import synthlib

from ironbark import browse
from ironbark.library_db import LibraryDB

pytestmark = pytest.mark.perf

SIZE = 50_000
ROOT = Path("C:/Music")
# What someone types, a keystroke at a time, and a few whole searches.
KEYSTROKES = ["lo", "lov", "love", "ni", "nig", "nigh", "night", "bj", "bjo", "bjork", "gu", "gud", "gudm", "19",
              "199", "1997", "ro", "roc", "rock", "th", "the", "be", "bee", "beet", "ba", "bac", "bach", "lo", "lod",
              "lodz", "love ni", "love night", "sigur ros", "ac/dc", "guns n roses", "ight", "zzzz", "qx",
              "greatest hits", "live", "dvorak", "faure", "orsted", "strasse", "aeon", "oeuvre"]


def timed(fn, *args, repeat: int = 1):
    best, value = None, None
    for _ in range(repeat):
        started = time.perf_counter()
        value = fn(*args)
        took = time.perf_counter() - started
        best = took if best is None else min(best, took)
    return best, value


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    folder = tmp_path_factory.mktemp("perf")
    files = synthlib.make_library(SIZE, ROOT, seed=50)
    started = time.perf_counter()
    with LibraryDB.open(folder / "library.db", [ROOT]) as db:
        synthlib.load(db, ROOT, files)
        print(f"\nfirst build of {SIZE:,} songs ({db.counts()['albums']:,} albums) stored in "
              f"{time.perf_counter() - started:.1f} s")
    return folder / "library.db", files


def test_opening_is_quick(big):
    path, _files = big
    took, db = timed(LibraryDB.open, path)
    try:
        print(f"open {took * 1000:.1f} ms")
        assert took < 0.1
    finally:
        db.close()


def test_the_whole_album_list_within_150_ms(big):
    path, files = big
    with LibraryDB.open(path) as db:
        db.albums()   # (the first read warms SQLite's page cache, as the Library's start-up read does)
        took, albums = timed(db.albums, repeat=3)
        print(f"albums() {len(albums):,} albums in {took * 1000:.1f} ms")
        assert took < 0.150
        assert len(albums) == len(browse.albums_from(synthlib.tracks_of(files), None, ROOT))


def test_search_answers_each_keystroke_within_100_ms(big):
    path, _files = big
    with LibraryDB.open(path) as db:
        if not db.fts:
            pytest.skip("this SQLite has no FTS5")
        for query in KEYSTROKES:   # warm
            db.search_songs(query)
        times = []
        for query in KEYSTROKES:
            took, hits = timed(db.search_songs, query, repeat=2)
            times.append(took)
            print(f"{query!r:16} {took * 1000:6.1f} ms  {hits.total:>6,}{'+' if hits.capped else ' '} songs")
        p95 = statistics.quantiles(times, n=20)[-1]
        print(f"search p95 {p95 * 1000:.1f} ms, worst {max(times) * 1000:.1f} ms")
        assert p95 < 0.100


def test_one_album_and_new_music_are_quick(big):
    path, files = big
    with LibraryDB.open(path) as db:
        folders = [a.path for a in db.albums()[::200]]
        took = max(timed(db.album_tracks, folder, False, False)[0] for folder in folders)
        print(f"album_tracks (from the database) worst {took * 1000:.1f} ms")
        assert took < 0.020
        took, _new = timed(db.new_albums, 0.0, {}, repeat=3)
        print(f"new_albums {took * 1000:.1f} ms")
        assert took < 0.100


def test_bringing_a_big_json_index_across(big, tmp_path):
    _path, files = big
    json_path = tmp_path / "library_index.json"
    synthlib.index_of(ROOT, files, synthlib.BASE_TIME, json_path).save()
    with LibraryDB.open(tmp_path / "library.db", [ROOT]) as db:
        took, moved = timed(db.import_json_index, json_path)
        print(f"library_index.json with {SIZE:,} songs brought across in {took:.1f} s")
        assert moved and db.counts()["tracks"] == SIZE
        assert took < 60


def test_the_song_views_are_quick(big):
    """The Library's Songs view: every song's id in a column's order (on the query thread, so the
    window never waits for it), then a page of rows as it scrolls; Most played / Favourites: a few
    hundred songs found by their keys."""
    from ironbark import audiofile

    path, _files = big
    with LibraryDB.open(path) as db:
        for order in ("title", "artist", "year", "quality"):
            took, ids = timed(db.song_ids, order, repeat=2)
            print(f"song_ids({order}) {len(ids):,} songs in {took * 1000:.0f} ms")
            assert len(ids) == SIZE and took < 0.6
        page = ids[25_000:25_200]
        took, hits = timed(db.songs, page, repeat=3)
        print(f"a page of {len(hits)} songs {took * 1000:.1f} ms")
        assert len(hits) == 200 and took < 0.030
        keys = [audiofile.key(hit.track.path) for hit in db.songs(ids[::100])]
        took, found = timed(db.songs_by_keys, keys, repeat=2)
        print(f"songs_by_keys {len(keys)} keys {took * 1000:.1f} ms")
        assert len(found) == len(keys) and took < 0.300

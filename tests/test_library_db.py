"""library_db.py: the library database — schema and upgrades, damaged files set aside, tracks and
albums stored exactly (albums equal browse.albums_from), missing files kept with their dates,
new music from SQL (equal to the old JSON index's rules), library_index.json brought across, several
library folders, plays and favourites, song search (ranking, fallbacks, interrupting)."""
from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
from pathlib import Path

import legacy_index
import numpy as np
import pytest
import soundfile as sf
import synthlib
from tagging import write_vorbis

from ironbark import audiofile, browse, library, library_db, library_index, library_sql, searchfold, settings, watcher
from ironbark.audiofile import CuePath, CueRange
from ironbark.library import Track
from ironbark.library_db import FolderResult, LibraryDB

RNG = np.random.default_rng(31)
ROOT = Path("C:/Music") if os.name == "nt" else Path("/music")
_REAL_CACHE_BASE = settings._cache_base   # (tests/conftest.py points it at a temporary folder)
needs_fts = pytest.mark.skipif(bool(library_db.fts_problem()), reason="this SQLite has no FTS5")


@pytest.fixture
def db(tmp_path):
    database = LibraryDB.open(tmp_path / "library.db", [ROOT])
    yield database
    database.close()


def song(folder, n, title, **tags) -> Track:
    tags.setdefault("codec", "FLAC")
    tags.setdefault("sample_rate", 44100)
    tags.setdefault("bits", 16)
    return Track(path=Path(folder, f"{n:02d} {title}.flac"), title=title, track_no=n, **tags)


def result(folder, tracks: list[Track], created: float = 1.0e9, root=ROOT, **extra) -> FolderResult:
    """What reading `folder` would give, for these tracks (one file each)."""
    listing = {os.path.basename(os.fspath(t.path)): (1000 + i, 5_000 + i, created) for i, t in enumerate(tracks)}
    by_name: dict[str, list[Track]] = {}
    for t in tracks:
        by_name.setdefault(os.path.basename(os.fspath(t.path)), []).append(t)
    return library_db.folder_result(root, folder, listing, by_name, **extra)


def stored(db: LibraryDB, sql: str, *args):
    return db._con().execute(sql, args).fetchall()


# -- where it lives ------------------------------------------------------------------------------------
def test_the_database_lives_in_the_local_cache_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "_cache_base", _REAL_CACHE_BASE)
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert settings.cache_folder() == tmp_path / "Local" / "Ironbark"
    assert library_db.db_path() == settings.cache_folder() / "library.db"
    assert settings.cache_folder() != settings.settings_path().parent or os.name != "nt"


def test_the_cache_follows_settings_pointed_elsewhere(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    assert library_db.db_path() == tmp_path / "app" / "library.db"


def test_tests_never_see_the_real_cache_folder():
    assert "pytest" in str(settings.cache_folder()).lower() or "tmp" in str(settings.cache_folder()).lower()


def test_one_library_folder_becomes_the_list(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "settings_path", lambda: path)
    path.write_text(json.dumps({"library_folder": "D:\\Music"}), encoding="utf-8")
    assert settings.load()["library_folders"] == ["D:\\Music"]
    path.write_text(json.dumps({"library_folder": "D:\\Old", "library_folders": ["E:\\A", 3, "", "F:\\B"]}),
                    encoding="utf-8")
    assert settings.load()["library_folders"] == ["E:\\A", "F:\\B"]   # (the list wins; bad entries dropped)
    path.write_text(json.dumps({"library_folders": "E:\\A"}), encoding="utf-8")
    assert settings.load()["library_folders"] == []
    assert settings.DEFAULTS["library_folders"] == []


# -- the file ----------------------------------------------------------------------------------------------
def test_a_new_database_has_every_table_and_uses_wal(db):
    con = db._con()
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("PRAGMA user_version").fetchone()[0] == library_db.SCHEMA_VERSION
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"meta", "roots", "folders", "files", "tracks", "albums", "plays", "favourites"} <= tables
    assert db.fts == (not library_db.fts_problem())
    assert db.roots() == [str(ROOT)]
    assert db.generation == 1 and not db.needs_refold and db.problem == ""


def test_an_upgrade_step_runs_and_keeps_everything(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    with LibraryDB.open(path, [ROOT]) as db:
        db.apply(result(ROOT / "A", [song(ROOT / "A", 1, "One", album="A")]))
        db.record_play("x.flac", 300, 300)
    ran = []
    now = library_db.SCHEMA_VERSION

    def to_next(con):
        ran.append(True)
        con.execute("ALTER TABLE albums ADD COLUMN rating INTEGER")

    monkeypatch.setattr(library_sql, "SCHEMA_VERSION", now + 1)   # (the schema lives in library_sql)
    monkeypatch.setitem(library_db._UPGRADES, now, to_next)
    with LibraryDB.open(path) as db:
        assert ran == [True]
        assert db._con().execute("PRAGMA user_version").fetchone()[0] == now + 1
        assert [a.name for a in db.albums()] == ["A"] and db.play_counts(["x.flac"]) == {"x.flac": 1}
        assert "rating" in {r[1] for r in db._con().execute("PRAGMA table_info(albums)")}
    with LibraryDB.open(path) as db:
        assert ran == [True]   # once only


@pytest.mark.parametrize("version", [99, 0])
def test_a_version_with_no_way_up_rebuilds_the_library_but_keeps_plays(tmp_path, version):
    path = tmp_path / "library.db"
    with LibraryDB.open(path, [ROOT]) as db:
        db.apply(result(ROOT / "A", [song(ROOT / "A", 1, "One")]))
        db.record_play("C:/x.flac#2", 100, 150, when=5.0)
        db.set_favourite("C:/y.flac", when=6.0)
        db._con().execute(f"PRAGMA user_version = {version}")
    with LibraryDB.open(path, [ROOT]) as db:
        assert db.counts()["tracks"] == 0 and db.albums() == []
        assert db.recently_played() == [("C:/x.flac#2", 5.0)] and db.favourites() == ["C:/y.flac"]
        assert db._con().execute("PRAGMA user_version").fetchone()[0] == library_db.SCHEMA_VERSION
        db.apply(result(ROOT / "A", [song(ROOT / "A", 1, "One")]))
        assert [a.name for a in db.albums()] == ["A"]


def test_a_file_that_isnt_a_database_is_set_aside(tmp_path):
    path = tmp_path / "library.db"
    path.write_bytes(b"this is not SQLite" * 300)
    with LibraryDB.open(path, [ROOT]) as db:
        assert "set aside" in db.problem
        db.apply(result(ROOT / "A", [song(ROOT / "A", 1, "One")]))
        assert len(db.albums()) == 1
    aside = list(tmp_path.glob("library.db.damaged-*"))
    assert len(aside) == 1 and aside[0].name == f"library.db.damaged-{time.strftime('%d-%m-%Y')}"
    assert aside[0].read_bytes().startswith(b"this is not SQLite")


def test_recover_starts_again_keeping_folders_plays_and_favourites(db, tmp_path):
    db.apply(result(ROOT / "A", [song(ROOT / "A", 1, "One")]))
    db.record_play("C:/a.flac", 200, 200, when=10.0)
    db.set_favourite("C:/a.flac", when=11.0)
    assert db.quick_check() == "ok"
    aside = db.recover()
    assert aside is not None and aside.exists() and aside.name.startswith("library.db.damaged-")
    assert db.counts()["tracks"] == 0 and db.roots() == [str(ROOT)]
    assert db.play_counts(["C:/a.flac"]) == {"C:/a.flac": 1} and db.is_favourite("C:/a.flac")
    assert "damaged" in db.problem and db.quick_check() == "ok"


def test_a_garbled_page_fails_the_quick_check(tmp_path):
    path = tmp_path / "library.db"
    with LibraryDB.open(path, [ROOT]) as db:
        synthlib.load(db, ROOT, synthlib.make_library(600, ROOT, seed=2))
        db._con().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    data = bytearray(path.read_bytes())
    page = 4096
    for at in range(page * 3, min(len(data), page * 12)):
        data[at] = (data[at] * 7 + 13) % 256
    path.write_bytes(bytes(data))
    with LibraryDB(path) as db:   # (opened without the start-up checks)
        assert db.quick_check() != "ok"


def sqlite_error(code: int, text: str) -> sqlite3.DatabaseError:
    """An error as SQLite's own would carry it (its result code)."""
    error = sqlite3.DatabaseError(text)
    error.sqlite_errorcode = code
    return error


def test_a_busy_database_is_never_set_aside(tmp_path, monkeypatch):
    """Another copy of the player is writing as this one starts: the file is busy, not damaged. It
    stays where it is (with its plays), the error is raised, and the next use simply works."""
    monkeypatch.setattr(library_db, "BUSY_S", 0.2)
    path = tmp_path / "library.db"
    with LibraryDB.open(path, [ROOT]) as db:
        db.record_play("C:/x.flac", 300, 300)
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO favourites(key, added) VALUES ('C:/y.flac', 1)")
    db = LibraryDB.open(path, lazy=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.generation
        assert not list(tmp_path.glob("library.db.damaged-*")) and db.problem == ""
        other.execute("COMMIT")
        assert db.play_counts(["C:/x.flac"]) == {"C:/x.flac": 1} and db.is_favourite("C:/y.flac")
    finally:
        other.close()
        db.close()
    # A file not made yet, busy as it's first opened: the next use makes it properly (not just reconnects).
    fresh = tmp_path / "fresh.db"
    other = sqlite3.connect(fresh, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    other.execute("CREATE TABLE elsewhere(a)")
    db = LibraryDB.open(fresh, lazy=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.generation
        other.execute("COMMIT")
        assert db.generation == 0 and db.roots() == [] and db.problem == ""
    finally:
        other.close()
        db.close()


def test_what_counts_as_damage(tmp_path):
    con = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError) as mistake:
        con.execute("SELECT * FROM nothing_here")
    assert not library_db.is_damage(mistake.value)       # (an ordinary mistake, not a damaged file)
    con.close()
    (tmp_path / "not.db").write_bytes(b"this is not SQLite" * 300)
    con = sqlite3.connect(tmp_path / "not.db")
    with pytest.raises(sqlite3.DatabaseError) as garbage:
        con.execute("SELECT * FROM sqlite_master")
    con.close()
    assert library_db.is_damage(garbage.value)           # (SQLite's own "file is not a database")
    assert library_db.is_damage(sqlite_error(11, "database disk image is malformed"))
    assert library_db.is_damage(sqlite_error(26, "file is not a database"))
    assert library_db.is_damage(sqlite_error(11 | (2 << 8), "corrupt index"))   # (an extended code)
    for code in (5, 6, 10, 13, 14, 8):   # busy, locked, a disk error, full, can't open, read-only
        assert not library_db.is_damage(sqlite_error(code, "not damage"))
    assert not library_db.is_damage(OSError("no")) and not library_db.is_damage(ValueError())


def test_damage_found_while_running_is_put_right_at_the_next_start(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    with LibraryDB.open(path, [ROOT]) as db:
        db.apply(result(ROOT / "A", [song(ROOT / "A", 1, "One")]))
        db.record_play("C:/a.flac", 200, 200, when=10.0)
        assert not db.note_damage(sqlite_error(5, "database is locked"))   # busy: nothing noted
        assert not (tmp_path / "library.db.check").exists()
        assert db.note_damage(sqlite_error(11, "database disk image is malformed"))
    assert (tmp_path / "library.db.check").exists()
    with LibraryDB.open(path) as db:              # SQLite's check says it's sound: kept as it was
        assert db.counts()["tracks"] == 1 and db.problem == ""
    assert not (tmp_path / "library.db.check").exists() and not list(tmp_path.glob("library.db.damaged-*"))
    with LibraryDB.open(path) as db:
        db.note_damage(sqlite_error(11, "database disk image is malformed"))
    monkeypatch.setattr(LibraryDB, "_check_file", lambda self: "row 3 missing from index tracks_file")
    with LibraryDB.open(path) as db:              # really damaged: set aside, plays and folders kept
        assert "set aside" in db.problem and db.counts()["tracks"] == 0
        assert db.roots() == [str(ROOT)] and db.play_counts(["C:/a.flac"]) == {"C:/a.flac": 1}
    aside = [p for p in tmp_path.glob("library.db.damaged-*") if not p.name.endswith(("-wal", "-shm"))]
    assert len(aside) == 1 and not (tmp_path / "library.db.check").exists()


@needs_fts
def test_a_file_this_sqlite_cant_index_is_set_aside_keeping_plays(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    with LibraryDB.open(path, [ROOT]) as db:
        db.apply(result(ROOT / "A", [song(ROOT / "A", 1, "One")]))
        db.record_play("C:/a.flac", 200, 200, when=10.0)
        db.set_favourite("C:/a.flac", when=11.0)
    monkeypatch.setattr(library_db, "fts_problem", lambda: "no FTS5 here")
    with LibraryDB.open(path) as db:
        assert "set aside" in db.problem and not db.fts
        assert db.roots() == [str(ROOT)] and db.recently_played() == [("C:/a.flac", 10.0)]
        assert db.favourites() == ["C:/a.flac"]


def test_library_folders_are_kept_outermost():
    music, classical = ROOT, ROOT / "Classical"
    other = Path("D:/Other") if os.name == "nt" else Path("/other")
    assert library_db.outermost([music, classical, other]) == [music, other]
    assert library_db.outermost([classical, other, music]) == [other, music]   # (the outer keeps its place)
    assert library_db.outermost([music, music, Path(str(music) + "Extra")]) == [music, Path(str(music) + "Extra")]
    if os.name == "nt":
        assert library_db.outermost(["C:\\Music", "c:\\music\\Jazz", "C:\\MUSIC"]) == [Path("C:/Music")]
        assert library_db.outermost(["E:\\", "E:\\Music"]) == [Path("E:/")]
    assert library_db.outermost([]) == []


def test_a_library_folder_inside_another_isnt_kept(db):
    db.set_roots([ROOT, ROOT / "Classical"])
    assert db.roots() == [str(ROOT)]


# -- tracks stored exactly ---------------------------------------------------------------------------------
def test_md5_keeps_all_128_bits(db):
    big = (1 << 128) - 1
    high = 0xF0E1D2C3B4A5968778695A4B3C2D1E0F
    tracks = [song(ROOT / "A", 1, "One", md5=big), song(ROOT / "A", 2, "Two", md5=high), song(ROOT / "A", 3, "Three")]
    db.apply(result(ROOT / "A", tracks))
    back = db.album_tracks(ROOT / "A", check=False)
    assert [t.md5 for t in back] == [big, high, 0]
    assert {r[0] for r in stored(db, "SELECT typeof(md5) FROM tracks")} == {"text", "null"}


def test_every_field_and_cue_paths_come_back_the_same(db):
    image = ROOT / "B" / "Disc.flac"
    cue_tracks = [Track(path=CuePath(image, cue=CueRange(str(ROOT / "B" / "Disc.cue"), n, (n - 1) * 1000,
                                                         None if n == 3 else n * 1000)),
                        title=f"Part {n}", artist="Ørsted", album="Disc", track_no=n, duration=12.5, sample_rate=44100,
                        bits=24, channels=2, bitrate=900_000, size=123456, codec="FLAC", details=(("CUE sheet", "x"),),
                        album_artist="Band", genre="Rock; Pop", composer="Łódź", date="2004-05-03", compilation=True)
                  for n in (1, 2, 3)]
    mp3 = Track(path=ROOT / "B" / "x.mp3", title="Lossy", codec="MP3", bitrate_mode="VBR", channel_mode="joint stereo",
                misnamed=".flac", float_samples=True, unsupported="no", encoder="LAME 3.100", disc_no=2)
    db.apply(result(ROOT / "B", cue_tracks + [mp3]))
    back = db.album_tracks(ROOT / "B", check=False)
    assert sorted(back, key=lambda t: t.title) == sorted(cue_tracks + [mp3], key=lambda t: t.title)
    cue = [t for t in back if t.cue is not None]
    assert all(isinstance(t.path, CuePath) for t in cue) and [t.cue.number for t in cue] == [1, 2, 3]
    ids = [r[0] for r in stored(db, "SELECT id FROM tracks ORDER BY id DESC")]
    assert [t.title for t in db.tracks(ids + [999])] == ["Lossy", "Part 3", "Part 2", "Part 1"]


@pytest.mark.parametrize("seed, size", [(1, 400), (2, 1500), (3, 3000), (4, 800)])
def test_album_rows_equal_browse_albums_from(tmp_path, seed, size):
    files = synthlib.make_library(size, ROOT, seed=seed)
    with LibraryDB.open(tmp_path / "library.db", [ROOT]) as db:
        synthlib.load(db, ROOT, files)
        got = db.albums()
    want = browse.albums_from(synthlib.tracks_of(files), synthlib.added_of(files), ROOT)
    assert {a.path: a for a in got} == {a.path: a for a in want}
    assert [a.name for a in got] == [a.name for a in want]
    assert any("  (" in a.name for a in got)              # the library has repeated folder names
    assert any(a.path == ROOT for a in got)                # and songs straight in the library folder


def test_album_tracks_come_in_play_order(tmp_path):
    files = synthlib.make_library(1200, ROOT, seed=7)
    with LibraryDB.open(tmp_path / "library.db", [ROOT]) as db:
        synthlib.load(db, ROOT, files)
        for album in browse.albums_from(synthlib.tracks_of(files), None, ROOT)[:60]:
            group = [t for t in synthlib.tracks_of(files)
                     if library.album_folder_text(os.path.dirname(os.fspath(t.path))) == str(album.path)]
            assert db.album_tracks(album.path, check=False) == library.play_order(album.path, group)
        assert db.album_tracks(ROOT.parent / "Elsewhere", check=False) is None


def test_repeated_folder_names_follow_what_is_stored(db):
    a, b = ROOT / "Queen" / "Greatest Hits", ROOT / "ABBA" / "Greatest Hits"
    db.apply(result(a, [song(a, 1, "One")]))
    assert [x.name for x in db.albums()] == ["Greatest Hits"]
    db.apply(result(b, [song(b, 1, "Two")]))
    assert sorted(x.name for x in db.albums()) == ["Greatest Hits  (ABBA)", "Greatest Hits  (Queen)"]
    db.apply(library_db.folder_result(ROOT, b, {}, {}))   # its music gone
    assert [x.name for x in db.albums()] == ["Greatest Hits"]


# -- dates, missing files and folders that can't be listed ----------------------------------------------
def test_first_look_dates_by_creation_then_by_arrival(db):
    folder = ROOT / "A"
    db.apply(result(folder, [song(folder, 1, "One")], created=100.0), now=500.0)
    (added, seen), = stored(db, "SELECT added, seen FROM files")
    assert (added, seen) == (100.0, 500.0)
    db.mark_built(ROOT, 600.0)
    db.mark_built(ROOT, 900.0)
    assert db.built(ROOT) == 600.0                  # set once
    db.apply(result(folder, [song(folder, 1, "One"), song(folder, 2, "Two")], created=100.0), now=700.0)
    rows = dict((name, (added, seen)) for name, added, seen in stored(db, "SELECT name, added, seen FROM files"))
    assert rows == {"01 One.flac": (100.0, 500.0), "02 Two.flac": (700.0, 700.0)}   # a re-read keeps its dates


def test_gone_files_are_kept_missing_and_come_back_with_their_dates(db):
    folder = ROOT / "A"
    tracks = [song(folder, 1, "Alpha song"), song(folder, 2, "Beta song")]
    db.apply(result(folder, tracks, created=100.0), now=200.0)
    db.mark_built(ROOT, 250.0)
    first = db.generation
    listing = {"01 Alpha song.flac": (1000, 5000, 100.0)}
    assert db.apply(library_db.folder_result(ROOT, folder, listing, {}), now=300.0)
    assert db.generation > first
    assert db.counts()["missing"] == 1 and db.albums()[0].track_count == 1
    if db.fts:
        assert [h.track.title for h in db.search_songs("beta").rows] == []
    # back, unchanged (a drive plugged in again): shown again, with its old dates, and not new music
    listing["02 Beta song.flac"] = (1001, 5001, 100.0)
    db.apply(library_db.folder_result(ROOT, folder, listing, {}), now=400.0)
    assert db.counts()["missing"] == 0 and db.albums()[0].track_count == 2
    assert stored(db, "SELECT added, seen FROM files WHERE name = '02 Beta song.flac'") == [(100.0, 200.0)]
    assert db.new_albums(0.0, {}) == []
    if db.fts:
        assert [h.track.title for h in db.search_songs("beta").rows] == ["Beta song"]


def test_purging_removes_missing_rows_only_when_told(db):
    a, b = ROOT / "A", ROOT / "B" / "CD1"
    db.apply(result(a, [song(a, 1, "One")]), now=100.0)
    db.apply(result(b, [song(b, 1, "Two")]), now=100.0)
    ids = {path: state.id for path, state in db.folders(ROOT).items()}
    assert db.mark_missing([ids[str(b)]], now=1000.0) == {ids[str(ROOT / "B")]}
    assert [x.path for x in db.albums()] == [a]
    assert db.purge_missing(ROOT, before=999.0) == 0                  # marked after that
    assert db.purge_missing(ROOT.parent / "Elsewhere", before=1000.0) == 0
    assert db.purge_missing(ROOT, before=1000.0) == 1
    assert db.counts()["files"] == 1 and set(db.folders(ROOT)) == {str(a)}   # empty folders went too


def test_purging_waits_seven_days_by_default(db):
    a = ROOT / "A"
    db.apply(result(a, [song(a, 1, "One"), song(a, 2, "Two")]))
    db.apply(library_db.folder_result(ROOT, a, {"01 One.flac": (1000, 5000, 1.0e9)}, {}))   # marked now
    assert db.purge_missing() == 0 and db.counts()["missing"] == 1
    assert db.purge_missing(before=time.time()) == 1 and db.counts()["missing"] == 0


def test_a_folder_that_couldnt_be_listed_keeps_everything(db):
    folder = ROOT / "A"
    db.apply(result(folder, [song(folder, 1, "One")]))
    before = db.counts()
    assert db.apply(library_db.folder_result(ROOT, folder, {}, {}, listed_ok=False)) == set()
    assert db.counts() == before and db.counts()["missing"] == 0
    assert not db.folders(ROOT)[str(folder)].listed_ok


def test_a_file_that_couldnt_be_read_keeps_what_was_known(db):
    folder = ROOT / "A"
    db.apply(result(folder, [song(folder, 1, "One", artist="Known")]))
    listing = {"01 One.flac": (2000, 9000, 1.0e9)}   # changed, but locked just now
    db.apply(library_db.folder_result(ROOT, folder, listing, {}, retry=["01 One.flac"]))
    states = db.file_states([db.folders(ROOT)[str(folder)].id])
    state = next(iter(states.values()))["01 One.flac"]
    assert state.mtime_ns == -1 and not state.missing and state.read_format == library_db.READ_FORMAT
    assert db.album_tracks(folder, check=False)[0].artist == "Known"


def test_folder_states_and_cue_folders(db):
    image = ROOT / "C" / "Disc.flac"
    cue = Track(path=CuePath(image, cue=CueRange("s.cue", 1, 0, None)), title="Part 1", codec="FLAC")
    db.apply(result(ROOT / "C", [cue], cue_sig='[["Disc.cue",10,20]]'))
    db.apply(result(ROOT / "D", [song(ROOT / "D", 1, "One")]))
    folders = db.folders(ROOT)
    assert folders[str(ROOT / "C")].cue_sig == '[["Disc.cue",10,20]]' and folders[str(ROOT / "D")].cue_sig == ""
    assert db.cue_track_folders([f.id for f in folders.values()]) == {folders[str(ROOT / "C")].id}


# -- real files ----------------------------------------------------------------------------------------------
def flac(path: Path, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, (RNG.standard_normal((4410, 2)) * 3000).astype(np.int16), 44100, subtype="PCM_16", format="FLAC")
    if tags:
        write_vorbis(path, **tags)
    return path


def cue_image(folder: Path, tracks: int = 3) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    sf.write(folder / "Image.flac", (RNG.standard_normal((44100 * 2 * tracks, 2)) * 3000).astype(np.int16), 44100,
             subtype="PCM_16")
    lines = ['FILE "Image.flac" WAVE']
    for n in range(1, tracks + 1):
        lines += [f"  TRACK {n:02d} AUDIO", f'    TITLE "Part {n}"', f"    INDEX 01 00:{2 * (n - 1):02d}:00"]
    (folder / "Image.cue").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder / "Image.flac"


def look(db: LibraryDB, root: Path, folder: Path, names=None) -> set[int]:
    listing, sig = library_db.list_folder(folder)
    return db.apply(library_db.read_folder(root, folder, listing, names, sig))


def test_real_files_read_stored_and_checked_against_the_disk(tmp_path):
    root = tmp_path / "Music"
    album = root / "Artist" / "Album"
    for n, title in enumerate(["One", "Two", "Three"], 1):
        flac(album / f"0{n} {title}.flac", title=title, artist="Artist", album="Album", tracknumber=str(n))
    flac(album / "CD2" / "01 Four.flac", title="Four", album="Album", discnumber="2", tracknumber="1")
    cue_image(root / "Live")
    with LibraryDB.open(tmp_path / "library.db", [root]) as db:
        for folder in (album, album / "CD2", root / "Live"):
            look(db, root, folder)
        assert db.album_tracks(album) == library.scan_album(album)
        assert db.album_tracks(root / "Live") == library.scan_album(root / "Live")
        assert [t.title for t in db.album_tracks(root / "Live")] == ["Part 1", "Part 2", "Part 3"]
        assert db.album_tracks(root, whole_tree=True) == library.scan_folder(root)
        assert {a.path for a in db.albums()} == {album, root / "Live"}
        assert db.album_of(album).track_count == 4 and db.album_of(album / "Nope") is None
        # anything changed on the disk: the caller reads the files itself
        later = time.time_ns() + 5_000_000_000
        os.utime(album / "02 Two.flac", ns=(later, later))
        assert db.album_tracks(album) is None
        assert db.album_tracks(album, check=False) is not None
        (root / "Live" / "Image.cue").write_text((root / "Live" / "Image.cue").read_text() + "REM x\n")
        assert db.album_tracks(root / "Live") is None
        flac(album / "04 New.flac", title="New")
        look(db, root, album, names=["04 New.flac", "02 Two.flac"])
        assert db.album_tracks(album) == library.scan_album(album)


def test_reading_a_folder_with_its_own_cuesheet_reads_it_whole(tmp_path):
    root = tmp_path / "Music"
    image = cue_image(root / "Disc")
    calls = []

    def reader(path):
        calls.append(path.name)
        return library.read_track(path)

    listing, sig = library_db.list_folder(image.parent)
    assert set(listing) == {"Image.flac"} and "Image.cue" in sig
    found = library_db.read_folder(root, image.parent, listing, names=[], cue_sig=sig, reader=reader)
    assert calls == ["Image.flac"] and len(found.tracks["Image.flac"]) == 3


def test_a_locked_file_goes_in_retry(tmp_path):
    root = tmp_path / "Music"
    flac(root / "A" / "01.flac", title="One")
    flac(root / "A" / "02.flac", title="Two")

    def reader(path):
        if path.name == "02.flac":
            return Track(path=path, title="02", unreadable=True)
        return library.read_track(path)

    listing, sig = library_db.list_folder(root / "A")
    found = library_db.read_folder(root, root / "A", listing, reader=reader)
    assert found.retry == ["02.flac"] and set(found.tracks) == {"01.flac", "02.flac"}


# -- new music and the opened list ---------------------------------------------------------------------------
def test_new_albums_and_tidying_match_the_watchers_rules(tmp_path):
    files = synthlib.make_library(2500, ROOT, seed=11)
    rng = random.Random(5)
    built = synthlib.BASE_TIME + 5e7
    for f in files:   # some music arrives after the first build, at different times
        if rng.random() < 0.3:
            f.seen = built + rng.random() * 1e6
    index = synthlib.index_of(ROOT, files, built, tmp_path / "library_index.json")
    index.save()
    with LibraryDB.open(tmp_path / "library.db", [ROOT]) as db:
        assert db.import_json_index(tmp_path / "library_index.json")
        assert db.album_folders() == {str(watcher.album_folder(Path(p))) for p in index.entries}
        folders = sorted(db.album_folders())
        for since in (0.0, built + 2e5, built + 9e5, built + 2e6):
            opened = {folder: built + rng.random() * 1e6 for folder in rng.sample(folders, 40)}
            assert db.new_albums(since, opened) == legacy_index.new_albums(index, since, opened)
            tidy = {f: w for f, w in opened.items() if f in db.album_folders() and w > since}
            opened["C:/Not/In/The/Library"] = built + 5
            assert tidy == legacy_index.tidy_opened(opened, index, since)


# -- library_index.json brought across ---------------------------------------------------------------------
def test_the_json_index_is_brought_across_exactly(tmp_path):
    files = synthlib.make_library(2000, ROOT, seed=13)
    built = synthlib.BASE_TIME + 1.5
    path = tmp_path / "library_index.json"
    index = synthlib.index_of(ROOT, files, built, path)
    for f in files[::7]:
        index.entries[f.path].mtime = -1   # (due to be read again: kept that way)
    index.save()
    steps = []
    with LibraryDB.open(tmp_path / "library.db", [ROOT]) as db:
        assert db.import_json_index(path, progress=lambda done, total: steps.append((done, total)))
        assert steps[-1] == (len(files), len(files))
        assert db.built(ROOT) == built
        rows = {os.path.join(root, rel, name) if rel else os.path.join(root, name): (size, mtime, added, seen, fmt)
                for root, rel, name, size, mtime, added, seen, fmt in stored(
                    db, "SELECT r.path, d.rel, f.name, f.size, f.mtime_ns, f.added, f.seen, f.read_format FROM files f"
                        " JOIN folders d ON d.id = f.folder_id JOIN roots r ON r.id = d.root_id")}
        assert rows == {name: (e.size, e.mtime, e.added, e.seen, library_index.FORMAT)
                        for name, e in index.entries.items()}
        assert {a.path: a for a in db.albums()} == {a.path: a for a in index.albums()}
        assert sorted((t for t in db.album_tracks(ROOT, whole_tree=True, check=False)), key=str_key) == \
            sorted(index.tracks(), key=str_key)
        sheets = {folder: state.cue_sig for folder, state in db.folders(ROOT).items() if state.cue_sig}
        assert sheets == {folder: library_db.cue_signature_of(tuple(s) for s in sheet)
                          for folder, sheet in index.cues.items()}
    assert not path.exists() and (tmp_path / "library_index.json.migrated").exists()


def str_key(track: Track):
    return (str(track.path), track.cue.number if track.cue else 0)


def test_what_cant_be_brought_across_is_left_for_a_first_build(tmp_path):
    files = synthlib.make_library(300, ROOT, seed=17)
    path = tmp_path / "library_index.json"
    synthlib.index_of(ROOT, files, 5.0, path).save()
    good = path.read_text(encoding="utf-8")
    with LibraryDB.open(tmp_path / "other.db", [ROOT.parent / "Elsewhere"]) as db:
        assert not db.import_json_index(path)                     # another library folder
    with LibraryDB.open(tmp_path / "old.db", [ROOT]) as db:
        data = json.loads(good)
        data["format"] = 0
        path.write_text(json.dumps(data), encoding="utf-8")
        assert not db.import_json_index(path)                     # an older format
        path.write_text(good[: len(good) // 2], encoding="utf-8")
        assert not db.import_json_index(path)                     # damaged
        assert not db.import_json_index(tmp_path / "none.json")   # none there
        data = json.loads(good)
        outside = "D:\\Elsewhere\\x.flac" if os.name == "nt" else "/elsewhere/x.flac"
        data["files"][outside] = next(iter(data["files"].values()))
        path.write_text(json.dumps(data), encoding="utf-8")
        assert not db.import_json_index(path)                     # a file outside the folder: nothing kept
        assert db.counts()["files"] == 0
        path.write_text(good, encoding="utf-8")
        db.apply(result(ROOT / "A", [song(ROOT / "A", 1, "One")]))
        assert not db.import_json_index(path)                     # already has rows of its own
    assert path.exists()


# -- several library folders ----------------------------------------------------------------------------
def test_several_library_folders(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    with LibraryDB.open(tmp_path / "library.db", [a, b, str(a)]) as db:
        assert db.roots() == [str(a), str(b)]
        db.apply(result(a / "X", [song(a / "X", 1, "One")], root=a))
        db.apply(result(b / "X", [song(b / "X", 1, "Two")], root=b))
        db.apply(result(b, [song(b, 1, "Loose")], root=b))
        assert sorted(x.name for x in db.albums()) == ["B", "X  (A)", "X  (B)"]
        db.record_play(b / "X" / "01 Two.flac", 100, 100)
        db.set_roots([a])
        assert [x.name for x in db.albums()] == ["X"] and db.roots() == [str(a)]
        assert db.play_counts([b / "X" / "01 Two.flac"]) == {str(b / "X" / "01 Two.flac"): 1}
        if db.fts:
            assert db.search_songs("two").total == 0
        assert stored(db, "SELECT count(*) FROM tracks") == [(1,)]


def test_a_library_folder_that_moved_keeps_everything(tmp_path):
    old, new = tmp_path / "E" / "Music", tmp_path / "F" / "Music"
    with LibraryDB.open(tmp_path / "library.db", [old]) as db:
        db.apply(result(old / "X", [song(old / "X", 1, "One")], root=old), now=100.0)
        assert db.move_root(old, new)
        assert db.roots() == [str(new)] and [a.path for a in db.albums()] == [new / "X"]
        assert db.album_tracks(new / "X", check=False)[0].path == new / "X" / "01 One.flac"
        assert stored(db, "SELECT seen FROM files") == [(100.0,)]
        assert not db.move_root(old, new) and not db.move_root(new, new)


# -- plays and favourites --------------------------------------------------------------------------------
@pytest.mark.parametrize("heard, duration, counts", [
    (100, 200, True), (99.9, 200, False), (240, 1200, True), (239, 1200, False), (5, 8, True), (0, 0, False),
    (240, 0, True), (100, 0, False), (-1, 10, False),
])
def test_what_counts_as_a_play(heard, duration, counts):
    assert library_db.counts_as_play(heard, duration) is counts


def test_plays_and_favourites(db):
    cue3 = CuePath("C:/m/Disc.flac", cue=CueRange("C:/m/Disc.cue", 3, 0, None))
    k3 = audiofile.key(cue3)   # the file's own spelling + "#3"
    first = db.generation
    assert db.record_play("C:/m/a.flac", 150, 200, when=10.0)
    assert not db.record_play("C:/m/a.flac", 20, 200, when=11.0)        # skipped: not a play
    assert db.record_play(cue3, 300, 300, when=12.0)
    assert db.record_play("C:/m/a.flac", 200, 200, when=13.0)
    assert db.record_play("C:/m/b.flac", 300, 600, when=14.0)
    assert db.generation == first                                       # (plays aren't library changes)
    assert db.play_counts(["C:/m/a.flac", k3, "C:/m/Disc.flac", "C:/m/z.flac"]) == {
        "C:/m/a.flac": 2, k3: 1, "C:/m/Disc.flac": 0, "C:/m/z.flac": 0}
    assert db.last_played([cue3, "C:/m/a.flac", "C:/m/z.flac"]) == {k3: 12.0, "C:/m/a.flac": 13.0}
    assert db.most_played(2) == [("C:/m/a.flac", 2), ("C:/m/b.flac", 1)]
    assert db.recently_played() == [("C:/m/b.flac", 14.0), ("C:/m/a.flac", 13.0), (k3, 12.0)]
    assert stored(db, "SELECT completed FROM plays ORDER BY played_at") == [(0,), (1,), (1,), (0,)]
    db.set_favourite("C:/m/a.flac", when=1.0)
    db.set_favourite(cue3, when=2.0)
    db.set_favourite("C:/m/a.flac", when=3.0)                           # (already: kept as it was)
    assert db.favourites() == [k3, "C:/m/a.flac"]
    assert db.is_favourite(cue3) and not db.is_favourite("C:/m/Disc.flac")
    db.set_favourite("C:/m/a.flac", on=False)
    assert db.favourites() == [k3]


def test_plays_survive_the_tracks_being_stored_again(db):
    folder = ROOT / "A"
    db.apply(result(folder, [song(folder, 1, "One")]))
    key = str(ROOT / "A" / "01 One.flac")
    db.record_play(key, 300, 300)
    db.set_favourite(key)
    db.apply(result(folder, [song(folder, 1, "One", artist="Retagged")]))   # its rows made again
    db.set_roots([])
    assert db.play_counts([key]) == {key: 1} and db.favourites() == [key]


# -- search -----------------------------------------------------------------------------------------------------
@pytest.fixture
def searchable(db):
    if not db.fts:
        pytest.skip("this SQLite has no FTS5")
    a = ROOT / "Radiohead" / "1997 - OK Computer"
    db.apply(result(a, [song(a, n, t, artist="Radiohead", album="OK Computer", date="1997", genre="Rock")
                        for n, t in enumerate(["Airbag", "Paranoid Android", "Lucky", "Karma Police",
                                               "No Surprises"], 1)]))
    b = ROOT / "Beethoven" / "Symphonies"
    db.apply(result(b, [song(b, 9, "Symphony No. 9 in D minor", artist="Berliner Philharmoniker",
                             album="Symphonies", composer="Ludwig van Beethoven", genre="Classical", date="1963"),
                        song(b, 5, "Symphony No. 5", artist="Berliner Philharmoniker", album="Symphonies",
                             composer="Ludwig van Beethoven", genre="Classical")]))
    c = ROOT / "Various" / "Łódź Nights"
    db.apply(result(c, [song(c, 1, "Lucky Star", artist="Madonna", album="Łódź Nights"),
                        song(c, 2, "Night Train", artist="Łódź Orchestra", album="Łódź Nights"),
                        song(c, 3, "Karma", artist="Lucky Dube", album="Łódź Nights"),
                        song(c, 4, "Highway to Hell", artist="AC/DC", album="Łódź Nights")]))
    return db


def titles(hits) -> list[str]:
    return [h.track.title for h in hits.rows]


def test_search_finds_words_starting_anywhere_folded(searchable):
    # the artist's name before the album's; then by artist (AC/DC, Lucky Dube, Madonna)
    assert titles(searchable.search_songs("lodz")) == ["Night Train", "Highway to Hell", "Karma", "Lucky Star"]
    assert titles(searchable.search_songs("ŁÓDŹ")) == titles(searchable.search_songs("lodz"))
    assert titles(searchable.search_songs("beet 9")) == ["Symphony No. 9 in D minor"]
    assert set(titles(searchable.search_songs("1997 radiohead"))) == {"Airbag", "Paranoid Android", "Lucky",
                                                                       "Karma Police", "No Surprises"}
    assert titles(searchable.search_songs("AC/DC")) == ["Highway to Hell"]
    hits = searchable.search_songs("para andr")
    assert titles(hits) == ["Paranoid Android"] and hits.total == 1 and not hits.capped


def test_search_ranks_title_then_artist_then_album_and_keeps_albums_in_order(searchable):
    hits = searchable.search_songs("lucky")
    assert titles(hits) == ["Lucky Star", "Lucky", "Karma"]   # titles first (Madonna, Radiohead), then the artist
    assert [h.where for h in hits.rows] == ["", "", ""]
    hits = searchable.search_songs("karma")
    assert titles(hits) == ["Karma", "Karma Police"]          # both in a title: by artist
    hits = searchable.search_songs("radio")
    assert titles(hits) == ["Airbag", "Paranoid Android", "Lucky", "Karma Police", "No Surprises"]   # track order
    hits = searchable.search_songs("kar")                     # prefix only: titles still before anything else
    assert titles(hits) == ["Karma", "Karma Police"]
    assert all(h.album in (ROOT / "Various" / "Łódź Nights", ROOT / "Radiohead" / "1997 - OK Computer")
               for h in hits.rows)


def test_whole_word_hits_come_before_prefix_hits(searchable):
    folder = ROOT / "Z"
    searchable.apply(result(folder, [song(folder, 1, "Nightfall", artist="Aardvark"),
                                     song(folder, 2, "Night", artist="Zed")]))
    assert titles(searchable.search_songs("night"))[:2] == ["Night", "Night Train"]


def test_search_says_where_it_found_a_song_outside_title_artist_album(searchable):
    hits = searchable.search_songs("ludwig")
    assert {h.where for h in hits.rows} == {"composer"}
    assert {h.where for h in searchable.search_songs("classical").rows} == {"genre"}
    assert {h.where for h in searchable.search_songs("1963").rows} == {"year"}


def test_search_needs_two_letters_and_takes_any_syntax(searchable):
    assert searchable.search_songs("l") == library_db.SongHits([], 0)
    assert searchable.search_songs("a b") == library_db.SongHits([], 0)
    for query in ('"', "NEAR", "title:", "-", "- -", "AND", "lucky OR", '"lucky', "(", "*", "{x}", "^"):
        searchable.search_songs(query)   # never an error


def test_the_middle_of_a_word_is_found_the_slower_way(searchable):
    hits = searchable.search_songs("ight")    # "Night Train", and the album "Łódź Nights"
    assert set(titles(hits)) == {"Night Train", "Lucky Star", "Karma", "Highway to Hell"}
    assert hits.total == 4 and not hits.capped
    assert hits.rows[0].track.title == "Night Train"          # found in its title
    assert searchable.search_songs("ig").total == 0             # under three letters: not looked for that way
    assert titles(searchable.search_songs("rnin")) == []         # nothing anywhere


def test_the_slower_search_counts_up_to_a_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(library_db, "COUNT_CAP", 50)
    with LibraryDB.open(tmp_path / "library.db", [ROOT]) as db:
        if not db.fts:
            pytest.skip("this SQLite has no FTS5")
        folder = ROOT / "Many"
        db.apply(result(folder, [song(folder, n, f"Midnight {n}") for n in range(1, 81)]))
        hits = db.search_songs("dnig", limit=10)
        assert len(hits.rows) == 10 and hits.total == 50 and hits.capped


def test_without_fts5_every_search_is_done_the_slower_way(tmp_path, monkeypatch):
    monkeypatch.setattr(library_db, "fts_problem", lambda: "no FTS5 here")
    monkeypatch.setattr(library_db, "COUNT_CAP", 30)
    with LibraryDB.open(tmp_path / "library.db", [ROOT]) as db:
        assert not db.fts
        a = ROOT / "A"
        db.apply(result(a, [song(a, 1, "Łódź Blues", artist="X"), song(a, 2, "Night", artist="Y")]
                     + [song(a, n, f"Filler {n}") for n in range(3, 60)]))
        assert titles(db.search_songs("lodz")) == ["Łódź Blues"]
        assert titles(db.search_songs("igh")) == ["Night"]
        hits = db.search_songs("fill", limit=5)
        assert len(hits.rows) == 5 and hits.total == 30 and hits.capped
        assert db.search_songs("x").total == 0
        db.apply(library_db.folder_result(ROOT, a, {"02 Night.flac": (1001, 5001, 1.0e9)}, {}))
        assert titles(db.search_songs("lodz")) == []           # missing files aren't found


def test_an_interrupted_search_says_so(searchable):
    con = searchable._con()
    con.set_progress_handler(lambda: 1, 1)   # stop every statement at once, as interrupt() does
    try:
        with pytest.raises(library_db.Interrupted):
            searchable.search_songs("lucky")
    finally:
        con.set_progress_handler(None, 1)
    searchable.interrupt(threading.current_thread())   # (nothing running: nothing happens)
    searchable.interrupt(123456789)
    assert titles(searchable.search_songs("lucky"))


def test_refolding_rebuilds_the_search_texts(searchable):
    con = searchable._con()
    con.execute("UPDATE meta SET value = 0 WHERE key = 'fold_version'")
    con.execute("INSERT INTO song_fts(song_fts) VALUES ('delete-all')")
    con.execute("UPDATE tracks SET haystack = ''")
    assert searchable.needs_refold and titles(searchable.search_songs("lucky")) == []
    assert not searchable.refold(batch=3, cancel=lambda: True)       # stopped after one batch
    assert searchable.refold(batch=3)
    assert not searchable.needs_refold
    assert titles(searchable.search_songs("lucky")) == ["Lucky Star", "Lucky", "Karma"]
    assert len(titles(searchable.search_songs("ight"))) == 4


# -- threads --------------------------------------------------------------------------------------------------
def test_each_thread_reads_through_its_own_connection(db):
    folder = ROOT / "A"
    db.apply(result(folder, [song(folder, 1, "One")]))
    seen = {}

    def read():
        seen["con"] = db._con()
        seen["albums"] = [a.name for a in db.albums()]
        db.release()

    worker = threading.Thread(target=read)
    worker.start()
    worker.join()
    assert seen["con"] is not db._con() and seen["albums"] == ["A"]


def test_a_reader_keeps_reading_while_a_writer_writes(db):
    if not db.fts:
        pytest.skip("this SQLite has no FTS5")
    folder = ROOT / "A"
    db.apply(result(folder, [song(folder, 1, "Lucky")]))
    holding, done = threading.Event(), threading.Event()

    def write():
        with db._writing() as con:
            con.execute("UPDATE files SET seen = seen")
            holding.set()
            done.wait(5)
        db.release()

    writer = threading.Thread(target=write)
    writer.start()
    holding.wait(5)
    started = time.perf_counter()
    assert titles(db.search_songs("lucky")) == ["Lucky"]
    assert time.perf_counter() - started < 1.0   # (the write lock never blocks a reader)
    done.set()
    writer.join()


def test_a_closed_database_says_so(tmp_path):
    db = LibraryDB.open(tmp_path / "library.db", [ROOT])
    db.close()
    with pytest.raises(sqlite3.ProgrammingError):
        db.albums()


def test_search_texts_match_searchfold():
    t = Track(path=Path("C:/m/A/03 Ørsted.flac"), title="Ørsted", artist="Łódź", album="Æon", album_artist="X",
              composer="Þór", genre="Rock; Pop", date="2004-05-03")
    values, fts = library_db.track_row(t)
    assert fts == ("orsted", "lodz", "aeon", "x", "thor", "rock; pop", "2004", "03 orsted")
    assert values[-1] == searchfold.haystack(t)


# -- crashes and resuming ----------------------------------------------------------------------------------
_KILLED = r"""
import os, sys
sys.path[:0] = [{repo!r}, {tests!r}]
import synthlib
from ironbark import library_db
from pathlib import Path
root = Path({root!r})
results = synthlib.folder_results(root, synthlib.make_library(900, root, seed=21))
db = library_db.LibraryDB.open(Path({path!r}), [root])
for result in results[:{stop}]:
    db.apply(result, now=2e9)
con = db._con()
con.execute("BEGIN IMMEDIATE")                       # half-way through the next folder…
con.execute("UPDATE files SET seen = 0")
con.execute("DELETE FROM albums")
os._exit(3)                                           # …the process dies
"""


def test_a_process_killed_mid_folder_leaves_a_sound_database_that_resumes(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "library.db"
    results = synthlib.folder_results(ROOT, synthlib.make_library(900, ROOT, seed=21))
    stop = len(results) // 2
    script = _KILLED.format(repo=str(Path(library_db.__file__).parents[1]), tests=str(Path(__file__).parent),
                            root=str(ROOT), path=str(path), stop=stop)
    finished = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert finished.returncode == 3, finished.stderr
    with LibraryDB.open(path, [ROOT]) as db:
        assert db.quick_check() == "ok"
        assert db.counts()["files"] == sum(len(r.listing) for r in results[:stop])   # the folders done, nothing more
        assert {r[0] for r in stored(db, "SELECT seen FROM files")} == {2e9}         # the half-done one undone
        for result in results[stop:]:                                                # carry on where it stopped
            db.apply(result, now=2e9)
        resumed = db.albums()
    with LibraryDB.open(tmp_path / "whole.db", [ROOT]) as db:
        for result in results:
            db.apply(result, now=2e9)
        assert db.albums() == resumed

"""The library keeps what it knows when the drive under it changes: another drive at a library
folder's place changes nothing; a drive back under another letter keeps every play, favourite and
check; a renamed or moved album (or one put back from the Recycle Bin) keeps its dates, plays,
favourites and checks; song ids are never given twice, and a whole-library tab reads songs read
again; a worker that dies with two songs in hand doesn't call the good one damaged; a folder
Windows can't open by its name doesn't stop new music being spotted, and is said; a newer
version's database keeps its plays, favourites and checks; a favourite or play is never dropped
because a long look held the database; and a file that can't be read just now keeps its tags."""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import favourites, indexer, library_check, library_db, library_tracks, ui
from ironbark.indexer import Indexer, Unreachable
from ironbark.library import Track
from ironbark.library_check import CHECK_FORMAT, Checker
from ironbark.library_db import LibraryDB


# -- helpers -------------------------------------------------------------------------------------------
class Reader:
    """Stands in for library.read_track: a track from the file's name alone, every read noted;
    `locked`: file names that can't be read just now (as a file another program holds)."""

    def __init__(self, locked=()):
        self.read: list[Path] = []
        self.locked = set(locked)

    def __call__(self, path):
        path = Path(path)
        self.read.append(path)
        if path.name in self.locked:
            return Track(path=path, title=path.stem, unreadable=True)
        return Track(path=path, title=path.stem, artist="Band", album=path.parent.name, track_no=int(path.stem[:2]),
                     codec="FLAC", sample_rate=44100, bits=16, channels=2, size=path.stat().st_size, duration=1.0)


def tree(root: Path, albums: dict[str, int]) -> Path:
    """Album folders of small .flac files (Reader doesn't open them), no two the same size."""
    for name, count in albums.items():
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        for n in range(1, count + 1):
            (folder / f"{n:02d}.flac").write_bytes(f"{folder}{n}".encode() * 7)
    return root


def rows(db: LibraryDB) -> dict[str, tuple]:
    """Every stored file: {path: (size, modified ns, added, seen, missing?)}."""
    return {os.path.join(root, rel, name) if rel else os.path.join(root, name): (size, mtime, added, seen, missing)
            for root, rel, name, size, mtime, added, seen, missing in db._con().execute(
                "SELECT r.path, d.rel, f.name, f.size, f.mtime_ns, f.added, f.seen, f.missing_since IS NOT NULL"
                " FROM files f JOIN folders d ON d.id = f.folder_id JOIN roots r ON r.id = d.root_id")}


def remember(db: LibraryDB, song: Path) -> None:
    """A favourite, a play and a quality check for this song."""
    st = song.stat()
    db.set_favourite(song)
    assert db.record_play(str(song), 60.0, 60.0)
    db.save_check(str(song), st.st_size, st.st_mtime_ns, CHECK_FORMAT, "", 0.5, "")


def remembered(db: LibraryDB, song: Path) -> bool:
    key = str(song)
    return (key in db.favourites() and dict(db.most_played()).get(key) == 1 and key in db.check_states()
            and [k for k, _hit in db.songs_for_keys([key])] == [key])


@pytest.fixture
def db(tmp_path):
    database = LibraryDB.open(tmp_path / "library.db")
    yield database
    database.close()


# -- another drive where the library folder was ----------------------------------------------------------
def test_another_drive_at_the_library_folders_place_changes_nothing(tmp_path, db, monkeypatch):
    root = tree(tmp_path / "E", {"A": 3, "B": 2})
    drive = {"serial": 111}
    monkeypatch.setattr(indexer, "volume_of", lambda folder: drive["serial"], raising=False)
    look = Indexer(db, reader=Reader())
    look.check([root])
    remember(db, root / "A" / "01.flac")
    before, since = rows(db), time.time()
    away = root.rename(tmp_path / "E (unplugged)")   # the music drive is unplugged…
    (root / "DCIM").mkdir(parents=True)              # …and a photo stick gets its letter
    (root / "DCIM" / "IMG_0001.jpg").write_bytes(b"jpeg")
    drive["serial"] = 222
    events = look.listen()
    for _look in range(3):
        diff = look.check([root])
        assert diff.error == f"A different drive is at {root}" and diff.nothing
    said = []
    while not events.empty():
        said.append(events.get())
    assert [e.why for e in said if isinstance(e, Unreachable)] == [f"A different drive is at {root}"] * 3
    assert rows(db) == before and library_check.plan(db).gone == []
    shutil.rmtree(root)
    away.rename(root)                                 # the music drive is back
    drive["serial"] = 111
    diff = look.check([root])
    assert diff.nothing and not diff.error and rows(db) == before
    assert db.new_album_times(since - 1) == [] and remembered(db, root / "A" / "01.flac")


def test_the_same_music_on_a_new_drive_and_a_drive_replaced_for_a_week_are_taken_on(tmp_path, db, monkeypatch):
    root = tree(tmp_path / "E", {"A": 3})
    drive = {"serial": 111}
    monkeypatch.setattr(indexer, "volume_of", lambda folder: drive["serial"], raising=False)
    look = Indexer(db, reader=Reader())
    look.check([root])
    drive["serial"] = 333        # the music copied (times and all) onto a new drive at the same letter
    diff = look.check([root])
    assert not diff.error and diff.nothing
    shutil.rmtree(root / "A")
    tree(root, {"Other": 2})     # a drive with other music in its place…
    drive["serial"] = 444
    assert look.check([root]).error == f"A different drive is at {root}"
    db.set_volume(root, 333, since=time.time() - library_db.MISSING_DAYS * 86400 - 60)   # …for over a week
    diff = look.check([root])
    assert not diff.error and [p.parent.name for p in diff.added] == ["Other", "Other"]


def test_without_a_drive_to_tell_by_another_drives_files_are_kept_for_a_week(tmp_path, db):
    root = tree(tmp_path / "E", {"A": 3, "B": 2})
    look = Indexer(db, reader=Reader())
    look.check([root])
    song = root / "A" / "01.flac"
    remember(db, song)
    before, since = rows(db), time.time()
    away = root.rename(tmp_path / "E (unplugged)")
    tree(root, {"Backup": 1})
    look.check([root])
    look.check([root])           # (once, the second clean look removed everything for good)
    assert library_check.plan(db).gone == []
    shutil.rmtree(root)
    away.rename(root)
    look.check([root])
    assert {path: row for path, row in rows(db).items() if "Backup" not in path} == before
    assert db.new_album_times(since - 1) == [] and remembered(db, song)


# -- a drive back under another letter ---------------------------------------------------------------------
def test_a_drive_back_under_another_letter_keeps_plays_favourites_and_checks(tmp_path, db):
    old = tree(tmp_path / "E" / "Music", {"A": 3, "B": 2})
    look = Indexer(db, reader=Reader())
    look.check([old])
    remember(db, old / "A" / "01.flac")
    before, since = rows(db), time.time()
    (tmp_path / "E").rename(tmp_path / "F")
    new = tmp_path / "F" / "Music"
    reader = look.reader = Reader()
    diff = look.check([new])     # Settings → Library → Change…: F:\Music
    assert reader.read == [] and diff.nothing and db.roots() == [str(new)]
    assert {path.replace(str(new), str(old)): row for path, row in rows(db).items()} == before
    assert remembered(db, new / "A" / "01.flac") and db.new_album_times(since - 1) == []
    assert library_check.plan(db).gone == []


def test_checks_of_a_folder_taken_out_of_the_library_are_kept_for_its_return(tmp_path, db):
    one, two = tree(tmp_path / "One", {"A": 2}), tree(tmp_path / "Two", {"B": 2})
    look = Indexer(db, reader=Reader())
    look.check([one, two])
    remember(db, two / "B" / "01.flac")
    look.check([one])            # Settings → Library → Remove: Two
    assert library_check.plan(db).gone == []
    look.check([one, two])       # and added back
    assert remembered(db, two / "B" / "01.flac")


# -- renamed, moved and restored albums --------------------------------------------------------------------
def test_a_renamed_album_folder_keeps_its_dates_plays_favourites_and_checks(tmp_path, db):
    root = tree(tmp_path / "Music", {"Album": 2, "Other": 1})
    look = Indexer(db, reader=Reader())
    look.check([root])
    song = root / "Album" / "01.flac"
    remember(db, song)
    before, since = rows(db), time.time()
    (root / "Album").rename(root / "Album (2019 Remaster)")
    look.check([root])
    look.check([root])
    moved = root / "Album (2019 Remaster)" / "01.flac"
    assert rows(db)[str(moved)][2:4] == before[str(song)][2:4]   # added and seen, as they were
    assert str(song) not in rows(db) and remembered(db, moved)
    assert db.new_album_times(since - 1) == [] and library_check.plan(db).gone == []


def test_a_file_renamed_in_its_folder_keeps_its_plays(tmp_path, db):
    root = tree(tmp_path / "Music", {"Album": 2})
    look = Indexer(db, reader=Reader())
    look.check([root])
    song = root / "Album" / "01.flac"
    remember(db, song)
    since = time.time()
    renamed = song.rename(song.with_name("01 Intro.flac"))
    look.check([root])
    assert remembered(db, renamed) and db.new_album_times(since - 1) == []


def test_an_album_put_back_from_the_recycle_bin_days_later_is_as_it_was(tmp_path, db):
    root = tree(tmp_path / "Music", {"Album": 2, "Other": 1})
    look = Indexer(db, reader=Reader())
    look.check([root])
    song = root / "Other" / "01.flac"
    remember(db, song)
    before, since = rows(db), time.time()
    shutil.move(root / "Other", tmp_path / "Recycle Bin")
    look.check([root])
    look.check([root])
    shutil.move(tmp_path / "Recycle Bin", root / "Other")
    look.check([root])
    assert rows(db) == before and remembered(db, song) and db.new_album_times(since - 1) == []


# -- song ids and whole-library tabs ---------------------------------------------------------------------
def test_a_whole_library_tab_never_shows_another_albums_songs_under_a_header(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 2, "B": 3})
    look = Indexer(db, reader=Reader())
    look.check([root])
    tab = library_tracks.build(db, db.albums())
    _folder, start, end = next(group for group in tab.groups if group[0].name == "B")
    shutil.rmtree(root / "B")    # the album added last (the highest ids) deleted…
    look.check([root])
    db.purge_missing(before=time.time() + 1)
    tree(root, {"C": 3})         # …and another copied in
    look.check([root])
    shown = [tab[i] for i in range(start, end)]
    assert all(track.missing for track in shown), [track.path for track in shown]


def test_every_song_of_a_library_tab_is_read_after_another_program_retags_an_album(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 3, "B": 3})
    look = Indexer(db, reader=Reader())
    look.check([root])
    tab = library_tracks.build(db, db.albums())
    for song in (root / "A").iterdir():   # re-tagged: read again, so new song ids
        song.write_bytes(song.read_bytes() + b"tags")
    look.check([root])
    songs = tab.many(range(len(tab)))
    assert len(songs) == 6 and not any(track.missing for track in songs)
    assert sorted(track.path.name for track in songs) == sorted(["01.flac", "02.flac", "03.flac"] * 2)


# -- the quality check's worker processes ------------------------------------------------------------------
RATE = 44100


def _noise(seconds: float, seed: int) -> np.ndarray:
    return (0.2 * np.random.default_rng(seed).standard_normal((int(seconds * RATE), 2))).clip(-0.9, 0.9)


class _TwoWorkers:
    """Stands in for two worker processes: a song named `crash` kills its worker a moment after it
    starts, which fails every song then in hand; the others take a little longer to check."""

    def __init__(self, crash: str):
        self.crash = crash
        self.futures: list[Future] = []
        self.lock = threading.Lock()

    def submit(self, fn, song):
        future = Future()
        with self.lock:
            self.futures.append(future)
        if Path(song.path).stem == self.crash:
            threading.Timer(0.05, self._die).start()
        else:
            threading.Timer(0.6, self._finish, (future, song)).start()
        return future

    def _die(self):
        with self.lock:
            for future in self.futures:
                if not future.done():
                    future.set_exception(BrokenProcessPool("a worker died"))

    def _finish(self, future, song):
        outcome = library_check.check_song(song)
        with self.lock:
            if not future.done():
                future.set_result(outcome)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def test_a_worker_dying_with_two_songs_in_hand_calls_only_the_bad_one_damaged(tmp_path, db):
    root = tmp_path / "Music"
    for n, name in enumerate(("01 Good", "02 Bad", "03 Good")):
        path = root / "Album" / f"{name}.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(path, _noise(1, n), RATE, format="FLAC", subtype="PCM_16")
    Indexer(db).check([root])
    checker = Checker(lambda: db, workers=2, pool_factory=lambda workers: (_TwoWorkers("02 Bad"), threading.Event()))
    checker.start()
    assert checker.idle(60) and checker.status.error == ""
    damaged = {hit.track.path.stem for hit, look in library_check.needs_look(db) if "damaged" in look.words}
    assert damaged == {"02 Bad"}


# -- folders Windows can't open by their names -------------------------------------------------------------
def test_a_folder_windows_cant_open_doesnt_stop_new_music_and_is_said(tmp_path, db, monkeypatch):
    root = tree(tmp_path / "Music", {"A": 2, "R.E.M": 2})
    odd = str(root / "R.E.M")
    real = os.scandir

    def scandir(path="."):   # (as Windows does with "R.E.M." made on a Mac: listed, but not opened by that name)
        if os.fspath(path) == odd:
            raise FileNotFoundError(2, "The system cannot find the path specified", odd)
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    look = Indexer(db, reader=Reader())
    look.check([root])
    diff = look.check([root])
    assert db.built(root) and diff.unreadable == [Path(odd)] and "can't be read" in diff.note()
    since = time.time()
    time.sleep(0.02)
    tree(root, {"New": 1})
    look.check([root])
    assert [folder.name for folder, _seen in db.new_album_times(since)] == ["New"]


# -- a newer version's database ----------------------------------------------------------------------------
def test_a_newer_versions_database_keeps_plays_favourites_and_checks(tmp_path):
    path = tmp_path / "library.db"
    song = str(tmp_path / "Music" / "01.flac")
    db = LibraryDB.open(path)
    db.set_favourite(song)
    db.record_play(song, 100.0, 100.0)
    db.save_check(song, 10, 20, CHECK_FORMAT, "", None, "")
    db.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE favourites ADD COLUMN rating INTEGER NOT NULL DEFAULT 0")   # (a later version's)
    con.execute("ALTER TABLE plays ADD COLUMN device TEXT")
    con.execute("ALTER TABLE checks ADD COLUMN loudness REAL")
    con.execute("PRAGMA user_version = 9")
    con.commit()
    con.close()
    db = LibraryDB.open(path)      # (going back to this version)
    try:
        assert db.favourites() == [song] and db.most_played() == [(song, 1)] and db.check_of(song) is not None
        db.set_favourite(song + "2")
        assert db.record_play(song, 100.0, 100.0) and db.most_played() == [(song, 2)]
    finally:
        db.close()


def test_a_kept_table_this_version_cant_use_is_set_aside_never_dropped(tmp_path):
    path = tmp_path / "library.db"
    LibraryDB.open(path).close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE favourites")
    con.execute("CREATE TABLE favourites(song TEXT PRIMARY KEY, added REAL NOT NULL) WITHOUT ROWID")
    con.execute("INSERT INTO favourites VALUES ('C:\\Music\\01.flac', 1.0)")
    con.execute("PRAGMA user_version = 9")
    con.commit()
    con.close()
    db = LibraryDB.open(path)
    try:
        assert db.favourites() == []
        tables = {row[0] for row in db._con().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        aside = next(name for name in tables if name.startswith("favourites_") and name != "favourites")
        assert db._con().execute(f'SELECT song FROM "{aside}"').fetchall() == [("C:\\Music\\01.flac",)]
    finally:
        db.close()


# -- writes held up by a long look -----------------------------------------------------------------------
@pytest.fixture
def busy_db(tmp_path, monkeypatch):
    """A database whose writers give up waiting quickly, and another connection to hold it."""
    monkeypatch.setattr(library_db, "BUSY_S", 0.2)
    monkeypatch.setattr(library_db, "BUSY_WAITS", (0.2,) * 50, raising=False)
    database = LibraryDB.open(tmp_path / "library.db")
    hog = sqlite3.connect(tmp_path / "library.db", isolation_level=None, check_same_thread=False)
    yield database, hog
    hog.close()
    database.close()


def _hold(hog: sqlite3.Connection, seconds: float) -> threading.Thread:
    hog.execute("BEGIN IMMEDIATE")   # (as a long look's transaction: the JSON index brought across, say)
    thread = threading.Timer(seconds, hog.execute, ("COMMIT",))
    thread.start()
    return thread


def test_a_favourite_set_while_a_long_look_holds_the_database_is_kept(busy_db):
    db, hog = busy_db
    marks = favourites.Favourites(lambda: db)
    held = _hold(hog, 1.0)
    marks.set(["C:\\Music\\01.flac"], True)
    held.join()
    assert marks.flush(10) and db.favourites() == ["C:\\Music\\01.flac"]


def test_a_play_heard_while_a_long_look_holds_the_database_is_counted(busy_db):
    db, hog = busy_db
    index = SimpleNamespace(db=db)
    held = _hold(hog, 1.0)
    ui._store_play(index, "C:\\Music\\01.flac", 200.0, 200.0)
    held.join()
    assert db.most_played() == [("C:\\Music\\01.flac", 1)]


# -- files that can't be read just now ---------------------------------------------------------------------
def test_a_file_that_cant_be_read_just_now_keeps_its_tags_and_is_tried_again_quietly(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 2})
    look = Indexer(db, reader=Reader())
    look.check([root])
    song = root / "A" / "01.flac"
    song.write_bytes(song.read_bytes() + b"x")   # changed, and then held by another program
    look.reader = Reader(locked={"01.flac"})
    look.check([root])
    stored = db.album_tracks(root / "A", check=False)
    assert [(t.title, t.artist) for t in stored] == [("01", "Band"), ("02", "Band")]
    generation = db.generation
    look.reader = Reader(locked={"01.flac"})
    diff = look.check([root])    # still held: tried again, quietly
    assert diff.nothing and db.generation == generation and look.reader.read == [song]
    look.reader = Reader()
    look.check([root])           # free again: read
    assert rows(db)[str(song)][1] == song.stat().st_mtime_ns

"""indexer.py: the library's only writer — an unreachable library folder changes nothing and makes
nothing new; a cancelled first look carries on where it stopped (equal to one that wasn't
stopped); one changed folder is all that's read; CUE folders are read whole; a real pool of
worker processes reads tiny FLACs (and loads neither Tk nor the player); prioritise; changing the
library folders stops the old look first; the query thread (latest wins, interrupting); the
watcher's quick looks at a copy in progress and its one thread; plays counted from a worker; Load
all from the database; Settings → Library; and the thread rule — no Tk call off Tk's thread
while the indexer, the query thread and the watcher run."""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fakework import FakeWork
from tagging import write_vorbis
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

from ironbark import indexer, library, library_db, library_index, settings, watcher
from ironbark.engine import State
from ironbark.indexer import AlbumsChanged, Diff, Failed, Finished, Indexer, Progress, Queries, Started, Unreachable
from ironbark.library import Track
from ironbark.library_db import LibraryDB


# -- helpers -------------------------------------------------------------------------------------------
class Reader:
    """Stands in for library.read_track: a track from the file's name alone (nothing is opened),
    every read noted."""

    def __init__(self, then=None):
        self.read: list[Path] = []
        self.then = then   # called after each read with the reader (e.g. to cancel part-way)

    def __call__(self, path):
        path = Path(path)
        self.read.append(path)
        if self.then is not None:
            self.then(self)
        return Track(path=path, title=path.stem, artist="Band", album=path.parent.name, track_no=int(path.stem[:2]),
                     codec="FLAC", sample_rate=44100, bits=16, channels=2, size=path.stat().st_size)


def tree(root: Path, albums: dict[str, int]) -> Path:
    """Album folders of empty .flac files (Reader doesn't open them): {"A": 3} → A/01.flac … A/03.flac."""
    for name, count in albums.items():
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        for n in range(1, count + 1):
            (folder / f"{n:02d}.flac").write_bytes(b"x" * n)
    return root


def rows(db: LibraryDB) -> dict[str, tuple]:
    """Every stored file: {path: (size, modified ns, added, seen, missing?)}."""
    return {os.path.join(root, rel, name) if rel else os.path.join(root, name): (size, mtime, added, seen, missing)
            for root, rel, name, size, mtime, added, seen, missing in db._con().execute(
                "SELECT r.path, d.rel, f.name, f.size, f.mtime_ns, f.added, f.seen, f.missing_since IS NOT NULL"
                " FROM files f JOIN folders d ON d.id = f.folder_id JOIN roots r ON r.id = d.root_id")}


def drain(events) -> list:
    found = []
    while not events.empty():
        found.append(events.get())
    return found


@pytest.fixture
def db(tmp_path):
    database = LibraryDB.open(tmp_path / "library.db")
    yield database
    database.close()


def flac(path: Path, frames: int = 441, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros((frames, 2), np.int16), 44100, subtype="PCM_16", format="FLAC")
    if tags:
        write_vorbis(path, **tags)
    return path


# -- an unreachable library folder ---------------------------------------------------------------------
def test_an_unplugged_library_folder_changes_nothing_and_nothing_comes_back_new(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 3, "B": 2, "B/CD2": 1})
    look = Indexer(db, reader=Reader())
    events = look.listen()
    assert len(look.check([root]).added) == 6 and db.built(root)
    since = time.time()
    before = rows(db)
    away = root.with_name("Music (unplugged)")
    root.rename(away)
    diff = look.check([root])
    assert diff.error == f"Can't see the library folder {root}" and diff.nothing
    assert rows(db) == before and db.counts()["missing"] == 0
    assert any(isinstance(e, Unreachable) and e.root == str(root) for e in drain(events))
    root.mkdir()                                  # what's left when a drive unmounts: an empty folder
    diff = look.check([root])
    assert diff.error == f"The library folder {root} is empty" and rows(db) == before
    root.rmdir()
    away.rename(root)                             # plugged in again
    diff = look.check([root])
    assert diff.nothing and not diff.error and rows(db) == before
    assert db.new_albums(since - 1, {}) == []     # nothing comes back "new"


def test_a_fresh_look_forgets_what_a_normal_one_keeps_and_reads_everything_again(tmp_path, db):
    """Library → ⋯ → Rebuild the library: a library folder that's gone (or emptied) keeps its music
    on a normal look, never on a fresh one; what's there is read again, as on a first look, with
    plays and favourites kept and nothing coming back "new"."""
    music = tree(tmp_path / "Music", {"A": 2})
    old = tree(tmp_path / "Old", {"B": 2})
    reader = Reader()
    look = Indexer(db, reader=reader)
    events = look.listen()
    look.check([music, old])
    key = str(music / "A" / "01.flac")
    db.record_play(key, 300, 300)
    db.set_favourite(key)
    since = time.time()
    shutil.rmtree(old)
    old.mkdir()                                   # (emptied: on a normal look, an unplugged drive's mount point)
    assert look.check([music, old]).error and len(rows(db)) == 4
    reader.read.clear()
    drain(events)
    diff = look.check([music, old], fresh=True)
    assert sorted(rows(db)) == [key, str(music / "A" / "02.flac")]
    assert sorted(reader.read) == [music / "A" / "01.flac", music / "A" / "02.flac"]   # (read again)
    assert sorted(diff.added) == [music / "A" / "01.flac", music / "A" / "02.flac"]
    assert db.built(music) and db.play_counts([key]) == {key: 1} and db.favourites() == [key]
    assert db.new_albums(since - 1, {}) == []
    assert any(isinstance(e, AlbumsChanged) and not e.album_ids for e in drain(events))


def test_a_folder_that_cant_be_listed_keeps_everything_and_a_vanished_one_comes_back_with_its_dates(
        tmp_path, db, monkeypatch):
    root = tree(tmp_path / "Music", {"A": 2, "B": 2})
    look = Indexer(db, reader=Reader())
    look.check([root])
    before = rows(db)
    real = os.scandir
    locked = str(root / "A")

    def scandir(path="."):
        if os.fspath(path) == locked:
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    diff = look.check([root])
    assert diff.nothing and rows(db) == before and not db.folders(root)[locked].listed_ok
    monkeypatch.setattr(os, "scandir", real)
    assert look.check([root]).nothing and db.folders(root)[locked].listed_ok and rows(db) == before
    shutil.move(root / "B", tmp_path / "B moved away")        # a folder gone from its parent's listing
    diff = look.check([root])
    assert sorted(p.name for p in diff.removed) == ["01.flac", "02.flac"] and db.counts()["missing"] == 2
    assert [a.name for a in db.albums()] == ["A"]
    shutil.move(tmp_path / "B moved away", root / "B")        # and back: shown again with its old dates
    reader = look.reader = Reader()
    diff = look.check([root])
    assert diff.nothing and reader.read == [] and rows(db) == before and len(db.albums()) == 2


def refuse(monkeypatch, folder: Path, error: OSError):
    """os.scandir fails for this one folder with `error` (the indexer's walk lists through it)."""
    real = os.scandir

    def scandir(path="."):
        if os.fspath(path) == str(folder):
            raise error
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


def test_an_access_denied_folder_doesnt_hold_up_the_first_look_but_a_dropped_one_does(tmp_path, db, monkeypatch):
    """A folder nobody may list is the same on every look: the first look still counts as
    finished, so new music can be spotted. One that failed another way (a network drive dropping
    out) waits for a look that goes cleanly, or its music would all look new once it's read."""
    real_scandir = os.scandir
    root = tree(tmp_path / "Music", {"A": 2, "Private": 1})
    refuse(monkeypatch, root / "Private", PermissionError(13, "Access is denied", str(root / "Private")))
    look = Indexer(db, reader=Reader())
    assert len(look.check([root]).added) == 2 and db.built(root)
    since = time.time()
    time.sleep(0.01)
    tree(root, {"New": 1})
    look.check([root])
    assert db.new_albums(since, {}) == [root / "New"]

    other = LibraryDB.open(tmp_path / "other.db")
    refuse(monkeypatch, root / "Private", OSError(64, "The specified network name is no longer available"))
    look = Indexer(other, reader=Reader())
    look.check([root])
    assert not other.built(root)
    monkeypatch.setattr(os, "scandir", real_scandir)   # (the drive is back)
    look.check([root])
    assert other.built(root) and other.new_albums(since - 60, {}) == []
    other.close()


def test_a_library_folder_inside_another_is_looked_at_once(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 2, "Classical/B": 2})
    reader = Reader()
    look = Indexer(db, reader=reader)
    diff = look.check([root, root / "Classical"])
    assert len(diff.added) == 4 and len(reader.read) == 4 and db.roots() == [str(root)]
    assert sorted(a.name for a in db.albums()) == ["A", "B"]
    assert look.update([root / "Classical", root], [root / "Classical" / "B" / "01.flac"]).changed \
        == [root / "Classical" / "B" / "01.flac"]


def test_no_library_folders_change_nothing(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 2})
    look = Indexer(db, reader=Reader())
    look.check([root])
    assert look.check([]).error and db.roots() == [str(root)] and db.counts()["files"] == 2


def test_a_damaged_database_is_noted_to_be_put_right_at_the_next_start(tmp_path, db, monkeypatch):
    root = tree(tmp_path / "Music", {"A": 2})
    look = Indexer(db, reader=Reader())
    events = look.listen()

    def damaged(result, now=None):
        error = sqlite3.DatabaseError("database disk image is malformed")
        error.sqlite_errorcode = 11   # SQLITE_CORRUPT, as SQLite's own error carries it
        raise error

    monkeypatch.setattr(db, "apply", damaged)
    diff = look.check([root])
    assert "repaired when the player next starts" in diff.error
    assert any(isinstance(e, Failed) and "repaired" in e.text for e in drain(events))
    assert (tmp_path / "library.db.check").exists()


def test_a_look_brings_search_texts_folded_another_way_up_to_date(tmp_path, db):
    root = tree(tmp_path / "Music", {"Bjork": 2})
    look = Indexer(db, reader=Reader())
    look.check([root])
    with db._writing():   # (as if folded by an older searchfold)
        db._con().execute("UPDATE meta SET value = 0 WHERE key = 'fold_version'")
        db._con().execute("UPDATE tracks SET haystack = ''")
    assert db.needs_refold
    look.check([root])
    assert not db.needs_refold and all(row[0] for row in db._con().execute("SELECT haystack FROM tracks"))


# -- cancel and carry on ---------------------------------------------------------------------------------
def test_a_cancelled_first_look_carries_on_and_ends_equal_to_one_that_wasnt(tmp_path):
    root = tree(tmp_path / "Music", {f"Album {n:02d}": 3 for n in range(12)})

    def cancel_after_ten(reader):
        if len(reader.read) == 10:
            look.cancel()

    stopped = LibraryDB.open(tmp_path / "stopped.db")
    reader = Reader(then=cancel_after_ten)
    look = Indexer(stopped, reader=reader)
    first = look.check([root])
    assert first.cancelled and 0 < len(stopped.albums()) < 12 and not stopped.built(root)
    assert len(first.added) == 12   # the folder in hand when it was asked to stop was finished (4 albums)
    reader.then = None
    second = look.check([root])     # resumed: only what wasn't stored yet is read
    assert not second.cancelled and stopped.built(root) and len(reader.read) == 36
    assert len(first.added) + len(second.added) == 36
    whole = LibraryDB.open(tmp_path / "whole.db")
    Indexer(whole, reader=Reader()).check([root])
    assert stopped.albums() == whole.albums()
    assert {p: v[2] for p, v in rows(stopped).items()} == {p: v[2] for p, v in rows(whole).items()}   # added dates
    stopped.close()
    whole.close()


def test_a_cancel_while_walking_stores_nothing(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 1, "B": 1})
    look = Indexer(db, reader=Reader())
    real = indexer._list

    def listing(folder):
        look.cancel()
        return real(folder)

    indexer._list = listing
    try:
        diff = look.check([root])
    finally:
        indexer._list = real
    assert diff.cancelled and diff.nothing and db.counts()["files"] == 0 and not db.built(root)


# -- only what changed is read ------------------------------------------------------------------------
def test_one_changed_folder_is_all_that_is_read(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 3, "B": 3, "C": 2})
    reader = Reader()
    look = Indexer(db, reader=reader)
    look.check([root])
    assert len(reader.read) == 8
    reader.read.clear()
    assert look.check([root]).nothing and reader.read == []
    changed = root / "B" / "02.flac"
    changed.write_bytes(b"retagged")
    (root / "C" / "03.flac").write_bytes(b"new")
    (root / "A" / "01.flac").unlink()
    diff = look.check([root])
    assert sorted(reader.read) == [changed, root / "C" / "03.flac"]   # just those two files
    assert diff.changed == [changed] and diff.added == [root / "C" / "03.flac"] and diff.removed == [root / "A" / "01.flac"]
    assert [a.track_count for a in db.albums()] == [2, 3, 3]


@pytest.mark.skipif(os.name != "nt", reason="Windows' folder names ignore case")
def test_a_library_folder_asked_for_in_other_letters_is_the_same_one(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 2, "B": 1})
    reader = Reader()
    look = Indexer(db, reader=reader)
    look.check([root])
    before = rows(db)
    reader.read.clear()
    other = Path(str(root).upper())
    diff = look.check([other])                    # (as a folder chosen again might be typed)
    assert diff.nothing and reader.read == [] and rows(db) == before and db.roots() == [str(root)]
    assert look.update([other], [other / "A" / "01.flac"]).changed == [root / "A" / "01.flac"]


def test_a_folder_of_cue_sheets_alone_stores_nothing(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 1})
    (root / "A" / "Archive").mkdir()
    (root / "A" / "Archive" / "Old rip.cue").write_text('FILE "x.wav" WAVE\n', encoding="utf-8")
    look = Indexer(db, reader=Reader())
    look.check([root])
    generation = db.generation
    assert set(db.folders(root)) == {str(root / "A")}
    assert look.check([root]).nothing and db.generation == generation   # nothing written on a quiet look


def test_start_looks_on_a_thread_of_its_own(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 2})
    look = Indexer(db, reader=Reader())
    events = look.listen()
    thread = look.start([root], reason="start-up")
    assert thread.name == "library-index" and look.start([root]) in (thread, look._thread)
    thread.join(10)
    found = drain(events)
    assert isinstance(found[0], Started) and found[0].reason == "start-up" and isinstance(found[-1], Finished)
    assert len(look.last_diff.added) == 2 and look.looked > 0


def test_a_tag_edit_update_rereads_just_those_files_even_unchanged(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 3})
    reader = Reader()
    look = Indexer(db, reader=reader)
    look.check([root])
    reader.read.clear()
    events = look.listen()
    diff = look.update([root], [root / "A" / "02.flac"])
    assert reader.read == [root / "A" / "02.flac"] and diff.changed == [root / "A" / "02.flac"]
    found = drain(events)
    assert any(isinstance(e, AlbumsChanged) for e in found) and isinstance(found[-1], Finished)


def test_files_read_by_an_older_reader_are_read_again_quietly(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 2})
    reader = Reader()
    look = Indexer(db, reader=reader)
    look.check([root])
    reader.read.clear()
    with db._writing():   # as if stored by the reader before this one
        db._con().execute("UPDATE files SET read_format = ?", (library_db.READ_FORMAT - 1,))
    diff = look.check([root])
    assert len(reader.read) == 2 and diff.nothing                     # read again, but nothing to report
    assert all(s.read_format == library_db.READ_FORMAT for s in next(iter(db.file_states(
        [db.folders(root)[str(root / "A")].id]).values())).values())


# -- CUE folders ---------------------------------------------------------------------------------------------
def cue_image(folder: Path, tracks: int = 3) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    sf.write(folder / "Image.flac", np.zeros((44100 * 2 * tracks, 2), np.int16), 44100, subtype="PCM_16")
    lines = ['FILE "Image.flac" WAVE']
    for n in range(1, tracks + 1):
        lines += [f"  TRACK {n:02d} AUDIO", f'    TITLE "Part {n}"', f"    INDEX 01 00:{2 * (n - 1):02d}:00"]
    (folder / "Image.cue").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder / "Image.flac"


def test_cue_folders_are_read_whole_and_only_when_they_change(tmp_path, db):
    root = tmp_path / "Music"
    image = cue_image(root / "Live")
    flac(root / "Live" / "bonus.flac", title="Bonus")    # (separate files + .cue: listed as files)
    flac(root / "Other" / "01.flac", title="Other")
    read: list[Path] = []
    look = Indexer(db, reader=lambda p: read.append(Path(p)) or library.read_track(p))
    look.check([root])
    assert db.album_tracks(root / "Live") == library.scan_album(root / "Live")
    read.clear()
    flac(root / "Other" / "02.flac", title="Other two")  # another folder changes: the CUE folder isn't read
    look.check([root])
    assert read == [root / "Other" / "02.flac"]
    read.clear()
    (root / "Live" / "bonus.flac").unlink()             # now every music file is the image: it splits
    look.check([root])
    assert read == [image]                                # (the whole folder: what's left of it)
    assert [t.title for t in db.album_tracks(root / "Live")] == ["Part 1", "Part 2", "Part 3"]
    read.clear()
    sheet = root / "Live" / "Image.cue"
    sheet.write_text(sheet.read_text(encoding="utf-8").replace('"Part 2"', '"Deux"'), encoding="utf-8")
    os.utime(sheet, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    diff = look.check([root])
    assert read == [image] and diff.nothing             # a changed sheet: the folder again, no file changed
    assert [t.title for t in db.album_tracks(root / "Live")] == ["Part 1", "Deux", "Part 3"]
    read.clear()
    sheet.unlink()                                        # the sheet goes: it held CUE tracks, so read whole
    look.check([root])
    assert read == [image] and [t.title for t in db.album_tracks(root / "Live")] == ["Image"]


# -- worker processes ------------------------------------------------------------------------------------
def test_a_real_pool_of_two_workers_reads_fifty_tiny_flacs(tmp_path):
    root = tmp_path / "Music"
    for a in range(10):
        for n in range(1, 6):
            flac(root / f"Album {a}" / f"{n:02d} Song.flac", title=f"Song {a}.{n}", album=f"Album {a}",
                 artist="Band", tracknumber=str(n))
    with LibraryDB.open(tmp_path / "pool.db") as pooled, LibraryDB.open(tmp_path / "here.db") as here:
        look = Indexer(pooled, pool_min=0, fast=2, gentle=2)
        diff = look.check([root])
        assert look.used_pool and len(diff.added) == 50 and not diff.error
        Indexer(here, pool_min=10_000).check([root])      # the same, read in this process
        assert pooled.albums() == here.albums() and len(pooled.albums()) == 10
        for album in pooled.albums():
            assert pooled.album_tracks(album.path) == here.album_tracks(album.path) == library.scan_album(album.path)
    pool = indexer.make_pool(1)
    try:
        loaded = pool.submit(indexer.worker_check).result(timeout=120)
    finally:
        pool.shutdown()
    assert loaded == {"pid": loaded["pid"], "tkinter": False, "player": False} and loaded["pid"] != os.getpid()


def test_without_worker_processes_the_reading_is_done_here(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 3})
    look = Indexer(db, reader=Reader(), pool_min=0, pool_factory=lambda workers: None)
    assert len(look.check([root]).added) == 3 and not look.used_pool


def test_a_pool_that_breaks_carries_on_here(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 2, "B": 2})

    class Broken:
        def submit(self, *args):
            from concurrent.futures import Future
            from concurrent.futures.process import BrokenProcessPool
            future = Future()
            future.set_exception(BrokenProcessPool("a worker died"))
            return future

        def shutdown(self, **kwargs):
            pass

    look = Indexer(db, reader=Reader(), pool_min=0, pool_factory=lambda workers: Broken())
    assert len(look.check([root]).added) == 4 and len(db.albums()) == 2


def test_worker_processes_that_never_answer_are_given_up_and_the_reading_carries_on_here(tmp_path, db):
    """(Seen on Windows 10: a big library's first look that never got anywhere.) Workers that
    hand nothing back — they never started, or hung — are let go after `stall` seconds without an
    answer, and every folder is read in this process instead."""
    root = tree(tmp_path / "Music", {"A": 2, "B": 2, "C": 1})

    class Silent:
        shut = False

        def submit(self, *args):
            from concurrent.futures import Future
            return Future()   # (never finishes)

        def shutdown(self, **kwargs):
            Silent.shut = True

    look = Indexer(db, reader=Reader(), pool_min=0, pool_factory=lambda workers: Silent(), stall=0.5)
    began = time.monotonic()
    diff = look.check([root])
    assert len(diff.added) == 5 and len(db.albums()) == 3 and Silent.shut
    assert time.monotonic() - began < 10

# -- prioritise, events, failures ---------------------------------------------------------------------
def test_an_album_asked_for_is_read_next(tmp_path, db):
    root = tree(tmp_path / "Music", {f"{n:02d}": 1 for n in range(10)})

    def ask_on_first(reader):
        if len(reader.read) == 1:
            look.prioritise(root / "07")

    reader = Reader(then=ask_on_first)
    look = Indexer(db, reader=reader)
    look.check([root])
    assert [p.parent.name for p in reader.read[:3]] == ["00", "07", "01"]


def test_events_say_what_a_look_did(tmp_path, db):
    root = tree(tmp_path / "Music", {"A": 2, "B": 1})
    look = Indexer(db, reader=Reader())
    events = look.listen()
    look.check([root], reason="test")
    found = drain(events)
    assert isinstance(found[0], Started) and found[0].reason == "test" and found[0].roots == (str(root),)
    assert any(isinstance(e, Progress) and e.phase == "reading" for e in found)
    changed = set().union(*(e.album_ids for e in found if isinstance(e, AlbumsChanged)))
    assert len(changed) == 2 and isinstance(found[-1], Finished) and len(found[-1].diff.added) == 3
    assert look.progress == Progress("", 3, 3)               # idle: what the last look read
    look.unlisten(events)
    look.check([root])
    assert events.empty()


def test_a_database_that_cant_be_written_is_said_and_the_next_look_carries_on(tmp_path, db, monkeypatch):
    root = tree(tmp_path / "Music", {"A": 2})
    look = Indexer(db, reader=Reader())
    events = look.listen()

    def full(result, now=None):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(db, "apply", full)
    diff = look.check([root])
    assert "disk is full" in diff.error and any(isinstance(e, Failed) for e in drain(events))
    monkeypatch.undo()
    assert len(look.check([root]).added) == 2


def test_changing_the_library_folders_stops_the_old_look_first(tmp_path, monkeypatch):
    """A long first look at the old folder is under way when the folders change: it stops, the
    old folder's rows go, and nothing of it comes back afterwards."""
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    old = tree(tmp_path / "Old", {f"{n:02d}": 4 for n in range(20)})
    new = tree(tmp_path / "New", {"X": 2})
    slow = Reader(then=lambda reader: time.sleep(0.01))
    index = library_index.LibraryIndex(old)
    index.indexer.reader = slow
    looking = threading.Thread(target=index.refresh)
    looking.start()
    deadline = time.monotonic() + 20
    while len(slow.read) < 5 and time.monotonic() < deadline:
        time.sleep(0.005)
    index.set_roots([new])                          # (as Settings → Library does)
    index.refresh()
    looking.join(20)
    assert not looking.is_alive()
    assert index.db.roots() == [str(new)] and [a.name for a in index.db.albums()] == ["X"]
    assert not index.db.folders(old) and index.db.counts()["files"] == 2
    library_index.close_shared()


# -- the query thread ---------------------------------------------------------------------------------------
def test_queries_answer_in_order_and_the_newest_ask_wins(tmp_path, db):
    queries = Queries(db)
    gate = threading.Event()
    first = queries.ask("albums", gate.wait, 10)
    deadline = time.monotonic() + 10
    while queries._running != "albums" and time.monotonic() < deadline:   # (started: it won't be replaced)
        time.sleep(0.005)
    skipped = queries.ask("albums", lambda: "middle")
    last = queries.ask("albums", lambda: "last")
    other = queries.ask("other", lambda: 42)
    gate.set()
    found = []
    deadline = time.monotonic() + 10
    while len(found) < 3 and time.monotonic() < deadline:
        found += queries.drain()
        time.sleep(0.01)
    assert [(a.kind, a.ask) for a in found] == [("albums", first), ("albums", last), ("other", other)]
    assert skipped not in [a.ask for a in found] and found[1].value == "last" and found[2].value == 42
    broken = queries.ask("albums", lambda: 1 / 0)
    deadline = time.monotonic() + 10
    while not (found := queries.drain()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert found[0].ask == broken and isinstance(found[0].error, ZeroDivisionError)
    queries.stop()
    queries.join(5)


def test_a_search_typed_over_is_interrupted(tmp_path, db):
    asked = threading.Event()

    def long_search():
        asked.set()
        try:
            return db._con().execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c"
                                     " WHERE x < 1000000000) SELECT count(*) FROM c").fetchone()
        except sqlite3.OperationalError as error:
            raise library_db.Interrupted() from error

    queries = Queries(db)
    queries.ask("search", long_search)
    assert asked.wait(10)
    time.sleep(0.2)   # (well into the statement)
    started = time.monotonic()
    newest = queries.ask("search", lambda: "fresh", interrupt=True)
    found = []
    while not found and time.monotonic() < started + 10:
        found = queries.drain()
        time.sleep(0.005)
    assert [(a.ask, a.value) for a in found] == [(newest, "fresh")] and time.monotonic() - started < 5
    queries.stop()


@pytest.mark.parametrize("kind", ["search", "albums"])
def test_an_interrupt_that_lands_on_the_newest_ask_runs_it_again(tmp_path, db, kind):
    """The search typed over finishes just as the interrupt meant for it is sent, so it lands on
    the ask after: that one is still the newest of its kind, so it runs again rather than being
    dropped (a search box that never shows the last search) or answered with an error."""
    queries = Queries(db)
    gate, started = threading.Event(), threading.Event()
    runs = []

    def newest():
        runs.append(1)
        if len(runs) > 1:
            return "fresh"
        started.set()
        return db._con().execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c"
                                 " WHERE x < 1000000000) SELECT count(*) FROM c").fetchone()

    real = db.interrupt

    def late(thread):   # (the one it was meant for ends first; it reaches the next ask's statement)
        gate.set()
        assert started.wait(10)
        time.sleep(0.2)
        real(thread)

    db.interrupt = late
    queries.ask("search", gate.wait, 10)
    deadline = time.monotonic() + 10
    while queries._running != "search" and time.monotonic() < deadline:
        time.sleep(0.005)
    if kind == "search":
        number = queries.ask("search", newest, interrupt=True)
    else:   # (another kind's ask was waiting behind the search: the interrupt reaches it instead)
        number = queries.ask("albums", newest)
        queries.ask("search", lambda: "typed over", interrupt=True)
    found = []
    while not any(a.ask == number for a in found) and time.monotonic() < deadline + 10:
        found += queries.drain()
        time.sleep(0.01)
    answer = next(a for a in found if a.ask == number)
    assert answer.value == "fresh" and answer.error is None and len(runs) == 2
    queries.stop()
    queries.join(5)


# -- the watcher ----------------------------------------------------------------------------------------------
class GrowingIndex:
    """Stands in for the library index: a file that keeps growing (a download into the library)."""

    def __init__(self, root: Path):
        self.roots = [root]
        self.root = root
        self.built = 1.0
        self.file = root / "Downloading" / "01.flac"
        self.calls: list[tuple[float, object]] = []

    def refresh(self, hold=None, folders=None):
        self.calls.append((time.monotonic(), folders))
        return Diff(waiting=[self.file])


def test_reports_carry_the_new_albums_and_a_quiet_look_works_nothing_out(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    root = tmp_path / "Music"
    flac(root / "Old" / "01.flac", title="Old")
    index = library_index.LibraryIndex(root)
    look = watcher.Watcher(lambda: index, settle_s=0)
    look.check()
    look.since = time.time()
    time.sleep(0.01)
    flac(root / "New" / "01.flac", title="New")
    report = look.check()
    assert [folder for folder, _seen in report.new] == [root / "New"]
    assert report.album_folders == {str(root / "Old"), str(root / "New")}
    quiet = look.check()
    assert quiet.diff.nothing and quiet.new is None and quiet.album_folders is None
    assert watcher.still_new(report.new, {str(root / "New"): time.time() + 1}) == []   # opened since
    library_index.close_shared()


# -- the player's side: plays, Load all, Settings → Library -----------------------------------------------
def test_a_song_heard_long_enough_is_counted_as_a_play_from_a_worker(tmp_path, monkeypatch):
    from ironbark.ui import PlayerWindow

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    index = library_index.LibraryIndex(tmp_path / "Music")
    one, two = Track(path=tmp_path / "Music" / "1.flac", title="One", duration=100.0), \
        Track(path=tmp_path / "Music" / "2.flac", title="Two", duration=100.0)
    statuses = []
    player = SimpleNamespace(status=lambda: statuses.pop(0))
    w = SimpleNamespace(player=player, _listening=None, _index=lambda: index, work=FakeWork())
    for name in ("_count_listening", "_record_play"):
        setattr(w, name, getattr(PlayerWindow, name).__get__(w))

    def play(track, positions, state=State.PLAYING):
        for position in positions:
            statuses.append(SimpleNamespace(track=track, state=state, position=float(position)))
            w._count_listening()

    play(one, range(0, 61))                      # 60 s of a 100 s song: counts
    play(two, [0, 1, 80, 81])                    # a seek to near the end: 2 s heard, doesn't count
    play(two, [81], state=State.STOPPED)         # stopped
    deadline = time.monotonic() + 10
    while index.db.play_counts([str(one.path)])[str(one.path)] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert index.db.play_counts([str(one.path), str(two.path)]) == {str(one.path): 1, str(two.path): 0}
    assert w._listening is None
    library_index.close_shared()


def test_load_all_takes_the_songs_from_the_database_without_looking_at_each_file(tmp_path, monkeypatch):
    from ironbark.ui import PlayerWindow

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    root = tmp_path / "Music"
    for a in ("A", "B"):
        for n in (1, 2):
            flac(root / a / f"0{n}.flac", title=f"{a}{n}", album=a, tracknumber=str(n))
    index = library_index.LibraryIndex(root)
    index.refresh()
    w = SimpleNamespace(_index=lambda: index, _scan=lambda folder, whole_tree=False: pytest.fail("read from disk"))
    for name in ("_library_albums", "_library_tracks"):
        setattr(w, name, getattr(PlayerWindow, name).__get__(w))
    looked = []
    real = os.stat
    monkeypatch.setattr(os, "stat", lambda path, *a, **k: looked.append(path) or real(path, *a, **k))
    albums = w._library_albums(root)
    tracks = w._library_tracks(albums)
    assert [t.title for t in tracks] == ["A1", "A2", "B1", "B2"]
    assert not [p for p in looked if str(p).endswith(".flac")]
    library_index.close_shared()


def test_settings_lists_the_library_folders(make_window, tmp_path):  # noqa: F811  (the fixture)
    window, root, _player, errors = make_window()

    def shown():
        return [(row.winfo_children()[0].cget("text"), row.winfo_children()[1].text_value)
                for row in window.library_rows.winfo_children()]

    first = tmp_path / "Music"
    assert shown() == [(str(first) if len(str(first)) <= 30 else "…" + str(first)[-29:], "Change…")]
    other = tmp_path / "More"
    other.mkdir()
    window._set_library_folders([first, other])
    _pump(root, 0.3)
    assert [button for _text, button in shown()] == ["Remove", "Remove"]
    assert window.prefs["library_folders"] == [str(first), str(other)] and window.prefs["library_folder"] == str(first)
    assert window._index().roots == [first, other]
    from ironbark.popups import Prompt

    root.after(100, lambda: Prompt.stack[-1]._chosen(0))   # it asks first: Remove
    window._remove_library_folder(other)
    _pump(root, 0.3)
    assert [button for _text, button in shown()] == ["Change…"] and window._index().roots == [first]
    assert "Took" in window._notice and not errors


# -- the thread rule ----------------------------------------------------------------------------------------
def test_the_thread_rule_catches_a_tk_call_off_tks_thread(tk_thread_rule):
    import tkinter

    worker = threading.Thread(target=lambda: tkinter._stringify("x"), name="breaks-the-rule")
    worker.start()
    worker.join()
    assert [name for name, _what in tk_thread_rule] == ["breaks-the-rule"]
    tk_thread_rule.clear()


def test_no_tk_call_off_tks_thread_while_the_library_is_built_and_rescanned(tk_thread_rule, tmp_path, monkeypatch):
    import gc
    import tkinter as tk

    from ironbark import theme
    from ironbark.library_view import LibraryView

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    music = tmp_path / "Music"
    for a in range(6):
        for n in (1, 2):
            flac(music / f"Album {a}" / f"0{n}.flac", title=f"Song {n}", album=f"Album {a}", artist="Band")
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    root.withdraw()
    theme.apply(root)
    try:
        window = LibraryView(root, music, None, lambda p: None, lambda p: None, lambda r, q: None)
        deadline = time.monotonic() + 30
        while (window._refresh is not None or window._loading or len(window.albums) < 6) \
                and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        flac(music / "New" / "01.flac", title="New", album="New")
        window.rescan()                                                   # (0.38.0: one look, when asked)
        while len(window.albums) < 7 and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert len(window.albums) == 7
        window.close()
        root.update()
    finally:
        root.destroy()
        del root
        gc.collect()   # on this thread (Tk objects freed on another thread abort Python)
    library_index.close_shared()
    assert tk_thread_rule == []

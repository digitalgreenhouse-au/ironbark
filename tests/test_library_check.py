"""The whole-library quality check (library_check.py): what needs a look and how it's worded, results
kept in the library database (and kept across a rebuild of its library tables), the checker's runs
— only new or changed files read again, pause / hold / stop, carrying on after a restart, songs
gone from the library forgotten — in worker processes too, a worker that dies on a file, and the
stored results Now Playing and the clipping guard use."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from ironbark import library_check, library_db
from ironbark.analysis import Analysis, analyse
from ironbark.formats import FormatFlags, HdcdResult, MqaResult, PreEmphasis
from ironbark.indexer import Indexer
from ironbark.library import Track
from ironbark.library_check import Checker, Song
from ironbark.library_db import LibraryDB

RATE = 44100


def noise(seconds, amp=0.25, seed=1):
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal((int(seconds * RATE), 2))).clip(-0.99, 0.99)


def lowpass(data, cutoff):
    spectrum = np.fft.rfft(data, axis=0)
    spectrum[np.fft.rfftfreq(len(data), 1 / RATE) > cutoff] = 0
    return np.fft.irfft(spectrum, n=len(data), axis=0)


def flac(path: Path, data, subtype="PCM_16") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, data, RATE, format="FLAC", subtype=subtype)
    return path


@pytest.fixture
def music(tmp_path):
    """A library of five songs: one fine, one padded, one clipped, one from a lossy source, one
    damaged (cut off half-way)."""
    root = tmp_path / "Music"
    flac(root / "Fine" / "01 Fine.flac", noise(3, seed=1))
    flac(root / "Padded" / "01 Padded.flac", np.round(noise(3, seed=2) * 32767) / 32768, subtype="PCM_24")
    clipped = noise(3, amp=0.1, seed=3)
    clipped[RATE: RATE + 5] = 1.0
    flac(root / "Clipped" / "01 Clipped.flac", clipped)
    flac(root / "Lossy" / "01 Lossy.flac", lowpass(noise(6, seed=4), 16000))
    damaged = flac(root / "Damaged" / "01 Damaged.flac", noise(12, seed=5))
    data = damaged.read_bytes()
    damaged.write_bytes(data[: len(data) * 3 // 4])
    return root


@pytest.fixture
def db(tmp_path, music):
    database = LibraryDB.open(tmp_path / "library.db", [music])
    Indexer(database).check([music])
    yield database
    database.close()


def run(checker: Checker) -> None:
    checker.kick()
    assert checker.idle(60)


def song_named(db: LibraryDB, stem: str) -> Track:
    return next(track for track, *_rest in db.check_songs() if track.path.stem.endswith(stem))


# -- what needs a look ---------------------------------------------------------------------------------
def test_findings_worst_first_and_their_words(tmp_path):
    result = Analysis(md5_ok=False, dr=8, peak_dbfs=0.0, rate=44100, bits=24, bits_used=16, cutoff_hz=16000.0,
                      cutoff_sharp=True, clip_runs=3, damaged_at=133.4,
                      flags=FormatFlags(hdcd=HdcdResult(detected=True), mqa=MqaResult(detected=True),
                                        pre_emphasis=PreEmphasis(True, ["its cue sheet"])))
    words = library_check.findings(result, misnamed=".mp3")
    assert words == ["damaged", "lossy", "padded", "clipping", "misnamed", "hdcd", "mqa", "pre_emphasis"]
    track = Track(Path("x.mp3"), "x", codec="FLAC", misnamed=".mp3")
    text = library_check.describe(words, result, track)
    assert text.startswith("Damaged: stops decoding at 2:13 · Suggests a lossy source (sound stops sharply at "
                           "16.0 kHz) · Only 16 of 24 bits used: 16-bit stored as 24-bit · Clipped when mastered "
                           "(3 runs stuck at full scale) · Really FLAC, but named .mp3 · HDCD-encoded")
    assert "fake" not in text.lower()   # clues, never accusations
    assert library_check.findings(Analysis(md5_ok=False, dr=None, peak_dbfs=0.0)) == ["md5"]
    assert library_check.findings(Analysis(md5_ok=True, dr=10, peak_dbfs=-1.0, lossy=True, cutoff_hz=16000.0,
                                           cutoff_sharp=True)) == []   # an MP3 trims the top by design
    assert library_check.findings(Analysis(md5_ok=None, dr=10, peak_dbfs=-1.0, rate=96000, cutoff_hz=22000.0,
                                           cutoff_sharp=True)) == ["upsampled"]
    assert library_check.summary({"clipping": 30, "damaged": 2, "hdcd": 1, "md5": 1}) == (
        "2 damaged · 1 MD5 mismatch · 30 clipped · 1 HDCD")


def test_results_go_to_json_and_back():
    result = Analysis(md5_ok=None, dr=12, peak_dbfs=float("-inf"), lufs=-14.2, true_peak=1.05,
                      dc_offset=(0.001, -0.002), flags=FormatFlags(hdcd=None, mqa=MqaResult(encoder="x"),
                                                                    pre_emphasis=PreEmphasis(True, ["a .cue"])))
    back = library_check.from_json(library_check.to_json(result))
    assert back == result and back.dc_offset == (0.001, -0.002) and back.flags.pre_emphasis.sources == ["a .cue"]
    newer = library_check.to_json(result).replace('"dr":12', '"dr":12,"something_new":1')
    assert library_check.from_json(newer).dr == 12            # a newer version's extra field is ignored
    assert library_check.from_json("") is None and library_check.from_json("{broken") is None


def test_estimates_and_their_words():
    hour = [Song("k", Path("x.flac"), 16, 0, 3600.0, 44100)]
    assert library_check.estimate(hour, workers=2, speed=40) == pytest.approx(45.0)
    assert library_check.estimate([Song("k", Path("x.flac"), 24, 0, 3600.0, 96000)], workers=1, speed=40) == (
        pytest.approx(3600 * 96000 / 44100 / 40))
    assert library_check.duration_text(30) == "about 1 minute"
    assert library_check.duration_text(40 * 60) == "about 40 minutes"
    assert library_check.duration_text(100 * 60) == "about 1 hour 40 minutes"
    assert library_check.duration_text(120 * 60) == "about 2 hours"
    assert library_check.duration_text(13 * 3600) == "about 13 hours"
    assert library_check.music_text(49.2 * 3600) == "49.2 hours" and library_check.music_text(600) == "10 minutes"


# -- kept in the database -------------------------------------------------------------------------------
def test_checks_are_kept_by_song_and_survive_a_rebuild_of_the_library_tables(tmp_path):
    path = tmp_path / "library.db"
    with LibraryDB.open(path) as db:
        assert db._con().execute("PRAGMA user_version").fetchone()[0] == library_db.SCHEMA_VERSION == 2
        db.save_check("C:/a.flac#2", 10, 20, 1, "clipping", 1.2, "{}", when=5.0)
        assert db.check_of("C:/a.flac#2") == (10, 20, 1, 5.0, "clipping", 1.2, "{}")
        assert db.flagged_checks() == [("C:/a.flac#2", "clipping", 5.0, "{}", 10, 20)]
        db._con().execute("PRAGMA user_version = 99")       # a version with no way up: library tables remade
    with LibraryDB.open(path) as db:
        assert db.check_states() == {"C:/a.flac#2": (10, 20, 1)}
        assert db.forget_checks(["C:/a.flac#2", "C:/other.flac"]) == 1 and db.check_states() == {}


def test_a_version_1_database_gains_the_checks_table(tmp_path):
    path = tmp_path / "library.db"
    with LibraryDB.open(path) as db:
        db.record_play("x.flac", 300, 300)
        con = db._con()
        con.execute("DROP TABLE checks")
        con.execute("PRAGMA user_version = 1")
    with LibraryDB.open(path) as db:
        assert db._con().execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.play_counts(["x.flac"]) == {"x.flac": 1}
        db.save_check("x.flac", 1, 2, 1, "", None, "")
        assert db.check_states() == {"x.flac": (1, 2, 1)}


def test_checks_are_kept_while_the_library_is_read_again(tmp_path, music):
    """The checks outlive the library tables (a damaged database set aside, a version with no way up):
    a run starting before the library has been read again must not take every check for a song
    gone — they took hours to make."""
    path = tmp_path / "library.db"
    with LibraryDB.open(path, [music]) as db:
        Indexer(db).check([music])
        assert db.built(music)
        checker = Checker(lambda: db, in_process=True)
        checker.start()
        assert checker.idle(60)
        checker.close()
        stored = db.check_states()
        assert len(stored) == 5
        db._con().execute("PRAGMA user_version = 99")     # (the library tables are made again)
    with LibraryDB.open(path, [music]) as db:
        assert db.counts()["tracks"] == 0 and not db.built(music) and db.check_states() == stored
        found = library_check.plan(db)                     # (a run before the library is read again)
        assert found.gone == [] and found.total == 0
        Indexer(db).check([music])
        again = library_check.plan(db)
        assert again.gone == [] and again.done == 5 and again.count == 0   # nothing to read again
        # A song in no library folder (Now Playing's check of it, or a folder taken out of the library)
        # keeps its check, as it keeps its plays and favourites; one gone from a library folder doesn't.
        db.save_check(os.fspath(tmp_path / "Elsewhere" / "x.flac"), 1, 2, 1, "", None, "")
        db.save_check(os.fspath(music / "Gone" / "x.flac"), 1, 2, 1, "", None, "")
        assert library_check.plan(db).gone == [os.fspath(music / "Gone" / "x.flac")]


def test_the_check_reads_light_rows_and_the_estimate_holds_no_songs(db, tmp_path):
    """Working out a run (every run's start, every new album) reads only what the check needs of each
    song, one at a time — a big library's whole Tracks cost hundreds of megabytes; and the question's
    estimate only counts. The light rows say the same as whole Tracks, CUE tracks included."""
    import synthlib

    from ironbark import audiofile

    counted = library_check.plan(db, keep=False)
    full = library_check.plan(db)
    assert counted.songs == [] and counted.count == full.count == len(full.songs) == 5
    assert counted.total == full.total and counted.work == pytest.approx(sum(song.work for song in full.songs))
    assert library_check.estimate(counted.work, workers=2) == pytest.approx(library_check.estimate(full.songs, 2))
    assert all(isinstance(song.path, str) and song.path is song.key for song in full.songs)   # (no Path each)
    root = Path("C:/Music")
    with LibraryDB.open(tmp_path / "synth.db", [root]) as synth:
        synthlib.load(synth, root, synthlib.make_library(600, root, seed=3))
        light = list(synth.check_list())
        whole = synth.check_songs()
        assert len(light) == len(whole) == 600
        cues = 0
        for (path, bits, md5, duration, rate, misnamed, unsupported, size, mtime, missing), (track, *rest) in zip(
                light, whole):
            assert audiofile.key(path) == audiofile.key(track.path) and os.fspath(path) == os.fspath(track.path)
            assert audiofile.cue_of(path) == audiofile.cue_of(track.path)
            cues += audiofile.cue_of(path) is not None
            assert (bits, md5, duration, rate, misnamed, unsupported) == (
                track.bits, track.md5, track.duration, track.sample_rate, track.misnamed, track.unsupported)
            assert [size, mtime, missing] == rest
        assert cues


def test_the_upgrade_keeps_the_library_as_it_was(tmp_path, music):
    """Moving a version 1 file up adds the checks table and nothing else: the songs, albums,
    favourites and search index stay, so a big library (a NAS) is never read again for it."""
    path = tmp_path / "library.db"
    with LibraryDB.open(path, [music]) as db:
        Indexer(db).check([music])
        db.set_favourite(os.fspath(music / "Fine" / "01 Fine.flac"), True)
    with LibraryDB.open(path) as db:
        before = (db.counts(), db.generation, db.albums())
        con = db._con()
        con.execute("DROP TABLE checks")
        con.execute("PRAGMA user_version = 1")
    with LibraryDB.open(path) as db:
        assert db._con().execute("PRAGMA user_version").fetchone()[0] == 2
        assert (db.counts(), db.generation, db.albums()) == before and not db.needs_refold
        assert db.favourites() == [os.fspath(music / "Fine" / "01 Fine.flac")]
        assert [h.track.path.stem for h in db.search_songs("padded").rows] == ["01 Padded"]


# -- the checker -------------------------------------------------------------------------------------------
def test_a_run_checks_every_song_and_needs_a_look_lists_what_it_found(db, music):
    states = []
    checker = Checker(lambda: db, on_state=states.append, in_process=True)
    checker.start()
    assert checker.idle(60) and states == ["on"]
    status = checker.status
    assert (status.done, status.total, status.running, status.error) == (5, 5, False, "") and status.finished
    found = {hit.track.path.stem: look for hit, look in library_check.needs_look(db)}
    assert sorted(found) == ["01 Clipped", "01 Damaged", "01 Lossy", "01 Padded"]
    assert found["01 Damaged"].words == ("damaged",) and found["01 Damaged"].text.startswith("Damaged: stops decoding")
    assert found["01 Padded"].words == ("padded",) and "16-bit stored as 24-bit" in found["01 Padded"].text
    assert "clipping" in found["01 Clipped"].words and "lossy" in found["01 Lossy"].words
    ordered = sorted(library_check.needs_look(db), key=library_check.worst_first)
    assert ordered[0][0].track.path.stem == "01 Damaged"
    assert library_check.summary(library_check.counts(found.values())).startswith("1 damaged · 1 lossy-source clue")
    # Run again: nothing changed, nothing read.
    seen = []
    real = library_check.check_song
    library_check.check_song = lambda song, cancel=None: seen.append(song.key) or real(song, cancel)
    try:
        checker.kick()
        assert checker.idle(30) and seen == []
        fine = song_named(db, "Fine").path
        flac(fine, noise(3, seed=9) * 0.5)          # the file changes (and the library notices)…
        Indexer(db).check([music])
        checker.kick()
        assert checker.idle(30) and seen == [os.fspath(fine)]   # …so only it is read again
    finally:
        library_check.check_song = real


def test_stored_results_for_now_playing_and_the_clipping_guard(db):
    checker = Checker(lambda: db, in_process=True)
    checker.start()
    assert checker.idle(60)
    clipped = song_named(db, "Clipped").path
    result, when = library_check.stored(db, clipped)
    assert result.clip_runs == 2 and abs(when - time.time()) < 120
    assert library_check.stored_peak(db, clipped) == pytest.approx(result.true_peak)
    stat = os.stat(clipped)
    os.utime(clipped, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))   # changed since: not used
    assert library_check.stored(db, clipped) is None and library_check.stored_peak(db, clipped) is None
    # A check made elsewhere (Now Playing's) is kept the same way.
    track = song_named(db, "Clipped")
    fresh = analyse(clipped, 16, 0)
    stat = os.stat(clipped)
    library_check.remember(db, track, fresh, (stat.st_size, stat.st_mtime_ns))
    assert library_check.stored(db, clipped)[0].clip_runs == 2
    assert db.check_of(clipped)[4] == "clipping"


def test_a_damaged_read_leaves_no_peak_for_the_clipping_guard(db):
    """A check that stopped at damage measured only the part before it, and the player plays on
    past the damage: its peak would let the guard (and volume levelling) turn up too far, so none
    is kept — the guard measures for itself."""
    checker = Checker(lambda: db, in_process=True)
    checker.start()
    assert checker.idle(60)
    damaged = song_named(db, "Damaged")
    row = db.check_of(damaged.path)
    assert row[4] == "damaged" and row[5] is None
    assert library_check.stored_peak(db, damaged.path) is None
    partial = library_check.stored(db, damaged.path)[0]
    assert partial.damaged_at is not None and partial.true_peak       # (a peak was measured, of the start)
    stat = os.stat(damaged.path)
    library_check.remember(db, damaged, partial, (stat.st_size, stat.st_mtime_ns))   # (Now Playing's check)
    assert db.check_of(damaged.path)[5] is None and library_check.stored_peak(db, damaged.path) is None
    # One stored by an earlier build with its peak: still not used.
    db.save_check(damaged.path, stat.st_size, stat.st_mtime_ns, library_check.CHECK_FORMAT, "damaged", 0.2, "")
    assert library_check.stored_peak(db, damaged.path) is None


def test_a_song_checked_under_two_spellings_of_its_path_is_listed_once(db):
    """Now Playing keeps its check by the path it played from; a playlist may write the library's
    "C:\\…" as "c:\\…". Needs a look lists the song once, by its own key's check — and one checked
    only the other way is listed too (it used to stop the whole view)."""
    checker = Checker(lambda: db, in_process=True)
    checker.start()
    assert checker.idle(60)

    def other(path) -> str:
        key = os.fspath(path)
        return key[0].swapcase() + key[1:]

    clipped, fine = song_named(db, "Clipped").path, song_named(db, "Fine").path
    stat = os.stat(clipped)
    db.save_check(other(clipped), stat.st_size, stat.st_mtime_ns, library_check.CHECK_FORMAT, "lossy", None, "",
                  when=time.time() + 60)
    listed = library_check.needs_look(db)
    assert sorted(hit.track.path.stem for hit, _look in listed) == ["01 Clipped", "01 Damaged", "01 Lossy",
                                                                    "01 Padded"]
    assert next(look for hit, look in listed if hit.track.path == clipped).words == ("clipping",)
    assert [hit.track.path.stem for hit, _look in listed] == [
        hit.track.path.stem for hit, _look in sorted(listed, key=library_check.worst_first)]   # worst first
    db.forget_checks([os.fspath(fine)])         # (Fine checked only the other way: listed by that check)
    stat = os.stat(fine)
    db.save_check(other(fine), stat.st_size, stat.st_mtime_ns, library_check.CHECK_FORMAT, "padded", None, "")
    found = {hit.track.path.stem: look.words for hit, look in library_check.needs_look(db)}
    assert found["01 Fine"] == ("padded",) and len(found) == 5


def test_pause_hold_stop_and_carrying_on_after_a_restart(db, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real = library_check.check_song

    def slow(song, cancel=None):      # each song waits until let go (or stopped)
        started.set()
        while not release.is_set():
            if cancel is not None and cancel.is_set():
                return library_check.Outcome("stopped")
            time.sleep(0.01)
        return real(song, cancel)

    monkeypatch.setattr(library_check, "check_song", slow)
    states = []
    checker = Checker(lambda: db, on_state=states.append, in_process=True)
    checker.start()
    assert started.wait(10)
    checker.pause()                               # the song in hand stops part-way; nothing stored for it
    assert checker.idle(10) and checker.state == "paused" and db.check_states() == {}
    checker.kick()                                # (paused: a kick does nothing)
    assert checker.idle(5) and db.check_states() == {}
    release.set()
    checker.resume()
    assert checker.idle(60) and len(db.check_states()) == 5
    assert states == ["on", "paused", "on"]
    # A hold stops a run and carries on when let go.
    release.clear()
    started.clear()
    db.forget_checks(list(db.check_states()))
    checker.kick()
    assert started.wait(10)
    checker.hold("abx")
    assert checker.idle(10) and checker.status.held and db.check_states() == {}
    release.set()
    checker.hold("abx", False)
    assert checker.idle(60) and not checker.status.held and len(db.check_states()) == 5
    checker.stop()
    assert states[-1] == "off" and checker.state == "off"
    # Restart: a checker that starts "on" carries on from what's stored (here: everything).
    later = Checker(lambda: db, state="on", in_process=True)
    later.kick()
    assert later.idle(30) and (later.status.done, later.status.total) == (5, 5)
    assert Checker(lambda: db, state="nonsense").state == "off"


def test_songs_gone_from_the_library_are_forgotten_and_unreachable_files_wait(db, music):
    checker = Checker(lambda: db, in_process=True)
    checker.start()
    assert checker.idle(60) and len(db.check_states()) == 5
    gone = song_named(db, "Lossy").path
    os.remove(gone)
    Indexer(db).check([music])                   # marked missing: its check stays for now
    checker.kick()
    assert checker.idle(30) and os.fspath(gone) in db.check_states() and checker.status.total == 4
    db.purge_missing(before=time.time() + 1)     # gone for good: so is its check
    checker.kick()
    assert checker.idle(30) and os.fspath(gone) not in db.check_states()
    # A file that can't be opened just now (in use, a drive asleep) isn't damage: tried again later.
    song = Song("k", music / "nowhere.flac", 16, 0, 1.0, RATE)
    assert library_check.check_song(song).state == "gone"


def test_a_failing_database_is_said_and_the_next_kick_tries_again(db):
    calls = []

    def broken():
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    checker = Checker(broken, in_process=True)
    checker.start()
    assert checker.idle(10) and checker.status.error == "database is locked"
    checker.db_for = lambda: db
    checker.kick()
    assert checker.idle(60) and checker.status.error == "" and checker.status.done == 5


class _CrashingPool:
    """Stands in for the worker processes: one file "crashes its worker" every time."""

    def __init__(self, crash_stem: str):
        self.crash_stem = crash_stem

    def submit(self, fn, song):
        future = Future()
        if Path(song.path).stem.endswith(self.crash_stem):
            future.set_exception(BrokenProcessPool("a worker died"))
        else:
            future.set_result(library_check.check_song(song))
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def test_a_file_that_kills_its_worker_twice_is_kept_as_damaged(db):
    made = []

    def pools(workers):
        made.append(workers)
        return _CrashingPool("Fine"), threading.Event()

    checker = Checker(lambda: db, workers=1, pool_factory=pools)
    checker.start()
    assert checker.idle(60) and checker.status.error == ""
    assert len(made) == library_check.CRASHES + 1          # new workers after each death
    fine = next(look for hit, look in library_check.needs_look(db) if hit.track.path.stem == "01 Fine")
    assert fine.words == ("damaged",) and fine.text == "Damaged: can't be decoded from the start"


def test_worker_processes_check_the_songs(db):
    """The real thing: spawned worker processes at below-normal priority (they take a moment to start)."""
    checker = Checker(lambda: db, workers=2)
    checker.start()
    assert checker.idle(120), checker.status
    assert checker.status.error == "" and len(db.check_states()) == 5
    assert {hit.track.path.stem for hit, _look in library_check.needs_look(db)} == {
        "01 Clipped", "01 Damaged", "01 Lossy", "01 Padded"}


@pytest.mark.perf
def test_planning_and_listing_a_big_library_is_quick(tmp_path):
    """50,000 songs: working out what's left to check (the start of every run, and the question's
    estimate), and Needs a look listing 5,000 flagged songs — both on worker threads, never Tk's."""
    import synthlib

    root = Path("C:/Music")
    files = synthlib.make_library(50_000, root, seed=7)
    with LibraryDB.open(tmp_path / "library.db", [root]) as db:
        synthlib.load(db, root, files)
        started = time.perf_counter()
        found = library_check.plan(db)
        took = time.perf_counter() - started
        print(f"\nplan of {found.total:,} songs: {took * 1000:.0f} ms")
        assert found.total >= 49_000 and took < 5.0
        result = library_check.to_json(Analysis(md5_ok=True, dr=9, peak_dbfs=0.0, clip_runs=3))
        for song in found.songs[:5000]:
            db.save_check(song.key, 1, 1, library_check.CHECK_FORMAT, "clipping", 1.1, result)
        started = time.perf_counter()
        listed = library_check.needs_look(db)
        took = time.perf_counter() - started
        print(f"needs_look of {len(listed):,} songs: {took * 1000:.0f} ms")
        assert len(listed) == 5000 and took < 5.0


def test_the_choice_is_a_setting_checked_on_load():
    from ironbark import settings

    assert settings.DEFAULTS["library_check"] == "off"
    assert settings._checked({"library_check": "paused"}) == {"library_check": "paused"}
    assert settings._checked({"library_check": "sometimes"}) == {}
    assert settings._checked({"library_check": 1}) == {}

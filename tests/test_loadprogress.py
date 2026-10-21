"""The status line while a tab loads (loadprogress.py): the words, the throttle, and the scans
telling it how far they have got."""

from __future__ import annotations

from pathlib import Path

from test_library import make_flac

from ironbark import library, loadprogress
from ironbark.library import scan_album, scan_folder


def test_the_words_say_what_is_being_done():
    assert loadprogress.opening_text("NAS Music") == "Opening “NAS Music”…"
    assert loadprogress.loading_text("NAS Music", library.LOOKING, 0, 0) == "Looking for music in “NAS Music”…"
    assert loadprogress.loading_text("NAS Music", library.LOOKING, 1, 0) == "Looking for music in “NAS Music”… 1 music file found"
    assert loadprogress.loading_text("NAS Music", library.LOOKING, 1200, 0) == "Looking for music in “NAS Music”… 1,200 music files found"
    assert loadprogress.loading_text("NAS Music", library.READING, 12, 40) == "Reading “NAS Music”… 12 of 40 songs (30%)"
    assert loadprogress.loading_text("NAS Music", library.READING, 40, 40) == "Reading “NAS Music”… 40 of 40 songs (100%)"
    assert loadprogress.loading_text("NAS Music", library.READING, 0, 0) == "Reading “NAS Music”…"
    assert loadprogress.album_text("Blue", 2, 12) == "Reading album 3 of 12: “Blue”…"


def test_a_library_look_says_how_far_it_has_got_and_for_how_long():
    """A big library's first look takes minutes: each stage is named, with counts and the time so
    far (it ticks every second, so it's plain nothing has frozen)."""
    from ironbark.indexer import Progress

    text = loadprogress.library_text
    assert text(Progress("looking", 0, 0, "", 100.0), 100.4) == "Building the library: looking through your folders… · 0:00"
    assert (text(Progress("looking", 1234, 0, "D:/Music/A", 100.0), 142.0)
            == "Building the library: looking through your folders… 1,234 folders · 0:42")
    assert (text(Progress("reading", 5000, 80000, "", 100.0), 290.0)
            == "Building the library: reading songs… 5,000 of 80,000 files (6%) · 3:10")
    assert (text(Progress("reading", 1, 2, "", 0.0), 3725.0, "Rebuilding the library")
            == "Rebuilding the library: reading songs… 1 of 2 files (50%)")   # (no start time: no clock)
    assert text(Progress("upgrading", 3, 4, "", 10.0), 11.0).startswith("Building the library: bringing the old")
    assert text(Progress("tidying", 0, 0, "", 10.0), 3735.0) == "Building the library: tidying up… · 1:02:05"
    assert text(Progress(), 5.0) == "Building the library…"   # (not started yet)


def test_a_long_read_says_about_how_long_is_left():
    """A very large library's first read can take hours: once it's well under way (20 s of reading
    and 1% done) the line says roughly how long the rest will take."""
    from ironbark.indexer import Progress

    assert loadprogress.left_text(20) == "less than a minute left"
    assert loadprogress.left_text(12 * 60 + 10) == "about 12 min left"
    assert loadprogress.left_text(80 * 60) == "about 1 h 20 min left"
    assert loadprogress.left_text(120 * 60) == "about 2 h left"
    text = loadprogress.library_text
    early = Progress("reading", 500, 100_000, "", 0.0, 100.0)
    assert "left" not in text(early, 110.0)                        # (10 s of reading: too soon to say)
    assert "left" not in text(Progress("reading", 500, 100_000, "", 0.0, 100.0), 200.0)   # (0.5%: too soon)
    going = Progress("reading", 5_000, 100_000, "", 50.0, 100.0)  # 5,000 read in 100 s: 95,000 more ≈ 32 min
    assert text(going, 200.0) == ("Building the library: reading songs… 5,000 of 100,000 files (5%) · 2:30 · "
                                  "about 32 min left")


def test_the_reporter_throttles_but_never_drops_the_end_of_a_stage():
    now = [0.0]
    said = []
    reporter = loadprogress.Reporter("Folder", said.append, every=0.1, clock=lambda: now[0])
    reporter(library.LOOKING, 0, 0)          # the first word goes at once
    reporter(library.LOOKING, 5, 0)          # too soon: dropped
    now[0] = 0.05
    reporter(library.LOOKING, 9, 0)          # still too soon
    now[0] = 0.11
    reporter(library.LOOKING, 20, 0)         # late enough
    reporter(library.READING, 0, 20)         # a new stage goes at once
    reporter(library.READING, 1, 20)         # too soon
    reporter(library.READING, 20, 20)        # the end of the stage always goes
    assert said == ["Looking for music in “Folder”…", "Looking for music in “Folder”… 20 music files found",
                    "Reading “Folder”… 0 of 20 songs (0%)", "Reading “Folder”… 20 of 20 songs (100%)"]
    assert reporter.text == said[-1]
    reporter.say("Opening…")                 # a line of its own, at once; the same line again isn't repeated
    reporter.say("Opening…")
    assert said[-1] == "Opening…" and said.count("Opening…") == 1


def test_the_scans_report_looking_then_every_file_read(tmp_path: Path):
    album = tmp_path / "Album"
    for n in (1, 2, 3):
        make_flac(album / f"0{n} Song.flac", tags={"title": f"Song {n}", "tracknumber": str(n)})
    make_flac(album / "Disc 2" / "01 More.flac", tags={"title": "More", "tracknumber": "1"})
    (album / "notes.txt").write_text("not music")

    calls = []
    tracks = scan_folder(album, lambda stage, done, total: calls.append((stage, done, total)))
    assert [t.title for t in tracks] == ["Song 1", "Song 2", "Song 3", "More"]
    assert calls[0] == (library.LOOKING, 0, 0)
    looking = [c for c in calls if c[0] == library.LOOKING]
    assert looking[-1] == (library.LOOKING, 4, 0)                        # the files found, as the walk went
    reading = [c for c in calls if c[0] == library.READING]
    assert reading == [(library.READING, n, 4) for n in range(5)]        # before each file, and the end

    calls.clear()
    tracks = scan_album(album, lambda stage, done, total: calls.append((stage, done, total)))
    assert len(tracks) == 4 and calls[0] == (library.LOOKING, 0, 0)
    assert (library.LOOKING, 4, 0) in calls and calls[-1] == (library.READING, 4, 4)

    assert [t.title for t in scan_folder(album)] == ["Song 1", "Song 2", "Song 3", "More"]   # (no reporter: as before)

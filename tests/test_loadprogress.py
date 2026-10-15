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

"""Lyrics: LRC parsing (several stamps on a line, offsets, bad lines, enhanced word stamps), every
embedded form (ID3 SYLT in milliseconds and MPEG frames, syllables joined into lines; ID3 USLT;
Vorbis LYRICS / UNSYNCEDLYRICS in FLAC and Ogg; MP4 ©lyr), the files beside the song and which
wins, and the window: the line being sung lights up, and "No lyrics found" says where it looked."""
import gc
import struct
import time
import tkinter as tk
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from mp4_maker import write_m4a
from tagging import write_id3, write_vorbis

from ironbark import audiofile, lyrics, tagio, theme
from ironbark.library import Track
from ironbark.lyrics import Line, Lyrics, parse_lrc
from ironbark.tagio import id3, mp4

RNG = np.random.default_rng(31)


# -- LRC --------------------------------------------------------------------------------------------
def test_lrc_lines_and_times():
    found = parse_lrc("[ti:Song]\n[ar:Singer]\n[00:12.00]First line\n[00:17.20]Second line\n[01:02.345]Third\n")
    assert found.synced and [(l.time, l.text) for l in found.lines] == [
        (12.0, "First line"), (17.2, "Second line"), (pytest.approx(62.345), "Third")]
    assert found.line_at(0) == -1 and found.line_at(12.0) == 0 and found.line_at(17.19) == 0
    assert found.line_at(17.2) == 1 and found.line_at(600) == 2


def test_several_stamps_on_one_line_and_order():
    found = parse_lrc("[00:45.10][00:21.10]Chorus\n[00:30.00]Verse\n[00:05]Intro\n")
    assert [(l.time, l.text) for l in found.lines] == [(5.0, "Intro"), (21.1, "Chorus"), (30.0, "Verse"),
                                                       (45.1, "Chorus")]


@pytest.mark.parametrize("offset,first", [("+500", 9.5), ("-250", 10.25), ("0", 10.0), ("+20000", 0.0)])
def test_offset_moves_every_line_plus_is_sooner(offset, first):
    found = parse_lrc(f"[offset:{offset}]\n[00:10.00]a\n[00:20.00]b\n")
    assert found.lines[0].time == pytest.approx(first)


def test_bad_lines_are_left_out():
    text = ("no stamp here\n[xx:yy]not a stamp\n[00:61.00]sixty-one seconds is fine\n[offset:soon]\n"
            "[00:01.00]   spaced   out   \n[1:2:3:4]odd\n\n[00:02.00]\n")
    found = parse_lrc(text)
    assert [(l.time, l.text) for l in found.lines] == [(1.0, "spaced out"), (2.0, ""), (61.0, "sixty-one seconds is fine")]
    assert parse_lrc("just words\nmore words") is None and parse_lrc("") is None


def test_enhanced_word_stamps_are_taken_out():
    found = parse_lrc("[00:00.00] <00:00.04> When <00:00.16> the <00:00.82> truth\n")
    assert found.lines[0].text == "When the truth"


def test_plain_text_and_auto_detection():
    found = lyrics.from_text("Line one\r\nLine two\r\n\r\nVerse two\n", "x")
    assert not found.synced and [l.text for l in found.lines] == ["Line one", "Line two", "", "Verse two"]
    assert found.line_at(30) == -1
    assert lyrics.from_text("[00:01.00]synced").synced
    assert lyrics.from_text("   \n  ") is None


# -- embedded --------------------------------------------------------------------------------------------
def noise(frames=20000):
    return (RNG.standard_normal((frames, 2)) * 3000).astype(np.int16)


def mp3(tmp_path, name="a.mp3"):
    path = tmp_path / name
    sf.write(path, noise() / 32768, 44100, format="MP3")
    return path


def sylt_body(entries, encoding=3, stamp_format=2, content=1, desc="", lang=b"eng"):
    """A SYLT frame body, laid out as id3.org's "Native Frames" §4.9 says."""
    def text(s):
        if encoding == 1:
            return b"\xff\xfe" + s.encode("utf-16-le") + b"\x00\x00"
        return s.encode("utf-8" if encoding == 3 else "latin-1") + b"\x00"
    body = bytes([encoding]) + lang + bytes([stamp_format, content]) + text(desc)
    for words, stamp in entries:
        body += text(words) + struct.pack(">I", stamp)
    return body


def test_sylt_syllables_joined_into_lines(tmp_path):
    # The spec's own example, syllable by syllable, a newline starting the second line.
    entries = [("Strang", 1000), ("ers", 1300), (" in", 1600), (" the", 1800), (" night", 2000),
               ("\nEx", 4000), ("chang", 4300), ("ing", 4500), (" glan", 4800), ("ces", 5000)]
    path = write_id3(mp3(tmp_path), [id3.Frame("SYLT", sylt_body(entries), source=4)])
    found = lyrics.embedded(path)
    assert found.synced and found.source == "the file's tags (ID3 SYLT)"
    assert [(l.time, l.text) for l in found.lines] == [(1.0, "Strangers in the night"), (4.0, "Exchanging glances")]


def test_sylt_whole_lines_utf16_v23_and_mpeg_frames(tmp_path):
    entries = [("Première ligne", 38), ("Second line", 77)]   # MPEG frames: 1152 samples each
    path = write_id3(mp3(tmp_path), [id3.Frame("SYLT", sylt_body(entries, encoding=1, stamp_format=1), source=3)],
                     version=3)
    found = lyrics.embedded(path)
    assert [l.text for l in found.lines] == ["Première ligne", "Second line"]
    assert found.lines[0].time == pytest.approx(38 * 1152 / 44100)


def test_sylt_is_written_back_exactly_as_read(tmp_path):
    body = sylt_body([("la", 10), ("la", 20)])
    path = write_id3(mp3(tmp_path), [id3.Frame("SYLT", body, source=4), id3.TextFrame("TIT2", ["Song"])])
    audio = tagio.open_file(path, "mp3")
    frame = audio.tags.getall("SYLT")[0]
    assert isinstance(frame, id3.SyncedLyricsFrame) and frame.entries == [("la", 10), ("la", 20)]
    audio.tags.delall("TIT2")
    audio.tags.add(id3.TextFrame("TIT2", ["Renamed"]))
    audio.save(4)
    assert tagio.open_file(path, "mp3").tags.text("TIT2") == ["Renamed"]
    again = tagio.open_file(path, "mp3").tags.getall("SYLT")[0]
    assert again.raw == body


def test_a_cut_short_sylt_keeps_what_it_has():
    body = sylt_body([("one", 100), ("two", 200)])[:-6]   # "two" lost its time stamp
    frame = id3.parse_frame("SYLT", body, 4)
    assert frame.entries == [("one", 100)]


def test_uslt(tmp_path):
    path = write_id3(mp3(tmp_path), [id3.CommentFrame("", ["First\nSecond"], "eng", id="USLT")])
    found = lyrics.embedded(path)
    assert not found.synced and [l.text for l in found.lines] == ["First", "Second"]
    assert found.source == "the file's tags (ID3 USLT)"


@pytest.mark.parametrize("kind,name", [("flac", "LYRICS"), ("flac", "UNSYNCEDLYRICS"), ("ogg", "LYRICS")])
def test_vorbis_comments(tmp_path, kind, name):
    path = tmp_path / f"a.{kind}"
    if kind == "flac":
        sf.write(path, noise(), 44100, subtype="PCM_16")
    else:
        sf.write(path, noise() / 32768, 44100, format="OGG", subtype="VORBIS")
    write_vorbis(path, **{name: "Hello\nWorld"})
    found = lyrics.embedded(path)
    assert [l.text for l in found.lines] == ["Hello", "World"] and name in found.source


def test_lrc_text_in_a_tag_is_synced(tmp_path):
    path = tmp_path / "a.flac"
    sf.write(path, noise(), 44100, subtype="PCM_16")
    write_vorbis(path, LYRICS="[00:01.00]One\n[00:02.00]Two")
    assert lyrics.embedded(path).synced


def test_mp4_lyr(tmp_path):
    path = write_m4a(tmp_path / "a.m4a", noise(), bits=16)
    audio = mp4.MP4File(path)
    audio["\xa9lyr"] = ["Apple\nLossless"]
    audio.save()
    found = lyrics.embedded(path)
    assert [l.text for l in found.lines] == ["Apple", "Lossless"] and "MP4 ©lyr" in found.source


# -- beside the song, and which wins --------------------------------------------------------------------
def test_files_beside_the_song_and_their_order(tmp_path):
    song = tmp_path / "Song.flac"
    sf.write(song, noise(), 44100, subtype="PCM_16")
    assert lyrics.find(song) is None
    where = lyrics.where_looked(song)
    assert "Song.lrc" in where and "Song.txt" in where and "Vorbis LYRICS" in where
    (tmp_path / "Song.txt").write_text("Plain words", encoding="utf-8")
    assert lyrics.find(song).source == "Song.txt beside the song"
    write_vorbis(song, LYRICS="From the tags")
    assert lyrics.find(song).lines[0].text == "From the tags"           # the file's own tags before a .txt
    (tmp_path / "Song.lrc").write_bytes("﻿[00:03.00]Synced ✓\n".encode())
    found = lyrics.find(song)
    assert found.synced and found.lines[0].text == "Synced ✓" and found.source == "Song.lrc beside the song"


def test_a_cue_track_has_no_file_of_its_own(tmp_path):
    image = tmp_path / "Disc.flac"
    sf.write(image, noise(44100 * 4), 44100, subtype="PCM_16")
    (tmp_path / "Disc.lrc").write_text("[00:01.00]the whole disc's", encoding="utf-8")
    track = audiofile._make_cue_path(str(image), audiofile.CueRange(str(tmp_path / "Disc.cue"), 2, 44100, 88200))
    assert lyrics.find(track) is None and "CUE image" in lyrics.where_looked(track)


# -- the window ---------------------------------------------------------------------------------------------
@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    r.withdraw()
    theme.apply(r)
    yield r
    r.destroy()
    del r
    gc.collect()   # here, on the main thread: a Tk interpreter freed by another thread's collection aborts


class FakePlayer:
    def __init__(self, track):
        self.track, self.position = track, 0.0

    def status(self):
        return SimpleNamespace(track=self.track, position=self.position)


SYNCED = Lyrics([Line(1.0, "one"), Line(2.0, "two"), Line(3.0, "three")] +
                [Line(4.0 + k, f"more {k}") for k in range(60)], True, "Song.lrc beside the song")


def wait(root, condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    assert condition()


def test_the_window_lights_up_the_line_being_sung(root, tmp_path):
    from ironbark.lyrics_window import LyricsWindow
    track = Track(tmp_path / "Song.flac", "Song", artist="Singer")
    player = FakePlayer(track)
    asked = []
    prefs = {}
    win = LyricsWindow(root, player, prefs, reader=lambda path: asked.append(path) or SYNCED)
    win.win.deiconify()
    wait(root, lambda: win.view.lyrics is SYNCED)
    assert asked == [track.path]                        # read once, on a worker
    assert "From Song.lrc beside the song · synced" == win.source_label.cget("text")
    player.position = 2.5
    wait(root, lambda: win.view.current == 1)
    text = win.view.text
    assert text.get(*text.tag_ranges("current")) == "two"
    assert text.tag_ranges("sung") and text.index(text.tag_ranges("sung")[1]) == "2.0"
    player.position = 50.0
    wait(root, lambda: win.view.current == SYNCED.line_at(50.0))
    first, last = text.yview()
    assert first > 0.0                                  # scrolled down to keep the line in view
    win.close()


def test_the_window_says_where_it_looked_when_there_are_none(root, tmp_path):
    from ironbark.lyrics_window import LyricsWindow
    song = tmp_path / "Quiet.flac"
    sf.write(song, noise(), 44100, subtype="PCM_16")
    player = FakePlayer(Track(song, "Quiet"))
    win = LyricsWindow(root, player, {})
    wait(root, lambda: "No lyrics found" in win.view.text.get("1.0", "end"))
    shown = win.view.text.get("1.0", "end")
    assert "No lyrics found for this song" in shown and "Quiet.lrc" in shown and "Quiet.txt" in shown
    player.track = None
    wait(root, lambda: win.view.text.get("1.0", "end").strip() == "Nothing playing")
    win.close()


def test_a_slow_read_of_an_earlier_song_doesnt_take_the_place_of_this_ones(root, tmp_path):
    # Song A's read (a sleeping drive, say) finishes after song B's, before the window looks:
    # B's lyrics must still show, not "Looking for lyrics…" for ever.
    import threading

    from ironbark.lyrics_window import LyricsWindow
    a, b = Track(tmp_path / "A.flac", "A"), Track(tmp_path / "B.flac", "B")
    release = {a.path: threading.Event(), b.path: threading.Event()}
    words = {a.path: Lyrics([Line(None, "a words")], False, "A.txt"), b.path: SYNCED}
    started = []

    def reader(path):
        started.append(path)
        release[path].wait(10)
        return words[path]

    player = FakePlayer(a)
    win = LyricsWindow(root, player, {}, reader=reader)
    wait(root, lambda: started == [a.path])
    player.track = b
    wait(root, lambda: started == [a.path, b.path])
    for path in (b.path, a.path):                     # B's read ends first, then A's — no tick between
        release[path].set()
        deadline = time.monotonic() + 5
        while path not in win.follower._found and time.monotonic() < deadline:
            time.sleep(0.01)
    wait(root, lambda: win.view.lyrics is SYNCED)
    win.close()


def test_a_line_break_inside_a_synced_line_keeps_the_lines_in_step(root):
    # (A SYLT syllable can hold a line break part-way through.) One text line per lyric line,
    # or the line lit up would be the wrong one.
    from ironbark.lyrics_window import LyricsView
    view = LyricsView(root)
    view.show(Lyrics([Line(1.0, "one\nstill one"), Line(2.0, "two")], True, "tags"))
    assert view.follow(2.5) == 1
    assert view.text.get(*view.text.tag_ranges("current")) == "two"
    assert view.text.get("1.0", "1.end") == "one still one"
    view.destroy()


from test_light_mini import _pump, make_window  # noqa: E402,F401  (make_window is a fixture)


def test_key_l_opens_one_lyrics_window(make_window):
    window, root, player, errors = make_window()
    found = window.keys.resolve(window.root, "l", 0)  # L is in the key table (keys.py)…
    assert found is not None and found[0].id == "lyrics"
    assert window.keys.run(window.root, "l", 0)       # …and opens the window
    window.show_lyrics()                              # (again: the same window comes forward)
    _pump(root, 0.5)
    lyric_windows = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel) and w.title() == "Lyrics"]
    assert len(lyric_windows) == 1 and window._lyrics.win is lyric_windows[0]
    in_lyrics = window.keys.resolve(window._lyrics.win, "space", 0)   # the shortcuts work in it too
    assert in_lyrics is not None and in_lyrics[0].id == "play"
    window._lyrics.close()
    assert not errors

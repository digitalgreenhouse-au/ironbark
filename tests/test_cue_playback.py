"""CUE sheet playback (0.14.0): one long file + a .cue (or the FLAC's own CUESHEET block) is listed
as its tracks; each track starts and stops on its exact sample; tracks of the same file join
gaplessly; the bit-perfect proof checks each track against its own stretch of the file; a
folder of separate files with a .cue is listed once, as the separate files."""
import pickle
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import sounddevice as sd
import soundfile as sf

from ironbark import analysis, audiofile, dragdrop, engine, formats, library, playlists
from ironbark.audiofile import CuePath, CueRange
from ironbark.engine import BLOCK_FRAMES, Player, _Session
from ironbark.tagio.flac import CueSheet as FlacCueSheet
from ironbark.tagio.flac import CueSheetIndex as CueSheetTrackIndex
from ironbark.tagio.flac import CueSheetTrack, FLACFile
from ironbark.tools import ripcheck
from ironbark.tools.details import describe_analysis

RNG = np.random.default_rng(11)
TIME_INFO = SimpleNamespace(outputBufferDacTime=0.0, currentTime=0.0)
OK = SimpleNamespace(output_underflow=False)
CD = 588  # samples per CD frame at 44.1 kHz

# Three tracks; track 2 has a 2-second pregap (INDEX 00) that stays at the end of track 1.
STARTS = {1: 0, 2: 300 * CD, 3: 520 * CD}   # INDEX 01, in samples
PREGAP_2 = 150 * CD                          # INDEX 00 of track 2
TOTAL = 700 * CD + 123                       # the file ends mid-frame, as real rips can


def cue_text(file_name="Album.flac", extra=""):
    def t(frames):
        return f"{frames // 75 // 60:02d}:{frames // 75 % 60:02d}:{frames % 75:02d}"
    return f"""REM DISCNUMBER 1
PERFORMER "The Band"
TITLE "The Album"
FILE "{file_name}" WAVE
  TRACK 01 AUDIO
    TITLE "One"
    INDEX 01 {t(0)}
  TRACK 02 AUDIO
    TITLE "Two"
    PERFORMER "Guest"
    FLAGS PRE
    INDEX 00 {t(PREGAP_2 // CD)}
    INDEX 01 {t(STARTS[2] // CD)}
  TRACK 03 AUDIO
    TITLE "Three"
    INDEX 01 {t(STARTS[3] // CD)}
{extra}"""


def make_image(folder: Path, name="Album.flac", cue_name="Album.cue", file_in_sheet=None, sheet_extra=""):
    folder.mkdir(parents=True, exist_ok=True)
    data = (RNG.standard_normal((TOTAL, 2)) * 3000).astype(np.int16)
    path = folder / name
    sf.write(path, data, 44100, subtype="PCM_16")
    if cue_name:
        (folder / cue_name).write_text(cue_text(file_in_sheet or name, sheet_extra), encoding="utf-8")
    return path, data


def ranges():
    return [(STARTS[1], STARTS[2]), (STARTS[2], STARTS[3]), (STARTS[3], TOTAL)]


# -- listing ----------------------------------------------------------------------------------
def test_an_image_with_its_cue_is_listed_as_its_tracks(tmp_path):
    path, data = make_image(tmp_path / "Album")
    tracks = library.scan_album(tmp_path / "Album")
    assert [t.title for t in tracks] == ["One", "Two", "Three"]
    assert [t.artist for t in tracks] == ["The Band", "Guest", "The Band"]
    assert {t.album for t in tracks} == {"The Album"} and [t.track_no for t in tracks] == [1, 2, 3]
    assert all(isinstance(t.path, CuePath) and str(t.path) == str(path) for t in tracks)
    assert len({t.path for t in tracks}) == 3            # three different tracks, one file
    for track, (start, end) in zip(tracks, ranges()):
        assert track.duration == pytest.approx((end - start) / 44100)
        assert track.md5 == 0 and "CUE sheet" in dict(track.details)
        with audiofile.open(track.path) as f:              # exactly its stretch of the file
            assert f.frames == end - start
            assert np.array_equal(f.read(dtype="int16"), data[start:end])
    assert library.find_albums(tmp_path)[0].track_count == 3


def test_separate_files_with_a_cue_are_listed_once(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    for n in (1, 2, 3):
        sf.write(album / f"0{n}.flac", np.zeros((CD * 10, 2)), 44100, subtype="PCM_16")
    sheet = "\n".join(f'FILE "0{n}.flac" WAVE\n  TRACK 0{n} AUDIO\n    INDEX 01 00:00:00' for n in (1, 2, 3))
    (album / "Album.cue").write_text(sheet)
    tracks = library.scan_album(album)
    assert [t.path.name for t in tracks] == ["01.flac", "02.flac", "03.flac"]
    assert not any(t.cue for t in tracks)
    assert library.find_albums(tmp_path)[0].track_count == 3


def test_image_plus_separate_files_uses_the_separate_files(tmp_path):
    path, _ = make_image(tmp_path / "Album")
    sf.write(tmp_path / "Album" / "01 One.flac", np.zeros((CD * 10, 2)), 44100, subtype="PCM_16")
    tracks = library.scan_album(tmp_path / "Album")
    assert len(tracks) == 2 and not any(t.cue for t in tracks)  # as before: no duplicates
    assert library.find_albums(tmp_path)[0].track_count == 2


def test_stale_file_names_still_match(tmp_path):
    make_image(tmp_path / "A", name="Album.flac", file_in_sheet="Album.wav")          # converted after ripping
    make_image(tmp_path / "B", name="Renamed.flac", file_in_sheet="CDImage.wav")      # only one file there
    assert [t.title for t in library.scan_album(tmp_path / "A")] == ["One", "Two", "Three"]
    assert [t.title for t in library.scan_album(tmp_path / "B")] == ["One", "Two", "Three"]


def test_a_sheet_that_does_not_fit_the_file_is_ignored(tmp_path):
    folder = tmp_path / "Short"
    folder.mkdir()
    sf.write(folder / "Album.flac", np.zeros((STARTS[2] - 10, 2)), 44100, subtype="PCM_16")  # ends before track 3
    (folder / "Album.cue").write_text(cue_text())
    tracks = library.scan_album(folder)
    assert len(tracks) == 1 and tracks[0].cue is None


def test_data_track_is_skipped(tmp_path):
    make_image(tmp_path / "Enhanced", sheet_extra='FILE "Data.bin" BINARY\n  TRACK 04 MODE1/2352\n    INDEX 01 00:00:00\n')
    assert [t.track_no for t in library.scan_album(tmp_path / "Enhanced")] == [1, 2, 3]


def test_the_flacs_own_cuesheet_block(tmp_path):
    folder = tmp_path / "Embedded"
    path, data = make_image(folder, cue_name=None)
    block = FlacCueSheet()
    block.lead_in_samples, block.compact_disc = 88200, True
    for number in (1, 2, 3):
        entry = CueSheetTrack(number, STARTS[number])
        entry.indexes.append(CueSheetTrackIndex(1, 0))
        block.tracks.append(entry)
    block.tracks.append(CueSheetTrack(170, TOTAL))
    tags = FLACFile(str(path))
    tags.cuesheet = block
    tags["ALBUM"] = "Tagged album"
    tags.save()
    tracks = library.scan_album(folder)
    assert [t.title for t in tracks] == ["Track 1", "Track 2", "Track 3"]
    assert {t.album for t in tracks} == {"Tagged album"}
    assert "own cue sheet" in dict(tracks[1].details)["CUE sheet"]
    with audiofile.open(tracks[2].path) as f:
        assert np.array_equal(f.read(dtype="int16"), data[STARTS[3]:])


def test_read_track_and_playlists_keep_a_cue_track(tmp_path, monkeypatch):
    make_image(tmp_path / "Album")
    tracks = library.scan_album(tmp_path / "Album")
    assert library.read_track(tracks[1].path).title == "Two"
    playlist = tmp_path / "mix.m3u8"
    playlists.write(playlist, [tracks[2], tracks[0]])
    text = playlist.read_text(encoding="utf-8")
    assert "#IRONBARK-CUE:3" in text and text.count(str(tracks[0].path)) == 2  # other players: the whole file
    back = playlists.read(playlist)
    assert back == [tracks[2].path, tracks[0].path]
    assert library.cue_track_path(tracks[0].path, 99) == Path(str(tracks[0].path))


def test_cue_path_identity():
    a = CuePath("C:/m/album.flac", cue=CueRange("C:/m/a.cue", 1, 0, 100))
    b = CuePath("C:/m/album.flac", cue=CueRange("C:/m/a.cue", 2, 100, None))
    plain = Path("C:/m/album.flac")
    assert a != b and a != plain and plain != a and len({a, b, plain}) == 3
    assert pickle.loads(pickle.dumps(a)) == a and type(a.parent) is type(plain)
    assert audiofile.key(b).endswith("album.flac#2") and audiofile.key(plain) == str(plain)
    assert audiofile.display_name(b) == "album.flac · track 2"


def test_dropped_cue_opens_its_album(tmp_path):
    make_image(tmp_path / "Album")
    assert dragdrop.cue_folder(tmp_path / "Album" / "Album.cue") == tmp_path / "Album"
    archive = tmp_path / "Album" / "Archive"
    archive.mkdir()
    (archive / "Old.cue").write_text(cue_text("Stale name.wav"))
    assert dragdrop.cue_folder(archive / "Old.cue") == tmp_path / "Album"  # the music is a level up
    lonely = tmp_path / "Lonely"
    lonely.mkdir()
    (lonely / "x.cue").write_text(cue_text("gone.flac"))
    assert dragdrop.cue_folder(lonely / "x.cue") == lonely


def test_rip_check_and_pre_emphasis_look_at_the_right_thing(tmp_path):
    path, _ = make_image(tmp_path / "Album")
    tracks = library.scan_album(tmp_path / "Album")
    assert ripcheck.disc_files(tracks[0].path) == [path]  # the disc is the whole image
    assert not formats.pre_emphasis(tracks[0].path).flagged
    assert formats.pre_emphasis(tracks[1].path).flagged  # FLAGS PRE is on track 2 only


def test_check_and_album_figures_per_cue_track(tmp_path):
    path, data = make_image(tmp_path / "Album")
    tracks = library.scan_album(tmp_path / "Album")
    result = analysis.analyse(tracks[1].path, 16, tracks[1].md5)
    assert result.duration == pytest.approx((STARTS[3] - STARTS[2]) / 44100) and result.md5_ok is None
    assert describe_analysis(result, "FLAC", cue=True)[0][1].startswith("Not per track")
    peak = np.abs(data[STARTS[2]:STARTS[3]].astype(np.float64)).max() / 32768
    assert result.peak_dbfs == pytest.approx(20 * np.log10(peak), abs=1e-6)
    from ironbark.tools.meters import album_files
    _, files = album_files(tracks[1].path)
    assert files[0] == tracks[1].path and set(files) == {t.path for t in tracks}  # no whole-file double count


# -- playback ---------------------------------------------------------------------------------
class FakeStream:
    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.latency, self.device = 0.1, 0

    def start(self):
        pass

    stop = abort = close = start


@pytest.fixture
def fake_card(monkeypatch):
    monkeypatch.setattr(engine.sd, "OutputStream", FakeStream)
    monkeypatch.setattr(engine.devices, "find_device", lambda name: None)
    monkeypatch.setattr(engine.devices, "mixer_rate", lambda index: None)


def pull(session, limit=400):
    out = []
    for _ in range(limit):
        deadline = time.monotonic() + 2
        while ((session._seek_request is not None or (session._queue.qsize() < 2 and not session._draining))
               and time.monotonic() < deadline):
            time.sleep(0.002)
        block = np.zeros((BLOCK_FRAMES, 2), np.float32)
        try:
            session._callback(block, BLOCK_FRAMES, TIME_INFO, OK)
        except sd.CallbackStop:
            return np.concatenate(out)
        if out or block.any():
            out.append(block)
    raise AssertionError("never reached the end")


def session_for(paths, advances, monitors=None):
    follow = {i: (i + 1, paths[i + 1]) for i in range(len(paths) - 1)}.get
    s = _Session(paths[0], lambda path: 1.0, lambda s: None, index=0, follow=follow, monitors=monitors,
                 on_advance=lambda s, previous, new: advances.append((previous.path, new.path)))
    s.start()
    return s


def test_cue_tracks_of_one_file_join_gaplessly_and_exactly(tmp_path, fake_card):
    _, data = make_image(tmp_path / "Album")
    tracks = library.scan_album(tmp_path / "Album")
    paths = [t.path for t in tracks]
    advances, feed = [], []
    import queue
    monitor = queue.Queue(maxsize=1000)
    s = session_for(paths, advances, monitors=[monitor])
    got = pull(s)
    want = data / np.float32(32768)  # 16-bit values are exact in float32
    assert np.array_equal(got[:TOTAL], want)     # every sample once, in order, nothing between tracks
    assert not got[TOTAL:].any()                 # and it stops on the file's last sample
    assert advances == [(paths[0], paths[1]), (paths[1], paths[2])]
    # each track's proof: checked against its own stretch through the same decode path
    for source in s._sources:
        with audiofile.open(source.path) as reference:
            source.sent.check(reference)
        assert source.sent.summary().complete
    while not monitor.empty():
        feed.append(monitor.get())
    offsets = {item["path"]: item["offset"] for item in feed}
    assert offsets[paths[2]] == STARTS[3] and feed[-1]["track"] == "Album.flac · track 3"
    s.close()


def test_a_cue_track_starts_and_stops_on_its_exact_sample(tmp_path, fake_card):
    _, data = make_image(tmp_path / "Album")
    middle = library.scan_album(tmp_path / "Album")[1]
    s = session_for([middle.path], [])
    got = pull(s)
    want = data[STARTS[2]:STARTS[3]] / np.float32(32768)
    assert np.array_equal(got[:len(want)], want) and not got[len(want):].any()
    assert s.total_frames == len(want) and s._src.offset == STARTS[2]
    s.close()


def test_seeking_inside_a_cue_track(tmp_path, fake_card):
    _, data = make_image(tmp_path / "Album")
    middle = library.scan_album(tmp_path / "Album")[1]
    s = session_for([middle.path], [])
    s.seek(1.0)                                 # one second into the track, not the file
    got = pull(s)
    want = data[STARTS[2] + 44100:STARTS[3]] / np.float32(32768)
    assert np.array_equal(got[:len(want)], want)
    s.close()


def test_the_player_follows_cue_tracks_by_their_own_paths(tmp_path):
    make_image(tmp_path / "Album")
    tracks = library.scan_album(tmp_path / "Album")
    p = Player()
    p.set_tracks(tracks)
    assert p._follow(0) == (1, tracks[1].path) and p._follow(1) == (2, tracks[2].path)
    fake = SimpleNamespace(retire=lambda source: None)
    p._joined(fake, SimpleNamespace(), SimpleNamespace(index=2, path=tracks[2].path))
    assert p.index == 2
    p.set_trim(tracks[0].path, 0.5)  # the clipping guard is per track, even within one file
    assert p.trim_for(tracks[0].path) == 0.5 and p.trim_for(tracks[1].path) == 1.0

"""VU 'Auto (per album)': 0 VU set where an album's loud passages sit."""
import numpy as np
import soundfile as sf

from ironbark.meters import album_alignment, album_files, alignment_from_levels, vu_levels

RATE = 44100


def tone(path, seconds, dbfs, silence=0.0):
    t = np.arange(int(RATE * seconds)) / RATE
    x = 10 ** (dbfs / 20) * np.sin(2 * np.pi * 440 * t)
    if silence:
        x = np.concatenate([x, np.zeros(int(RATE * silence))])
    sf.write(path, np.stack([x, x], axis=1), RATE, subtype="PCM_24")
    return path


def test_levels_and_silence(tmp_path):
    levels = vu_levels(tone(tmp_path / "a.flac", 3.0, -12.0, silence=3.0))
    assert len(levels) == 10                      # the 3 s of silence don't count
    assert np.all(np.abs(levels + 12.0) < 0.1)    # a -12 dBFS sine reads -12


def test_a_loud_master_and_a_quiet_one(tmp_path):
    loud = tmp_path / "loud"
    loud.mkdir()
    first = tone(loud / "01.flac", 10, -8.0)
    tone(loud / "02.flac", 2, -20.0)
    assert album_alignment(first) == -8.0
    quiet = tmp_path / "quiet"
    quiet.mkdir()
    assert album_alignment(tone(quiet / "01.flac", 10, -19.4)) == -19.0


def test_the_whole_album_counts_not_just_the_song_playing(tmp_path):
    album = tmp_path / "album"
    (album / "Disc 2").mkdir(parents=True)
    quiet_song = tone(album / "01.flac", 3, -22.0)
    tone(album / "Disc 2" / "01.flac", 30, -9.0)
    seen = []
    assert album_alignment(quiet_song, on_first=seen.append) == -9.0
    assert seen == [-22.0]                        # the song itself first, as a stand-in
    folder, files = album_files(quiet_song)
    assert folder == album and files[0] == quiet_song and len(files) == 2


def test_kept_within_a_sensible_range():
    assert alignment_from_levels(np.array([-1.0] * 20)) == -6.0
    assert alignment_from_levels(np.array([-40.0] * 20)) == -24.0
    assert alignment_from_levels(np.zeros(0)) is None

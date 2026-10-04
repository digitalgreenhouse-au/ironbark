import numpy as np
import soundfile as sf
from tagging import text, write_id3
from ironbark.analysis import analyse
from ironbark.details import describe_analysis, output_line
from ironbark.engine import OutputInfo
from ironbark.library import find_albums, read_track, scan_album


def make_wav(path, seconds=0.5, rate=48000, subtype="PCM_24", info=None, id3=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (0.25 * np.sin(np.linspace(0, 2000, int(seconds * rate)))).astype("float32")
    with sf.SoundFile(path, "w", rate, 2, subtype=subtype, format="WAV") as f:
        for key, value in (info or {}).items():
            setattr(f, key, value)       # RIFF INFO chunk
        f.write(np.column_stack([data, data]))
    if id3:
        write_id3(path, id3)
    return path


def test_wav_format_details(tmp_path):
    t = read_track(make_wav(tmp_path / "01 Song.wav", rate=96000, subtype="PCM_24"))
    assert t.codec == "WAV" and not t.is_flac
    assert (t.sample_rate, t.bits, t.channels) == (96000, 24, 2)
    assert t.bitrate == 96000 * 24 * 2
    assert t.format_line == "WAV · 24-bit / 96 kHz · stereo · 4,608 kbps"
    assert t.title == "Song" and abs(t.duration - 0.5) < 0.01


def test_wav_float_is_labelled(tmp_path):
    t = read_track(make_wav(tmp_path / "f.wav", subtype="FLOAT"))
    assert t.float_samples and t.format_line.startswith("WAV · 32-bit float / 48 kHz")


def test_wav_riff_info_tags(tmp_path):
    t = read_track(make_wav(tmp_path / "x.wav", info={"title": "Info Title", "artist": "Info Artist",
                                                       "album": "Info Album", "tracknumber": "4"}))
    assert (t.title, t.artist, t.album, t.track_no) == ("Info Title", "Info Artist", "Info Album", 4)


def test_wav_id3_tags_win_over_info(tmp_path):
    path = make_wav(tmp_path / "x.wav", info={"title": "Info Title"},
                    id3=[text("TIT2", "ID3 Title"), text("TPE1", "ID3 Artist"), text("TALB", "ID3 Album"),
                         text("TRCK", "2/9")])
    t = read_track(path)
    assert (t.title, t.artist, t.album, t.track_no) == ("ID3 Title", "ID3 Artist", "ID3 Album", 2)


def test_mixed_flac_and_wav_album(tmp_path):
    make_wav(tmp_path / "Album" / "02 Two.wav")
    sf.write(tmp_path / "Album" / "01 One.flac", np.zeros((4800, 2), "float32"), 48000, format="FLAC")
    assert [t.title for t in scan_album(tmp_path / "Album")] == ["One", "Two"]
    (album,) = find_albums(tmp_path)
    assert album.track_count == 2


def test_wav_analysis_has_no_fingerprint_but_measures_dr(tmp_path):
    path = make_wav(tmp_path / "x.wav", seconds=7)
    result = analyse(path, 24, 0)
    assert result.md5_ok is None and result.dr == 0  # steady sine
    assert describe_analysis(result, "WAV")[0][1].startswith("Not applicable")


def test_float_wav_is_never_called_bit_perfect():
    out = OutputInfo("DAC", 48000, 48000, exclusive=True, hardware_volume=True)
    text, tone = output_line(out, 1.0, float_source=True)
    assert "not bit-perfect" in text and tone == "warn"

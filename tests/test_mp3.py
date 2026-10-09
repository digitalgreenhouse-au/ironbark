import shutil

import numpy as np
import soundfile as sf
from tagging import text, write_id3
from ironbark.analysis import analyse
from ironbark.details import describe_analysis
from ironbark.statustext import output_line
from ironbark.engine import OutputInfo
from ironbark.library import find_albums, read_track, scan_album
from ironbark.mp3frames import build_index
from ironbark.tagio.mpeg import MP3File


def make_mp3(path, mode="CONSTANT", seconds=3.0, rate=44100, tags=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    noise = 0.3 * np.random.default_rng(0).uniform(-1, 1, (int(seconds * rate), 2))
    sf.write(path, noise.astype("float32"), rate, format="MP3", bitrate_mode=mode, compression_level=0.0)
    if tags:
        write_id3(path, tags, version=4)
    return path


def test_cbr_mp3_details(tmp_path):
    t = read_track(make_mp3(tmp_path / "01 Song.mp3"))
    assert t.codec == "MP3" and t.lossy and not t.is_flac
    assert t.bitrate_mode == "CBR" and t.bitrate == 320_000
    assert t.format_line.startswith("MP3 · 320 kbps CBR · 44.1 kHz") and t.format_line.endswith("lossy")
    facts = dict(t.details)
    assert facts["Codec"].startswith("MPEG-1 Layer III (MP3): lossy")
    assert facts["Bitrate mode"] == "constant (CBR)"
    assert facts["Encoder"].startswith("LAME")


def test_vbr_mp3_and_tags(tmp_path):
    path = make_mp3(tmp_path / "x.mp3", mode="VARIABLE",
                    tags=[text("TIT2", "Tagged"), text("TPE1", "Someone"), text("TALB", "Record"),
                          text("TRCK", "3/10")])
    t = read_track(path)
    assert t.bitrate_mode == "VBR" and "VBR" in t.format_line
    assert (t.title, t.artist, t.album, t.track_no) == ("Tagged", "Someone", "Record", 3)
    assert "-V" in dict(t.details)["Encoder"]


def test_mp3_disguised_as_flac_is_flagged(tmp_path):
    real = make_mp3(tmp_path / "real.mp3")
    fake = tmp_path / "01 Fake.flac"
    shutil.copy(real, fake)
    t = read_track(fake)
    assert t.codec == "MP3" and t.misnamed == ".flac"
    assert "⚠ really MP3, named .flac" in t.format_line


def test_library_labels_mp3_and_mixed_albums(tmp_path):
    make_mp3(tmp_path / "Lossy" / "01.mp3")
    make_mp3(tmp_path / "Mixed" / "01.mp3")
    sf.write(tmp_path / "Mixed" / "02.wav", np.zeros((4410, 2), "float32"), 44100)
    quality = {a.name: a.quality for a in find_albums(tmp_path)}
    assert quality == {"Lossy": "MP3", "Mixed": "mixed"}
    assert [t.codec for t in scan_album(tmp_path / "Mixed")] == ["MP3", "WAV"]


def test_mp3_frame_index_matches_file(tmp_path):
    path = make_mp3(tmp_path / "v.mp3", mode="VARIABLE", seconds=4)
    info = MP3File(path).info
    idx = build_index(path)
    assert idx.sample_rate == 44100
    assert abs(idx.samples[-1] / 44100 - info.length) < 0.1
    whole = idx.bitrate_at(2.0, window=4.0)
    assert abs(whole - info.bitrate) / info.bitrate < 0.05


def test_mp3_check_and_output_wording(tmp_path):
    result = analyse(make_mp3(tmp_path / "x.mp3", seconds=4), 16, 0)
    assert result.md5_ok is None and result.dr is not None
    assert describe_analysis(result, "MP3")[0][1].startswith("Not applicable: MP3 is lossy")
    out = OutputInfo("DAC", 44100, 48000, exclusive=True, hardware_volume=True)
    text, tone = output_line(out, 1.0, lossy=True)
    assert "source is lossy" in text and "bit-perfect" not in text

"""The small changes that join the code-review fixes up: what one part now reports, another shows."""

import numpy as np
import pytest
import soundfile as sf

from ironbark import artwork, audiofile, compare, formats, settings
from ironbark.analysis import AlbumResult, analyse
from ironbark.details import describe_album, describe_proof, describe_range, output_line
from ironbark.engine import OutputInfo
from ironbark.library_index import Diff, LibraryIndex
from ironbark.proof import Proof
from ironbark.watcher import Watcher


def noise(seconds, rate, amp=0.1, channels=2, seed=1):
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal((int(seconds * rate), channels))).clip(-0.99, 0.99)


# -- the device taking fewer bits, and lossy codecs named --------------------------------------------
def test_a_device_taking_fewer_bits_is_never_called_bit_perfect():
    out = OutputInfo("DAC", 96000, None, exclusive=True, device_bits=16)
    text, tone = output_line(out, 1.0, bits=24, codec="FLAC")
    assert "bit-perfect ✓" not in text and "16-bit samples" in text and tone == "warn"
    assert output_line(out, 1.0, bits=16, codec="FLAC")[0].endswith("bit-perfect ✓")
    unknown = OutputInfo("DAC", 96000, None, exclusive=True)          # read-back failed: no claim made
    assert output_line(unknown, 1.0, bits=24)[0].endswith("bit-perfect ✓")
    proof = Proof(sent=60, checked=60)
    text, tone = describe_proof(proof, out, 1.0, 1.0, bits=24, codec="FLAC")
    assert not text.startswith("✓ Bit-perfect") and tone == "warn"
    assert describe_proof(proof, out, 1.0, 1.0, bits=16)[0].startswith("✓ Bit-perfect, proven")


def test_the_lossy_line_names_the_codec():
    out = OutputInfo("DAC", 48000, None, exclusive=True)
    assert "decoded Opus sent untouched" in output_line(out, 1.0, lossy=True, codec="Opus")[0]
    assert "decoded MP3 sent untouched" in output_line(out, 1.0, lossy=True)[0]
    assert "Vorbis is lossy" in describe_proof(Proof(sent=5, checked=5), out, 1.0, 1.0, lossy=True,
                                               codec="Vorbis")[0]


# -- the Check's wording below CD rate, and the album's count -----------------------------------------
def test_a_32k_files_own_converter_wall_is_described_as_the_top_of_its_range(tmp_path):
    rate = 32000
    x = noise(10, rate)
    spectrum = np.fft.rfft(x, axis=0)
    spectrum[np.fft.rfftfreq(len(x), 1 / rate) > 0.45 * rate] = 0
    path = tmp_path / "a.flac"
    sf.write(path, np.fft.irfft(spectrum, n=len(x), axis=0), rate, format="FLAC", subtype="PCM_16")
    text = describe_range(analyse(path, 16, 0))
    assert text.startswith("✓ Sound reaches") and "top of a 32 kHz file's range" in text
    assert "fading out gradually" not in text


def test_the_album_line_says_what_it_left_out():
    whole = AlbumResult(-9.0, 6.0, 9, 8, 10, tracks=12)
    assert "12 tracks as one programme" in describe_album(whole)
    some = AlbumResult(-9.0, 6.0, 9, 8, 10, tracks=10, skipped=2, partial=1)
    text = describe_album(some)
    assert "10 of 12 tracks (2 couldn't be read)" in text and "1 counted only up to where it stops" in text


# -- settings writes that fail are reported -----------------------------------------------------------
def test_a_failed_settings_write_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    assert settings.save({"volume": 0.5}) is True

    def locked(path, text, errors="strict"):
        raise PermissionError("locked")

    monkeypatch.setattr(settings, "write_file", locked)
    assert settings.save({"volume": 0.4}) is False


# -- the library folder going away is reported by the watcher -----------------------------------------
def test_the_watcher_reports_a_library_folder_that_went_mid_look(tmp_path):
    index = LibraryIndex(tmp_path)

    def gone(hold=None):
        return Diff(error=f"Can't see the library folder {tmp_path}")

    index.refresh = gone
    report = Watcher(lambda: index).check()
    assert report.error.startswith("Can't see the library folder") and not report.news


# -- "._" files are skipped by every folder walk -------------------------------------------------------
def test_the_cover_search_skips_macos_dot_underscore_files(tmp_path):
    (tmp_path / "._01.flac").write_bytes(b"\x00\x05\x16\x07 Finder data")
    sf.write(tmp_path / "01.flac", np.zeros((441, 2)), 44100, format="FLAC")
    assert artwork.first_audio_file(tmp_path) == tmp_path / "01.flac"


@pytest.mark.parametrize("value, number", [("²", None), ("①/12", None), ("7/12", 7)])
def test_superscript_track_numbers_are_not_read_as_numbers(tmp_path, monkeypatch, value, number):
    from ironbark import tagio

    path = tmp_path / "a.flac"
    sf.write(path, np.zeros((441, 2)), 44100, format="FLAC")
    monkeypatch.setattr(tagio, "text_values", lambda audio: {"TRACKNUMBER": [value]})
    assert formats._tag_values(path)[1] == number


# -- Compare names a CUE track as the track ----------------------------------------------------------
def test_compare_names_a_cue_track_not_its_whole_file(tmp_path):
    image = tmp_path / "image.flac"
    sf.write(image, noise(4, 44100), 44100, format="FLAC", subtype="PCM_16")
    (tmp_path / "image.cue").write_text(
        'FILE "image.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
        '  TRACK 02 AUDIO\n    INDEX 01 00:02:00\n', encoding="utf-8")
    from ironbark.library import cue_track_path
    track = cue_track_path(image, 2)
    result = compare.compare_files(track, track)
    rows = compare.describe(result)
    assert rows[0][1].startswith(audiofile.display_name(track)) and "track 2" in rows[0][1]

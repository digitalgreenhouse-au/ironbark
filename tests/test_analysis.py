import numpy as np
import pytest
import soundfile as sf

from ironbark.analysis import analyse, dr_from_blocks
from ironbark.tagio.flac import FLACFile


def write(path, data, rate=44100, subtype="PCM_16"):
    sf.write(path, data, rate, format="FLAC", subtype=subtype)
    return FLACFile(path).info


def sine(seconds, rate=44100, amp=0.5, freq=1000):
    t = np.arange(int(seconds * rate)) / rate
    mono = amp * np.sin(2 * np.pi * freq * t)
    return np.column_stack([mono, mono])


@pytest.mark.parametrize("subtype,bits", [("PCM_16", 16), ("PCM_24", 24)])
def test_md5_matches_for_intact_file(tmp_path, subtype, bits):
    info = write(tmp_path / "a.flac", sine(4, amp=0.3), subtype=subtype)
    if info.md5_signature == 0:
        pytest.skip("encoder did not store an MD5")
    result = analyse(tmp_path / "a.flac", bits, info.md5_signature)
    assert result.md5_ok is True


def test_md5_mismatch_is_reported(tmp_path):
    info = write(tmp_path / "a.flac", sine(1))
    result = analyse(tmp_path / "a.flac", 16, info.md5_signature ^ 1)
    assert result.md5_ok is False


def test_no_stored_md5_is_unknown(tmp_path):
    write(tmp_path / "a.flac", sine(1))
    assert analyse(tmp_path / "a.flac", 16, 0).md5_ok is None


def test_pure_sine_has_zero_dynamic_range(tmp_path):
    # A steady sine: peak == RMS×√2, so DR is 0.
    info = write(tmp_path / "a.flac", sine(15, amp=0.5), subtype="PCM_24")
    result = analyse(tmp_path / "a.flac", 24, info.md5_signature)
    assert result.dr == 0
    assert result.peak_dbfs == pytest.approx(-6.02, abs=0.05)


def test_dr_uses_second_highest_peak():
    rms = np.full(10, 0.1)
    peaks = np.array([1.0, 1.0] + [0.1] * 8)
    assert dr_from_blocks(rms, peaks) == pytest.approx(20.0)
    peaks_one_spike = np.array([1.0] + [0.1] * 9)
    assert dr_from_blocks(rms, peaks_one_spike) == pytest.approx(0.0)


def test_track_loudness_comes_with_the_check(tmp_path):
    # EBU Tech 3341 case 1 as a 24-bit FLAC at 44.1 kHz: a 1 kHz stereo tone at -23 dBFS reads -23 LUFS.
    info = write(tmp_path / "a.flac", sine(20, amp=10 ** (-23 / 20)), subtype="PCM_24")
    result = analyse(tmp_path / "a.flac", 24, info.md5_signature)
    assert result.lufs == pytest.approx(-23.0, abs=0.1)
    assert result.lra == pytest.approx(0.0, abs=0.1)


def test_album_loudness_measures_the_tracks_as_one_programme(tmp_path):
    from ironbark.loudness import album_loudness

    write(tmp_path / "1.flac", sine(20, amp=10 ** (-20 / 20)), subtype="PCM_24")
    write(tmp_path / "2.flac", sine(20, amp=10 ** (-30 / 20)), subtype="PCM_24")
    lufs, lra = album_loudness([tmp_path / "1.flac", tmp_path / "2.flac"])
    assert lra == pytest.approx(10, abs=1)  # EBU Tech 3342 case 1, split over two tracks
    assert lufs == pytest.approx(10 * np.log10((10 ** -2.0 + 10 ** -3.0) / 2), abs=0.1)

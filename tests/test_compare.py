"""Compare two files (compare.py): identical, shifted, altered, level-shifted, resampled — and the
Compare window (skipped without a display)."""
import gc
import tkinter as tk

import numpy as np
import pytest
import soundfile as sf

from ironbark import theme
from ironbark.proof import resample
from ironbark.tools import compare

RATE = 44100
RNG = np.random.default_rng(11)


def music(seconds=12.0, rate=RATE, channels=2):
    """Band-limited noise with a slow swell: music-like enough to line up on, never periodic."""
    n = int(seconds * rate)
    x = RNG.standard_normal((n, channels))
    spectrum = np.fft.rfft(x, axis=0)
    spectrum[int(15000 * n / rate):] = 0
    x = np.fft.irfft(spectrum, n=n, axis=0)
    swell = 0.6 + 0.4 * np.sin(np.linspace(0, 6, n))[:, None]
    x = x * swell
    return 0.25 * x / np.max(np.abs(x))


def write16(path, x, rate=RATE, fmt="FLAC"):
    sf.write(path, x, rate, format=fmt, subtype="PCM_16")
    return path


def as16(x):
    return np.round(x * 32767) / 32768


def test_identical_files_even_in_different_containers_and_bit_depths(tmp_path):
    x = as16(music())
    a = write16(tmp_path / "a.flac", x)
    b = tmp_path / "b.wav"
    sf.write(b, x, RATE, subtype="PCM_24")          # the same 16-bit values padded into 24 bits
    result = compare.compare_files(a, b)
    assert result.verdict == "identical"
    assert result.offset == 0 and result.differing == 0
    assert result.compared == len(x)
    rows = compare.describe(result)
    assert rows[2][0] == "Result" and rows[2][2] == "good" and "stored differently" in rows[2][1]


def test_shifted_copy_is_found_and_lined_up(tmp_path):
    x = as16(music())
    a = write16(tmp_path / "a.flac", x)
    b = write16(tmp_path / "b.flac", np.concatenate([np.zeros((588, 2)), x]))   # one CD sector of silence first
    result = compare.compare_files(a, b)
    assert result.offset == 588
    assert result.verdict == "same_shifted" and result.differing == 0
    assert result.extra_start == 588 and result.extra_end == 0
    text = " ".join(t for _, t, _ in compare.describe(result))
    assert "B starts 588 samples (13.3 ms) later than A" in text
    # The other way round: B starts part-way in.
    c = write16(tmp_path / "c.flac", x[1000:])
    result = compare.compare_files(a, c)
    assert result.offset == -1000 and result.verdict == "same_shifted"


def test_altered_samples_are_counted_and_measured(tmp_path):
    x = as16(music())
    y = x.copy()
    y[50000, 0] += 100 / 32768                       # one sample, 100 steps out
    y[90000:90010, 1] = 0                            # ten more
    a, b = write16(tmp_path / "a.flac", x), write16(tmp_path / "b.flac", y)
    result = compare.compare_files(a, b)
    changed = int(np.count_nonzero(x[90000:90010, 1])) + 1
    assert result.verdict == "different" and result.offset == 0
    assert result.differing == changed
    assert result.first_difference == 50000
    assert result.largest == pytest.approx(max(100 / 32768, np.max(np.abs(x[90000:90010, 1]))))
    expected = np.sqrt(np.sum((y - x) ** 2) / np.sum(x ** 2))
    assert result.difference_db == pytest.approx(20 * np.log10(expected), abs=0.01)
    rows = compare.describe(result)
    assert rows[2][2] == "warn" and "the first at 0:01.134" in rows[2][1]
    assert any(label == "Largest" and "steps of 16-bit" in text for label, text, _ in rows)


def test_level_shifted_copy_explained_by_the_music_comparison(tmp_path):
    x = music()
    a = write16(tmp_path / "a.flac", as16(x))
    b = tmp_path / "b.wav"
    sf.write(b, x * 10 ** (-1 / 20), RATE, subtype="FLOAT")        # 1 dB quieter
    result = compare.compare_files(a, b)
    assert result.verdict == "different"
    assert result.level_db == pytest.approx(-1.0, abs=0.01)
    assert result.after_level_db < -70                  # the rest is just 16-bit rounding (-101 dBFS on -26 dBFS music)
    assert result.path is not None and result.path.level_db == pytest.approx(-1.0, abs=0.01)
    assert result.path.difference_db < -70
    text = " ".join(t for _, t, _ in compare.describe(result))
    assert "B is 1.00 dB quieter" in text and "level -1.00 dB" in text


def test_inverted_copy(tmp_path):
    x = as16(music())
    a, b = write16(tmp_path / "a.flac", x), write16(tmp_path / "b.flac", -x)
    result = compare.compare_files(a, b)
    assert result.verdict == "different" and result.inverted and result.offset == 0


def test_resampled_copy_is_compared_as_music(tmp_path):
    x = music(seconds=10)
    a = write16(tmp_path / "a.flac", as16(x))
    b = tmp_path / "b.wav"
    sf.write(b, resample(x, RATE, 48000), 48000, subtype="FLOAT")
    result = compare.compare_files(a, b)
    assert result.verdict == "not_comparable"
    assert result.path is not None and result.path.resampled == (RATE, 48000)
    assert abs(result.path.level_db) < 0.05 and result.path.difference_db < -50
    rows = compare.describe(result)
    assert "Different sample rates (44.1 → 48 kHz)" in rows[2][1]
    assert rows[-1][0] == "As music" and rows[-1][2] == "good"


def test_silent_files_compare_from_the_start(tmp_path):
    a = write16(tmp_path / "a.flac", np.zeros((RATE, 2)))
    b = write16(tmp_path / "b.flac", np.zeros((RATE, 2)))
    result = compare.compare_files(a, b)
    assert result.verdict == "identical" and not result.offset_found


def test_find_lag_ignores_polarity_and_reports_the_match():
    x = RNG.standard_normal(5000)
    region = np.concatenate([RNG.standard_normal(321) * 0.01, -x, np.zeros(100)])
    at, match = compare.find_lag(x, region)
    assert at == 321 and match > 0.99


def test_cancel_stops_the_comparison(tmp_path):
    import threading
    x = as16(music())
    a, b = write16(tmp_path / "a.flac", x), write16(tmp_path / "b.flac", x)
    cancel = threading.Event()
    cancel.set()
    assert compare.compare_files(a, b, cancel=cancel).verdict == "cancelled"


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
    gc.collect()   # here, on the main thread (Tk objects freed on another thread abort Python)


def test_compare_window_shows_the_result(root, tmp_path):
    import time

    from ironbark.tools.compare_window import CompareWindow
    x = as16(music(seconds=4))
    a, b = write16(tmp_path / "a.flac", x), write16(tmp_path / "b.flac", np.concatenate([np.zeros((10, 2)), x]))
    prefs = {}
    win = CompareWindow(root, prefs, path_a=a, path_b=b)
    win.win.update()
    win.start()
    deadline = time.time() + 20
    while win.result is None and time.time() < deadline:
        win.win.update()
        time.sleep(0.05)
    assert win.result is not None and win.result.verdict == "same_shifted"
    texts = [w.cget("text") for w in win.results.grid_slaves(column=1)]
    assert any("lined up differently" in t for t in texts)
    win.close()


def test_compare_window_keeps_a_cue_track(root, tmp_path):
    import time

    from ironbark.audiofile import CuePath, CueRange
    from ironbark.tools.compare_window import CompareWindow
    x = as16(music(seconds=4))
    image = write16(tmp_path / "image.flac", np.concatenate([np.zeros((RATE, 2)), x]))
    single = write16(tmp_path / "02.flac", x)
    second = CuePath(str(image), cue=CueRange(sheet=str(tmp_path / "image.cue"), number=2, start=RATE, end=None))
    win = CompareWindow(root, {}, path_a=second, path_b=single)
    assert win.rows["A"].path == second and "image.flac · track 2" in win.rows["A"].label.cget("text")
    win.start()
    deadline = time.time() + 20
    while win.result is None and time.time() < deadline:
        win.win.update()
        time.sleep(0.05)
    assert win.result is not None and win.result.verdict == "identical"   # the track itself, not the whole file
    win.close()


def test_file_chooser_lists_every_kind_of_file_the_player_plays():
    from ironbark.library import AUDIO_EXTENSIONS
    from ironbark.tools.compare_window import AUDIO_TYPES
    patterns = AUDIO_TYPES[0][1].split()
    assert AUDIO_TYPES[0][0] == "Audio files"
    assert {p.removeprefix("*") for p in patterns} == set(AUDIO_EXTENSIONS)   # AIFF, ALAC, Ogg… too
    assert AUDIO_TYPES[-1] == ("All files", "*.*")

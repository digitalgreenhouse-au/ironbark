"""0.7.0: the Spectrogram window's maths (spectrum.py), the bitscope's bit counting, and
smoke tests of the Spectrogram and PCM stream windows (skipped without a display)."""

import queue
import re
import time
import tkinter as tk
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import spectrum, theme
from ironbark.analysis import wall_verdict

RATE = 44100


def sine(freq, seconds=0.5, amp=1.0, rate=RATE, channels=2):
    t = np.arange(int(seconds * rate)) / rate
    return np.repeat((amp * np.sin(2 * np.pi * freq * t))[:, None], channels, axis=1)


def lowpass(data, rate, cutoff):
    """A brick wall: every frequency above `cutoff` removed (what a lossy encoder leaves)."""
    s = np.fft.rfft(data, axis=0)
    s[np.fft.rfftfreq(len(data), 1 / rate) > cutoff] = 0
    return np.fft.irfft(s, n=len(data), axis=0)


def noise(seconds, rate=RATE, amp=0.2, seed=1):
    return (amp * np.random.default_rng(seed).standard_normal((int(seconds * rate), 2))).clip(-0.99, 0.99)


# -- spectrum maths -----------------------------------------------------------------------

def test_a_1khz_tone_lands_in_its_bin():
    win = spectrum.window(spectrum.LIVE_FFT)
    db = spectrum.column(sine(1000.0), win)
    expected = round(1000.0 / spectrum.bin_hz(RATE, spectrum.LIVE_FFT))   # bin 93 (10.77 Hz steps)
    assert int(np.argmax(db)) == expected


def test_a_full_scale_sine_reads_0_dbfs_and_minus_20_reads_minus_20():
    win = spectrum.window(spectrum.LIVE_FFT)
    centre = 93 * spectrum.bin_hz(RATE, spectrum.LIVE_FFT)   # exactly on a bin: no scalloping loss
    assert spectrum.column(sine(centre), win).max() == pytest.approx(0.0, abs=0.01)
    assert spectrum.column(sine(centre, amp=0.1), win).max() == pytest.approx(-20.0, abs=0.01)
    # Off-centre, the Kaiser window's wide main lobe keeps the reading within a fraction of a dB.
    assert spectrum.column(sine(1000.0), win).max() == pytest.approx(0.0, abs=0.5)


def test_the_picture_row_for_1khz_is_the_brightest():
    height = 300
    db = spectrum.column(sine(1000.0), spectrum.window(spectrum.LIVE_FFT))
    rows = spectrum.to_rows(db, spectrum.log_rows(height, RATE, spectrum.LIVE_FFT))[::-1]  # top first
    assert len(rows) == height
    assert abs(int(np.argmax(rows)) - spectrum.row_of(1000.0, height, RATE)) <= 2


def test_log_scale_and_colours():
    assert spectrum.row_of(spectrum.LOW_HZ, 100, RATE) == pytest.approx(100)   # bottom
    assert spectrum.row_of(RATE / 2, 100, RATE) == pytest.approx(0)            # top
    # 20 Hz → 22.05 kHz is ~10 octaves: 1 kHz sits ~56% of the way up.
    assert spectrum.row_of(1000, 100, RATE) == pytest.approx(100 * (1 - np.log(50) / np.log(1102.5)))
    rgb = spectrum.colours(np.array([spectrum.DB_TOP, spectrum.DB_BOTTOM, spectrum.EMPTY]))
    assert rgb.shape == (3, 3)
    assert tuple(rgb[1]) == (0, 0, 0) and tuple(rgb[2]) == (0, 0, 0)       # quiet and "no data" are black
    assert rgb[0].sum() > 600                                              # loudest is bright


def test_live_makes_25_columns_a_second_and_restarts_on_a_seek():
    live = spectrum.LiveSpectrogram()
    x = sine(1000.0, seconds=2.0)
    made = sum(live.add(x[k:k + 4096], RATE, "a.flac", k) for k in range(0, len(x), 4096))
    assert made == len(x) // (RATE // spectrum.LIVE_COLUMNS_PER_SECOND)   # 50 for 2 s
    assert all(r == RATE for _, r in live.columns)
    assert int(np.argmax(live.columns[-1][0])) == 93
    live.add(x[:100], RATE, "a.flac", 10 * RATE)     # a jump: the slice starts afresh
    assert len(live._buffer) == 100
    live.add(x[:100], 48000, "a.flac", 10 * RATE + 100)   # a new rate: likewise
    assert len(live._buffer) == 100


def test_whole_track_marks_a_lossy_wall(tmp_path):
    path = tmp_path / "wall.flac"
    sf.write(path, lowpass(noise(20), RATE, 16000), RATE, format="FLAC", subtype="PCM_16")
    picture = spectrum.track_picture(path, columns=60, height=120)
    assert picture.levels.shape == (120, 60)
    assert picture.duration == pytest.approx(20.0, abs=0.01)
    assert picture.cutoff_hz == pytest.approx(16000, abs=300)
    assert picture.cutoff_sharp and picture.verdict == "lossy"
    above = int(spectrum.row_of(18000, 120, RATE))   # rows are top first
    below = int(spectrum.row_of(12000, 120, RATE))
    assert picture.levels[below].mean() - picture.levels[above].mean() > 40


def test_whole_track_full_band_has_no_verdict_and_can_be_cancelled(tmp_path):
    path = tmp_path / "full.flac"
    sf.write(path, noise(10), RATE, format="FLAC", subtype="PCM_16")
    picture = spectrum.track_picture(path, columns=30, height=60)
    assert picture.verdict == ""
    import threading
    cancel = threading.Event()
    cancel.set()
    assert spectrum.track_picture(path, columns=30, height=60, cancel=cancel) is None


def test_wall_verdict_matches_the_check_rules():
    assert wall_verdict(16000, True, 44100) == "lossy"
    assert wall_verdict(16000, False, 44100) == ""          # a gentle fade is the recording
    assert wall_verdict(20500, True, 44100) == ""           # a CD's own filter
    assert wall_verdict(22050, True, 96000) == "upsampled"
    assert wall_verdict(30000, True, 96000) == ""
    assert wall_verdict(None, True, 96000) == ""


# -- bitscope maths ------------------------------------------------------------------------

def test_bit_counts_sign_and_size():
    values = np.array([[1], [-1], [3], [-8]])   # 4-bit: sign + 3 size bits
    negative, counts = spectrum.bit_counts(values, 4)
    assert negative.tolist() == [2]
    # sizes 1, 1, 3, and -8 (too big for 3 bits) counted as the top bit only. MSB first: bit 2, 1, 0
    assert counts.tolist() == [[1, 1, 3]]
    # Full-scale negative samples in a padded 24-bit file don't light the low bits.
    assert spectrum.bits_used_in_size(np.array([[-(1 << 23)], [256]]), 24)[0, -8:].sum() == 0


def test_a_16_bit_recording_padded_to_24_leaves_the_bottom_8_bits_empty():
    rng = np.random.default_rng(3)
    v16 = rng.integers(-32768, 32768, size=(4096, 2))
    used = spectrum.bits_used_in_size(v16 << 8, 24)
    assert used.shape == (2, 23)
    assert used[:, :15].all() and not used[:, 15:].any()
    scope = spectrum.BitScope()
    scope.add(v16 << 8, 24, RATE, "x")
    assert scope.bits_in_use() == 16
    scope.add(v16 << 8 | 1, 24, RATE, "x")     # now the lowest bit carries sound too
    assert scope.bits_in_use() == 24


def test_quiet_music_only_lights_the_low_bits():
    rng = np.random.default_rng(4)
    quiet = rng.integers(-100, 101, size=(4096, 2))   # under 7 bits of size
    scope = spectrum.BitScope()
    scope.add(quiet, 16, RATE, "q")
    negative, size = scope.shares()
    assert 0.4 < negative[0] < 0.6                 # half the samples below zero
    assert not size[:, :8].any()                   # the top 8 size bits never used
    assert size[:, -1].min() > 0.3                 # the lowest bit is busy


def test_bitscope_keeps_about_the_last_second_and_resets_on_a_new_track():
    scope = spectrum.BitScope(seconds=1.0)
    block = np.ones((4096, 2), dtype=np.int64)
    for _ in range(30):                     # ~2.8 s
        scope.add(block, 16, RATE, "a")
    held = sum(n for n, *_ in scope.recent)
    assert RATE <= held < RATE + 4096
    scope.add(block * 4, 16, RATE, "b")     # a new track: start again
    assert len(scope.recent) == 1
    assert scope.bits_in_use() == 14        # size 4 = bit 2: sign + 15 size bits → 16 - 2


# -- windows (need a display) ----------------------------------------------------------------

class FakePlayer:
    def __init__(self, path=None):
        self.feed: queue.Queue = queue.Queue()
        self.track = SimpleNamespace(path=path) if path else None
        self.unmonitored = False

    def monitor(self):
        return self.feed

    def unmonitor(self, feed):
        self.unmonitored = True

    def status(self):
        return SimpleNamespace(track=self.track, position=1.0)


def blocks(data_int32, rate=RATE, bits=16, path="t.flac"):
    """Monitor-feed items for a stretch of int32 samples, all already heard."""
    past = time.perf_counter() - 10
    for k in range(0, len(data_int32), 4096):
        piece = data_int32[k:k + 4096]
        yield {"frame": k, "dac_time": past, "rate": rate, "bits": bits, "source": piece,
               "sent": piece, "track": "Test", "path": path}


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


def int32(x):
    return (np.clip(x, -1, 1 - 2**-15) * 32768).astype(np.int32) << 16


def test_spectrogram_window_live_and_whole_track(root, tmp_path):
    from ironbark.spectrogram_window import SpectrogramWindow
    path = tmp_path / "wall.flac"
    sf.write(path, lowpass(noise(8), RATE, 16000), RATE, format="FLAC", subtype="PCM_16")
    player = FakePlayer(path)
    prefs = {"spectrogram_view": "live"}
    win = SpectrogramWindow(root, player, prefs)
    win.win.geometry("700x400")
    win.win.update()
    for item in blocks(int32(sine(1000.0, seconds=1.0))):
        player.feed.put(item)
    win._tick()
    assert len(win.live.columns) == 25 and len(win._rgb) == 25
    assert "Live, newest on the right" in win.info.cget("text")
    win.view.set("track")
    win._view_changed()
    deadline = time.time() + 20
    while win._picture is None and time.time() < deadline:
        time.sleep(0.05)
    win._tick()
    assert win._picture is not None
    assert re.search(r"reaches 1[56]\.\d kHz", win.info.cget("text"))
    assert "lossy" in win.info.cget("text")
    assert prefs["spectrogram_view"] == "track"
    win._lossy_by_design = True                      # the same wall in an MP3 is by design
    assert win._cutoff(win._picture)[2] == ""
    assert "by design" in win._track_info(win._picture)
    win.close()
    assert player.unmonitored


def test_pcm_window_bitscope_strip(root):
    from ironbark.pcm_window import PcmWindow
    player = FakePlayer()
    win = PcmWindow(root, player, {})
    win.win.update()
    rng = np.random.default_rng(5)
    v16 = rng.integers(-32768, 32768, size=(8192, 2)).astype(np.int32)
    for item in blocks(v16 << 16, bits=24):         # 16-bit sound in a 24-bit stream (padded)
        player.feed.put(item)
    win._tick()
    assert win.scope.bits_in_use() == 16
    text = win.scope_info.cget("text")
    assert "16 of 24 bits used" in text and "not checked yet" in text
    assert win.scope_canvas.find_all()             # bars drawn
    win.close()
    assert player.unmonitored

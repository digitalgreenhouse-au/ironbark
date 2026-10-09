"""0.8.0: the stereo scope (stereo.py + scope_window.py), PSR in the Meters, and VU
"Auto (loudness)"."""

import gc
import math
import queue
import time
import tkinter as tk
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import theme
from ironbark.tools import stereo
from ironbark.tools.meters import (AUTO_RANGE, MeterEngine, album_alignment, album_alignment_loudness,
                               alignment_from_loudness, tone_offset)

RATE = 48000


def noise(seconds, amp=0.2, seed=1, channels=1):
    return amp * np.random.default_rng(seed).standard_normal((int(seconds * RATE), channels))


def sine(freq, seconds, dbfs=0.0, rate=RATE):
    t = np.arange(int(rate * seconds)) / rate
    return 10 ** (dbfs / 20) * np.sin(2 * np.pi * freq * t)


def scope(signal, block=4096, path="t.flac"):
    """Feed `signal` (frames x channels) in player-sized blocks, heard from time 0."""
    engine = stereo.ScopeEngine(queue.Queue())
    for start in range(0, len(signal), block):
        engine.process(signal[start:start + block], RATE, start / RATE, path)
    return engine


def end(signal) -> float:
    return len(signal) / RATE - 0.001


# -- correlation, balance, DC -------------------------------------------------------------

def test_identical_inverted_and_independent_noise_read_plus_one_minus_one_and_zero():
    a = noise(2.0)
    b = noise(2.0, seed=2)
    same = np.hstack([a, a])
    opposite = np.hstack([a, -a])
    unrelated = np.hstack([a, b])
    assert scope(same).reading_at(end(same)).correlation == pytest.approx(1.0, abs=1e-9)
    assert scope(opposite).reading_at(end(opposite)).correlation == pytest.approx(-1.0, abs=1e-9)
    # Independent noise: 0, give or take chance (600 ms at 48 kHz → about ±0.006 standard deviation).
    assert abs(scope(unrelated).reading_at(end(unrelated)).correlation) < 0.03
    assert abs(scope(unrelated).reading_at(end(unrelated)).track_correlation) < 0.02


def test_correlation_covers_the_last_600_ms_only():
    a = noise(3.0)
    signal = np.vstack([np.hstack([a[:RATE * 2], a[:RATE * 2]]),      # 2 s identical...
                        np.hstack([a[RATE * 2:], -a[RATE * 2:]])])     # ...then 1 s inverted
    engine = scope(signal)
    assert engine.reading_at(1.9).correlation == pytest.approx(1.0, abs=1e-9)
    assert engine.reading_at(2.0 + stereo.CORRELATION_WINDOW + 0.05).correlation == pytest.approx(-1.0, abs=1e-9)
    halfway = engine.reading_at(2.0 + stereo.CORRELATION_WINDOW / 2).correlation
    assert abs(halfway) < 0.1                                      # half each: about 0
    assert engine.reading_at(10.0) is None                         # long after: nothing playing


def test_block_size_makes_no_difference():
    a, b = noise(1.5), noise(1.5, seed=3)
    signal = np.hstack([a, 0.5 * a + b])
    # (read between two 10 ms steps: exactly on one, rounding could take it either side)
    readings = [scope(signal, block=size).reading_at(1.4049) for size in (4096, 1000, 333)]
    for r in readings[1:]:
        assert r.correlation == pytest.approx(readings[0].correlation, abs=1e-9)
        assert r.balance_db == pytest.approx(readings[0].balance_db, abs=1e-9)


def test_balance_in_db_and_one_sided_sound():
    a = noise(1.0)
    signal = np.hstack([a, a * 10 ** (-6 / 20)])           # right 6 dB down
    reading = scope(signal).reading_at(0.95)
    assert reading.balance_db == pytest.approx(6.0, abs=1e-6)
    assert reading.correlation == pytest.approx(1.0, abs=1e-9)  # a level difference isn't decorrelation
    left_only = np.hstack([a, np.zeros_like(a)])
    reading = scope(left_only).reading_at(0.95)
    assert reading.one_sided == "left" and reading.correlation == 0.0 and reading.balance_db is None


def test_dc_offset_and_new_track_starts_again():
    a = noise(1.0, amp=0.05)
    a -= a.mean()                                          # no DC of its own
    signal = np.hstack([a + 0.01, a - 10 ** (-50 / 20)])   # -40 dBFS and -50 dBFS
    engine = scope(signal)
    dc = engine.reading_at(0.95).dc_offset
    assert stereo.dc_db(dc[0]) == pytest.approx(-40.0, abs=0.01)
    assert stereo.dc_db(dc[1]) == pytest.approx(-50.0, abs=0.01)
    engine.process(np.hstack([a, a]), RATE, 1.0, "next.flac")  # a new track: from zero again
    assert abs(engine.reading_at(1.95).dc_offset[0]) < 0.002


def test_a_mono_file_reads_as_both_speakers():
    signal = noise(1.0)                                     # one channel
    reading = scope(signal).reading_at(0.95)
    assert reading.channels == 1 and reading.correlation == pytest.approx(1.0) and reading.balance_db == 0.0


def test_int32_blocks_from_the_player_are_scaled():
    x = (np.hstack([noise(1.0)] * 2) * 2**31).astype(np.int32)
    reading = scope(x).reading_at(0.95)
    assert reading.correlation == pytest.approx(1.0, abs=1e-6)


def test_samples_between_returns_what_was_heard_then():
    ramp = np.arange(RATE, dtype=np.float64)[:, None].repeat(2, axis=1) / RATE
    engine = scope(ramp)
    got = engine.samples_between(0.5, 0.6)
    assert len(got) == pytest.approx(0.1 * RATE, abs=2)
    assert got[0, 0] == pytest.approx(0.5, abs=1e-3) and got[-1, 0] == pytest.approx(0.6, abs=1e-3)
    assert len(engine.samples_between(5.0, 6.0)) == 0


# -- the scope's geometry ---------------------------------------------------------------------

def test_scope_directions_and_db_distance():
    pairs = np.array([[1.0, 1.0], [1.0, 0.0], [0.0, 1.0], [1.0, -1.0], [10 ** (-24 / 20), 0.0]])
    across, up = stereo.side_mid(pairs)
    assert across[0] == pytest.approx(0) and up[0] > 0                  # mono: straight up
    assert across[1] < 0 and up[1] == pytest.approx(-across[1])         # left only: up and left, 45°
    assert across[2] > 0 and up[2] == pytest.approx(across[2])          # right only: up and right
    assert up[3] == pytest.approx(0) and across[3] != 0                 # opposite: flat
    x, y = stereo.log_radius(across, up)
    assert math.hypot(x[1], y[1]) == pytest.approx(1.0)                 # 0 dBFS in one channel: the edge
    assert math.hypot(x[4], y[4]) == pytest.approx(0.5)                 # -24 dBFS: half way
    assert math.hypot(x[0], y[0]) == pytest.approx(1.0)                 # louder than the edge: on it
    fx, fy = stereo.fold_up(np.array([0.3, -0.3]), np.array([-0.5, 0.5]))
    assert np.allclose(fx, [-0.3, -0.3]) and np.allclose(fy, [0.5, 0.5])  # a sample and its negative: one dot


# -- PSR ----------------------------------------------------------------------------------------

def meters(signal):
    engine = MeterEngine(queue.Queue())
    for start in range(0, len(signal), 4096):
        engine.process(signal[start:start + 4096].astype(np.float32), RATE, start / RATE)
    return engine


def test_psr_of_a_steady_tone():
    # A stereo 1 kHz sine: true peak = its level, short-term loudness = its level → PSR 0.
    x = sine(1000, 5.0, -20.0)
    assert meters(np.stack([x, x], axis=1)).reading_at(4.9).psr == pytest.approx(0.0, abs=0.1)
    # The same tone in one channel only: half the loudness (3 dB less), the same peak → PSR 3.
    assert meters(np.stack([x, 0 * x], axis=1)).reading_at(4.9).psr == pytest.approx(3.0, abs=0.1)


def test_psr_sees_a_peak_within_the_last_3_seconds_only():
    x = sine(1000, 10.0, -30.0)
    x[int(5.0 * RATE)] = 10 ** (-1 / 20)                   # one sample at -1 dBFS, 5 s in
    engine = meters(np.stack([x, x], axis=1))
    near = engine.reading_at(6.0).psr
    assert near > 25                                       # the click counts: about -1 - (-30)
    later = engine.reading_at(9.5).psr                     # > 3 s after it
    assert later == pytest.approx(0.0, abs=0.2)


# -- VU Auto (loudness) --------------------------------------------------------------------------

def test_tone_calibration_measured_with_the_players_loudness_code():
    # Stereo 1 kHz: LUFS ≈ the sine's level in dBFS (0.006 LU over at 44.1k); mono: 3 dB less.
    assert tone_offset(44100, 2) == pytest.approx(0.0, abs=0.02)
    assert tone_offset(48000, 2) == pytest.approx(0.0, abs=0.02)
    assert tone_offset(44100, 1) == pytest.approx(-3.01, abs=0.02)
    assert alignment_from_loudness(-14.2, 44100, 2) == -14.0
    assert alignment_from_loudness(-14.2, 44100, 1) == -11.0     # a mono tone needs 3 dB more to be as loud
    assert alignment_from_loudness(-3.0, 44100, 2) == AUTO_RANGE[1]
    assert alignment_from_loudness(-40.0, 44100, 2) == AUTO_RANGE[0]
    assert alignment_from_loudness(None, 44100, 2) is None


def write_tone(path, seconds, dbfs, rate=44100):
    x = sine(1000, seconds, dbfs, rate)
    sf.write(path, np.stack([x, x], axis=1), rate, subtype="PCM_24")
    return path


def test_auto_loudness_puts_a_steady_tone_at_0_vu(tmp_path):
    album = tmp_path / "album"
    album.mkdir()
    first = write_tone(album / "01.flac", 6, -16.0)
    write_tone(album / "02.flac", 6, -16.0)
    seen = []
    value, lufs = album_alignment_loudness(first, on_first=lambda v, l: seen.append((v, l)))
    assert value == -16.0 and lufs == pytest.approx(-16.0, abs=0.05)
    assert seen and seen[0][0] == -16.0
    # For a steady tone both Autos agree; then a tone at that alignment reads 0 VU.
    assert album_alignment(first) == value
    engine = MeterEngine(queue.Queue(), vu_align_dbfs=value)
    x = sine(1000, 2.0, -16.0)
    for start in range(0, len(x), 4096):
        engine.process(np.stack([x, x], axis=1)[start:start + 4096].astype(np.float32), RATE, start / RATE)
    assert abs(engine.reading_at(1.9).vu[0]) < 0.1


def test_auto_loudness_measures_the_whole_album(tmp_path):
    album = tmp_path / "album"
    album.mkdir()
    quiet = write_tone(album / "01.flac", 4, -24.0)
    write_tone(album / "02.flac", 20, -10.0)
    value, lufs = album_alignment_loudness(quiet)
    assert value == -10.0                     # the gating and the long loud track dominate
    assert -11.0 < lufs < -10.0


# -- the window (needs a display) ------------------------------------------------------------------

class FakePlayer:
    def __init__(self):
        self.feed: queue.Queue = queue.Queue()
        self.unmonitored = False

    def monitor(self):
        return self.feed

    def unmonitor(self, feed):
        self.unmonitored = True

    def status(self):
        return SimpleNamespace(track=None, position=0.0)


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


@pytest.mark.parametrize("view", ["lissajous", "polar"])
def test_scope_window_draws_and_closes(root, view):
    from ironbark.tools.scope_window import ScopeWindow
    player = FakePlayer()
    prefs = {"scope_view": view}
    win = ScopeWindow(root, player, prefs)
    win.win.geometry("420x640")
    win.win.update()
    a = noise(1.0)
    data = (np.hstack([a, -a]) * 2**31 * 0.5).astype(np.int32)   # one channel inverted
    start = time.perf_counter() - 0.5
    for k in range(0, len(data), 4096):
        win.engine.process(data[k:k + 4096], RATE, start + k / RATE, "t.flac")
    win._last = start
    win._tick()
    assert win._glow is not None and win._glow.sum() > 0
    reading = win.engine.reading_at(start + 0.9)
    assert reading.correlation == pytest.approx(-1.0, abs=1e-6)
    win._draw(reading)
    # (0.22.0) shown with a true minus sign, as wide as "+" (theme.figures)
    assert win.texts["correlation"].cget("text").startswith("Correlation −1.00")
    assert "centred" in win.texts["balance"].cget("text")
    assert win.canvas.find_all()
    if view == "polar":
        _, _, width, height, *_ = win._geometry
        assert width > height                  # a half circle
    win.close()
    assert player.unmonitored


def test_meter_window_offers_auto_loudness_and_shows_psr(root):
    from ironbark.tools import meter_window
    assert meter_window.AUTO_LOUDNESS in meter_window.ALIGNMENTS
    player = FakePlayer()
    prefs = {"vu_align": meter_window.AUTO_LOUDNESS}
    win = meter_window.MeterWindow(root, player, prefs)
    win.win.update()
    assert "Auto (loudness): waiting for music" in win.align_text.cget("text")
    assert meter_window.describe_psr(SimpleNamespace(psr=9.34)) == "PSR 9.3 dB"
    assert meter_window.describe_psr(None) == "PSR —"
    win.close()


def tick(win, times=1):
    """One refresh by hand, then cancel the next one the window lines up (the test does the ticking)."""
    for _ in range(times):
        win._tick()
        win.win.after_cancel(win._job)


def count_pictures(monkeypatch, module):
    """Count the pictures a window makes (each frame of the scope is one)."""
    made = []
    real = module.Image.fromarray
    monkeypatch.setattr(module.Image, "fromarray", lambda *a, **k: made.append(1) or real(*a, **k))
    return made


def test_scope_glow_fades_to_exactly_nothing_and_then_nothing_is_drawn(root, monkeypatch):
    from ironbark.tools import scope_window
    win = scope_window.ScopeWindow(root, FakePlayer(), {})
    win.win.update()
    win.engine.process(np.zeros((8192, 2), np.int32), RATE, time.perf_counter() - 0.2, "t.flac")
    win._last = time.perf_counter() - 1
    tick(win)
    assert win._glow.max() > 1000            # silence heaps every sample on one spot: very bright
    tick(win, 300)                           # ten seconds of frames with nothing more heard
    assert win._glow.max() == 0              # gone to exactly 0 (not left as a tiny value that slows every frame)
    made = count_pictures(monkeypatch, scope_window)
    tick(win, 5)
    assert made == []                        # nothing heard and nothing fading: the picture is left alone
    win.close()


def test_scope_takes_a_new_theme_s_colours(root):
    from ironbark.tools import scope_window
    win = scope_window.ScopeWindow(root, FakePlayer(), {})
    win.win.update()
    try:
        theme.use(theme.LIGHT, root)
        assert win._colours[:3] == list(theme.rgb(theme.LIGHT.BG))   # the picture's background follows
    finally:
        theme.use(theme.DARK, root)
    win.close()


def test_scope_draws_nothing_while_minimised(root, monkeypatch):
    from ironbark.tools import scope_window
    win = scope_window.ScopeWindow(root, FakePlayer(), {})
    win.win.update()
    made = count_pictures(monkeypatch, scope_window)
    win.win.iconify()
    win.win.update()
    a = noise(0.5)
    data = (np.hstack([a, a]) * 2**31 * 0.5).astype(np.int32)
    win.engine.process(data, RATE, time.perf_counter() - 0.3, "t.flac")
    tick(win, 3)
    assert made == []                        # minimised: no frames made
    win.win.deiconify()
    win.win.update()
    tick(win)
    assert made                              # shown again: drawn at once
    win.close()


def test_meter_auto_finds_the_album_without_listing_its_folder_every_tick(root, tmp_path, monkeypatch):
    from pathlib import Path
    from ironbark.tools import meter_window
    album = tmp_path / "album"
    album.mkdir()
    song = album / "01.flac"
    song.write_bytes(b"")                                  # never read: the measuring is a stand-in
    monkeypatch.setattr(meter_window, "album_alignment", lambda track, on_first, cancel: -15.0)
    listed = []
    real_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda self: listed.append(self) or real_iterdir(self))
    win = meter_window.MeterWindow(root, FakePlayer(), {"vu_align": meter_window.AUTO})
    win.win.update()
    win.engine.current_path = song
    tick(win, 5)
    assert listed == []                                    # 30 times a second: no folder listing on the window's thread
    deadline = time.time() + 5
    while "measured for this album" not in win.align_text.cget("text") and time.time() < deadline:
        time.sleep(0.05)
        tick(win)
    assert "0 VU = -15 dBFS · Auto, measured for this album (album)" in win.align_text.cget("text")
    win.close()


def test_meter_auto_that_fails_says_so(root, tmp_path, monkeypatch):
    from ironbark.tools import meter_window

    def broken(track, on_first, cancel):
        raise MemoryError("too big")
    monkeypatch.setattr(meter_window, "album_alignment", broken)
    win = meter_window.MeterWindow(root, FakePlayer(), {"vu_align": meter_window.AUTO})
    win.win.update()
    win.engine.current_path = tmp_path / "album" / "01.flac"
    deadline = time.time() + 5
    while "couldn't" not in win.align_text.cget("text") and time.time() < deadline:
        time.sleep(0.05)
        tick(win)
    assert "Auto: couldn't measure this album" in win.align_text.cget("text")   # not "measuring…" for ever
    win.close()

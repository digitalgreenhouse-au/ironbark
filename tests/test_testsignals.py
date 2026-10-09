"""The hardware test (testsignals.py): the signals are what the published sources say, and the
measurement maths reads known answers from synthetic signals — THD+N of a tone with a known
harmonic and known noise, J-test sidebands from a known amount of jitter, the frequency response
of a known filter. The seamless loop (audition.Switcher) and the window run through stand-ins:
no sound device is opened anywhere here."""
import gc
import math
import time
import tkinter as tk
from types import SimpleNamespace

import numpy as np
import pytest

from ironbark import audition, testsignals as ts, theme
from ironbark.engine import State

RATE = 48000


# -- the signals --------------------------------------------------------------------------------
def test_jtest_is_the_published_sample_cycle():
    """C000 C000 4000 4000 (x24) then BFFF BFFF 3FFF 3FFF (x24), 16-bit; the same with 00 on the
    end at 24 bits — a 192-sample cycle, undithered."""
    for bits, words in ((16, (0xC000, 0x4000, 0xBFFF, 0x3FFF)), (24, (0xC00000, 0x400000, 0xBFFFFF, 0x3FFFFF))):
        signal = ts.jtest(44100, bits, ts.JTEST_TOP)
        values = np.rint(signal.period * 2 ** (bits - 1)).astype(np.int64) & ((1 << bits) - 1)
        first = [words[0], words[0], words[1], words[1]] * 24
        second = [words[2], words[2], words[3], words[3]] * 24
        assert values[:192].tolist() == first + second
        assert np.array_equal(values, np.tile(values[:192], ts.JTEST_FRAMES // 192))
        assert signal.level_db == pytest.approx(-3.0103, abs=1e-4)          # half-scale square at Fs/4
        assert signal.frequency == 44100 / 4
    assert 0x4000 ^ 0x3FFF == 0x7FFF                                        # every bit below the sign flips


def test_jtest_levels_keep_every_bit_flipping():
    assert ts.jtest_level(ts.JTEST_TOP) == (1, pytest.approx(-3.0103, abs=1e-4))
    k, level = ts.jtest_level(-20)
    assert k == 4 and level == pytest.approx(-21.07, abs=0.01)             # 6 dB steps, never louder than asked
    signal = ts.jtest(RATE, 16, -20)
    values = np.rint(signal.period * 32768).astype(int)
    assert set(values[:96]) == {-2048, 2048} and set(values[96:192]) == {-2049, 2047}   # 0x0800 -> 0x07FF
    with pytest.raises(ValueError):
        ts.jtest(RATE, 16, -100)                                           # too quiet for 16 bits
    with pytest.raises(ValueError):
        ts.jtest(RATE, 20)


def test_tone_and_sweep_loop_without_a_join():
    signal = ts.tone(44100, -20)
    assert signal.frames == 44100 and signal.frequency == 1000
    joined = np.concatenate([signal.period[-3:], signal.period[:3]])
    expected = 0.1 * np.sin(2 * np.pi * 1000 * np.arange(-3, 3) / 44100)
    assert np.allclose(joined, expected, atol=1e-12)                       # round the loop is the next sample
    assert np.max(np.abs(signal.period)) == pytest.approx(0.1, abs=1e-4)   # -20 dBFS peak
    sweep = ts.sweep(RATE, -20)
    assert sweep.period[0] == 0 and sweep.period[-1] == 0                  # starts and ends in silence
    assert np.max(np.abs(sweep.period)) <= 0.1 + 1e-12
    assert sweep.top == pytest.approx(0.49 * RATE)


# -- tone: THD+N -------------------------------------------------------------------------------
def test_a_perfect_tone_has_nothing_but_the_tone():
    signal = ts.tone(RATE, -20)
    r = ts.analyse_tone(np.tile(signal.period, 4), RATE)
    assert r.level_db == pytest.approx(-20, abs=0.001) and r.frequency == pytest.approx(1000, abs=0.01)
    assert r.thd_n_db < -180 and r.noise_db < -180


def test_thd_n_of_a_tone_with_a_known_harmonic_and_noise():
    rng = np.random.default_rng(7)
    a = 0.1                                                                # -20 dBFS
    n = np.arange(RATE * 8)
    tone = a * np.sin(2 * np.pi * 1000 * n / RATE)
    second = a * 1e-3 * np.sin(2 * np.pi * 2000 * n / RATE + 0.3)          # -60 dBc
    third = a * 10 ** (-80 / 20) * np.sin(2 * np.pi * 3000 * n / RATE)    # -80 dBc
    sigma = 1e-5                                                           # white noise, -97 dBFS over 0-24 kHz
    noise = rng.normal(0, sigma, len(n))
    r = ts.analyse_tone(tone + second + third + noise, RATE)
    fundamental = a * a / 2
    harmonics = (a * 1e-3) ** 2 / 2 + (a * 1e-4) ** 2 / 2
    in_band = sigma ** 2 * (20000 - 20) / (RATE / 2)                      # white: power spread evenly to Fs/2
    assert r.thd_db == pytest.approx(10 * math.log10(harmonics / fundamental), abs=0.05)
    assert r.harmonics[0][0] == 2 and r.harmonics[0][2] == pytest.approx(-60, abs=0.05)
    assert r.harmonics[1][2] == pytest.approx(-80, abs=0.1)
    expected = 10 * math.log10((harmonics + in_band) / (fundamental + harmonics + in_band))
    assert r.thd_n_db == pytest.approx(expected, abs=0.1)
    assert r.noise_db == pytest.approx(10 * math.log10(in_band / 0.5), abs=0.15)
    assert r.thd_n_percent == pytest.approx(100 * 10 ** (expected / 20), rel=0.02)


def test_a_slightly_wrong_clock_still_measures_cleanly():
    """A recording interface's clock is never exactly the player's: the tone falls between bins."""
    n = np.arange(RATE * 4)
    r = ts.analyse_tone(0.5 * np.sin(2 * np.pi * 1000.37 * n / RATE), RATE)
    assert r.frequency == pytest.approx(1000.37, abs=0.01)
    assert r.level_db == pytest.approx(-6.02, abs=0.01) and r.thd_n_db < -140


def test_the_picture_shows_a_tone_at_its_own_level():
    signal = ts.tone(RATE, -80)
    r = ts.analyse_tone(np.tile(signal.period, 2), RATE)
    m = ts.Measurement(signal, RATE, [r])
    freqs, levels, log_scale = ts.plot_points(m)
    assert log_scale and freqs[0] == pytest.approx(20, abs=1) and freqs[-1] < RATE / 2
    assert np.max(levels) == pytest.approx(-80, abs=0.05)
    assert freqs[np.argmax(levels)] == pytest.approx(1000, rel=0.01)


def test_undithered_16_bit_tone_shows_quantisation_distortion():
    signal = ts.tone(RATE, -20)
    q = np.round(np.tile(signal.period, 4) * 32768) / 32768
    r = ts.analyse_tone(q, RATE)
    assert -110 < r.noise_db < -100 and r.thd_n_db < -75                 # 16-bit steps, ~-101 dBFS


# -- J-test: sidebands -------------------------------------------------------------------------
def test_the_jtest_s_own_lines_match_the_square_wave_s_harmonics():
    """Fs/4 ± Fs/192 is the LSB square's 47th / 49th harmonic: 2 / (192 sin(πk/192)) LSB."""
    signal = ts.jtest(RATE, 16, ts.JTEST_TOP)
    r = ts.analyse_jtest(np.tile(signal.period, 2), RATE, 16, ts.JTEST_TOP)
    amp = 2 / (192 * math.sin(math.pi * 47 / 192)) * 2 ** -15
    assert r.sidebands[0].dbc == pytest.approx(20 * math.log10(amp / (math.sqrt(2) / 2)), abs=0.05)   # -123.8
    assert r.level_db == pytest.approx(-3.01, abs=0.01) and r.tone_hz == RATE / 4
    assert all(s.dbc == pytest.approx(s.own_dbc, abs=0.01) and not s.added for s in r.sidebands)
    assert r.worst is None and r.spur_dbc == ts.QUIET_DB


def jittered_jtest(rate, bits, jitter_s, jitter_hz, periods=3):
    """A 24-bit J-test whose Fs/4 tone is sampled with sinusoidal timing jitter (the LSB square
    stays perfect): the tone is A√2·sin(2π f0 t − 3π/4), the sampled form of [-A, -A, A, A]."""
    signal = ts.jtest(rate, bits, ts.JTEST_TOP)
    x = np.tile(signal.period, periods)
    n = np.arange(len(x))
    a = 0.5
    ideal = a * math.sqrt(2) * np.sin(2 * np.pi * (rate / 4) * n / rate - 3 * np.pi / 4)
    t = n / rate + jitter_s * np.sin(2 * np.pi * jitter_hz * n / rate)
    jittered = a * math.sqrt(2) * np.sin(2 * np.pi * (rate / 4) * t - 3 * np.pi / 4)
    return x - ideal + jittered


def test_known_jitter_gives_the_sidebands_it_should():
    f0 = RATE / 4
    x = jittered_jtest(RATE, 24, 100e-12, RATE / 192)                     # ±100 ps at the J-test's own Fs/192
    r = ts.analyse_jtest(x, RATE, 24, ts.JTEST_TOP)
    first = r.sidebands[0]
    assert first.dbc == pytest.approx(ts.from_jitter_ps(100, f0), abs=0.2)   # -108.5 dBc
    assert first.added and r.worst is first
    assert ts.jitter_ps(first.dbc, f0) == pytest.approx(100, rel=0.03)
    assert not r.sidebands[1].added                                       # pure sinusoidal: no 3rd-order pair
    m = ts.Measurement(ts.jtest(RATE, 24, ts.JTEST_TOP), RATE, [r, r])
    text = dict((label, text) for label, text, _ in ts.describe(m))["Sidebands"]
    assert "-108.5 dBc at ±250.0 Hz" in text and "about 100." in text and "ps of jitter" in text


def test_jitter_at_another_frequency_shows_as_another_line():
    x = jittered_jtest(RATE, 24, 1e-9, 50.0)                              # 1 ns at 50 Hz (mains)
    r = ts.analyse_jtest(x, RATE, 24, ts.JTEST_TOP)
    assert abs(r.spur_offset) == pytest.approx(50, abs=1.5)
    assert r.spur_dbc == pytest.approx(ts.from_jitter_ps(1000, RATE / 4), abs=0.3)
    assert r.worst is None                                                # not at the J-test's own places


def test_jitter_maths_round_trips():
    assert ts.jitter_ps(ts.from_jitter_ps(37.0, 11025), 11025) == pytest.approx(37.0)
    # β = 2π f0 J; each sideband β/2: 100 ps on 12 kHz -> 20·log10(π · 12000 · 1e-10)
    assert ts.from_jitter_ps(100, 12000) == pytest.approx(20 * math.log10(math.pi * 12000 * 1e-10))


# -- sweep: frequency response -----------------------------------------------------------------
def test_a_perfect_path_is_flat_whatever_the_delay_and_level():
    signal = ts.sweep(RATE, -20)
    recording = np.roll(np.tile(signal.period, 3), 12345) * 0.5             # late and 6 dB down
    r = ts.analyse_sweep(recording, signal)
    assert r.gain_db == pytest.approx(-6.02, abs=0.01)
    assert r.flat[0] > -0.01 and r.flat[1] < 0.01
    assert r.low_3db is None and r.high_3db is None
    assert r.freqs[0] < 25 and r.freqs[-1] > 20000


def test_frequency_response_of_a_known_filter():
    """A first-order low-pass y[n] = α·y[n-1] + (1-α)·x[n], applied in its steady state."""
    signal = ts.sweep(RATE, -20)
    alpha = 0.7
    f = np.fft.rfftfreq(signal.frames, 1 / RATE)
    h = (1 - alpha) / (1 - alpha * np.exp(-2j * np.pi * f / RATE))
    filtered = np.fft.irfft(np.fft.rfft(signal.period) * h, signal.frames)   # circular = steady state
    r = ts.analyse_sweep(np.tile(filtered, 2), signal)

    def gain(freq):
        return 20 * np.log10(np.abs((1 - alpha) / (1 - alpha * np.exp(-2j * np.pi * np.asarray(freq) / RATE))))
    assert np.max(np.abs(r.db - (gain(r.freqs) - gain(1000)))) < 0.05
    dense = np.linspace(1000, 20000, 200000)
    crossing = dense[np.argmax(gain(dense) - gain(1000) <= -3)]
    assert r.high_3db == pytest.approx(crossing, rel=0.02)
    assert r.low_3db is None


# -- the whole loop: seamless switcher -> recording -> measurement ------------------------------
@pytest.mark.parametrize("kind", ["tone", "jtest", "sweep"])
def test_seamless_loop_measures_perfect(kind):
    signal = ts.make(kind, RATE, -80, bits=16)                             # -80 dBFS, as the live loopback check
    switcher = audition.Switcher([signal.stereo()], RATE, seamless=True)
    switcher.reset(0)
    frames = int(signal.seconds_needed() * RATE)
    recording = np.concatenate([switcher.render(2048) for _ in range(frames // 2048 + 1)])[:frames]
    m = ts.measure(signal, recording, RATE)
    assert len(m.channels) == 2 and not m.notes
    rows = dict((label, (text, tone)) for label, text, tone in ts.describe(m))
    if kind == "tone":
        assert all(c.thd_n_db < -140 for c in m.channels)                  # float32 rounding only
        assert rows["THD+N"][1] == "good"
    elif kind == "jtest":
        assert all(c.worst is None for c in m.channels)
        assert rows["Sidebands"][0].startswith("✓ Nothing added")
    else:
        assert all(abs(lo) < 0.01 and abs(hi) < 0.01 for lo, hi in (c.flat for c in m.channels))
        assert rows["Response"][1] == "good"


def test_seamless_switcher_goes_round_without_a_dip():
    period = np.sin(2 * np.pi * np.arange(480) / 48)[:, None].astype(np.float32)
    s = audition.Switcher([period], RATE, seamless=True)
    s.reset(0)
    out = np.concatenate([s.render(1000) for _ in range(5)])
    tiled = np.tile(period, (11, 1))[:5000]
    fade = s.fade
    assert np.array_equal(out[fade:], tiled[fade:])                       # after the fade-in: the loop exactly
    plain = audition.Switcher([period], RATE, fade_s=0.001)
    plain.reset(0)
    assert np.any(plain.render(2000)[470:490] == 0)                      # the ordinary loop still dips


def test_wrong_rate_and_short_recordings_are_said_plainly():
    signal = ts.tone(RATE)
    m = ts.measure(signal, np.zeros((RATE * 5, 2)), 44100)
    assert m.channels == [] and "rates must match" in m.notes[0]
    assert ts.describe(m)[-1][2] == "warn"
    m = ts.measure(signal, np.zeros((RATE // 2, 2)), RATE)
    assert "too short" in m.notes[0]
    m = ts.measure(signal, np.zeros((RATE * 6, 2)), RATE)
    assert "Nothing came back" in ts.describe(m)[-1][1]
    loud = np.tile(ts.tone(RATE, 0).period, 6)
    m = ts.measure(signal, np.column_stack([loud, loud]), RATE)
    assert any(label == "Recording" and "clipped" in text for label, text, _ in ts.describe(m))


# -- the window ----------------------------------------------------------------------------------
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


class FakeStream:
    """Stands in for sounddevice.OutputStream: nothing is opened."""
    made = []

    def __init__(self, **kwargs):
        self.kwargs, self.callback = kwargs, kwargs["callback"]
        self.device, self.closed = 0, False
        FakeStream.made.append(self)

    def start(self):
        pass

    def abort(self):
        pass

    def close(self, ignore_errors=False):
        self.closed = True


class FakePlayer:
    output_device, exclusive = "Test out", True

    def __init__(self):
        self.suspended = 0

    def suspend(self):
        self.suspended += 1
        return True

    def status(self):
        return SimpleNamespace(state=State.PAUSED)


@pytest.fixture
def fake_devices(monkeypatch):
    from ironbark import testsignals_window as tw
    FakeStream.made = []
    connected = {"Test out": 3}
    monkeypatch.setattr(tw.devices, "find_device", lambda name, rescan_ok=True: connected.get(name))
    monkeypatch.setattr(tw.devices, "mixer_rate", lambda index: RATE)
    monkeypatch.setattr(tw.devices, "output_names", lambda: list(connected))
    monkeypatch.setattr(tw.devices, "input_names", lambda: ["Line in"])
    monkeypatch.setattr(tw.devices, "default_output_name", lambda: "Test out")
    monkeypatch.setattr(audition.devices, "find_device", lambda name, rescan_ok=True: connected.get(name))
    monkeypatch.setattr(audition.devices, "exclusive_supported", lambda: True)
    monkeypatch.setattr(audition.sd, "query_devices", lambda device: {"name": "Test out"})
    return connected


def test_window_asks_first_then_plays_records_and_measures(root, fake_devices):
    from ironbark.testsignals_window import LOOPBACK, TestSignalsWindow
    asked, recorded = [], []

    def recorder(source, output, rate, seconds):
        """A perfect loopback: what the stand-in sound card is asked to play."""
        recorded.append((source, output, rate))
        s = win.audition.switcher
        frames = int(seconds * rate)
        return np.concatenate([s.render(4096) for _ in range(frames // 4096 + 1)])[:frames], rate, []

    answer = {"ok": False}
    player = FakePlayer()
    prefs = {"testsignals_kind": "tone", "testsignals_level": -80.0}
    win = TestSignalsWindow(root, player, prefs, stream_factory=FakeStream, recorder=recorder,
                            confirm=lambda parent, signal, output: asked.append((signal.name, output)) or answer["ok"])
    assert win.input.get() == LOOPBACK and not win.exclusive.get()        # loopback can't record exclusive mode
    assert win.exclusive_check.instate(["disabled"])
    win.measure()                                                         # said no: nothing plays
    assert asked == [("1 kHz tone at -80.0 dBFS", "Test out")] and not FakeStream.made and player.suspended == 0
    assert "Not played" in win.status.cget("text")

    answer["ok"] = True
    win.measure()
    assert player.suspended == 1 and win.playing and win.measuring
    assert FakeStream.made[-1].kwargs["dtype"] == "float32" and FakeStream.made[-1].kwargs["device"] == 3
    assert win.measure_button.instate(["disabled"])
    deadline = time.time() + 30
    while win.measuring and time.time() < deadline:
        win.win.update()
        time.sleep(0.05)
        win._tick()
    assert recorded == [(LOOPBACK, "Test out", RATE)]
    assert not win.playing and FakeStream.made[-1].closed                 # the sound stops once measured
    texts = {child.cget("text") for child in win.results.winfo_children()}
    assert "THD+N" in texts and any("both channels" in t for t in texts)
    assert win.measurement.channels[0].thd_n_db < -140
    win.close()


def test_window_never_plays_on_a_device_that_isnt_there(root, fake_devices):
    from ironbark.testsignals_window import TestSignalsWindow
    asked = []
    win = TestSignalsWindow(root, FakePlayer(), {"testsignals_output": "Gone"}, stream_factory=FakeStream,
                            confirm=lambda *a: asked.append(a) or True)
    win.play_only()
    assert not asked and not FakeStream.made and "nothing was played" in win.status.cget("text")
    win.output.set("Test out")
    win.input.set("Line in")
    win._options_changed()
    assert win.exclusive_check.instate(["!disabled"])
    win.exclusive.set(True)
    win.play_only()
    assert win.playing and win.audition.exclusive and FakeStream.made[-1].kwargs["dtype"] == "int32"
    out = np.zeros((2048, 2), np.int32)
    FakeStream.made[-1].callback(out, 2048, None, None)                 # J-test-free check: the tone, 32-bit
    win.stop()
    assert not win.playing
    win.kind.set("jtest")
    win.level.set("-3 dBFS")
    win._options_changed()
    assert "6 dB steps" not in win.signal_line.cget("text")              # -3 is (nearly) the J-test's own level
    win.level.set("-20 dBFS")
    win._options_changed()
    assert "-21.07 dBFS" in win.signal_line.cget("text")
    win.close()


def test_stop_works_while_measuring_and_drops_that_measurement(root, fake_devices):
    import threading
    from ironbark.testsignals_window import TestSignalsWindow
    release = threading.Event()

    def slow_recorder(source, output, rate, seconds):
        release.wait(10)
        return np.zeros((int(seconds * rate), 2)), rate, []

    win = TestSignalsWindow(root, FakePlayer(), {"testsignals_level": -80.0}, stream_factory=FakeStream,
                            recorder=slow_recorder, confirm=lambda *a: True)
    win.measure()
    assert win.playing and win.measuring
    assert win.stop_button.instate(["!disabled"])                       # the tone can always be stopped
    assert "<Key-space>" in win.win.bind()                               # space stops it too
    win.stop()
    assert not win.playing and not win.measuring and FakeStream.made[-1].closed
    assert "no measurement" in win.status.cget("text")
    release.set()
    for thread in threading.enumerate():
        if thread.name == "test-signals":
            thread.join(5)
    win._tick()
    assert win.measurement is None                                       # the late recording is dropped
    win.close()


def test_a_dac_plugged_in_after_start_up_is_found(root, fake_devices, monkeypatch):
    from ironbark import testsignals_window as tw
    events = []

    def find_device(name, rescan_ok=True):
        if name == "New DAC" and rescan_ok and name not in fake_devices:
            events.append("rescan")                                      # PortAudio looks again and finds it
            fake_devices[name] = 4
        return fake_devices.get(name)

    monkeypatch.setattr(tw.devices, "find_device", find_device)
    monkeypatch.setattr(audition.devices, "find_device", find_device)
    monkeypatch.setattr(tw.devices, "output_names", lambda: ["Test out", "New DAC"])   # Windows lists it already
    player = FakePlayer()
    player.suspend = lambda: events.append("suspend") or True
    win = tw.TestSignalsWindow(root, player, {"testsignals_output": "New DAC"}, stream_factory=FakeStream,
                               confirm=lambda *a: True)
    assert win.output_box.cget("postcommand")                            # the list is read again when opened
    win.play_only()
    assert win.playing and FakeStream.made[-1].kwargs["device"] == 4
    assert events.index("suspend") < events.index("rescan")             # the player let go of the device first
    win.close()


def test_a_dac_windows_doesn_t_know_still_plays_nothing(root, fake_devices, monkeypatch):
    from ironbark import testsignals_window as tw
    rescans = []
    monkeypatch.setattr(tw.devices, "find_device",
                        lambda name, rescan_ok=True: rescans.append(name) if rescan_ok else fake_devices.get(name))
    win = tw.TestSignalsWindow(root, FakePlayer(), {"testsignals_output": "Gone"}, stream_factory=FakeStream,
                               confirm=lambda *a: True)
    win.play_only()
    assert rescans == [] and not FakeStream.made and "nothing was played" in win.status.cget("text")
    win.close()

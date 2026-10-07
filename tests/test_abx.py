"""The ABX blind test: p-values against published tables, the trial logic (score hidden till the
end), the log, lining up the excerpts, the switching (audition.py) through a stand-in sound card,
the player letting go of the device (Player.suspend) — and the window (skipped without a display).
No real sound device is opened anywhere here."""
import gc
import random
import time
import tkinter as tk
from types import SimpleNamespace

import numpy as np
import pytest
import sounddevice as sd
import soundfile as sf

from ironbark import abx, audition, engine, settings, theme
from ironbark.engine import Player, State
from ironbark.library import Track

RATE = 44100
RNG = np.random.default_rng(3)


# -- p-values -----------------------------------------------------------------------------------
# Published binomial table, p = 0.50 column, P(X = k) to 4 decimals: University of Toronto,
# Department of Statistical Sciences, "Binomial Probability Table"
# (https://sol.utstat.utoronto.ca/extraresources/BinomialDistributionTable.pdf), read 04/10/2026.
TABLE_P50 = {
    10: [0.0010, 0.0098, 0.0439, 0.1172, 0.2051, 0.2461, 0.2051, 0.1172, 0.0439, 0.0098, 0.0010],
    12: [0.0002, 0.0029, 0.0161, 0.0537, 0.1208, 0.1934, 0.2256, 0.1934, 0.1208, 0.0537, 0.0161, 0.0029,
         0.0002],
    15: [0.0000, 0.0005, 0.0032, 0.0139, 0.0417, 0.0916, 0.1527, 0.1964, 0.1964, 0.1527, 0.0916, 0.0417,
         0.0139, 0.0032, 0.0005, 0.0000],
    20: [0.0000, 0.0000, 0.0002, 0.0011, 0.0046, 0.0148, 0.0370, 0.0739, 0.1201, 0.1602, 0.1762, 0.1602,
         0.1201, 0.0739, 0.0370, 0.0148, 0.0046, 0.0011, 0.0002, 0.0000, 0.0000],
}
# n = 16 (3 decimals): LibreTexts Statistics, "13.3: The Binomial Distribution Table for n = 16 to n = 18".
TABLE_16 = [0.000, 0.000, 0.002, 0.009, 0.028, 0.067, 0.122, 0.175, 0.196, 0.175, 0.122, 0.067, 0.028, 0.009,
            0.002, 0.000, 0.000]


@pytest.mark.parametrize("n", sorted(TABLE_P50))
def test_point_probabilities_match_the_published_table(n):
    for k, published in enumerate(TABLE_P50[n]):
        assert round(abx.point_probability(k, n), 4) == pytest.approx(published, abs=1e-9), (n, k)


def test_sixteen_trials_match_the_published_table():
    for k, published in enumerate(TABLE_16):
        assert round(abx.point_probability(k, 16), 3) == pytest.approx(published, abs=1e-9), k


def test_p_value_is_the_tail_of_the_table():
    for n, column in TABLE_P50.items():
        for k in range(n + 1):
            # The table's own figures summed (each rounded to 4 decimals) agree within that rounding.
            assert abx.p_value(k, n) == pytest.approx(sum(column[k:]), abs=0.0001 * (n + 1 - k) / 2 + 1e-9)
    # The ABX figure quoted from Leventhal's work (Stereophile, "The Highs & Lows of Double-Blind
    # Testing"): 12 or more right of 16 has a 0.0384 chance by guessing.
    assert round(abx.p_value(12, 16), 4) == 0.0384
    assert abx.needed(16) == 12 and abx.needed(10) == 9 and abx.needed(20) == 15
    assert abx.p_value(0, 16) == 1.0 and abx.p_value(16, 16) == 2 ** -16


# -- the test's logic ---------------------------------------------------------------------------
def test_trials_are_fixed_up_front_and_the_score_stays_hidden():
    test = abx.AbxTest(16, rng=random.Random(1))
    secret = [test._secret[i] for i in range(16)]
    assert set(secret) <= {"A", "B"}
    with pytest.raises(RuntimeError):
        test.score
    with pytest.raises(RuntimeError):
        test.rows()
    for i in range(16):
        assert test.trial == i and test.x == secret[i]
        test.answer(secret[i] if i < 13 else ("A" if secret[i] == "B" else "B"))   # 13 right, 3 wrong
    assert test.done and test.score == 13
    assert test.p == pytest.approx(abx.p_value(13, 16))
    verdict, heard = test.verdict()
    assert heard and verdict.startswith("13 of 16 right. You heard a difference")
    with pytest.raises(RuntimeError):
        test.answer("A")
    with pytest.raises(RuntimeError):
        test.x


def test_a_chance_score_is_not_proof_of_sameness():
    test = abx.AbxTest(10, rng=random.Random(2))
    for i in range(10):
        test.answer(test.x if i % 2 else ("A" if test.x == "B" else "B"))
    verdict, heard = test.verdict()
    assert test.score == 5 and not heard
    assert "Not proven" in verdict and "doesn't show they sound the same" in verdict and "9 of 10" in verdict


def test_log_is_saved_in_the_abx_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "Ironbark" / "settings.json")
    a = np.zeros((RATE, 2), np.float32)
    pair = abx.Pair(tmp_path / "a.flac", tmp_path / "b.flac", a, a, RATE, 61.0, level_a=-14.0, level_b=-14.05)
    test = abx.AbxTest(8, rng=random.Random(4))
    while not test.done:
        test.answer(test.x)
    path = abx.save_log(abx.log_text(test, pair, output="LG test output, shared mode, volume 0%", matched=True))
    assert path.parent == tmp_path / "Ironbark" / "ABX" and path.name.startswith("ABX_")
    text = path.read_text(encoding="utf-8")
    assert "Score: 8 of 8" in text and "p-value: 0.003906" in text
    assert "Excerpt: 1:01–1:02 of A" in text and "Level matching: on" in text
    assert text.count("  yes  ") == 8
    assert abx.save_log("again", folder=path.parent, when=test.finished) != path   # never overwrites


# -- lining up the excerpts -----------------------------------------------------------------------
def music(seconds, rate=RATE, channels=2):
    n = int(seconds * rate)
    x = RNG.standard_normal((n, channels))
    spectrum = np.fft.rfft(x, axis=0)
    spectrum[int(15000 * n / rate):] = 0
    x = np.fft.irfft(spectrum, n=n, axis=0)
    return 0.3 * x / np.max(np.abs(x))


def test_prepare_lines_b_up_and_measures_the_levels(tmp_path):
    x = music(20)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(a, x, RATE, subtype="FLOAT")
    sf.write(b, np.concatenate([np.zeros((300, 2)), x * 10 ** (-0.5 / 20)]), RATE, subtype="FLOAT")   # later, quieter
    pair = abx.prepare(a, b, start_s=5.0, length_s=6.0)
    assert pair.rate == RATE and pair.a.shape == (6 * RATE, 2) and pair.b.shape == pair.a.shape
    assert pair.lined_up and pair.offset_s * RATE == pytest.approx(300)
    assert np.allclose(pair.b, pair.a * 10 ** (-0.5 / 20), atol=1e-6)   # sample for sample
    assert pair.level_unit == "LUFS" and pair.difference_db == pytest.approx(-0.5, abs=0.001)
    assert not pair.levels_match
    text, fine = abx.describe_levels(pair)
    assert not fine and "A is 0.50 dB louder" in text
    ga, gb = audition.level_gains(pair.difference_db)
    assert gb == 1.0 and 20 * np.log10(ga) == pytest.approx(-0.5)      # the louder one turned down


def test_prepare_upsamples_the_lower_rate_and_doubles_mono(tmp_path):
    x = music(12, channels=1)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(a, x, RATE, subtype="FLOAT")
    from ironbark.proof import resample
    sf.write(b, np.repeat(resample(x, RATE, 96000), 2, axis=1), 96000, subtype="FLOAT")
    pair = abx.prepare(a, b, start_s=3.0, length_s=5.0)
    assert pair.rate == 96000 and pair.channels == 2
    assert any("A was upsampled 44.1 → 96 kHz" in n for n in pair.notes)
    assert abs(pair.offset_s) * 96000 <= 1
    assert abs(pair.difference_db) < 0.05


# -- switching ------------------------------------------------------------------------------------
def ramp_slots(frames=20000):
    """Each slot's samples say which slot and which frame they came from."""
    t = np.arange(frames, dtype=np.float32)[:, None] / 1e6
    return [t + 0.1, t + 0.2, t + 0.1]   # slot 2 = the same values as slot 0 (X = A)


def test_switch_happens_at_the_same_sample_through_the_dip():
    s = audition.Switcher(ramp_slots(), 1000, fade_s=0.01)        # fade = 10 frames each way
    s.reset(0)
    first = s.render(100)                                         # fades in, then steady on slot 0
    assert first[0, 0] < 0.003 and first[-1, 0] == pytest.approx(0.1 + 99e-6)   # faded in from silence
    s.select(1)
    out = s.render(100)
    frames = 100 + np.arange(100)
    # Down over 10 frames on slot 0, up over 10 on slot 1 — and the music carries on at the next frame.
    assert np.all(out[:10, 0] <= 0.1 + frames[:10] * 1e-6 + 1e-6)
    assert out[9, 0] == 0                                         # silent at the bottom of the dip
    assert np.allclose(out[20:, 0], 0.2 + frames[20:] * 1e-6)    # slot 1, frame for frame
    assert s.position == 200 and s.slot == 1 and s.switches == 1


def test_every_switch_sounds_the_same_whatever_the_slots_hold():
    """A → X when X holds A must dip exactly like A → B: otherwise the switch gives it away."""
    def envelope(target):
        s = audition.Switcher([np.ones((4000, 1))] * 3, 1000, fade_s=0.01)
        s.reset(0)
        s.render(200)
        s.select(target)
        return s.render(40)[:, 0]
    to_b, to_x = envelope(1), envelope(2)
    assert np.array_equal(to_b, to_x)
    assert to_b.min() == 0 and to_b[-1] == 1.0
    s = audition.Switcher([np.ones((4000, 1))] * 3, 1000, fade_s=0.01)
    s.reset(0)
    s.render(200)
    s.select(0)                                                   # the one already playing: no dip
    assert np.all(s.render(40) == 1.0)


def test_loop_wraps_with_the_same_dip_and_gains_apply():
    s = audition.Switcher(ramp_slots(1000), 1000, loop=(100, 300), gains=[0.5, 1, 1], fade_s=0.01, volume=0.5)
    s.reset(0)
    out = s.render(400)[:, 0]
    # 200-frame loop: frames 100..299, then round again from 100.
    assert out[199] == 0 and out[200] < 0.001                     # the seam: down to silence, then up
    assert out[100] == pytest.approx((0.1 + 200e-6) * 0.25)       # steady: frame 200 at gain x volume
    assert out[300] == pytest.approx((0.1 + 200e-6) * 0.25)       # and the same frame next time round
    assert 100 <= s.position < 300


def test_assign_waits_for_the_dip_when_the_slot_is_heard():
    s = audition.Switcher(ramp_slots(), 1000, fade_s=0.01)
    s.reset(2)
    s.render(100)
    s.assign(2, ramp_slots()[1], gain=1.0)                        # the next trial's X is B
    out = s.render(60)[:, 0]
    assert out[9] == 0                                            # dipped, and back to the loop's start
    assert out[30] == pytest.approx(0.2 + 20e-6)
    s.assign(0, ramp_slots()[1] * 0, gain=1.0)                    # not heard: straight in
    assert not s.slots[0].any()


class FakeStream:
    """Stands in for sounddevice.OutputStream: nothing is opened; the test calls the callback."""
    made = []

    def __init__(self, **kwargs):
        self.kwargs, self.callback = kwargs, kwargs["callback"]
        self.device, self.started, self.closed = 0, False, False
        FakeStream.made.append(self)

    def start(self):
        self.started = True

    def abort(self):
        pass

    def close(self, ignore_errors=False):
        self.closed = True


@pytest.fixture
def no_device(monkeypatch):
    FakeStream.made = []
    monkeypatch.setattr(audition.devices, "find_device", lambda name: None)
    monkeypatch.setattr(audition.devices, "exclusive_supported", lambda: True)
    monkeypatch.setattr(audition.sd, "query_devices", lambda device: {"name": "Stand-in output"})


def test_audition_plays_through_the_callback_and_lets_go(no_device):
    s = audition.Switcher(ramp_slots(), RATE, volume=0.25)
    a = audition.Audition(s, stream_factory=FakeStream)
    a.play(1)
    stream = FakeStream.made[-1]
    assert stream.started and stream.kwargs["dtype"] == "float32" and stream.kwargs["samplerate"] == RATE
    out = np.zeros((4096, 1), np.float32)
    stream.callback(out, 4096, None, None)
    assert out[-1, 0] == pytest.approx((0.2 + 4095e-6) * 0.25)
    assert a.playing and a.device_name == "Stand-in output" and not a.exclusive
    a.play(0)                                                     # already playing: a switch, not a new stream
    assert len(FakeStream.made) == 1 and s.target == 0
    a.stop()
    assert stream.closed and not a.playing


def test_audition_in_bit_perfect_mode_sends_int32(monkeypatch, no_device):
    monkeypatch.setattr(audition.devices, "find_device", lambda name: 3)
    s = audition.Switcher([np.full((8000, 2), 0.5, np.float32)] * 3, RATE)
    a = audition.Audition(s, device="Test device", exclusive=True, stream_factory=FakeStream)
    a.play(0)
    stream = FakeStream.made[-1]
    assert a.exclusive and stream.kwargs["dtype"] == "int32" and stream.kwargs["device"] == 3
    out = np.zeros((4096, 2), np.int32)
    stream.callback(out, 4096, None, None)
    assert out[-1, 0] == 2 ** 30


def test_audition_falls_back_to_shared_when_exclusive_is_refused(monkeypatch, no_device):
    monkeypatch.setattr(audition.devices, "find_device", lambda name: 3)

    def picky(**kwargs):
        if kwargs["dtype"] == "int32":
            raise sd.PortAudioError("busy")
        return FakeStream(**kwargs)
    a = audition.Audition(audition.Switcher(ramp_slots(), RATE), exclusive=True, stream_factory=picky)
    a.play(0)
    assert not a.exclusive and "bit-perfect" in a.note


# -- the player lets go of the device ---------------------------------------------------------------
class EngineStream:
    def __init__(self, **kwargs):
        self.latency, self.device = 0.1, 0

    def start(self):
        pass

    stop = abort = close = start


@pytest.fixture
def fake_card(monkeypatch):
    monkeypatch.setattr(engine.sd, "OutputStream", EngineStream)
    monkeypatch.setattr(engine.devices, "find_device", lambda name: None)
    monkeypatch.setattr(engine.devices, "mixer_rate", lambda index: None)


def test_suspend_lets_go_of_the_device_and_play_carries_on(tmp_path, fake_card):
    path = tmp_path / "t.wav"
    sf.write(path, np.zeros((RATE * 20, 2)), RATE, subtype="PCM_16")
    player = Player()
    player.set_tracks([Track(path=path, title="t", duration=20.0)])
    player.play(0, start_at=7.5)
    assert player.suspend()
    assert player._session is None                               # the stream is closed: the device is free
    status = player.status()
    assert status.state is State.PAUSED and status.position == pytest.approx(7.5)
    player.seek(9.0)                                             # the seek bar still works while suspended
    player.toggle_pause()                                        # play: carries on from there
    assert player._session is not None and player.status().position == pytest.approx(9.0)
    assert player.status().state is State.PLAYING
    player.stop()
    assert player.status().state is State.STOPPED and not player.suspend()


# -- the window ---------------------------------------------------------------------------------------
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


class WindowPlayer:
    def __init__(self):
        self.volume, self.output_device, self.exclusive = 0.0, None, False
        self.suspended = 0

    def suspend(self):
        self.suspended += 1
        return True

    def status(self):
        return SimpleNamespace(state=State.PAUSED)


def test_abx_window_runs_a_whole_test_without_sound(root, tmp_path, monkeypatch, no_device):
    from ironbark.abx_window import AbxWindow
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    x = music(15)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(a, x, RATE, subtype="FLOAT")
    sf.write(b, x * 0.9, RATE, subtype="FLOAT")
    player = WindowPlayer()
    win = AbxWindow(root, player, {"abx_length": 5, "abx_trials": 8}, path_a=a, start_s=6.0,
                    stream_factory=FakeStream)
    win._set_file("B", b)
    win.load()
    deadline = time.time() + 20
    while win.pair is None and time.time() < deadline:
        win.win.update()
        time.sleep(0.05)
        win._tick()
    assert win.pair is not None and not win.pair.levels_match
    assert "Levels matched: A was 0.92 dB louder" in win.level_label.cget("text")
    win.match.set(False)
    win._gains_changed()
    assert "A is 0.92 dB louder" in win.level_label.cget("text") and win.audition.switcher.gains == [1.0, 1.0, 1.0]
    win.match.set(True)
    win._gains_changed()
    assert win.audition.switcher.gains[0] == pytest.approx(10 ** (-0.92 / 20), abs=0.001)
    assert "X" in win.play_buttons and win.play_buttons["X"].instate(["disabled"])   # no X before the test
    win.hear("A")
    assert player.suspended == 1 and win.playing
    assert win.audition.switcher.volume == 0.0                    # the player's volume (0 here)
    win.start_test()
    assert win.trial_label.cget("text") == "Trial 1 of 8: which is X?"
    win.hear("X")
    while not win.test.done:
        win.answer(win.test.x)
        if not win.test.done:
            assert "Trial" in win.trial_label.cget("text") and "right" not in win.result_label.cget("text")
    assert win.trial_label.cget("text") == "Result: 8 of 8"
    assert win.log_path is not None and win.log_path.parent == tmp_path / "app" / "ABX"
    assert "8 of 8 right. You heard a difference" in win.result_label.cget("text")
    win.close()
    assert not win.playing and FakeStream.made[-1].closed


def wait_loaded(win, seconds=20):
    deadline = time.time() + seconds
    while win._loading is not None and time.time() < deadline:
        time.sleep(0.05)
        win._tick()
        win.win.after_cancel(win._job)


def test_abx_keys_play_nothing_old_while_a_new_excerpt_loads_or_after_it_fails(root, tmp_path, no_device):
    from ironbark.abx_window import AbxWindow
    x = music(15)
    a, b, six = tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "six.wav"
    sf.write(a, x, RATE, subtype="FLOAT")
    sf.write(b, x * 0.9, RATE, subtype="FLOAT")
    sf.write(six, music(15, channels=6), RATE, subtype="FLOAT")
    player = WindowPlayer()
    win = AbxWindow(root, player, {"abx_length": 5}, path_a=a, start_s=6.0, stream_factory=FakeStream)
    win._set_file("B", b)
    win.load()
    wait_loaded(win)
    assert win.pair is not None and win.audition is not None
    win._set_file("B", six)                                 # B can't be paired with A (6 channels)
    win.load()
    win.hear("A")                                           # key a, while the new excerpt loads
    assert player.suspended == 0 and not win.playing
    wait_loaded(win)
    assert "Couldn't load the excerpt" in win.level_label.cget("text")
    win.hear("A")                                           # …and after it failed
    win.hear("B")
    assert player.suspended == 0 and not win.playing and len(FakeStream.made) == 0
    win.match.set(False)
    win._gains_changed()                                    # (raised AttributeError: no pair)
    win.close()


def test_abx_starts_from_a_cue_track_not_its_whole_file(root, tmp_path, no_device):
    from ironbark.abx_window import AbxWindow
    from ironbark.audiofile import CuePath, CueRange
    image = tmp_path / "image.wav"
    sf.write(image, music(60), RATE, subtype="FLOAT")
    second = CuePath(str(image), cue=CueRange(sheet=str(tmp_path / "image.cue"), number=2, start=40 * RATE, end=None))
    win = AbxWindow(root, WindowPlayer(), {"abx_length": 5}, path_a=second, start_s=5.0, stream_factory=FakeStream)
    assert win._duration == pytest.approx(20.0, abs=0.01)  # the track, not the 60 s file
    assert win.start_s == pytest.approx(2.5)                # 5 s in, a 5 s excerpt around it
    assert win.rows["A"].path == second and "image.wav · track 2" in win.rows["A"].label.cget("text")
    win.close()

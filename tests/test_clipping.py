"""0.24.0: inter-sample overs — finding them (clipping.py), the clipping guard at 8x
with "My DAC has headroom", hearing them (Hear it), and the Clipping window
(smoke tests skip without a display)."""

from __future__ import annotations

import gc
import math
import queue
import time
import tkinter as tk
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import analysis, experiments, settings, theme
from ironbark.analysis import guard_trim, headroom_index, headroom_label, max_under_read, true_peak
from ironbark.audiofile import CuePath, CueRange
from ironbark.engine import State
from ironbark.tools import audition, clipping

RATE = 44100


def faded(x: np.ndarray, seconds: float = 0.2) -> np.ndarray:
    """Raised-cosine fade in and out (an abrupt start would ring past the tone's own height)."""
    n = int(seconds * RATE)
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)
    env = np.ones(len(x))
    env[:n], env[-n:] = ramp, ramp[::-1]
    return x * env


def quarter_tone(seconds=2.0, height=math.sqrt(2)) -> np.ndarray:
    """A tone at a quarter of the sample rate, 45° out: every sample lands at 0.707 of its
    height — with height √2 the samples sit exactly at full scale and the wave is +3.01 dB over."""
    n = np.arange(int(seconds * RATE))
    return faded(height * np.sin(np.pi / 2 * n + np.pi / 4))


def burst(freq, height, frames=2048, phase=0.3) -> np.ndarray:
    """A short band-limited burst (Hann-shaped) of a tone."""
    t = np.arange(frames) / RATE
    return np.hanning(frames) * height * np.sin(2 * np.pi * freq * t + phase)


def fine_peak(x: np.ndarray) -> float:
    """The real height of the rebuilt wave, near enough: 64x."""
    return float(np.abs(clipping.oversample(np.asarray(x).reshape(len(x), -1), 64)).max())


def bursts_file(tmp_path, name="bursts.wav"):
    """5 s of near-silence with three bursts: two over full scale, one under."""
    x = np.zeros(5 * RATE)
    places = {1.0: burst(9000, 1.3), 2.5: burst(15000, 1.15, phase=1.1), 4.0: burst(3000, 0.9)}
    for at, b in places.items():
        start = int(at * RATE) - len(b) // 2
        x[start:start + len(b)] += b
    path = tmp_path / name
    sf.write(path, np.column_stack([x, x * 0.5]).astype("float32"), RATE, subtype="FLOAT")
    return path, x, places


# -- the standard's figures ------------------------------------------------------------------
def test_under_read_matches_bs1770_5_annex_2_table():
    # ITU-R BS.1770-5 Annex 2, Attachment 1: 4x 0.554 / 0.688 dB, 8x 0.136 / 0.169 dB (f = 0.45 / 0.5 fs).
    assert max_under_read(4, 0.45) == pytest.approx(0.554, abs=0.0005)
    assert max_under_read(4, 0.5) == pytest.approx(0.688, abs=0.0005)
    assert max_under_read(8, 0.45) == pytest.approx(0.136, abs=0.0005)
    assert max_under_read(8, 0.5) == pytest.approx(0.169, abs=0.0005)
    assert analysis.GUARD_SPARE_DB >= max_under_read(8, 0.5)     # the guard's spare covers 8x's worst


def test_our_8x_filter_under_reads_no_more_than_the_standard_says():
    n = np.arange(RATE // 2)
    worst = {4: 0.0, 8: 0.0}
    for phase in np.linspace(0, np.pi, 25):
        x = faded(np.sin(2 * np.pi * 0.4 * n + phase), 0.05)[:, None]
        for factor in worst:
            y = clipping.oversample(x, factor)[RATE // 8 * factor: -RATE // 8 * factor]
            worst[factor] = max(worst[factor], -20 * math.log10(float(np.abs(y).max())))
    assert worst[8] <= max_under_read(8, 0.4) + 0.01
    assert worst[4] > 3 * worst[8]                                # 8x is far closer


def test_oversample_keeps_the_samples_and_decimate_undoes_it():
    rng = np.random.default_rng(3)
    spectrum = np.fft.rfft(rng.standard_normal((RATE, 2)) * 0.1, axis=0)
    spectrum[np.fft.rfftfreq(RATE, 1 / RATE) > 0.4 * RATE] = 0
    x = np.fft.irfft(spectrum, RATE, axis=0)
    up = clipping.oversample(x)
    assert np.abs(up[::8] - x).max() < 1e-4                       # phase 0 is the samples
    back = clipping.decimate(up)
    assert np.abs(back - x)[1000:-1000].max() < 1e-3 * np.abs(x).max()


# -- finding overs ---------------------------------------------------------------------------
def test_quarter_tone_at_45_degrees_is_3_01_db_over(tmp_path):
    x = quarter_tone()
    assert np.abs(x).max() <= 1.0 + 1e-9                          # no sample past full scale
    path = tmp_path / "tone.wav"
    sf.write(path, np.column_stack([x, x]).astype("float64"), RATE, subtype="DOUBLE")
    result = clipping.scan(path)
    assert len(result.overs) == 1                                 # one long over (the whole tone)
    assert result.worst.db == pytest.approx(10 * math.log10(2), abs=0.02)   # +3.01 dB
    assert result.true_peak == pytest.approx(math.sqrt(2), rel=0.003)
    assert clipping.summary(result).startswith("1 over, worst +3.0 dB at 0:0")


def test_bursts_are_found_where_they_are_with_their_real_height(tmp_path):
    path, x, places = bursts_file(tmp_path)
    result = clipping.scan(path)
    assert [round(o.at, 1) for o in result.overs] == [1.0, 2.5]   # the 0.9 burst isn't over
    for over, at in zip(result.overs, (1.0, 2.5)):
        real = fine_peak(places[at])
        assert over.db == pytest.approx(20 * math.log10(real), abs=0.17)   # within 8x's worst under-read
        assert over.start <= over.at <= over.end
    assert result.worst is result.overs[0]
    worst = result.overs[0]
    assert clipping.summary(result) == f"2 overs, worst {worst.db:+.1f} dB at {clipping.fmt_clock(worst.at)}"


def test_overs_found_alike_whatever_the_block_size():
    x = np.zeros((3 * RATE, 2))
    for at in (0.5, 0.51, 1.7):                                  # 10 ms apart counts as one
        b = burst(12000, 1.25, frames=256)
        start = int(at * RATE)
        x[start:start + len(b), 0] += b
    found = []
    for size in (777, 4096, len(x)):
        finder = clipping.OverFinder(2, RATE)
        for k in range(0, len(x), size):
            finder.add(x[k:k + size])
        finder.finish()
        found.append([(round(o.at, 4), round(o.db, 4)) for o in finder.overs])
    assert found[0] == found[1] == found[2]
    assert len(found[0]) == 2


def test_no_overs_summary(tmp_path):
    path = tmp_path / "quiet.wav"
    sf.write(path, np.column_stack([burst(1000, 0.5, RATE)] * 2), RATE, subtype="FLOAT")
    result = clipping.scan(path)
    assert result.overs == [] and clipping.summary(result).startswith("No overs")


def test_a_cue_track_is_scanned_on_its_own_with_its_own_times(tmp_path):
    path, x, places = bursts_file(tmp_path, "image.wav")
    second = CuePath(path, cue=CueRange(str(tmp_path / "image.cue"), 2, 2 * RATE, None))   # from 2.0 s
    result = clipping.scan(second)
    assert result.duration == pytest.approx(3.0)
    assert [round(o.at, 1) for o in result.overs] == [0.5]        # the 2.5 s burst, 0.5 s into track 2
    versions = clipping.listening_versions(second, result.overs[0].at)
    assert versions.start == pytest.approx(0.0)                   # the loop can't start before the track
    assert len(versions.a) == 3 * RATE                            # …and stays inside it


# -- the clipping guard ----------------------------------------------------------------------
def test_guard_leaves_tracks_alone_until_they_would_clip():
    assert guard_trim(0.0) == 1.0 and guard_trim(0.99) == 1.0 and guard_trim(1.0) == 1.0
    trim = guard_trim(1.5)
    assert 20 * math.log10(1.5 * trim) == pytest.approx(-analysis.GUARD_SPARE_DB)


def test_guard_measured_at_8x_keeps_the_real_wave_under_full_scale():
    worst = 0.0
    for freq in (3000, 9000, 15000, 18000, 19845):
        for phase in np.linspace(0, np.pi, 9):
            b = burst(freq, 1.4, phase=phase)
            tp8 = float(np.abs(clipping.oversample(b[:, None], 8)).max())
            worst = max(worst, fine_peak(b) * guard_trim(tp8))
    assert worst <= 1.0


def test_headroom_setting_lets_overs_through_up_to_its_ceiling():
    assert settings.DEFAULTS["clip_headroom"] == 0.0                         # off by default
    assert analysis.GUARD_HEADROOM_CHOICES == (0.0, 1.0, 2.0, 3.0, 3.5)
    assert [headroom_label(v) for v in analysis.GUARD_HEADROOM_CHOICES] == ["Off", "+1 dB", "+2 dB", "+3 dB",
                                                                             "+3.5 dB"]
    assert headroom_index(3.5) == 4 and headroom_index("rubbish") == 0 and headroom_index(None) == 0
    assert guard_trim(1.2, headroom_db=2.0) == 1.0                # +1.6 dB: the DAC has room
    trim = guard_trim(1.5, headroom_db=2.0)                       # +3.5 dB: down to +2 dB less the spare
    assert 20 * math.log10(1.5 * trim) == pytest.approx(2.0 - analysis.GUARD_SPARE_DB)


def test_guard_true_peak_is_8x(tmp_path):
    n = np.arange(RATE)
    x = faded(np.sin(2 * np.pi * 0.4 * n + 0.3), 0.05)
    path = tmp_path / "high.wav"
    sf.write(path, np.column_stack([x, x]), RATE, subtype="DOUBLE")
    assert -20 * math.log10(true_peak(path)) <= max_under_read(8, 0.4) + 0.01


def test_guard_wins_over_bit_perfect_and_says_so():
    from ironbark.engine import OutputInfo
    from ironbark.statustext import output_line
    from ironbark.tools.details import describe_proof
    out = OutputInfo("DAC", 44100, 44100, exclusive=True, hardware_volume=True)
    text, tone = output_line(out, 1.0, trim=10 ** (-1.1 / 20))
    assert "clipping guard -1.1 dB" in text and "isn't bit-perfect (the guard comes first)" in text
    proof = SimpleNamespace(sent=10.0, checked=10.0, mismatch_at=None, untouched=False, changed_from=0.0)
    text, tone = describe_proof(proof, out, 1.0, 10 ** (-1.1 / 20))
    assert "clipping guard (it comes before Bit-perfect" in text and "not bit-perfect" in text
    assert tone == "warn"


def test_settings_say_by_just_enough():
    from pathlib import Path

    import ironbark.ui as ui
    source = Path(ui.__file__).read_text(encoding="utf-8")
    assert "by just enough" in source and "by about 1 dB" not in source


# -- hearing it ------------------------------------------------------------------------------
def test_simulated_dac_chops_at_full_scale():
    x = np.column_stack([burst(9000, 1.4, 4096)] * 2)
    up = clipping.oversample(x)
    assert np.abs(up).max() > 1.3
    assert np.abs(clipping.dac_output(up)).max() <= 1.0           # the converter's wave never passes FS
    chopped, clean = clipping.simulate_dac(x)
    assert np.abs(clean - x).max() < 1e-3                         # unchopped, the path changes nothing
    assert np.abs(chopped - clean).max() > 0.05                   # the chop is real…
    quiet = np.abs(x[:, 0]) < 0.05                                # …and only near the over
    assert np.abs(chopped - clean)[quiet].max() < np.abs(chopped - clean).max()


def test_listening_versions_are_loudness_matched_and_never_clip(tmp_path):
    path, x, places = bursts_file(tmp_path)
    v = clipping.listening_versions(path, 1.0)
    assert v.rate == RATE and v.a.shape == v.b.shape == v.difference.shape == (4 * RATE, 2)
    assert v.start == 0.0                                          # 4 s around 1.0 s: from the start
    assert abs(v.matched_db) <= 0.1                                # matched (warn above 0.1 dB, as ABX)
    assert v.true_peak > 1.0 and v.gain_b == pytest.approx(guard_trim(v.true_peak), rel=0.01)
    assert fine_peak(v.b[:, 0] * v.gain_b) <= 1.0                  # B: turned down just below its peak
    assert fine_peak(v.a[:, 0] * v.gain_a) <= 1.0                  # A as played doesn't clip again
    assert np.abs(v.difference).max() > 0 and np.abs(v.difference).max() * v.gain_difference <= 0.5 + 1e-6


# -- the window (needs a display) ---------------------------------------------------------------
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


class WindowPlayer:
    def __init__(self, path):
        self.feed: queue.Queue = queue.Queue()
        self.track = SimpleNamespace(path=path)
        self.volume, self.output_device, self.exclusive = 0.0, None, False
        self.suspended, self.seeks, self.unmonitored = 0, [], False
        self.state = State.PLAYING

    def monitor(self):
        return self.feed

    def unmonitor(self, feed):
        self.unmonitored = True

    def status(self):
        return SimpleNamespace(track=self.track, position=1.0, state=self.state)

    def seek(self, seconds):
        self.seeks.append(seconds)

    def suspend(self):
        self.suspended += 1
        self.state = State.PAUSED
        return True

    def check_default(self):
        return None


def feed_blocks(player, x, path, rate=RATE):
    """Monitor-feed items for float samples, as int32 the player would send, all already heard."""
    past = time.perf_counter() - 10
    data = (np.clip(x, -1, 1 - 2 ** -31) * 2 ** 31).astype(np.int64).clip(-2 ** 31, 2 ** 31 - 1).astype(np.int32)
    for k in range(0, len(data), 4096):
        piece = data[k:k + 4096]
        player.feed.put({"frame": k, "dac_time": past, "rate": rate, "bits": 24, "source": piece, "sent": piece,
                         "track": "Test", "path": path})


def wait_for(win, condition, seconds=20):
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        win.win.update()
        time.sleep(0.05)
        win._tick()
    assert condition()


def test_clip_window_live_view_and_whole_song_strip(root, tmp_path, no_device):
    from ironbark.tools.clip_window import ClipWindow
    path, x, places = bursts_file(tmp_path)
    player = WindowPlayer(path)
    prefs = {}
    win = ClipWindow(root, player, prefs, stream_factory=FakeStream)
    win.win.geometry("900x640")
    win.win.update()
    # Live: the loudest moment heard — a 1.3 burst whose samples are all under full scale.
    stereo = np.column_stack([x, x * 0.5])
    feed_blocks(player, stereo[: int(1.2 * RATE)], path)
    win._tick()
    assert win.shown is not None and win.shown["peak"] > 1.0 and abs(win.shown["at"] - 1.0) < 0.05
    assert "overs 1" in win.readout.cget("text") and "dBTP" in win.readout.cget("text")
    assert win.canvas.find_all()                                  # the picture and its labels
    assert "past full scale" in win.info.cget("text")
    for zoom in ("mid", "close"):
        win.zoom.set(zoom)
        win._zoom_changed()
    assert prefs["clip_zoom"] == "close" and "Dots: the file's samples" in win.info.cget("text")
    win._toggle_freeze()
    assert win.frozen and "Frozen at 0:01" in win.info.cget("text")
    win._toggle_freeze()
    # The whole song: read in the background, summary, the worst marked, a click seeks.
    wait_for(win, lambda: win.scan_result is not None)
    win._tick()
    assert win.summary.cget("text").startswith("2 overs, worst +")
    x0, _, width, _ = win._strip_geometry()
    second = win.scan_result.overs[1]
    mark = x0 + second.at / win.scan_result.duration * (width - 1)
    assert win.over_near(mark + 2) is second and win.over_near(mark + 60) is None
    win._strip_click(SimpleNamespace(x=mark))
    assert win.chosen is second and player.seeks == [pytest.approx(second.at - 1.0)]
    assert "Chosen: 0:02.5" in win.chosen_label.cget("text")
    assert win.listen_frame is not None                       # Hear it is simply there (0.37.0)
    win.close()
    assert player.unmonitored


def test_hear_it_plays_a_and_b_and_the_difference(root, tmp_path, no_device):
    from ironbark.tools.clip_window import ClipWindow
    assert "clip_audition" not in experiments.known()           # rolled in at 0.37.0: simply there
    path, x, places = bursts_file(tmp_path)
    player = WindowPlayer(path)
    prefs = {"experiments": {"clip_audition": False}}            # an old settings file: ignored
    win = ClipWindow(root, player, prefs, stream_factory=FakeStream)
    win.win.update()
    assert win.listen_frame is not None
    wait_for(win, lambda: win.audition is not None)
    assert win.versions.start == pytest.approx(0.0, abs=0.01)    # 4 s around the worst over (1.0 s)
    text = win.level_label.cget("text")
    assert text.startswith("✓ Levels matched") and str(win.level_label.cget("style")) == "GoodValue.TLabel"
    win.hear("A")
    assert player.suspended == 1 and win.playing
    stream = FakeStream.made[-1]
    assert stream.kwargs["samplerate"] == RATE and stream.kwargs["channels"] == 2
    assert win.audition.switcher.volume == 0.0                    # the player's volume (0 here)
    win.hear("B")
    assert len(FakeStream.made) == 1 and win.audition.switcher.target == 1
    win.hear("D")
    assert win.audition.switcher.target == 2
    out = np.zeros((2048, 2), np.float32)
    stream.callback(out, 2048, None, None)                        # it renders (silently: volume 0)
    assert "Hearing just the difference" in win.hearing.cget("text")
    win.close()
    assert stream.closed


def test_clip_audition_lets_go_of_the_last_song_s_loop(root, tmp_path, no_device):
    from ironbark.tools.clip_window import ClipWindow
    path, x, places = bursts_file(tmp_path)
    clean = tmp_path / "clean.wav"
    sf.write(clean, np.zeros((3 * RATE, 2), np.float32), RATE, subtype="FLOAT")
    player = WindowPlayer(path)
    prefs = {}
    win = ClipWindow(root, player, prefs, stream_factory=FakeStream)
    win.win.update()
    wait_for(win, lambda: win.audition is not None)
    win.hear("A")
    assert win.playing and player.suspended == 1
    player.track = SimpleNamespace(path=clean)                    # the next song: no overs
    wait_for(win, lambda: win.scan_path == clean and win.scan_result is not None)
    assert not win.playing and FakeStream.made[-1].closed         # the old song's loop stopped…
    assert win.audition is None and win.versions is None          # …and is gone
    assert win.level_label.cget("text").startswith("No overs in this song")
    win.hear("A")
    assert player.suspended == 1 and len(FakeStream.made) == 1    # nothing old is played
    win.close()


def test_clip_window_says_so_when_its_work_fails(root, tmp_path, no_device, monkeypatch):
    from ironbark.tools.clip_window import ClipWindow
    path, x, places = bursts_file(tmp_path)

    def out_of_memory(*args, **kwargs):
        raise MemoryError("too big")
    real_scan = clipping.scan
    monkeypatch.setattr(clipping, "scan", out_of_memory)
    player = WindowPlayer(path)
    win = ClipWindow(root, player, {}, stream_factory=FakeStream)
    win.win.update()
    wait_for(win, lambda: win._scan_failed)
    win._tick()
    assert win.summary.cget("text") == "This file couldn't be read."      # not "Reading…" for ever
    win.close()
    # Making the A/B loop fails: said so, and another over can still be tried.
    monkeypatch.setattr(clipping, "scan", real_scan)
    monkeypatch.setattr(clipping, "listening_versions", out_of_memory)
    prefs = {}
    win = ClipWindow(root, player, prefs, stream_factory=FakeStream)
    win.win.update()
    wait_for(win, lambda: win._loading is None and "Couldn't" in win.hearing.cget("text"))
    assert win.level_label.cget("text") != "Making the loop…"
    win.close()


# -- in the player (the real window on temp settings; skipped without a display) ---------------------
from test_light_mini import _pump, make_window  # noqa: E402,F401  (make_window is a fixture)


def test_player_guard_follows_the_headroom_setting_and_key_c_opens_the_window(make_window):
    window, root, player, errors = make_window(clip_guard=True)
    path = player.track.path
    window._open_settings()
    _pump(root, 0.2)
    assert window.headroom_box.get() == "Off"                     # off by default
    window._peaks[path] = 1.2                                     # +1.6 dB true peak
    window._apply_guard(player.track, 0)
    assert player.trim_for(path) == pytest.approx(guard_trim(1.2))
    window.headroom_box.current(2)                                # +2 dB: this DAC can take it
    window._headroom_chosen()
    assert window.prefs["clip_headroom"] == 2.0
    window._apply_guard(player.track, 0)
    assert player.trim_for(path) == 1.0
    window.show_clipping()
    assert window.tool_window("clipping") is not None and window.tool_window("clipping").win.title() == "Clipping"
    window.tool_window("clipping").close()
    assert not errors

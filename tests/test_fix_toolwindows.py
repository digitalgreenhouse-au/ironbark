"""The tool windows together: a refresh loop that keeps going after an error (and doesn't fill
crash.log with it), no drawing while a window is minimised, and one sound at a time between the
windows that play their own (ABX, Clipping → Hear it, Test signals). No sound device is opened:
stand-in streams, as in the windows' own tests. Skipped without a display."""

from __future__ import annotations

import gc
import queue
import time
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import devices, theme
from ironbark.engine import State
from ironbark.tools import audition

RATE = 44100


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    r.withdraw()
    theme.apply(r)
    errors = []
    r.report_callback_exception = lambda *info: errors.append(info)
    r.errors = errors
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


@pytest.fixture
def stand_in_devices(monkeypatch):
    FakeStream.made = []
    connected = {"Test out": 3}
    monkeypatch.setattr(devices, "find_device", lambda name, rescan_ok=True: connected.get(name) if name else 3)
    monkeypatch.setattr(devices, "exclusive_supported", lambda: True)
    monkeypatch.setattr(devices, "output_names", lambda: list(connected))
    monkeypatch.setattr(devices, "input_names", lambda: ["Line in"])
    monkeypatch.setattr(devices, "default_output_name", lambda: "Test out")
    monkeypatch.setattr(devices, "mixer_rate", lambda index: RATE)
    monkeypatch.setattr(audition.sd, "query_devices", lambda device: {"name": "Test out"})


class AnyPlayer:
    """Enough of the player for every tool window; plays nothing."""
    volume, output_device, exclusive = 0.0, "Test out", False

    def __init__(self):
        self.feed: queue.Queue = queue.Queue()
        self.track, self.state, self.suspended = None, State.STOPPED, 0

    def monitor(self):
        return self.feed

    def unmonitor(self, feed):
        pass

    def status(self):
        return SimpleNamespace(track=self.track, position=0.0, state=self.state, duration=0.0, index=0)

    def suspend(self):
        self.suspended += 1
        self.state = State.PAUSED
        return True

    def seek(self, seconds):
        pass

    def check_default(self):
        return None


def fake_audition():
    """A sound that's playing (for the windows' "stop if the player plays" check)."""
    return SimpleNamespace(playing=True, switcher=SimpleNamespace(volume=0.0), stop=lambda: None,
                           device_name="Test out", exclusive=False, note="")


def make(kind, root, player):
    """(window, object, attribute): the window, and something its every refresh uses."""
    if kind == "meters":
        from ironbark.tools.meter_window import MeterWindow
        win = MeterWindow(root, player, {"vu_align": "-18 dBFS (EBU R68)"})
        return win, win, "_draw_live"
    if kind == "pcm":
        from ironbark.tools.pcm_window import PcmWindow
        win = PcmWindow(root, player, {})
        return win, win, "_drain"
    if kind == "spectrogram":
        from ironbark.tools.spectrogram_window import SpectrogramWindow
        win = SpectrogramWindow(root, player, {})
        return win, win, "_drain"
    if kind == "scope":
        from ironbark.tools.scope_window import ScopeWindow
        win = ScopeWindow(root, player, {})
        return win, win.engine, "reading_at"
    if kind == "clipping":
        from ironbark.tools.clip_window import ClipWindow
        win = ClipWindow(root, player, {}, stream_factory=FakeStream)
        return win, win, "_drain"
    if kind == "abx":
        from ironbark.tools.abx_window import AbxWindow
        win = AbxWindow(root, player, {}, stream_factory=FakeStream)
        win.audition = fake_audition()
        return win, player, "status"
    if kind == "testsignals":
        from ironbark.tools.testsignals_window import TestSignalsWindow
        win = TestSignalsWindow(root, player, {}, stream_factory=FakeStream, confirm=lambda *a: False)
        win.audition, win.signal = fake_audition(), win.build_signal()
        return win, player, "status"
    from ironbark.tools.details import NowPlayingWindow
    win = NowPlayingWindow(root, player, prefs={})
    return win, player, "status"


KINDS = ["meters", "pcm", "spectrogram", "scope", "clipping", "abx", "testsignals", "now playing"]


def break_it(target, name, times=1):
    """Make target.name raise for its next `times` calls, then work again."""
    real = getattr(target, name)
    left = [times]

    def broken(*args, **kwargs):
        if left[0] > 0:
            left[0] -= 1
            raise RuntimeError("broken for a moment")
        return real(*args, **kwargs)

    setattr(target, name, broken)


def pending(win) -> bool:
    """Is the window's next refresh lined up?"""
    return win._job in win.win.tk.splitlist(win.win.tk.call("after", "info"))


@pytest.mark.parametrize("kind", KINDS)
def test_a_window_keeps_refreshing_after_an_error(root, stand_in_devices, kind):
    win, target, name = make(kind, root, AnyPlayer())
    win.win.update()
    break_it(target, name)
    win._tick()                                       # (the error used to end the refresh loop for good)
    assert pending(win)
    assert len(root.errors) == 1 and "broken for a moment" in str(root.errors[0][1])   # reported (crash.log)
    win._tick()
    assert pending(win) and len(root.errors) == 1
    win.close()


def test_an_error_that_keeps_happening_is_reported_once(root, stand_in_devices):
    win, target, name = make("pcm", root, AnyPlayer())
    win.win.update()
    break_it(target, name, times=50)
    for _ in range(5):
        win._tick()
        win.win.after_cancel(win._job)
    assert len(root.errors) == 1                      # not 30 tracebacks a second in crash.log
    win.close()


@pytest.mark.parametrize("kind,drawing", [("meters", "_draw_live"), ("pcm", "_paint_live"), ("spectrogram", "_draw"),
                                          ("clipping", "_draw_live"), ("now playing", "_update_live")])
def test_a_minimised_window_draws_nothing(root, stand_in_devices, kind, drawing, tmp_path):
    player = AnyPlayer()
    if kind == "now playing":   # (Now Playing shows its live figures only while something plays)
        from ironbark.engine import Health, OutputInfo, Status
        from ironbark.library import read_track
        path = tmp_path / "01 Song.flac"
        sf.write(path, np.zeros((RATE, 2)), RATE, subtype="PCM_16")
        track = read_track(path)
        player.status = lambda: Status(State.PLAYING, 0, track, 0.5, track.duration,
                                       output=OutputInfo("Test out", RATE, RATE, False), levels=(0.1, 0.1),
                                       health=Health())
    win, _, _ = make(kind, root, player)
    win.win.update()
    drawn = []
    real = getattr(win, drawing)
    setattr(win, drawing, lambda *a, **k: drawn.append(1) or real(*a, **k))
    win.win.iconify()
    win.win.update()
    for _ in range(3):
        win._tick()
        win.win.after_cancel(win._job)
    assert drawn == []
    win.win.deiconify()
    win.win.update()
    win._tick()
    assert drawn                                      # drawn again once it's back on screen
    win.close()
    assert not root.errors


def write_pair(tmp_path):
    rng = np.random.default_rng(4)
    x = 0.1 * rng.standard_normal((RATE * 12, 2))
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(a, x, RATE, subtype="FLOAT")
    sf.write(b, x * 0.9, RATE, subtype="FLOAT")
    return a, b


def test_one_sound_at_a_time_between_the_windows(root, stand_in_devices, tmp_path):
    from ironbark.tools.abx_window import AbxWindow
    from ironbark.tools.testsignals_window import TestSignalsWindow
    a, b = write_pair(tmp_path)
    player = AnyPlayer()
    abx = AbxWindow(root, player, {"abx_length": 5}, path_a=a, start_s=4.0, stream_factory=FakeStream)
    abx._set_file("B", b)
    abx.load()
    deadline = time.time() + 20
    while abx._loading is not None and time.time() < deadline:
        time.sleep(0.05)
        abx._tick()
        abx.win.after_cancel(abx._job)
    assert abx.audition is not None
    signals = TestSignalsWindow(root, player, {"testsignals_level": -80.0}, stream_factory=FakeStream,
                                confirm=lambda *a: True)
    signals.play_only()
    assert signals.playing
    abx.hear("A")                                     # ABX starts: the test tone stops first
    assert abx.playing and not signals.playing and FakeStream.made[0].closed
    assert "another window" in signals.status.cget("text")
    signals.play_only()                               # and the other way round
    assert signals.playing and not abx.playing
    assert "another window" in abx.hearing.cget("text")
    open_streams = [s for s in FakeStream.made if not s.closed]
    assert len(open_streams) == 1                     # never two sounds at once
    abx.close()
    signals.close()
    assert not root.errors

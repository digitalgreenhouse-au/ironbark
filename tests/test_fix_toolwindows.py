"""The tool windows together: a refresh loop that keeps going after an error (and doesn't fill
crash.log with it), no drawing while a window is minimised, one sound at a time between the
windows that play their own (ABX, Clipping → Hear it, Test signals), and a closed window freed on
Tk's thread even while its worker thread is still busy. No sound device is opened: stand-in
streams, as in the windows' own tests. Skipped without a display."""

from __future__ import annotations

import gc
import queue
import threading
import time
import weakref
from pathlib import Path
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


# -- a closed window is freed on Tk's thread --------------------------------------------------------
# A worker thread that held its window would be the last to let go of it when its work outlasts the
# window, and the window's Tk variables and pictures would then be freed on that thread, which fails
# ("main thread is not in main loop"). Each worker below is held inside its slow step while the
# window closes; the window must already be gone once the main thread collects, the thread still busy.

def held_tk_variables(win) -> list[str]:
    import tkinter
    return [name for name, value in vars(win).items() if isinstance(value, tkinter.Variable)]


@pytest.mark.parametrize("kind", ["meters", "spectrogram", "clipping", "abx", "testsignals", "now playing"])
def test_a_closed_window_lets_go_of_its_tk_variables(root, stand_in_devices, kind):
    win, _, _ = make(kind, root, AnyPlayer())
    win.win.update()
    win.close()
    assert not held_tk_variables(win)
    assert not root.errors


def cd_album(tmp_path) -> list[Path]:
    """Two short CD-quality FLAC tracks in an album folder (rip verification wants 16-bit 44.1 kHz)."""
    folder = tmp_path / "Album"
    folder.mkdir()
    rng = np.random.default_rng(7)
    paths = []
    for n in (1, 2):
        path = folder / f"0{n}.flac"
        sf.write(path, 0.1 * rng.standard_normal((RATE * 2, 2)), RATE, subtype="PCM_16")
        paths.append(path)
    return paths


def now_playing(root, tmp_path, monkeypatch, **kwargs):
    from ironbark.library import read_track
    from ironbark.tools.details import NowPlayingWindow
    for cache in ("_analysis_cache", "_stored_when", "_index_cache", "_album_cache", "_log_cache"):
        monkeypatch.setattr(NowPlayingWindow, cache, {})   # (this test's results stay out of the others')
    player = AnyPlayer()
    player.track = read_track(cd_album(tmp_path)[0])
    win = NowPlayingWindow(root, player, prefs={}, **kwargs)
    win.win.update()
    return win


def slow_stored(root, tmp_path, monkeypatch, slow):
    return now_playing(root, tmp_path, monkeypatch, stored=slow)


def slow_frame_index(root, tmp_path, monkeypatch, slow):
    from ironbark.tools import details
    monkeypatch.setattr(details, "build_index", slow)
    return now_playing(root, tmp_path, monkeypatch)


def slow_check(root, tmp_path, monkeypatch, slow):
    from ironbark.tools import details
    monkeypatch.setattr(details, "analyse", slow)
    win = now_playing(root, tmp_path, monkeypatch, remember=lambda *args: None)
    win.check_button.invoke()
    return win


def slow_album(root, tmp_path, monkeypatch, slow):
    from ironbark.tools import details
    monkeypatch.setattr(details, "album_measure", slow)
    win = now_playing(root, tmp_path, monkeypatch)
    win.album_button.invoke()
    return win


def slow_rip_log(root, tmp_path, monkeypatch, slow):
    from ironbark.tools import ripcheck
    monkeypatch.setattr(ripcheck, "check_log", slow)
    win = now_playing(root, tmp_path, monkeypatch)
    win.log_button.invoke()
    return win


def slow_spectrogram(root, tmp_path, monkeypatch, slow):
    from ironbark.tools import spectrum
    from ironbark.tools.spectrogram_window import SpectrogramWindow
    monkeypatch.setattr(spectrum, "track_picture", slow)
    player = AnyPlayer()
    player.track = SimpleNamespace(path=cd_album(tmp_path)[0], lossy=False)
    win = SpectrogramWindow(root, player, {"spectrogram_view": "track"})
    win.win.update()
    win._step()
    return win


def slow_test_signals(root, tmp_path, monkeypatch, slow):
    from ironbark.tools.testsignals_window import TestSignalsWindow
    win = TestSignalsWindow(root, AnyPlayer(), {"testsignals_level": -80.0}, stream_factory=FakeStream,
                            recorder=slow, confirm=lambda *a: True)
    win.win.update()
    win.measure()
    return win


def slow_compare(root, tmp_path, monkeypatch, slow):
    from ironbark.tools import compare
    from ironbark.tools.compare_window import CompareWindow
    monkeypatch.setattr(compare, "compare_files", slow)
    a, b = write_pair(tmp_path)
    win = CompareWindow(root, {}, path_a=a, path_b=b)
    win.win.update()
    win.start()
    return win


def slow_abx(root, tmp_path, monkeypatch, slow):
    from ironbark.tools import abx
    from ironbark.tools.abx_window import AbxWindow
    monkeypatch.setattr(abx, "prepare", slow)
    a, b = write_pair(tmp_path)
    win = AbxWindow(root, AnyPlayer(), {"abx_length": 5}, path_a=a, start_s=4.0, stream_factory=FakeStream)
    win._set_file("B", b)
    win.win.after_cancel(win._reload_job)   # (loaded here and now, once)
    win.load()
    return win


SLOW_WORKERS = {"now playing: stored check": slow_stored, "now playing: frame index": slow_frame_index,
                "now playing: check": slow_check, "now playing: album": slow_album,
                "now playing: rip log": slow_rip_log, "spectrogram": slow_spectrogram,
                "test signals": slow_test_signals, "compare": slow_compare, "abx": slow_abx}


@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize("worker", list(SLOW_WORKERS))
def test_a_closed_window_is_freed_here_while_its_thread_works_on(root, stand_in_devices, tmp_path, monkeypatch,
                                                                 worker):
    go_on, busy = threading.Event(), []

    def slow(*args, **kwargs):
        busy.append(threading.current_thread())
        go_on.wait(20)
        raise RuntimeError("stopped by the test")   # (each window says a failure its own way)

    try:
        win = SLOW_WORKERS[worker](root, tmp_path, monkeypatch, slow)
        deadline = time.time() + 20
        while not busy and time.time() < deadline:
            time.sleep(0.01)
        assert busy, "the worker never started"
        thread = busy[0]
        win.close()
        assert not held_tk_variables(win)
        gone = weakref.ref(win)
        del win
        gc.collect()                                  # (here, on the main thread)
        assert thread.is_alive()                      # the worker is still busy…
        assert gone() is None                         # …but the window is already freed
    finally:
        go_on.set()
    thread.join(20)
    assert not thread.is_alive()
    assert not root.errors

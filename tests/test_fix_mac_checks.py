"""Three fixes from the Mac's checks of 0.51.0, tested on any platform with stand-ins:

- Test signals playing on and recording from one device goes through a single duplex stream where
  the platform needs it (Core Audio hangs a second, separate input stream on the same device), and a
  recording never waits for good on a device that stops sending sound.
- Test signals offers the system's own recording of the output only where there is one: Windows'
  loopback, the sound server's monitor on Linux; nothing on a Mac.
- The system's request to close (SIGTERM) runs the player's ordinary close, on Tk's thread.
"""

import signal
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from ironbark import platforms, quitsignal
from ironbark.tools import audition
from ironbark.tools import testsignals_window as tw

RATE = 48000


# -- the duplex stream (audition.py) --------------------------------------------------------------
class FakeDuplex:
    """Stands in for sounddevice.Stream: its input hears exactly what its output is asked to play,
    one block per `run` call. `deaf`: the input sends nothing (a device that stopped)."""
    made: list = []

    def __init__(self, **kwargs):
        self.kwargs, self.callback = kwargs, kwargs["callback"]
        self.device, self.started, self.closed, self.deaf = kwargs["device"], False, False, False
        FakeDuplex.made.append(self)

    def start(self):
        self.started = True

    def abort(self):
        pass

    def close(self, ignore_errors=False):
        self.closed = True

    def run(self, blocks: int, frames: int = 2048):
        """Like a cable from the output back into the input: each block, the input hears what
        the output played the block before (the first block hears silence)."""
        in_channels, out_channels = self.kwargs["channels"]
        heard = np.zeros((frames, in_channels), np.float32)
        for _ in range(blocks):
            if self.deaf:   # (a device that stopped: no callbacks at all)
                return
            out = np.zeros((frames, out_channels), np.float32)
            self.callback(heard, out, frames, None, None)
            heard = out[:, :in_channels].copy()


@pytest.fixture
def no_device_lookups(monkeypatch):
    FakeDuplex.made = []
    monkeypatch.setattr(audition.devices, "find_device", lambda name, rescan_ok=True: 5)
    monkeypatch.setattr(audition.devices, "exclusive_supported", lambda: True)
    monkeypatch.setattr(audition.sd, "query_devices", lambda device: {"name": "Scarlett 2i2"})


def _tone_switcher():
    tone = (0.25 * np.sin(2 * np.pi * 1000 * np.arange(RATE) / RATE)).astype(np.float32)
    return audition.Switcher([np.column_stack([tone, tone])], RATE, seamless=True)


def test_one_device_plays_and_records_through_one_duplex_stream(no_device_lookups):
    def never(**kwargs):
        raise AssertionError("no separate output stream")

    player = audition.Audition(_tone_switcher(), device="Scarlett 2i2", exclusive=True, stream_factory=never,
                               record_input=(5, 2), duplex_factory=FakeDuplex)
    player.play(0)
    stream = FakeDuplex.made[-1]
    assert stream.started and stream.device == (5, 5) and stream.kwargs["channels"] == (2, 2)
    assert stream.kwargs["dtype"] == "float32" and not player.exclusive and "shared mode" in player.note
    assert player.records and player.device_name == "Scarlett 2i2"
    got = {}
    worker = threading.Thread(target=lambda: got.update(take=player.recording(0.5, timeout=10)))
    worker.start()
    deadline = time.time() + 5
    while player._take is None and time.time() < deadline:
        time.sleep(0.01)
    stream.run(blocks=20)
    worker.join(10)
    audio, overflowed = got["take"]
    assert audio.shape == (RATE // 2, 2) and not overflowed
    assert not audio[:2048].any()   # (the first block: silence, as through a real cable)
    assert abs(np.sqrt(np.mean(audio[2048:, 0] ** 2)) - 0.25 / np.sqrt(2)) < 0.005   # the tone came back
    player.stop()
    assert stream.closed


def test_a_device_that_stops_sending_sound_is_given_up_on(no_device_lookups):
    player = audition.Audition(_tone_switcher(), device="Scarlett 2i2", record_input=(5, 2),
                               duplex_factory=FakeDuplex)
    player.play(0)
    stream = FakeDuplex.made[-1]
    stream.deaf = True
    started = time.perf_counter()
    with pytest.raises(TimeoutError, match="stopped sending sound"):
        player.recording(0.2, timeout=0.5)
    stream.run(blocks=3)   # (and nothing reaches it)
    assert time.perf_counter() - started < 3
    player.stop()


def test_stop_ends_a_recording_at_once(no_device_lookups):
    player = audition.Audition(_tone_switcher(), device="Scarlett 2i2", record_input=(5, 2),
                               duplex_factory=FakeDuplex)
    player.play(0)
    errors = []

    def record():
        try:
            player.recording(5.0, timeout=30)
        except RuntimeError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=record)
    worker.start()
    deadline = time.time() + 5
    while player._take is None and time.time() < deadline:
        time.sleep(0.01)
    player.stop()
    worker.join(3)
    assert not worker.is_alive() and errors == ["stopped before the recording was complete"]


def test_without_a_duplex_stream_there_is_nothing_to_record(no_device_lookups):
    class Out:
        def __init__(self, **kwargs):
            self.device = kwargs["device"]

        def start(self):
            pass

    player = audition.Audition(_tone_switcher(), device="Scarlett 2i2", stream_factory=Out)
    player.play(0)
    assert not player.records
    with pytest.raises(RuntimeError):
        player.recording(0.1)


# -- Test signals: which recording (testsignals_window.py) ----------------------------------------
@pytest.mark.parametrize("duplex_platform, same, expected", [
    (True, True, (7, 2)),     # a Mac, the interface's own input: one duplex stream
    (True, False, None),      # a Mac, another device's input: two streams are fine
    (False, True, None),      # elsewhere: two streams on one device are fine
])
def test_one_device_is_recognised_only_where_it_matters(monkeypatch, duplex_platform, same, expected):
    monkeypatch.setattr(platforms, "DUPLEX_SAME_DEVICE", duplex_platform)
    monkeypatch.setattr(tw.devices, "find_input", lambda name: 7)
    monkeypatch.setattr(tw.devices, "find_device", lambda name, rescan_ok=True: 7 if same else 3)
    monkeypatch.setattr(audition.sd, "query_devices",
                        lambda index: {"name": "Scarlett 2i2" if index == 7 else "Speakers", "max_input_channels": 2})
    assert tw.same_device("Scarlett 2i2", "Scarlett 2i2") == expected


def test_the_systems_recording_is_never_mistaken_for_the_same_device(monkeypatch):
    monkeypatch.setattr(platforms, "DUPLEX_SAME_DEVICE", True)
    monkeypatch.setattr(tw, "LOOPBACK", "The sound server's monitor (what the output plays)")
    assert tw.same_device("pipewire", tw.LOOPBACK) is None


def test_an_input_that_stops_sending_sound_ends_the_recording(monkeypatch):
    """The separate input stream (every platform's ordinary route) waits with a time limit."""
    class SilentInput:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(tw.devices, "find_input", lambda name: 1)
    monkeypatch.setattr(audition.sd, "query_devices", lambda index: {"max_input_channels": 2})
    monkeypatch.setattr(audition.sd, "InputStream", SilentInput)
    monkeypatch.setattr(tw, "RECORD_TIMEOUT_S", 0.3)
    started = time.perf_counter()
    with pytest.raises(RuntimeError, match="stopped sending sound"):
        tw.record("Line in", "Speakers", RATE, 0.2)
    assert time.perf_counter() - started < 3


def test_an_input_is_recorded_from_its_callback(monkeypatch):
    class Input:
        def __init__(self, **kwargs):
            self.callback, self.channels = kwargs["callback"], kwargs["channels"]

        def __enter__(self):
            block = np.full((4800, self.channels), 0.5, np.float32)
            for _ in range(3):
                self.callback(block, len(block), None, None)
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(tw.devices, "find_input", lambda name: 1)
    monkeypatch.setattr(audition.sd, "query_devices", lambda index: {"max_input_channels": 2})
    monkeypatch.setattr(audition.sd, "InputStream", Input)
    audio, rate, notes = tw.record("Line in", "Speakers", RATE, 0.25)
    assert audio.shape == (12000, 2) and rate == RATE and notes == [] and np.all(audio == 0.5)


@pytest.mark.skipif(sys.platform != "darwin", reason="a Mac")
def test_on_a_mac_test_signals_offers_no_loopback():
    assert tw.LOOPBACK is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_on_windows_test_signals_offers_windows_loopback():
    assert tw.LOOPBACK == "Windows loopback (what the output plays)"


# -- SIGTERM (quitsignal.py) ----------------------------------------------------------------------
class FakeRoot:
    def __init__(self):
        self.jobs = {}
        self._next = 0

    def after(self, ms, func, *args):
        self._next += 1
        self.jobs[self._next] = (func, args)
        return self._next

    def after_cancel(self, job):
        self.jobs.pop(job, None)

    def run_timers(self):
        for job in list(self.jobs):
            func, args = self.jobs.pop(job)
            func(*args)


@pytest.fixture
def a_signal(monkeypatch):
    """A signal the test can send to itself safely: SIGUSR1 where there is one."""
    number = getattr(signal, "SIGUSR1", None)
    if number is None:
        pytest.skip("no SIGUSR1 here")
    monkeypatch.setattr(platforms, "QUIT_ON_SIGTERM", True)
    previous = signal.getsignal(number)
    yield number
    signal.signal(number, previous)


def test_the_systems_request_to_close_runs_the_ordinary_close_on_tks_thread(a_signal):
    root, closed = FakeRoot(), []
    watcher = quitsignal.QuitOnSignal(root, lambda: closed.append(threading.current_thread()), signals=(a_signal,))
    assert root.jobs and signal.getsignal(a_signal) == watcher._heard
    root.run_timers()
    assert closed == [] and root.jobs   # nothing asked: still watching
    signal.raise_signal(a_signal)
    assert closed == []                 # the handler itself closes nothing
    root.run_timers()
    assert closed == [threading.main_thread()]
    assert not root.jobs and signal.getsignal(a_signal) != watcher._heard   # stopped watching


def test_stopping_gives_the_signal_back(a_signal):
    before = signal.getsignal(a_signal)
    root = FakeRoot()
    watcher = quitsignal.QuitOnSignal(root, lambda: None, signals=(a_signal,))
    watcher.stop()
    assert signal.getsignal(a_signal) == before and not root.jobs


def test_on_windows_nothing_is_watched(monkeypatch):
    monkeypatch.setattr(platforms, "QUIT_ON_SIGTERM", False)
    root = FakeRoot()
    watcher = quitsignal.QuitOnSignal(root, lambda: None)
    assert not root.jobs and watcher._previous == {}


def test_the_entry_point_watches_while_the_window_is_open(monkeypatch):
    from ironbark import __main__ as start

    made = []

    class Watcher:
        def __init__(self, root, close):
            made.append(("watch", close))

        def stop(self):
            made.append(("stop",))

    monkeypatch.setattr(quitsignal, "QuitOnSignal", Watcher)
    window = SimpleNamespace(quit=lambda: None)
    root = SimpleNamespace(after=lambda *a: None, mainloop=lambda: made.append(("mainloop",)))
    watcher = start._quit_on_signal(root, window)
    root.mainloop()
    watcher.stop()
    assert made == [("watch", window.quit), ("mainloop",), ("stop",)]
    assert start._quit_on_signal(SimpleNamespace(), window) is None   # (a stand-in root: nothing to do)

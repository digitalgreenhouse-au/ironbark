"""The check of what the mixer does, and Test signals' loopback, on Linux (monitor_linux.py through
loopback.py): the sound server's outputs are read from `pactl`, the right one's monitor recorded with
`parec` as raw 32-bit float, a card's own (hw:) device is never "recorded" (its sound bypasses the
server). Runs anywhere: pactl and parec are stood in for."""

import io
import subprocess
import sys
import threading

import numpy as np
import pytest

from ironbark.tools import loopback, monitor_linux

SINKS = """Sink #47
	State: RUNNING
	Name: alsa_output.pci-0000_00_1f.3.analog-stereo
	Description: Built-in Audio Analog Stereo
	Driver: PipeWire
	Sample Specification: float32le 2ch 48000Hz
	Channel Map: front-left,front-right
	Monitor Source: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor

Sink #63
	State: SUSPENDED
	Name: alsa_output.usb-Topping_E30_II-00.analog-stereo
	Description: E30 II Analog Stereo
	Sample Specification: s32le 2ch 96000Hz
	Monitor Source: alsa_output.usb-Topping_E30_II-00.analog-stereo.monitor
"""


@pytest.fixture
def server(monkeypatch):
    calls = []

    def run(*args, timeout=5.0):
        calls.append(args)
        if args[:3] == ("pactl", "list", "sinks"):
            return SINKS
        if args[:2] == ("pactl", "get-default-sink"):
            return "alsa_output.usb-Topping_E30_II-00.analog-stereo\n"
        raise AssertionError(args)

    monkeypatch.setattr(monitor_linux, "_run", run)
    monkeypatch.setattr(monitor_linux, "tools_found", lambda: True)
    monkeypatch.setattr(monitor_linux, "AVAILABLE", True)
    return calls


def test_the_servers_outputs_are_read_with_their_monitors(server):
    sinks = monitor_linux._sinks()
    assert [(s.name.split(".")[1][:3], s.rate, s.channels) for s in sinks] == [("pci", 48000, 2), ("usb", 96000, 2)]
    assert sinks[1].monitor.endswith(".monitor")


@pytest.mark.parametrize("output, wanted", [
    (None, "usb"), ("pipewire", "usb"), ("default", "usb"),                 # the server's default
    ("alsa_output.pci-0000_00_1f.3.analog-stereo", "pci"),                  # a server output by name
])
def test_the_players_output_maps_to_the_servers(server, output, wanted):
    assert monitor_linux.sink_for(output).name.split(".")[1].startswith(wanted)


def test_a_cards_own_device_has_nothing_to_record(server):
    assert monitor_linux.sink_for("Topping E30 II: USB Audio (hw:2,0)") is None
    assert server == []   # (never even asked the server)


def test_the_mixers_rate_is_reported_for_the_check(server, monkeypatch):
    monkeypatch.setattr(loopback, "ON_WINDOWS", False)
    monkeypatch.setattr(loopback, "AVAILABLE", True)
    found = loopback.settings("pipewire")
    assert (found.mixer_rate, found.mixer_bits) == (96000, 32)   # (the default sink: s32le)
    found = loopback.settings("alsa_output.pci-0000_00_1f.3.analog-stereo")
    assert (found.mixer_rate, found.mixer_bits) == (48000, 32)   # (float32le)


@pytest.mark.parametrize("spec, bits", [("s16le 2ch 44100Hz", 16), ("s24le 2ch 48000Hz", 24),
                                        ("s24-32le 2ch 48000Hz", 24), ("float32le 2ch 48000Hz", 32),
                                        ("aLaw 1ch 8000Hz", None)])
def test_the_mixers_bits_come_from_its_sample_format(monkeypatch, spec, bits):
    listing = f"Sink #1\n\tName: out\n\tSample Specification: {spec}\n\tMonitor Source: out.monitor\n"
    monkeypatch.setattr(monitor_linux, "_run", lambda *args, timeout=5.0: listing)
    assert monitor_linux.mixer("out")[1] == bits


def test_the_monitor_is_recorded_as_float_at_its_own_rate(server, monkeypatch):
    rate, channels, seconds = 96000, 2, 0.5
    sent = (np.sin(np.arange(int(rate * seconds) + 999) / 7.0)[:, None] * [0.5, -0.25]).astype("<f4")
    started = []

    class Parec:
        def __init__(self, args, stdout=None, stderr=None):
            started.append(args)
            self.stdout = io.BufferedReader(io.BytesIO(sent.tobytes()))
            self.done = False

        def terminate(self):
            self.done = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", Parec)
    monkeypatch.setattr(loopback, "ON_WINDOWS", False)
    monkeypatch.setattr(loopback, "AVAILABLE", True)
    flowing = threading.Event()
    take = loopback.record("pipewire", seconds, flowing)
    args = started[0]
    assert args[0] == "parec" and "--format=float32le" in args and "--rate=96000" in args and "--channels=2" in args
    assert any(a.startswith("--device=") and a.endswith("Topping_E30_II-00.analog-stereo.monitor") for a in args)
    assert flowing.is_set() and take.rate == rate
    assert take.audio.shape == (int(rate * seconds), channels)
    assert np.array_equal(take.audio, sent[: int(rate * seconds)].astype(np.float64))   # (unchanged)


def test_without_the_servers_tools_it_says_so(monkeypatch):
    monkeypatch.setattr(monitor_linux, "AVAILABLE", True)
    monkeypatch.setattr(monitor_linux, "tools_found", lambda: False)
    assert monitor_linux.record("pipewire", 1.0) is None


class _SilentParec:
    """A parec that sends nothing: it ends at once (`hangs` False) or waits until stopped."""
    hangs = False

    def __init__(self, args, stdout=None, stderr=None):
        import os

        self._read, self._write = os.pipe()
        self.stdout = os.fdopen(self._read, "rb")
        self.stopped = threading.Event()
        if not self.hangs:
            self.terminate()

    def terminate(self):
        import os

        if not self.stopped.is_set():
            self.stopped.set()
            os.close(self._write)   # (parec gone: the read ends)

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminate()


@pytest.mark.parametrize("hangs", [False, True])
def test_a_monitor_that_sends_nothing_gives_no_recording_and_never_waits_for_good(server, monkeypatch, hangs):
    import time

    monkeypatch.setattr(_SilentParec, "hangs", hangs)
    monkeypatch.setattr(subprocess, "Popen", _SilentParec)
    flowing = threading.Event()
    started = time.perf_counter()
    assert monitor_linux.record("pipewire", 0.2, flowing) is None
    assert not flowing.is_set()
    assert time.perf_counter() - started < 0.2 + 2.0 + 1.0   # (the deadline, plus a margin)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux")
def test_on_linux_the_check_and_test_signals_are_offered():
    from ironbark import platforms
    from ironbark.tools import testsignals_window

    assert platforms.LOOPBACK_RECORDING and loopback.AVAILABLE
    assert "monitor" in testsignals_window.LOOPBACK


def _check_words():
    from ironbark.proof import PathCheck, describe_path

    checks = [PathCheck("no_match"), PathCheck("untouched", identical=True),
              PathCheck("changed", resampled=(44100, 48000), level_db=-3.0)]
    return " ".join(text for check in checks for _, text, _ in describe_path(check))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_on_windows_the_checks_words_are_unchanged():
    words = _check_words()
    assert "what Windows played" in words and "Windows' recording isn't available" in words
    assert "by the Windows mixer" in words and "Windows' Volume mixer" in words


def test_off_windows_the_checks_words_name_the_sound_server(monkeypatch):
    from ironbark import proof

    monkeypatch.setattr(proof, "_WHO", "the sound server")
    monkeypatch.setattr(proof, "_WHOSE", "the sound server's")
    monkeypatch.setattr(proof, "_THE_MIXER", "the sound server's mixer")
    monkeypatch.setattr(proof, "_VOLUME_MIXER", "the desktop's sound settings")
    words = _check_words()
    assert "Windows" not in words
    assert "what the sound server played" in words and "by the sound server's mixer" in words

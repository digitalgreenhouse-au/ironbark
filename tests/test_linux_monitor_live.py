"""The check of what the mixer does, through a real sound server (PipeWire with its PulseAudio
side, in the test container: a null sink, no hardware): the player plays a known signal into the
server, the monitor is recorded with the real pactl and parec, and what came back is the signal.
Linux with PipeWire running only (a desktop, or a test container that starts it); skipped elsewhere."""

import shutil
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not shutil.which("pactl")
    or subprocess.run(["pactl", "info"], capture_output=True).returncode != 0,
    reason="Linux with a running sound server")


def test_what_the_server_plays_is_recorded_from_its_monitor():
    from ironbark.tools import monitor_linux

    sink = monitor_linux.sink_for(None)
    assert sink is not None and sink.rate and sink.channels and sink.monitor.endswith(".monitor")
    rate = sink.rate
    tone = (0.25 * np.sin(2 * np.pi * 1000 * np.arange(rate * 3) / rate)).astype("<f4")
    stereo = np.repeat(tone[:, None], sink.channels, axis=1)
    player = subprocess.Popen(["pacat", "--raw", "--format=float32le", f"--rate={rate}",
                               f"--channels={sink.channels}", f"--device={sink.name}"], stdin=subprocess.PIPE)
    feeder = threading.Thread(target=lambda: (player.stdin.write(stereo.tobytes()), player.stdin.close()))
    feeder.start()
    time.sleep(0.3)
    flowing = threading.Event()
    take = monitor_linux.record(None, 1.0, flowing)
    feeder.join(5)
    player.wait(10)
    assert take is not None and flowing.is_set() and take.rate == rate
    assert take.audio.shape[1] == sink.channels and len(take.audio) > rate // 2
    level = np.sqrt(np.mean(take.audio[:, 0] ** 2))
    assert abs(level - 0.25 / np.sqrt(2)) < 0.02   # the tone came back, at its level

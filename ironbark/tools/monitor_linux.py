"""Linux: recording what the sound server plays on an output ("monitor"), for the check of what the
mixer does and for Test signals' loopback — the counterpart of Windows' loopback (loopback.py).

PipeWire and PulseAudio both keep a monitor of every output: exactly the samples they send it,
after their mixing and resampling. It's recorded with `parec`, the sound server's own recorder
(pulseaudio-utils, or PipeWire's compatible one), at the monitor's own rate, channels and float
format, so nothing here converts what's compared. No new library: `pactl` names the outputs and
their monitors, `parec` streams raw samples to a pipe.

Which output: the sound server's, never a card's own (hw:) device — a stream there bypasses the
server, so its monitor hears nothing (the same as Windows' exclusive mode). The player's output
"pipewire" / "default" / a server output name maps to the server's default sink, or the sink of
that name.

Blocks for the length of the recording: call it from a worker thread. Read-only.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

import numpy as np

from .. import platforms

log = logging.getLogger(__name__)

AVAILABLE = platforms.IS_LINUX
SERVER_NAMES = frozenset({"pipewire", "pulse", "default", "sysdefault", ""})
TIMEOUT_S = 5.0
# The bits a sink's sample format carries, as `pactl` names it (float32: the 24 its mantissa holds
# exactly would undersell it to a reader; the check's line says 32-bit, as Windows' float mixer does).
FORMAT_BITS = {"u8": 8, "s16le": 16, "s16be": 16, "s24le": 24, "s24be": 24, "s24-32le": 24, "s24-32be": 24,
               "s32le": 32, "s32be": 32, "float32le": 32, "float32be": 32}


@dataclass
class Recording:
    rate: int
    audio: np.ndarray   # frames x channels, float64
    gaps: int = 0       # (the server reports none: always 0)


@dataclass
class Sink:
    name: str
    monitor: str
    rate: int
    channels: int
    bits: int = 0   # 0 = a format not in FORMAT_BITS


def _run(*args: str, timeout: float = TIMEOUT_S) -> str:
    # pactl prints its headings ("Name:", "Monitor Source:" …) in the desktop's language: read them in English.
    env = {**os.environ, "LC_ALL": "C"}
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True, env=env).stdout


def _default_sink() -> str:
    """The server's default output. `pactl get-default-sink` is newer than some distributions'
    pactl; `pactl info` has the same name on its "Default Sink:" line everywhere."""
    try:
        return _run("pactl", "get-default-sink").strip()
    except subprocess.CalledProcessError:
        for line in _run("pactl", "info").splitlines():
            if line.strip().startswith("Default Sink:"):
                return line.split(":", 1)[1].strip()
        return ""


def tools_found() -> bool:
    """Are the sound server's tools here (pactl to find the monitor, parec to record it)?"""
    return bool(shutil.which("pactl") and shutil.which("parec"))


def _sinks() -> list[Sink]:
    """The server's outputs, from `pactl list sinks` (the one listing every version prints)."""
    found, name, monitor, rate, channels, bits = [], None, None, 0, 0, 0
    for line in _run("pactl", "list", "sinks").splitlines() + ["Sink #end"]:
        text = line.strip()
        if text.startswith("Sink #"):
            if name and monitor:
                found.append(Sink(name, monitor, rate, channels, bits))
            name, monitor, rate, channels, bits = None, None, 0, 0, 0
        elif text.startswith("Name:"):
            name = text.split(":", 1)[1].strip()
        elif text.startswith("Monitor Source:"):
            monitor = text.split(":", 1)[1].strip()
        elif text.startswith("Sample Specification:"):
            for part in text.split(":", 1)[1].split():
                if part.endswith("ch"):
                    channels = int(part[:-2]) if part[:-2].isdigit() else 0
                elif part.endswith("Hz"):
                    rate = int(part[:-2]) if part[:-2].isdigit() else 0
                else:
                    bits = FORMAT_BITS.get(part.lower(), bits)
    return found


def sink_for(output: str | None) -> Sink | None:
    """The server output the player's `output` plays through: its default for the server's own
    names, the sink of that name otherwise. None for a card's own device (no monitor hears it)."""
    from .. import devices

    if output and devices.alsa_card(output) is not None:
        return None
    sinks = _sinks()
    if not sinks:
        return None
    wanted = None if (output or "") in SERVER_NAMES else output
    if wanted is None:
        wanted = _default_sink()
    return next((sink for sink in sinks if sink.name == wanted), sinks[0] if output in (None, "") else None)


def mixer(output: str | None) -> tuple[int | None, int | None]:
    """(rate, bits) the server runs this output at, (None, None) when it can't tell."""
    try:
        sink = sink_for(output)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    return (sink.rate or None, sink.bits or None) if sink else (None, None)


def record(output: str | None, seconds: float, flowing: threading.Event | None = None) -> Recording | None:
    """Record `seconds` of what the server plays on `output`. None when it can't (no sound server
    tools, a card's own device, the output not found, nothing came). `flowing` is set once samples
    arrive."""
    if not AVAILABLE or not tools_found():
        return None
    sink = sink_for(output)
    if sink is None or not sink.rate or not sink.channels:
        return None
    frames_wanted = int(sink.rate * seconds)
    process = subprocess.Popen(
        ["parec", f"--device={sink.monitor}", "--raw", "--format=float32le", f"--rate={sink.rate}",
         f"--channels={sink.channels}", "--latency-msec=20"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_bytes = 4 * sink.channels
    data = bytearray()
    deadline = time.perf_counter() + seconds + 2.0
    # A read waits for parec, and a monitor that sends nothing would keep it waiting for good: past
    # the deadline parec is stopped, which ends the read.
    stopper = threading.Timer(seconds + 2.0, process.terminate)
    stopper.daemon = True
    stopper.start()
    try:
        assert process.stdout is not None
        while len(data) < frames_wanted * frame_bytes and time.perf_counter() < deadline:
            chunk = process.stdout.read1(65536) if hasattr(process.stdout, "read1") else process.stdout.read(65536)
            if not chunk:
                break
            data += chunk
            if flowing is not None:
                flowing.set()
    finally:
        stopper.cancel()
        process.terminate()
        try:
            process.wait(2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
    usable = len(data) - len(data) % frame_bytes
    if not usable:
        log.info("the sound server's monitor %s sent nothing", sink.monitor)
        return None
    audio = np.frombuffer(bytes(data[:usable]), dtype="<f4").reshape(-1, sink.channels).astype(np.float64)
    return Recording(sink.rate, audio[:frames_wanted], 0)

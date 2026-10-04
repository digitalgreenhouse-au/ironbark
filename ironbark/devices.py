"""Output devices: list them, turn a chosen name into a PortAudio device, ask what they can do.

On Windows everything goes through WASAPI, so one device list serves both shared
and exclusive (bit-perfect) mode. Windows' own device list (read via pycaw) is
live; PortAudio's is scanned once at start-up. When a device appears that
PortAudio hasn't seen — a USB DAC plugged in after launch — we re-scan PortAudio,
which is only safe with no stream open (callers guarantee that).

Devices are remembered by name, because PortAudio's numbering changes between runs.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, TypeVar

import sounddevice as sd

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
T = TypeVar("T")


def _com_call(fn: Callable[[], T], default: T | None = None, timeout: float = 3.0) -> T | None:
    """Run a Windows Core Audio query on a throwaway thread with COM set up, so COM
    state never leaks into the UI or audio threads. Any failure returns `default`."""
    result: list = [default]

    def work():
        try:
            import comtypes

            comtypes.CoInitialize()
            result[0] = fn()
        except Exception:  # noqa: BLE001 — a failed query just means "don't know"
            log.info("core audio query failed", exc_info=True)

    thread = threading.Thread(target=work, name="core-audio-query", daemon=True)
    thread.start()
    thread.join(timeout)
    return result[0]


def _windows_render_devices():
    from pycaw.pycaw import AudioUtilities

    # Render endpoint ids start {0.0.0. (capture devices are {0.0.1.).
    return [d for d in AudioUtilities.GetAllDevices()
            if d.id and d.id.startswith("{0.0.0.") and d.state.name == "Active"]


# -- PortAudio side -------------------------------------------------------------
def _host_api() -> int | None:
    try:
        apis = sd.query_hostapis()
        if IS_WINDOWS:
            for index, api in enumerate(apis):
                if "WASAPI" in api["name"]:
                    return index
            return None
        return sd.default.hostapi
    except sd.PortAudioError:
        return None


def _portaudio_outputs() -> list[tuple[int, str]]:
    api = _host_api()
    if api is None:
        return []
    found = []
    for index in sd.query_hostapis(api)["devices"]:
        info = sd.query_devices(index)
        if info["max_output_channels"] > 0:
            found.append((index, info["name"]))
    return found


def _portaudio_default() -> int | None:
    api = _host_api()
    if api is None:
        return None
    index = sd.query_hostapis(api)["default_output_device"]
    return index if index >= 0 else None


def rescan() -> None:
    """Make PortAudio see devices plugged in since start-up. No stream may be open."""
    log.info("re-scanning audio devices")
    sd._terminate()
    sd._initialize()


# -- public ---------------------------------------------------------------------
def exclusive_supported() -> bool:
    return IS_WINDOWS and _host_api() is not None


def output_names() -> list[str]:
    """Every output device that's connected right now."""
    if IS_WINDOWS:
        names = _com_call(lambda: [d.FriendlyName for d in _windows_render_devices()])
        if names is not None:
            return names
    return [name for _, name in _portaudio_outputs()]


def _portaudio_inputs() -> list[tuple[int, str]]:
    api = _host_api()
    if api is None:
        return []
    found = []
    for index in sd.query_hostapis(api)["devices"]:
        info = sd.query_devices(index)
        if info["max_input_channels"] > 0:
            found.append((index, info["name"]))
    return found


def input_names() -> list[str]:
    """Every recording input PortAudio knows (WASAPI on Windows) — for the hardware test."""
    try:
        return [name for _, name in _portaudio_inputs()]
    except sd.PortAudioError:
        return []


def find_input(name: str) -> int | None:
    """PortAudio device index of the recording input called `name`, or None."""
    try:
        return next((index for index, found in _portaudio_inputs() if found == name), None)
    except sd.PortAudioError:
        return None


def default_output_name() -> str | None:
    """The system's default output right now (on Windows, as set in Sound settings)."""
    if IS_WINDOWS:
        def query():
            from pycaw.pycaw import AudioUtilities

            return AudioUtilities.GetSpeakers().FriendlyName

        name = _com_call(query)
        if name:
            return name
    index = _portaudio_default()
    return sd.query_devices(index)["name"] if index is not None else None


def find_device(name: str | None, rescan_ok: bool = True) -> int | None:
    """PortAudio device index for `name` (None = system default). Re-scans PortAudio once
    if it hasn't seen the device yet (not with `rescan_ok=False`: a stream may be open).
    Returns None if a named device isn't connected."""
    target = name or default_output_name()
    for attempt in range(2):
        for index, device_name in _portaudio_outputs():
            if device_name == target:
                return index
        if attempt == 0 and target and rescan_ok:
            rescan()
    return None if name else _portaudio_default()


def mixer_rate(index: int | None) -> int | None:
    """The rate the OS mixes this device at (for WASAPI devices: the shared-mode format)."""
    try:
        info = sd.query_devices(kind="output") if index is None else sd.query_devices(index)
        return int(info["default_samplerate"])
    except (sd.PortAudioError, ValueError, KeyError, TypeError):
        return None


def has_hardware_volume(name: str | None) -> bool | None:
    """Can the device turn itself down? In exclusive mode the Windows mixer is bypassed, so
    the system volume slider only works if it can drive the device's own volume control.
    None = couldn't tell (or not Windows)."""
    if not IS_WINDOWS:
        return None

    def query():
        from pycaw.pycaw import AudioUtilities

        if name is None:
            device = AudioUtilities.GetSpeakers()
        else:
            device = next((d for d in _windows_render_devices() if d.FriendlyName == name), None)
            if device is None:
                return None
        return bool(device.EndpointVolume.QueryHardwareSupport() & 1)  # bit 1 = volume

    return _com_call(query)

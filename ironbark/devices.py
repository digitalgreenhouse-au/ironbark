"""Output devices: list them, turn a chosen name into a PortAudio device, ask what they can do.

On Windows everything goes through WASAPI, so one device list serves both shared
and exclusive (bit-perfect) mode. Windows' own device list (read through Core Audio)
is live; PortAudio's is scanned once at start-up. When Windows lists a device that
PortAudio hasn't seen — a USB DAC plugged in after launch — we re-scan PortAudio. A
re-scan pulls every open PortAudio stream out from under its owner, so it only happens
while none is open: whoever opens a stream reports it with `holding` / `released`.

The Core Audio queries ask only for the active output endpoints and only the property
needed (the name, or the volume control): a few milliseconds, where reading every
endpoint's every property took the best part of a second.

Devices are remembered by name, because PortAudio's numbering changes between runs.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import weakref
from collections.abc import Callable
from typing import TypeVar

import sounddevice as sd

from . import platforms

log = logging.getLogger(__name__)

IS_WINDOWS = platforms.IS_WINDOWS
T = TypeVar("T")

_FRIENDLY_NAME = ("{A45C254E-DF1C-4EFD-8020-67D146A850E0}", 14)   # PKEY_Device_FriendlyName
_DEVICE_DESC = ("{A45C254E-DF1C-4EFD-8020-67D146A850E0}", 2)      # PKEY_Device_DeviceDesc (if no friendly name)
_RENDER, _ACTIVE, _MULTIMEDIA = 0, 1, 1   # eRender, DEVICE_STATE_ACTIVE, eMultimedia
_STGM_READ = 0


class DeviceMissing(sd.PortAudioError):
    """The chosen output isn't connected (and bit-perfect mode won't play on another one)."""


class DefaultChanged(DeviceMissing):
    """Bit-perfect on the system default: Windows has made another device the default since its
    volume control was checked (that one may have none, and bit-perfect sends full scale)."""


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


# -- Windows side (Core Audio; call these only inside _com_call) -------------------------
def _enumerator():
    import comtypes
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator

    return comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)


def _property(store, key: tuple[str, int]):
    from comtypes import GUID
    from pycaw.api.mmdeviceapi.depend.structures import PROPERTYKEY

    pk = PROPERTYKEY()
    pk.fmtid, pk.pid = GUID(key[0]), key[1]
    value = store.GetValue(pk)
    try:
        return value.GetValue()
    finally:
        value.clear()


def _name_of(device) -> str:
    """An endpoint's name as Windows shows it (and PortAudio's WASAPI list uses)."""
    store = device.OpenPropertyStore(_STGM_READ)
    name = _property(store, _FRIENDLY_NAME)
    if not isinstance(name, str) or not name:
        name = _property(store, _DEVICE_DESC)
    return name if isinstance(name, str) else ""


def _active_outputs() -> list[tuple[str, object]]:
    """(name, endpoint) for every output connected right now."""
    collection = _enumerator().EnumAudioEndpoints(_RENDER, _ACTIVE)
    found = []
    for k in range(collection.GetCount()):
        device = collection.Item(k)
        if device is not None:
            found.append((_name_of(device), device))
    return found


def _default_output():
    return _enumerator().GetDefaultAudioEndpoint(_RENDER, _MULTIMEDIA)


def _volume_support(device) -> bool:
    import comtypes
    from pycaw.api.endpointvolume import IAudioEndpointVolume

    volume = device.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
    return bool(volume.QueryInterface(IAudioEndpointVolume).QueryHardwareSupport() & 1)  # bit 1 = volume


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


# Streams open in this process (the player's, an ABX / Clipping / test-signal audition's).
_streams: weakref.WeakSet = weakref.WeakSet()
_streams_lock = threading.Lock()


def holding(stream) -> None:
    """A PortAudio stream has been opened: no re-scan until it's `released`."""
    with _streams_lock:
        _streams.add(stream)


def released(stream) -> None:
    with _streams_lock:
        _streams.discard(stream)


def streams_open() -> bool:
    with _streams_lock:
        return len(_streams) > 0


def rescan() -> None:
    """Make PortAudio see devices plugged in since start-up. No stream may be open."""
    log.info("re-scanning audio devices")
    sd._terminate()
    sd._initialize()


def _system_lists(name: str) -> bool:
    """Does Windows list `name` as connected (so a re-scan would find it)? True when it can't
    tell, and off Windows (PortAudio's list is all there is)."""
    if not IS_WINDOWS:
        return True
    names = _com_call(lambda: [found for found, _ in _active_outputs()])
    return names is None or name in names


# -- public ---------------------------------------------------------------------
def exclusive_supported() -> bool:
    return IS_WINDOWS and _host_api() is not None


def output_names() -> list[str]:
    """Every output device that's connected right now."""
    if IS_WINDOWS:
        names = _com_call(lambda: [name for name, _ in _active_outputs()])
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
        name = _com_call(lambda: _name_of(_default_output()))
        if name:
            return name
    index = _portaudio_default()
    return sd.query_devices(index)["name"] if index is not None else None


def find_device(name: str | None, rescan_ok: bool = True) -> int | None:
    """PortAudio device index for `name` (None = system default). Re-scans PortAudio once
    when Windows lists the device but PortAudio hasn't seen it yet — never while a stream is
    open (see `holding`), nor with `rescan_ok=False`. Returns None if a named device isn't
    connected."""
    target = name or default_output_name()
    for attempt in range(2):
        for index, device_name in _portaudio_outputs():
            if device_name == target:
                return index
        if attempt or not target or not rescan_ok or streams_open() or not _system_lists(target):
            break
        rescan()
    return None if name else _portaudio_default()


def mixer_rate(index: int | None) -> int | None:
    """The rate the OS mixes this device at (for WASAPI devices: the shared-mode format)."""
    try:
        info = sd.query_devices(kind="output") if index is None else sd.query_devices(index)
        return int(info["default_samplerate"])
    except (sd.PortAudioError, ValueError, KeyError, TypeError):
        return None


def max_output_channels(index: int | None) -> int | None:
    """How many channels the device plays (for WASAPI: its mixer's channel count). None = can't tell."""
    try:
        info = sd.query_devices(kind="output") if index is None else sd.query_devices(index)
        return int(info["max_output_channels"]) or None
    except (sd.PortAudioError, ValueError, KeyError, TypeError):
        return None


_volume_answers: dict = {}   # name (None = the default) -> the last answer has_hardware_volume got


def has_hardware_volume(name: str | None) -> bool | None:
    """Can the device turn itself down? In exclusive mode the Windows mixer is bypassed, so
    the system volume slider only works if it can drive the device's own volume control.
    None = couldn't tell (or not Windows). A query that fails or times out gives the last
    answer for that device, so a slow moment never turns a known "yes" into "don't know"."""
    if not IS_WINDOWS:
        return None

    def query():
        if name is None:
            return _volume_support(_default_output())
        device = next((d for found, d in _active_outputs() if found == name), None)
        return None if device is None else _volume_support(device)

    missing = object()
    answer = _com_call(query, default=missing)
    if answer is missing:
        return _volume_answers.get(name)
    _volume_answers[name] = answer
    return answer


# -- what an exclusive stream really sends -------------------------------------------
class _WaveFormat(ctypes.Structure):
    """WAVEFORMATEXTENSIBLE (mmreg.h / ksmedia.h)."""
    _fields_ = [("wFormatTag", ctypes.c_uint16), ("nChannels", ctypes.c_uint16),
                ("nSamplesPerSec", ctypes.c_uint32), ("nAvgBytesPerSec", ctypes.c_uint32),
                ("nBlockAlign", ctypes.c_uint16), ("wBitsPerSample", ctypes.c_uint16),
                ("cbSize", ctypes.c_uint16), ("wValidBitsPerSample", ctypes.c_uint16),
                ("dwChannelMask", ctypes.c_uint32), ("SubFormat", ctypes.c_uint8 * 16)]


_WAVE_FORMAT_PCM, _WAVE_FORMAT_FLOAT, _WAVE_FORMAT_EXTENSIBLE = 1, 3, 0xFFFE
FLOAT32_BITS = 24   # a float32 sample holds a 24-bit integer exactly (its mantissa)


def sample_bits(raw: bytes) -> int | None:
    """The bits per sample a device takes, from its WAVEFORMATEX(TENSIBLE): the valid bits for an
    integer format, FLOAT32_BITS for 32-bit float. None if it can't be read."""
    if len(raw) < 18:
        return None
    wave = _WaveFormat.from_buffer_copy(raw.ljust(ctypes.sizeof(_WaveFormat), b"\0"))
    tag, bits = wave.wFormatTag, wave.wBitsPerSample
    if tag == _WAVE_FORMAT_EXTENSIBLE and len(raw) >= ctypes.sizeof(_WaveFormat):
        tag = int.from_bytes(bytes(wave.SubFormat[:4]), "little")   # the KSDATAFORMAT_SUBTYPE's first field
        bits = wave.wValidBitsPerSample or bits
    if tag == _WAVE_FORMAT_FLOAT:
        return FLOAT32_BITS if bits >= 32 else None
    return bits if tag == _WAVE_FORMAT_PCM and bits else None


_current_format = None


def _wasapi_current_format():
    """PortAudio's PaWasapi_GetDeviceCurrentFormat, from the same library sounddevice loaded."""
    global _current_format
    if _current_format is None:
        function = ctypes.CDLL(sd._libname).PaWasapi_GetDeviceCurrentFormat
        function.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
        function.restype = ctypes.c_int
        _current_format = function
    return _current_format


def exclusive_bits(stream) -> int | None:
    """The bits per sample an open exclusive (WASAPI) output stream actually sends its device.
    PortAudio picks the nearest format the device takes when it refuses the one asked for, and
    converts to it — so a 24-bit file on a 16-bit-only device loses its low bits there, however
    exactly the player hands its samples over. None = can't tell (not Windows, or a stand-in)."""
    if not IS_WINDOWS:
        return None
    try:
        pointer = int(sd._ffi.cast("uintptr_t", stream._ptr))
        buffer = (ctypes.c_uint8 * ctypes.sizeof(_WaveFormat))()
        got = _wasapi_current_format()(pointer, buffer, ctypes.sizeof(buffer), 1)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return sample_bits(bytes(buffer)[:got]) if got > 0 else None

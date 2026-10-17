"""Output devices: list them, turn a chosen name into a PortAudio device, ask what they can do,
and hold one for bit-perfect playback.

On Windows everything goes through WASAPI, so one device list serves both shared
and exclusive (bit-perfect) mode. On a Mac it's Apple's Core Audio, with bit-perfect done by
hand (coreaudio.py): hog mode, and the device switched to the file's rate and its deepest
format. The system's own device list (Windows' Core Audio, or Apple's) is live;
PortAudio's is scanned once at start-up. So the system's list is the one shown, and when it
has a device that PortAudio hasn't seen — headphones or a USB DAC plugged in after launch —
we re-scan PortAudio. A re-scan pulls every open PortAudio stream out from under its owner,
so it only happens while none is open: whoever opens a stream reports it with `holding` /
`released`.

The Core Audio queries ask only for the active output endpoints and only the property
needed (the name, or the volume control): a few milliseconds, where reading every
endpoint's every property took the best part of a second.

Devices are remembered by name, because PortAudio's numbering changes between runs.
"""

from __future__ import annotations

import ctypes
import logging
import re
import threading
import weakref
from collections.abc import Callable
from typing import TypeVar

import sounddevice as sd

from . import platforms

log = logging.getLogger(__name__)

IS_WINDOWS = platforms.IS_WINDOWS
IS_MAC = platforms.IS_MAC
IS_LINUX = platforms.IS_LINUX
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


class DeviceBusy(DeviceMissing):
    """Linux: the chosen card's own (hw:) device is there, but the sound server or another app has
    it open, so PortAudio can't (`missing`)."""


def missing(name: str) -> DeviceMissing:
    """The error for output `name` not found to play on: DeviceBusy for a card's own device that ALSA
    lists (busy, not gone), else DeviceMissing ("isn't connected")."""
    if IS_LINUX and alsa_card(name) is not None and name in (alsa_playback_names() or []):
        return DeviceBusy(f"{name} is busy: the sound server or another app is playing on it. Stop that "
                          "sound and wait a few seconds for the sound server to let go of it")
    return DeviceMissing(f"{name} isn't connected")


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


def _live_output_names() -> list[str] | None:
    """The system's own list of the outputs connected right now (PortAudio's is from start-up).
    None = can't tell: the query failed, or a system with no list of its own."""
    if IS_WINDOWS:
        return _com_call(lambda: [name for name, _ in _active_outputs()])
    if IS_MAC:
        from . import coreaudio

        try:
            return coreaudio.output_names()
        except OSError:
            log.info("core audio device list failed", exc_info=True)
    if IS_LINUX:
        return _linux_output_names()
    return None


def _linux_output_names() -> list[str] | None:
    """PortAudio's outputs with its cards' own (hw:) devices brought up to date from ALSA's list.
    PortAudio skips a card's device it can't open at start-up — one the sound server is playing on
    just then — and never looks again; ALSA lists it whatever is using it. None = no ALSA list."""
    cards = alsa_playback_names()
    if cards is None:
        return None
    try:
        portaudio = [name for _, name in _portaudio_outputs()]
    except sd.PortAudioError:
        portaudio = []
    kept = [name for name in portaudio if alsa_card(name) is None or name in cards]   # (an unplugged card goes)
    return kept + [name for name in cards if name not in kept]


def _system_lists(name: str) -> bool:
    """Does the system list `name` as connected (so a re-scan would find it)? True when it
    can't tell (then PortAudio's list is all there is)."""
    names = _live_output_names()
    return names is None or name in names


# -- public ---------------------------------------------------------------------
def exclusive_supported() -> bool:
    return platforms.EXCLUSIVE_OUTPUT and _host_api() is not None


def exclusive_settings():
    """PortAudio's settings for a bit-perfect stream: WASAPI exclusive on Windows; on a Mac,
    run the device at the stream's own rate and refuse to open rather than resample. On Linux
    none: a hw: device takes the stream as it is or refuses it."""
    if IS_WINDOWS:
        return sd.WasapiSettings(exclusive=True)
    if IS_LINUX:
        return None
    return sd.CoreAudioSettings(change_device_parameters=True, fail_if_conversion_required=True)


def shared_settings(index: int | None):
    """PortAudio's settings for a shared stream. On Windows: WASAPI with auto-convert, so
    Windows resamples to its mixer rate."""
    return sd.WasapiSettings(auto_convert=True) if IS_WINDOWS and index is not None else None


class Refused(Exception):
    """The device can't be held for bit-perfect playback; the message says why, for the user.
    `advice`: the message is what to do ("choose … under Output"), so it's shown first."""

    def __init__(self, message: str, advice: bool = False):
        super().__init__(message)
        self.advice = advice


class Claim:
    """A device held for bit-perfect playback, before its stream opens; `release` once the
    stream is closed. On Windows exclusive mode itself is the hold, so there's nothing to do.
    `bits`: bits per sample the device keeps (None = not known: on Windows, exclusive_bits
    reads it from the open stream instead)."""

    def __init__(self, ca_claim=None):
        self._ca = ca_claim
        self.bits = ca_claim.bits if ca_claim else None
        self.device = ca_claim.device if ca_claim else None

    def release(self):
        if self._ca is not None:
            self._ca.release()
            self._ca = None


def _core_audio_id(index: int) -> int | None:
    """The Core Audio device behind PortAudio output `index`."""
    from . import coreaudio

    outputs = _portaudio_outputs()
    name = next((n for i, n in outputs if i == index), None)
    if name is None:
        return None
    nth = [i for i, n in outputs if n == name].index(index)   # displays can share a name
    return coreaudio.find_device(name, nth)


def _hw_hint() -> str:
    """Why a sound-server output can't be bit-perfect on Linux, short, with what to choose first —
    the card's own name when there's one — so a narrow status line keeps the part that matters."""
    cards = [name for name in (alsa_playback_names() or []) if "HDMI" not in name.upper()]
    pick = f"“{cards[0]}”" if len(cards) == 1 else "a sound card ending in (hw:…)"
    return f"choose {pick} under Output: bit-perfect on Linux plays straight to the card"


def claim(index: int, rate: int) -> Claim:
    """Hold output `index` for bit-perfect playback at `rate`, before opening its stream.
    Raises Refused if it can't be done."""
    if IS_LINUX:
        # Bit-perfect is the sound card's own device (hw:): no resampler or mixer between. A
        # sound-server output (pipewire, default) mixes; the card behind it isn't guessed (never
        # another device than the one chosen), so the listener picks the card under Output.
        try:
            name = sd.query_devices(index)["name"]
        except (sd.PortAudioError, ValueError, KeyError, TypeError):
            name = ""
        if alsa_card(name) is None:
            raise Refused(_hw_hint(), advice=True)
        return Claim()
    if not IS_MAC:
        return Claim()
    from . import coreaudio

    try:
        device = _core_audio_id(index)
        if device is None:
            raise Refused("Core Audio doesn't list this device")
        return Claim(coreaudio.Claim(device, rate))
    except (coreaudio.Busy, coreaudio.Unsupported) as exc:
        raise Refused(str(exc)) from None
    except OSError as exc:   # the framework itself failed to load or answer
        log.info("core audio claim failed", exc_info=True)
        raise Refused(f"Core Audio error: {exc}") from None


def formats_changed() -> bool:
    """Has bit-perfect changed a device's rate or format (and not put it back yet)?"""
    if not IS_MAC:
        return False
    from . import coreaudio

    return coreaudio.changed()


def restore_formats(keep: int | None = None) -> None:
    """Put devices back to the rate and format they had before bit-perfect changed them — all
    but Core Audio device `keep` (the one bit-perfect is about to use again). No stream may be
    open on them. Nothing to do off a Mac (Windows exclusive mode never changes the device)."""
    if not IS_MAC:
        return
    from . import coreaudio

    try:
        coreaudio.restore(keep)
    except OSError:
        log.info("restoring device formats failed", exc_info=True)


def core_audio_device(name: str | None) -> int | None:
    """The Core Audio id of output `name` (None = system default), or None (not a Mac, not found)."""
    if not IS_MAC:
        return None
    try:
        index = find_device(name, rescan_ok=False)
        return _core_audio_id(index) if index is not None else None
    except (OSError, sd.PortAudioError):
        return None


def output_names() -> list[str]:
    """Every output device that's connected right now."""
    names = _live_output_names()
    return names if names is not None else [name for _, name in _portaudio_outputs()]


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
    """The system's default output right now (as set in Sound settings)."""
    if IS_WINDOWS:
        name = _com_call(lambda: _name_of(_default_output()))
        if name:
            return name
    elif IS_MAC:
        from . import coreaudio

        try:
            name = coreaudio.default_output_name()
        except OSError:
            log.info("core audio default output failed", exc_info=True)
            name = None
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
    """The rate the OS mixes this device at (for WASAPI devices: the shared-mode format; on a
    Mac the device's own rate, read live: PortAudio's is from start-up)."""
    if IS_MAC:
        from . import coreaudio

        try:
            device = _core_audio_id(index if index is not None else _portaudio_default())
            rate = coreaudio.nominal_rate(device) if device is not None else None
            if rate:
                return rate
        except (OSError, sd.PortAudioError, ValueError):
            pass
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
    (A Mac never turns a device down in software: without a volume control of its own, the
    volume keys do nothing in either mode.) None = couldn't tell. A query that fails or times
    out gives the last answer for that device, so a slow moment never turns a known "yes" into
    "don't know"."""
    if not IS_WINDOWS:
        if not IS_MAC:
            return None
        from . import coreaudio

        device = core_audio_device(name)
        try:
            return coreaudio.has_volume(device) if device is not None else None
        except OSError:
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

# -- Linux: the sound card's own device ----------------------------------------------
# PortAudio names an ALSA device "<card name>: <device name> (hw:<card>,<device>)" for the card's
# own device, and plain names ("pipewire", "default", "sysdefault") for the sound server's.
_ALSA_HW = re.compile(r"\(hw:(\d+),(\d+)\)\s*$")
_ALSA_PCM = "/proc/asound/card{card}/pcm{device}p/sub{sub}/hw_params"
# ALSA's sample formats, and the bits each carries (S32 may hold 24 in a 32-bit slot: the card's
# own word length, which ALSA doesn't say; counted as 32, the widest it could be).
_ALSA_FORMAT_BITS = {"S16_LE": 16, "S16_BE": 16, "S24_3LE": 24, "S24_3BE": 24, "S24_LE": 24,
                     "S24_BE": 24, "S32_LE": 32, "S32_BE": 32, "FLOAT_LE": FLOAT32_BITS, "U8": 8, "S8": 8}


def alsa_card(name: str) -> tuple[int, int] | None:
    """(card, device) when the PortAudio device `name` is a sound card's own (hw:) device, else None."""
    match = _ALSA_HW.search(name or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


_ALSA_CARD_LINE = re.compile(r"^\s*(\d+)\s+\[[^\]]*\]:\s.*?\s-\s(.+?)\s*$")   # " 0 [PCH  ]: HDA-Intel - HDA Intel PCH"
# "00-00: <id> : <name> : playback 1 : capture 1" — PortAudio names a device by its name (the second)
_ALSA_PCM_LINE = re.compile(r"^(\d+)-(\d+):\s*.*?\s:\s(.*?)\s*:\s*playback\s+\d+")


def alsa_playback_names(root: str = "") -> list[str] | None:
    """Every sound card's own playback device as ALSA lists it now, named the way PortAudio names
    it ("HDA Intel PCH: CX20751/2 Analog (hw:0,0)"): from /proc/asound/cards (the card's name) and
    /proc/asound/pcm (each device's name, and whether it plays). None when ALSA's list can't be read."""
    try:
        with open(root + "/proc/asound/cards", encoding="utf-8", errors="replace") as f:
            card_lines = f.read().splitlines()
        with open(root + "/proc/asound/pcm", encoding="utf-8", errors="replace") as f:
            pcm_lines = f.read().splitlines()
    except OSError:
        return None
    cards = {}
    for line in card_lines:
        match = _ALSA_CARD_LINE.match(line)
        if match:
            cards[int(match.group(1))] = match.group(2)
    names = []
    for line in pcm_lines:
        match = _ALSA_PCM_LINE.match(line)
        if match and int(match.group(1)) in cards:
            card, device = int(match.group(1)), int(match.group(2))
            names.append(f"{cards[card]}: {match.group(3)} (hw:{card},{device})")
    return names


def alsa_open_bits(card: int, device: int, root: str = "") -> int | None:
    """The bits per sample the card's device is open with now, from ALSA's own status file (the
    first busy sub-device). None when it isn't open or can't be read."""
    for sub in range(8):
        try:
            with open(root + _ALSA_PCM.format(card=card, device=device, sub=sub), encoding="ascii") as f:
                text = f.read()
        except OSError:
            break
        for line in text.splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "format":
                return _ALSA_FORMAT_BITS.get(value.strip())
    return None


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
    exactly the player hands its samples over. On Linux, what the card itself was opened with
    (ALSA's own status file). None = can't tell (a Mac, or a stand-in)."""
    if IS_LINUX:
        try:
            card = alsa_card(sd.query_devices(stream.device)["name"])
        except (AttributeError, sd.PortAudioError, ValueError, KeyError, TypeError):
            return None
        return alsa_open_bits(*card) if card is not None else None
    if not IS_WINDOWS:
        return None
    try:
        pointer = int(sd._ffi.cast("uintptr_t", stream._ptr))
        buffer = (ctypes.c_uint8 * ctypes.sizeof(_WaveFormat))()
        got = _wasapi_current_format()(pointer, buffer, ctypes.sizeof(buffer), 1)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return sample_bits(bytes(buffer)[:got]) if got > 0 else None

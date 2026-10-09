"""macOS Core Audio, for bit-perfect playback: the few device controls PortAudio doesn't offer.

On a Mac every app's sound goes through the Core Audio mixer, in floating point, at whatever rate
and bit depth the device happens to be set to (Audio MIDI Setup shows it). The mixer leaves a
stream's samples alone only when nothing else plays, the device runs at the file's own rate, and
the device's own format has at least as many bits as the file. So bit-perfect here means three
things, done in this order before the stream opens:

- hog mode: the device is ours alone, so no other app (or alert sound) is mixed in;
- the device's physical format: the most bits it offers at the file's rate (a USB DAC is often
  left at 16-bit, which would cut a 24-bit file short);
- the stream asks PortAudio to run the device at its own rate and to fail rather than resample.

The format the device had before is remembered and put back when bit-perfect lets go of it, so
other apps find it as they left it.

Plain ctypes calls into the CoreAudio framework; nothing here is imported off a Mac.
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import time

log = logging.getLogger(__name__)

SYSTEM_OBJECT = 1
FORMAT_SETTLE_S = 2.0   # how long a device may take to switch rate or format


def _code(text: str) -> int:
    return struct.unpack(">I", text.encode("ascii"))[0]


SCOPE_GLOBAL, SCOPE_OUTPUT = _code("glob"), _code("outp")
DEVICES, NAME, STREAMS = _code("dev#"), _code("lnam"), _code("stm#")
HOG_MODE, NOMINAL_RATE, VOLUME = _code("oink"), _code("nsrt"), _code("volm")
PHYSICAL_FORMAT, PHYSICAL_FORMATS = _code("pft "), _code("pfta")
LINEAR_PCM = _code("lpcm")
FLAG_FLOAT, FLAG_NON_MIXABLE = 0x1, 0x40
CF_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


class Format(ctypes.Structure):
    """AudioStreamBasicDescription."""
    _fields_ = [("rate", ctypes.c_double), ("format_id", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("bytes_per_packet", ctypes.c_uint32), ("frames_per_packet", ctypes.c_uint32),
                ("bytes_per_frame", ctypes.c_uint32), ("channels", ctypes.c_uint32), ("bits", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32)]

    @property
    def usable_bits(self) -> int:
        """Bits a sample keeps. Every app's sound reaches the device through Core Audio as 32-bit
        floating point, which holds 24-bit samples exactly and no more, whatever the device takes."""
        return min(self.bits, 24)

    def describe(self) -> str:
        kind = "float" if self.flags & FLAG_FLOAT else "integer"
        return f"{self.channels}ch {self.bits}-bit {kind} {self.rate:g} Hz"


class _RangedFormat(ctypes.Structure):
    _fields_ = [("format", Format), ("low", ctypes.c_double), ("high", ctypes.c_double)]


_ca = _cf = None


def _framework(name: str) -> str:
    """A system framework's path: the same on every Mac (dlopen finds it in the system's shared
    cache even though the file itself isn't on disk), so a packaged app needs no lookup."""
    return f"/System/Library/Frameworks/{name}.framework/{name}"


def _libs():
    global _ca, _cf
    if _ca is None:
        _ca = ctypes.CDLL(_framework("CoreAudio"))
        _cf = ctypes.CDLL(_framework("CoreFoundation"))
        _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        _cf.CFRelease.argtypes = [ctypes.c_void_p]
    return _ca, _cf


def _get(obj: int, selector: int, ctype, scope: int = SCOPE_GLOBAL, element: int = 0) -> list | None:
    """A property's value, as a list of `ctype`; None if the object doesn't have it."""
    ca, _ = _libs()
    address, size = _Address(selector, scope, element), ctypes.c_uint32()
    if ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(address), 0, None, ctypes.byref(size)):
        return None
    count = size.value // ctypes.sizeof(ctype)
    buffer = (ctype * max(count, 1))()
    if ca.AudioObjectGetPropertyData(obj, ctypes.byref(address), 0, None, ctypes.byref(size), buffer):
        return None
    return list(buffer)[: size.value // ctypes.sizeof(ctype)]


def _set(obj: int, selector: int, value, scope: int = SCOPE_GLOBAL, element: int = 0) -> bool:
    ca, _ = _libs()
    address = _Address(selector, scope, element)
    return ca.AudioObjectSetPropertyData(obj, ctypes.byref(address), 0, None,
                                         ctypes.sizeof(value), ctypes.byref(value)) == 0


def _settable(obj: int, selector: int, scope: int, element: int) -> bool:
    ca, _ = _libs()
    address, flag = _Address(selector, scope, element), ctypes.c_ubyte()
    return bool(ca.AudioObjectHasProperty(obj, ctypes.byref(address))
                and ca.AudioObjectIsPropertySettable(obj, ctypes.byref(address), ctypes.byref(flag)) == 0
                and flag.value)


def _name(device: int) -> str:
    _, cf = _libs()
    ref = _get(device, NAME, ctypes.c_void_p)
    if not ref or not ref[0]:
        return ""
    buffer = ctypes.create_string_buffer(1024)
    ok = cf.CFStringGetCString(ref[0], buffer, len(buffer), CF_UTF8)
    cf.CFRelease(ref[0])
    return buffer.value.decode("utf-8", "replace") if ok else ""


def output_streams(device: int) -> list[int]:
    return _get(device, STREAMS, ctypes.c_uint32, SCOPE_OUTPUT) or []


def find_device(name: str, nth: int = 0) -> int | None:
    """The Core Audio id of the `nth` output device called `name` (two displays can share a name;
    PortAudio lists them in Core Audio's order, so the nth of each is the same device)."""
    matches = [d for d in _get(SYSTEM_OBJECT, DEVICES, ctypes.c_uint32) or []
               if output_streams(d) and _name(d) == name]
    return matches[nth] if nth < len(matches) else None


def nominal_rate(device: int) -> int | None:
    rate = _get(device, NOMINAL_RATE, ctypes.c_double)
    return int(rate[0]) if rate else None


def has_volume(device: int) -> bool:
    """Can the device turn itself down (a main volume, or one on the first channel)?"""
    return any(_settable(device, VOLUME, SCOPE_OUTPUT, element) for element in (0, 1))


def best_format(stream: int, rate: int) -> Format | None:
    """The physical format at `rate` that keeps the most bits (mixable before non-mixable, so
    the device is still usable the ordinary way once we let go); None if the rate isn't offered."""
    current = _get(stream, PHYSICAL_FORMAT, Format)
    channels = current[0].channels if current else None
    offered = [r.format for r in _get(stream, PHYSICAL_FORMATS, _RangedFormat) or []
               if r.format.format_id == LINEAR_PCM and r.low <= rate <= r.high
               and (channels is None or r.format.channels == channels)]
    if not offered:
        return None
    best = max(offered, key=lambda f: (f.usable_bits, not f.flags & FLAG_NON_MIXABLE, f.bits))
    best.rate = float(rate)
    return best


class Busy(Exception):
    """Another app has the device to itself."""


class Unsupported(Exception):
    """The device can't run at the file's rate."""


_original: dict[int, list[tuple[int, Format]]] = {}  # device -> its streams' formats before we changed them


class Claim:
    """The device held for bit-perfect playback: hogged, at the file's rate with the most bits.
    `release` lets other apps back in; the format stays until `restore` (no relocking the DAC
    twice between two tracks at different rates)."""

    def __init__(self, device: int, rate: int):
        self.device = device
        self.bits: int | None = None   # bits per sample the device keeps, at its new format
        if not _set(device, HOG_MODE, ctypes.c_int32(os.getpid())):
            raise Busy("another app has the device to itself")
        held = _get(device, HOG_MODE, ctypes.c_int32)
        if not held or held[0] != os.getpid():
            raise Busy("another app has the device to itself")
        try:
            self._set_format(rate)
        except BaseException:
            self.release()
            raise

    def _set_format(self, rate: int):
        changes = []
        for stream in output_streams(self.device):
            wanted = best_format(stream, rate)
            if wanted is None:
                raise Unsupported(f"the device doesn't offer {rate / 1000:g} kHz")
            current = (_get(stream, PHYSICAL_FORMAT, Format) or [None])[0]
            if current is not None and _same(current, wanted):
                changes.append(wanted)
                continue
            saved = _original.setdefault(self.device, [])
            if current is not None and all(s != stream for s, _ in saved):
                saved.append((stream, current))   # only the first time: that's how the user had it
            log.info("device %s stream %s: %s → %s", self.device, stream,
                     current.describe() if current else "?", wanted.describe())
            if not _set(stream, PHYSICAL_FORMAT, wanted):
                raise Unsupported(f"the device refused {wanted.describe()}")
            changes.append(wanted)
        if not changes:
            raise Unsupported("the device has no output streams")
        _wait_for(lambda: all(_same((_get(s, PHYSICAL_FORMAT, Format) or [None])[0], f)
                              for s, f in zip(output_streams(self.device), changes)))
        self.bits = min(f.usable_bits for f in changes)

    def release(self):
        """Let other apps use the device again (only if it's still ours)."""
        held = _get(self.device, HOG_MODE, ctypes.c_int32)
        if held and held[0] == os.getpid():
            _set(self.device, HOG_MODE, ctypes.c_int32(-1))


def changed() -> bool:
    return bool(_original)


def restore(keep: int | None = None):
    """Put back the formats bit-perfect changed, on every device but `keep`. No stream of ours
    may be open on them."""
    for device in [d for d in _original if d != keep]:
        for stream, fmt in _original.pop(device):
            log.info("device %s stream %s: back to %s", device, stream, fmt.describe())
            if not _set(stream, PHYSICAL_FORMAT, fmt):
                log.info("couldn't put device %s back to %s", device, fmt.describe())


def _same(a: Format | None, b: Format) -> bool:
    return (a is not None and a.rate == b.rate and a.bits == b.bits and a.channels == b.channels
            and a.flags == b.flags)


def _wait_for(done) -> bool:
    """A format change takes effect a moment later: wait for it (briefly)."""
    deadline = time.monotonic() + FORMAT_SETTLE_S
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.01)
    log.info("device format change still pending after %.1f s", FORMAT_SETTLE_S)
    return False

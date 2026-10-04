"""Windows' own recording of what a device is playing ("loopback"), plus the Windows
settings that can change the sound in shared mode. Windows only; read-only — nothing
here changes any setting.

PortAudio as shipped with sounddevice can't record loopback, so this talks to Windows'
audio service directly through comtypes/pycaw (already used by devices.py), with the one
interface pycaw doesn't define (IAudioCaptureClient) declared here.

Everything runs on the caller's thread and blocks for the length of the recording, so
call it from a worker thread — never from a window callback (a blocking call there freezes the window).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from ctypes import HRESULT, POINTER, c_uint32, c_uint64
from ctypes.wintypes import BYTE, DWORD, WORD
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

AVAILABLE = sys.platform == "win32"

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
CLSCTX_ALL = 23
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = "{00000003-0000-0010-8000-00AA00389B71}"


@dataclass
class Recording:
    rate: int
    audio: np.ndarray       # frames x channels, float64
    gaps: int = 0           # times Windows reported a break in the recording


@dataclass
class WindowsSettings:
    mixer_rate: int | None = None
    mixer_bits: int | None = None
    system_volume: float | None = None   # the device's volume slider, 0..1
    muted: bool | None = None
    app_volume: float | None = None      # this player's slider in Windows' Volume mixer, 0..1


def _interfaces():
    from comtypes import COMMETHOD, GUID, IUnknown

    class IAudioCaptureClient(IUnknown):
        _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
        _methods_ = (
            COMMETHOD([], HRESULT, "GetBuffer",
                      (["out"], POINTER(POINTER(BYTE)), "ppData"),
                      (["out"], POINTER(c_uint32), "pNumFramesToRead"),
                      (["out"], POINTER(DWORD), "pdwFlags"),
                      (["out"], POINTER(c_uint64), "pu64DevicePosition"),
                      (["out"], POINTER(c_uint64), "pu64QPCPosition")),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
            COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
        )

    return IAudioCaptureClient


class _WaveFormatExtensible(ctypes.Structure):
    _fields_ = [("wFormatTag", WORD), ("nChannels", WORD), ("nSamplesPerSec", DWORD),
                ("nAvgBytesPerSec", DWORD), ("nBlockAlign", WORD), ("wBitsPerSample", WORD),
                ("cbSize", WORD), ("wValidBitsPerSample", WORD), ("dwChannelMask", DWORD),
                ("SubFormat", BYTE * 16)]


def _device(name: str | None):
    from pycaw.pycaw import AudioUtilities

    if name is None:
        return AudioUtilities.GetSpeakers()
    for device in AudioUtilities.GetAllDevices():
        if device.id and device.id.startswith("{0.0.0.") and device.state.name == "Active" \
                and device.FriendlyName == name:
            return device
    return None


def _audio_client(device):
    from pycaw.api.audioclient import IAudioClient

    imm = device._dev  # the IMMDevice behind pycaw's wrapper
    return imm.Activate(IAudioClient._iid_, CLSCTX_ALL, None).QueryInterface(IAudioClient)


def _mix_format(client):
    """(rate, channels, bits, is_float) of the Windows mixer for this device."""
    from comtypes import GUID

    pointer = client.GetMixFormat()
    fmt = pointer.contents
    rate, channels, bits, tag = fmt.nSamplesPerSec, fmt.nChannels, fmt.wBitsPerSample, fmt.wFormatTag
    is_float = tag == WAVE_FORMAT_IEEE_FLOAT
    if tag == WAVE_FORMAT_EXTENSIBLE:
        ext = ctypes.cast(pointer, POINTER(_WaveFormatExtensible)).contents
        sub = GUID()
        ctypes.memmove(ctypes.byref(sub), ext.SubFormat, 16)
        is_float = str(sub).upper() == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
    return pointer, rate, channels, bits, is_float


def settings(device_name: str | None) -> WindowsSettings:
    """What Windows is set to for this device (read-only). Fields it couldn't read stay None."""
    found = WindowsSettings()
    if not AVAILABLE:
        return found
    device = _device(device_name)
    if device is None:
        return found
    try:
        volume = device.EndpointVolume
        found.system_volume = float(volume.GetMasterVolumeLevelScalar())
        found.muted = bool(volume.GetMute())
    except Exception:  # noqa: BLE001 — unknown is fine
        log.info("couldn't read the device volume", exc_info=True)
    try:
        pointer, found.mixer_rate, _, found.mixer_bits, _ = _mix_format(_audio_client(device))
        ctypes.windll.ole32.CoTaskMemFree(pointer)
    except Exception:  # noqa: BLE001
        log.info("couldn't read the mixer format", exc_info=True)
    try:  # this player's slider in the Volume mixer, on this device (not just the default one)
        from pycaw.pycaw import AudioSession, IAudioSessionControl2

        sessions = device.AudioSessionManager.GetSessionEnumerator()
        for i in range(sessions.GetCount()):
            control = sessions.GetSession(i)
            if control is None:
                continue
            session = AudioSession(control.QueryInterface(IAudioSessionControl2))
            if session.ProcessId == os.getpid():
                found.app_volume = float(session.SimpleAudioVolume.GetMasterVolume())
                break
    except Exception:  # noqa: BLE001
        log.info("couldn't read this app's volume", exc_info=True)
    return found


def record(device_name: str | None, seconds: float, flowing=None) -> Recording | None:
    """Record `seconds` of what Windows plays on the device (shared mode only: a device
    taken in exclusive mode bypasses Windows, so there's nothing to record). `flowing`
    (a threading.Event) is set once the recording is actually receiving sound."""
    if not AVAILABLE:
        return None
    IAudioCaptureClient = _interfaces()
    device = _device(device_name)
    if device is None:
        return None
    client = _audio_client(device)
    pointer, rate, channels, bits, is_float = _mix_format(client)
    try:
        client.Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, 10_000_000, 0, pointer, None)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(pointer)
    capture = client.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
    dtype = np.float32 if is_float and bits == 32 else {16: np.int16, 32: np.int32}.get(bits)
    if dtype is None:
        return None  # a mixer format we don't decode (e.g. 24-bit packed): give up quietly
    scale = 1.0 if dtype == np.float32 else float(2 ** (bits - 1))
    chunks, frames_wanted, gaps = [], int(rate * seconds), 0
    client.Start()
    try:
        got = 0
        deadline = time.perf_counter() + seconds + 2.0
        while got < frames_wanted and time.perf_counter() < deadline:
            time.sleep(0.01)
            while capture.GetNextPacketSize():
                data, frames, flags, _, _ = capture.GetBuffer()
                if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                    block = np.zeros((frames, channels))
                else:
                    raw = ctypes.string_at(data, frames * channels * np.dtype(dtype).itemsize)
                    block = np.frombuffer(raw, dtype=dtype).reshape(frames, channels) / scale
                if flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY and got:
                    gaps += 1
                capture.ReleaseBuffer(frames)
                chunks.append(block.astype(np.float64))
                got += frames
                if flowing is not None and frames:
                    flowing.set()
    finally:
        client.Stop()
    if not chunks:
        return Recording(rate, np.zeros((0, channels)), gaps)
    return Recording(rate, np.concatenate(chunks)[:frames_wanted], gaps)

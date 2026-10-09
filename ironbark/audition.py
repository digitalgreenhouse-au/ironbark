"""Listening side by side: loop a short stretch and switch between versions of it. No UI code.

Built once, for every "hear the difference" feature: the ABX blind test (abx.py) now, and the
Clipping window's "ordinary DAC vs clean" listening (clip_audition, Experimental). Whoever uses it
hands over the versions as arrays already in memory — the same length, sample rate and channel
count, lined up sample for sample — and a loop range.

- `Switcher` (no sound device; tested on its own) makes the sound: it loops the stretch and
  switches between the versions ("slots") **at the same sample**: the music carries on from
  exactly where it was, only the version changes. Every switch is the same short dip — a fade
  down, then up again — whichever slots are involved, even between two slots holding the same
  version (in ABX, A and X may be the very same recording), so the switch itself never gives
  the answer away. The loop's seam gets the same dip, so it never clicks.
- `Audition` plays a Switcher on a sound device, the way the player does: shared (float32), or
  exclusive (int32) when the player is in bit-perfect mode. What it plays is never bit-perfect
  (fades, level matching), and it doesn't pretend to be.

Each slot has its own gain (level matching: turn the louder version down), and there's one
`volume` on top (the player's own volume, so the test is no louder than the music was).

`seamless=True` (the hardware test's signals, testsignals.py) loops with no dip at the seam:
only for sound that is exactly periodic over the loop, so going round is just the next sample.
"""

from __future__ import annotations

import logging
import math
import threading

import numpy as np
import sounddevice as sd

from . import devices

log = logging.getLogger(__name__)

FADE_S = 0.02        # each half of the dip at a switch or the loop's seam (20 ms down, 20 ms up)
BLOCK_FRAMES = 2048  # frames per sound-card request: short, so a switch is heard at once
INT32_FULL = 2**31


def _shape(g: np.ndarray) -> np.ndarray:
    """A raised-cosine fade: 0 → 1 as g goes 0 → 1, smooth at both ends (no click)."""
    return 0.5 - 0.5 * np.cos(np.pi * np.clip(g, 0.0, 1.0))


class Switcher:
    """Loops `slots[slot][start:end]` and switches between slots with an identical dip.

    Thread-safe: `render` runs on the audio thread, the rest on the UI's."""

    def __init__(self, slots, rate: int, loop: tuple[int, int] | None = None, gains=None,
                 fade_s: float = FADE_S, volume: float = 1.0, seamless: bool = False):
        arrays = [np.asarray(s, dtype=np.float32) for s in slots]
        arrays = [a[:, None] if a.ndim == 1 else a for a in arrays]
        if not arrays:
            raise ValueError("nothing to play")
        frames, channels = arrays[0].shape
        if any(a.shape != (frames, channels) for a in arrays):
            raise ValueError("every version must be the same length and channel count")
        self.slots, self.rate, self.channels, self.frames = arrays, rate, channels, frames
        self.gains = [1.0] * len(arrays) if gains is None else [float(g) for g in gains]
        self.volume = float(volume)
        self.fade = max(1, int(round(fade_s * rate)))
        self.seamless = seamless     # no dip at the loop's seam (periodic sound only)
        self._lock = threading.Lock()
        self.start, self.end = 0, frames
        if loop is not None:
            self.set_loop(*loop)
        self.position = self.start   # the next frame to play
        self.slot = 0                # the slot being heard
        self._target = 0             # the slot to switch to at the bottom of the dip
        self._restart = False        # jump back to the loop's start at the bottom of the dip
        self._level = 0.0            # where in the fade (0 silent … 1 full); starts faded in
        self._falling = False
        self.switches = 0            # dips done for a switch (for tests and the log)
        self._waiting: dict = {}     # slot -> (sound, gain) to put in at the next dip (see assign)

    # -- control (UI thread) ------------------------------------------------------------
    def set_loop(self, start: int, end: int):
        start, end = max(0, int(start)), min(self.frames, int(end))
        if end - start < 4 * self.fade:
            raise ValueError("the loop is too short")
        with self._lock:
            self.start, self.end = start, end
            if hasattr(self, "position") and not start <= self.position < end:
                self.position = start

    def select(self, slot: int):
        """Switch to `slot` at the same sample, through the dip. Selecting the slot already
        playing (or already on its way) does nothing."""
        if not 0 <= slot < len(self.slots):
            raise IndexError(slot)
        with self._lock:
            if slot == self._target:
                return
            self._target = slot
            self._falling = True

    def restart(self, slot: int | None = None):
        """Back to the start of the loop (through the dip), optionally on another slot."""
        with self._lock:
            if slot is not None:
                self._target = slot
            self._restart = True
            self._falling = True

    def assign(self, slot: int, sound, gain: float = 1.0):
        """Put another version in `slot` (ABX's X changes every trial). If that slot is being
        heard, the change waits for a dip back to the loop's start, so nothing jumps mid-note."""
        array = np.asarray(sound, dtype=np.float32)
        array = array[:, None] if array.ndim == 1 else array
        if array.shape != self.slots[0].shape:
            raise ValueError("every version must be the same length and channel count")
        with self._lock:
            if slot == self.slot:
                self._waiting[slot] = (array, float(gain))
                self._restart = self._falling = True
            else:
                self.slots[slot], self.gains[slot] = array, float(gain)

    def reset(self, slot: int = 0):
        """Before playing starts: at the loop's start, on `slot`, fading in."""
        with self._lock:
            for waiting, (array, gain) in self._waiting.items():
                self.slots[waiting], self.gains[waiting] = array, gain
            self._waiting = {}
            self.position, self.slot, self._target = self.start, slot, slot
            self._level, self._falling, self._restart = 0.0, False, False

    @property
    def target(self) -> int:
        return self._target

    # -- sound (audio thread) -----------------------------------------------------------
    def render(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, self.channels), dtype=np.float32)
        with self._lock:
            filled = 0
            while filled < frames:
                filled += self._run(out, filled, frames - filled)
        return out

    def _run(self, out: np.ndarray, at: int, want: int) -> int:
        """Fill part of `out` from `at`: a steady stretch in one go, or a fade frame by frame-ish
        (vectorised over the fade's length). Returns the frames written."""
        step = 1.0 / self.fade
        if self.seamless and self.position >= self.end:
            self.position = self.start              # round the loop without a dip
        to_seam = self.end - self.position         # frames before the loop's end
        room = to_seam if self.seamless else to_seam - self.fade
        if not self._falling and self._level >= 1.0 and room > 0:
            n = min(want, room)                     # steady: full level, not yet near the seam
            self._copy(out, at, n, None)
            self.position += n
            return n
        # In a fade. The level rises or falls one step a frame; near the seam it's also capped
        # so it reaches 0 exactly at the loop's end.
        n = min(want, max(1, to_seam), self.fade)
        k = np.arange(1, n + 1, dtype=np.float64)
        if self._falling:
            levels = self._level - step * k
        else:
            levels = np.minimum(1.0, self._level + step * k)
        if not self.seamless:
            seam_cap = (to_seam - k) / self.fade   # how much room is left before the seam
            levels = np.minimum(levels, seam_cap)
        bottom = np.nonzero(levels <= 0)[0]
        if len(bottom):
            n = int(bottom[0]) + 1                 # stop at the bottom of the dip
            levels = levels[:n]
        levels = np.maximum(levels, 0.0)
        self._copy(out, at, n, _shape(levels))
        self.position += n
        self._level = float(levels[-1])
        if len(bottom):
            self._bottom()
        return n

    def _bottom(self):
        """Silent for an instant: the moment to change slot, or to go round the loop."""
        for slot, (array, gain) in self._waiting.items():
            self.slots[slot], self.gains[slot] = array, gain
        self._waiting = {}
        if self._target != self.slot:
            self.slot = self._target
            self.switches += 1
        if self._restart or (self.position >= self.end and not self.seamless):
            self.position = self.start
            self._restart = False
        self._falling = False
        self._level = 0.0

    def _copy(self, out, at, n, envelope):
        gain = self.gains[self.slot] * self.volume
        piece = self.slots[self.slot][self.position: self.position + n]
        if envelope is None:
            np.multiply(piece, np.float32(gain), out=out[at: at + n])
        else:
            out[at: at + n] = piece * (envelope[:, None] * gain).astype(np.float32)


class Audition:
    """Plays a Switcher on a sound device until `stop`. `stream_factory` stands in for
    sounddevice.OutputStream in tests (no sound device is ever opened there). `default_check`:
    for bit-perfect on the system default (device None), asked for the device just before it's
    opened — the player's check_default, which raises devices.DefaultChanged when Windows' default
    is no longer the device whose volume control was checked."""

    def __init__(self, switcher: Switcher, device: str | None = None, exclusive: bool = False,
                 stream_factory=None, default_check=None):
        self.switcher = switcher
        self.device, self.want_exclusive = device, exclusive
        self._default_check = default_check
        self._factory = stream_factory or sd.OutputStream
        self._stream = None
        self.exclusive = False
        self.device_name = ""
        self.note = ""   # e.g. why exclusive mode wasn't possible

    @property
    def playing(self) -> bool:
        return self._stream is not None

    def play(self, slot: int | None = None):
        """Start (from the loop's start) on `slot`, or switch to it if already playing."""
        if self._stream is not None:
            if slot is not None:
                self.switcher.select(slot)
            return
        self.switcher.reset(self.switcher.target if slot is None else slot)
        self._stream = self._open()

    def _open(self):
        device = self.device
        if self.want_exclusive and device is None and self._default_check is not None:
            device = self._default_check()   # (raises devices.DefaultChanged: nothing is opened)
        index = devices.find_device(device)
        self.note = ""
        if index is None and device is not None:
            if self.want_exclusive:
                # As the player: bit-perfect mode never moves to another device by itself (it could
                # have no volume control of its own).
                raise devices.DeviceMissing(f"{device} isn't connected")
            self.note = f"{device} isn't connected — playing on the system default"
            index = devices.find_device(None)
        kwargs = dict(samplerate=self.switcher.rate, channels=self.switcher.channels, blocksize=BLOCK_FRAMES,
                      latency="low", callback=self._callback)
        if self.want_exclusive and index is not None and devices.exclusive_supported():
            stream = None
            try:
                stream = self._factory(device=index, dtype="int32", extra_settings=sd.WasapiSettings(exclusive=True),
                                       dither_off=True, clip_off=True, **kwargs)
                stream.start()
                devices.holding(stream)
                self.exclusive = True
                self.device_name = self._name(stream)
                return stream
            except sd.PortAudioError as exc:
                log.info("audition: exclusive mode refused: %s", exc)
                if stream is not None:
                    stream.close(ignore_errors=True)
                self.note = "The device wouldn't take this in bit-perfect mode — playing shared"
        extra = sd.WasapiSettings(auto_convert=True) if devices.IS_WINDOWS and index is not None else None
        stream = self._factory(device=index, dtype="float32", extra_settings=extra, **kwargs)
        stream.start()
        devices.holding(stream)
        self.exclusive = False
        self.device_name = self._name(stream)
        return stream

    @staticmethod
    def _name(stream) -> str:
        try:
            return sd.query_devices(stream.device)["name"]
        except (sd.PortAudioError, ValueError, TypeError):
            return "Output device"

    def _callback(self, outdata, frames, time_info, status):
        sound = self.switcher.render(frames)
        if self.exclusive:
            outdata[:] = np.clip(np.rint(sound.astype(np.float64) * INT32_FULL), -INT32_FULL, INT32_FULL - 1)
        else:
            outdata[:] = sound

    def stop(self):
        """Stop and let go of the sound device."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
                stream.close()
            except sd.PortAudioError:
                log.exception("closing the audition stream")
            finally:
                devices.released(stream)

    close = stop


def level_gains(difference_db: float) -> tuple[float, float]:
    """Gains for (A, B) that match their levels when B is `difference_db` louder than A: the
    louder one is turned down, never the quieter one up (that could clip)."""
    if not math.isfinite(difference_db):
        return 1.0, 1.0
    down = 10 ** (-abs(difference_db) / 20)
    return (1.0, down) if difference_db > 0 else (down, 1.0)

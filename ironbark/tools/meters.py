"""Level meters to their published standards, computed from every sample sent to the
device (after the volume) — not decoration. No UI code in here.

- VU (IEC 60268-17): full-wave average, 2nd-order needle ballistics reaching 99% of the
  reading in 300 ms with 1.25% overshoot (the standard: 1-1.5%). 0 VU = a sine at the
  chosen alignment level (-18 dBFS is EBU R68; -20 dBFS is SMPTE RP155) — or "Auto": each
  album measured (`album_alignment`) so its loud passages sit at 0 VU, like an engineer
  setting a VU range switch for the job. "Auto (loudness)" instead sets 0 VU to a steady
  1 kHz tone exactly as loud as the whole album (`album_alignment_loudness`); how loud a tone
  is in LUFS is measured with loudness.py itself (`tone_offset`), not assumed.
- PPM (IEC 60268-10, quasi-peak): a model of the meter's own circuit — a full-wave
  rectifier charging a capacitor (fast at first, slower near the peak) — fitted to the
  standard's tone-burst table. Type I (DIN): 5 ms burst reads -2 dB, falls 20 dB in 1.7 s.
  Type II (BBC/EBU): 10 ms burst reads -2 dB, falls 24 dB in 2.8 s. Shown in dBFS.
- RMS: true RMS over the last 300 ms, AES17 convention (a full-scale sine reads 0 dB).
- True peak: 4x oversampled (as ITU-R BS.1770), the level a DAC actually reaches, with a
  3-second hold and the highest so far; sample peak alongside.
- Loudness (EBU R128 / ITU-R BS.1770, see loudness.py): momentary and short-term, read 100
  times a second, and the integrated loudness and loudness range of the track so far (they
  start again at each new track, or on reset), with the highest momentary and short-term.
- PSR (peak to short-term loudness ratio): the highest true peak of the last 3 seconds minus
  the short-term loudness — "the difference between the true-peak level and the short-term
  loudness", with a three-second window (MeterPlugs / Ian Shepherd's 2017 AES paper with
  Grimm, Nugen and RTW). Small = squashed; MeterPlugs suggests not going below 8 in the
  loudest parts.

The meters run a little ahead of the sound (the driver's buffer), so every reading is
stamped with the moment it reaches the speakers, and `reading_at(now)` returns what's
being heard now.
"""

from __future__ import annotations

import collections
import functools
import math
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import audiofile
from ..analysis import OVERSAMPLE, TAPS_PER_PHASE, oversampling_phases
from ..library import album_files
from ..loudness import BLOCK_HOP, MOMENTARY, SHORT_TERM, Loudness, Programme, file_powers, integrated

STEP = 0.001          # the meters' own clock: one reading per millisecond
SUBSTEPS = 10         # finer steps inside each millisecond for the PPM circuit (0.1 ms)

# VU needle: damping for 1.25% overshoot; natural frequency so a step reaches 99% at 300 ms.
VU_ZETA = 0.81272
VU_OMEGA = 13.5171    # rad/s
VU_ALIGN_DBFS = -18.0

# PPM: capacitor charge time constant (ms) fitted to IEC 60268-10's burst table; fall rate.
PPM_TYPES = {
    "I": {"name": "Type I (DIN)", "tau_ms": 1.3116, "fall_db_per_s": 20 / 1.7},
    "II": {"name": "Type II (BBC/EBU)", "tau_ms": 2.6358, "fall_db_per_s": 24 / 2.8},
}
AUTO_PERCENTILE = 90        # Auto: 0 VU = the level the loudest 10% of an album's moments exceed
AUTO_RANGE = (-24.0, -6.0)  # ...kept within these (dBFS)
AUTO_SILENCE = -60.0        # moments quieter than this (dBFS) don't count
RMS_WINDOW = 0.300
TRUE_PEAK_HOLD = 3.0
PSR_WINDOW = 3.0      # seconds of true peak that PSR looks back over
TONE_HZ = 1000.0      # Auto (loudness): the steady tone 0 VU is matched to
FLOOR = 1e-6          # -120 dB


def db(x: float) -> float:
    return 20 * math.log10(x) if x > FLOOR else -120.0


def _charge(u: float) -> float:
    """How hard a full-wave rectified sine (peak 1) charges a capacitor already at u (0..1)."""
    if u <= 0:
        return 2 / math.pi
    if u >= 1:
        return 0.0
    a = math.asin(u)
    return (2 * math.cos(a) - u * (math.pi - 2 * a)) / math.pi


def _ppm_steady(ppm_type: dict) -> float:
    """Where the PPM settles on a steady full-scale tone, with the leak running: the meter is
    calibrated against this, as a real one is against its reference tone."""
    tau, dt = ppm_type["tau_ms"] / 1000, STEP / SUBSTEPS
    fall = 10 ** (-ppm_type["fall_db_per_s"] * dt / 20)
    v = 0.0
    for _ in range(20000):
        v += dt / tau * _charge(v)
        v *= fall
    return v


@dataclass
class Reading:
    vu: tuple[float, ...]          # VU per channel (0 = alignment level)
    ppm: tuple[float, ...]         # dBFS
    rms: tuple[float, ...]         # dB, AES17
    true_peak: tuple[float, ...]   # dBTP, held for 3 s
    sample_peak: tuple[float, ...]  # dBFS, held for 3 s
    max_true_peak: float           # dBTP, highest since the last reset
    momentary: float | None = None       # LUFS, 400 ms
    short_term: float | None = None      # LUFS, 3 s
    integrated: float | None = None      # LUFS, this track so far (gated)
    loudness_range: float | None = None  # LU, this track so far
    max_momentary: float | None = None
    max_short_term: float | None = None
    measured: float = 0.0                # seconds the integrated measurement has run
    psr: float | None = None             # dB: true peak of the last 3 s minus short-term loudness


class _Channel:
    """The running state of every meter for one channel."""

    def __init__(self):
        self.vu_x = 0.0   # needle position (linear, mean-|x| units)
        self.vu_v = 0.0   # needle speed
        self.ppm = 0.0    # capacitor voltage (linear, peak units)
        self.squares: collections.deque = collections.deque()
        self.square_sum = 0.0


class MeterEngine:
    """Consumes blocks of sent audio (from Player.monitor) on its own thread and keeps a
    timeline of readings, one per millisecond, stamped with when they're heard."""

    def __init__(self, feed: queue.Queue, ppm_type: str = "I", vu_align_dbfs: float = VU_ALIGN_DBFS):
        self.feed = feed
        self.ppm_type = ppm_type
        self.vu_align_dbfs = vu_align_dbfs
        self._channels: list[_Channel] = []
        self._timeline: collections.deque = collections.deque(maxlen=4000)  # (time, Reading parts)
        self._phases = oversampling_phases()
        self._tp_history: collections.deque = collections.deque()  # (time, [tp per ch], [sp per ch])
        self.max_true_peak = 0.0
        self._tail: np.ndarray | None = None  # last samples, so oversampling runs across blocks
        self._carry: np.ndarray | None = None  # samples short of a whole millisecond, for next time
        self._ppm_scale = 1.0 / _ppm_steady(PPM_TYPES[ppm_type])  # a steady tone reads its level exactly
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.current_path = None  # the file being metered (for Auto alignment)
        self.loudness: Loudness | None = None
        self._loud_timeline: collections.deque = collections.deque(maxlen=1500)  # (time, momentary, short-term)
        self._loud_cache: tuple | None = None  # (integrated, range) of the measurement so far
        # (meters thread only) what _loud_cache is worked out from, and how many steps it has had
        self._programme = Programme()
        self._programme_fed = 0
        self._loudness_reset = False
        self.max_momentary: float | None = None
        self.max_short_term: float | None = None
        self._thread = threading.Thread(target=self._run, name="meters", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def reset_max(self):
        with self._lock:   # process() raises it under the lock: a reset in between would be undone
            self.max_true_peak = 0.0

    def reset_loudness(self):
        """Start the integrated loudness, loudness range and their maxima again from now."""
        self._loudness_reset = True

    def set_alignment(self, dbfs: float):
        """What 0 VU stands for. Takes effect at once; the needle's motion carries on unbroken."""
        self.vu_align_dbfs = dbfs

    def vu_reference(self) -> float:
        """The needle drive (mean |x|) that reads 0 VU: a sine at the alignment level."""
        return (2 / math.pi) * 10 ** (self.vu_align_dbfs / 20)

    # -- processing ------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                item = self.feed.get(timeout=0.1)
            except queue.Empty:
                continue
            path = item.get("path")
            if path != self.current_path and self.current_path is not None:
                self._loudness_reset = True  # a new track: a new integrated measurement
            self.current_path = path
            self.process(item["sent"], item["rate"], item["dac_time"])

    def process(self, block: np.ndarray, rate: int, heard_at: float):
        """One block of what was sent (frames x channels; int32 or float), heard from `heard_at`.
        Blocks are taken as one continuous stream: samples that don't fill a whole millisecond
        are carried into the next block, so none are skipped."""
        x = block.astype(np.float64)
        if block.dtype == np.int32:
            x /= 2.0 ** 31
        channels = x.shape[1]
        self._measure_loudness(x, rate, heard_at)
        if len(self._channels) != channels:
            self._channels = [_Channel() for _ in range(channels)]
            self._tail = None
            self._carry = None
        if self._carry is not None and len(self._carry):
            heard_at -= len(self._carry) / rate
            x = np.concatenate([self._carry, x])
        step = max(1, int(round(rate * STEP)))
        count = len(x) // step
        self._carry = x[count * step:]
        if not count:
            return
        x = x[: count * step]
        rectified = np.abs(x)
        chunks = rectified.reshape(count, step, channels)
        mean_abs = chunks.mean(axis=1)                       # what a VU's averaging rectifier sees
        mean_sq = (x.reshape(count, step, channels) ** 2).mean(axis=1)
        # The PPM needs a finer clock: its whole job happens within a few milliseconds.
        edges = np.round(np.arange(count * SUBSTEPS) * step / SUBSTEPS).astype(int)
        fine_peak = np.maximum.reduceat(rectified, edges, axis=0)  # count*SUBSTEPS x channels

        ppm_type = PPM_TYPES[self.ppm_type]
        tau = ppm_type["tau_ms"] / 1000
        dt = STEP / SUBSTEPS
        fall = 10 ** (-ppm_type["fall_db_per_s"] * dt / 20)
        window = int(round(RMS_WINDOW / STEP))
        vu, ppm, rms = (np.empty((count, channels)) for _ in range(3))
        for c, state in enumerate(self._channels):
            for i in range(count):
                drive = mean_abs[i, c]
                # VU needle: a damped 2nd-order system pulled towards the averaged signal (slow
                # enough that one step per millisecond is exact to well under 0.1 dB).
                accel = VU_OMEGA ** 2 * (drive - state.vu_x) - 2 * VU_ZETA * VU_OMEGA * state.vu_v
                state.vu_v += accel * STEP
                state.vu_x += state.vu_v * STEP
                vu[i, c] = state.vu_x  # raw: the alignment is applied when it's read
                # PPM: the capacitor charges from the rectified signal and always leaks away;
                # the meter shows the highest it got within the millisecond.
                highest = 0.0
                for k in range(i * SUBSTEPS, (i + 1) * SUBSTEPS):
                    p = fine_peak[k, c]
                    if p > state.ppm:
                        state.ppm += dt / tau * p * _charge(state.ppm / p)
                    state.ppm *= fall
                    if state.ppm > highest:
                        highest = state.ppm
                ppm[i, c] = highest * self._ppm_scale
                # RMS over the last 300 ms.
                state.squares.append(mean_sq[i, c])
                state.square_sum += mean_sq[i, c]
                if len(state.squares) > window:
                    state.square_sum -= state.squares.popleft()
                rms[i, c] = math.sqrt(max(state.square_sum, 0.0) / len(state.squares))

        tp, sp = self._peaks(x)
        with self._lock:
            for i in range(count):
                self._timeline.append((heard_at + i * STEP, vu[i].copy(), ppm[i].copy(), rms[i].copy()))
            self._tp_history.append((heard_at, tp, sp))
            self.max_true_peak = max(self.max_true_peak, max(tp))

    def _measure_loudness(self, x: np.ndarray, rate: int, heard_at: float):
        meter = self.loudness
        with self._lock:
            if meter is None or meter.rate != rate or meter.channels != x.shape[1]:
                meter = self.loudness = Loudness(rate, x.shape[1])
                self._loudness_reset = False
                self.max_momentary = self.max_short_term = None
                self._new_programme(meter)
            elif self._loudness_reset:
                self._loudness_reset = False
                meter.reset()
                self.max_momentary = self.max_short_term = None
                self._new_programme(meter)
            carried = meter.carried
            new = meter.add(x)
            if not new:
                return
            momentary, short = meter.trailing(MOMENTARY, new), meter.trailing(SHORT_TERM, new)
            into = np.arange(meter.count - new, meter.count) - meter.start  # steps into this measurement
            for values, steps, name in ((momentary, MOMENTARY, "max_momentary"),
                                        (short, SHORT_TERM, "max_short_term")):
                inside = values[into >= steps - 1]  # only windows wholly inside it
                inside = inside[~np.isnan(inside)]
                if len(inside):
                    best = float(inside.max())
                    current = getattr(self, name)
                    setattr(self, name, best if current is None else max(current, best))
            ends = heard_at + (np.arange(1, new + 1) * meter.step - carried) / rate
            self._loud_timeline.extend(zip(ends.tolist(), momentary.tolist(), short.tolist()))
        self._update_programme(meter)

    def _new_programme(self, meter: Loudness):
        """A new integrated measurement starts at `meter.start` (call with the lock held)."""
        self._programme = Programme()
        self._programme_fed = meter.start
        self._loud_cache = None

    def _update_programme(self, meter: Loudness):
        """(meters thread) The integrated loudness and range so far, brought up to date at most 10
        times a second of audio. Worked out here, outside the lock, so reading them on the
        window's thread costs nothing however long the track. Only this thread changes `meter`."""
        if meter.count - self._programme_fed < BLOCK_HOP and self._loud_cache is not None:
            return
        self._programme.add(meter.powers[self._programme_fed: meter.count])
        self._programme_fed = meter.count
        figures = (self._programme.integrated(), self._programme.loudness_range())
        with self._lock:
            self._loud_cache = figures

    def _loudness_now(self, now: float) -> tuple:
        """(momentary, short-term, integrated, range, seconds measured). Call with the lock held."""
        meter = self.loudness
        if meter is None:
            return None, None, None, None, 0.0
        chosen = None
        for entry in reversed(self._loud_timeline):
            if entry[0] <= now:
                chosen = entry
                break
        if chosen is None and self._loud_timeline:
            chosen = self._loud_timeline[0]
        whole, spread = self._loud_cache or (None, None)

        def clean(v):
            return None if v is None or math.isnan(v) or math.isinf(v) else v

        m, s = (chosen[1], chosen[2]) if chosen else (None, None)
        return clean(m), clean(s), whole, spread, meter.seconds()

    def _peaks(self, x: np.ndarray) -> tuple[list[float], list[float]]:
        overlap = TAPS_PER_PHASE - 1
        if self._tail is None or self._tail.shape[1] != x.shape[1]:
            self._tail = np.zeros((overlap, x.shape[1]))
        joined = np.concatenate([self._tail, x])
        self._tail = joined[-overlap:]
        true_peaks = []
        for ch in range(x.shape[1]):
            peak = 0.0
            for phase in self._phases:
                y = np.convolve(joined[:, ch], phase, mode="valid")
                if len(y):
                    peak = max(peak, float(np.abs(y).max()))
            true_peaks.append(peak)
        return true_peaks, [float(v) for v in np.abs(x).max(axis=0)]

    # -- reading ----------------------------------------------------------------
    def reading_at(self, now: float | None = None) -> Reading | None:
        """What the meters show at `now` (time.perf_counter()), i.e. for the sound being heard."""
        now = time.perf_counter() if now is None else now
        with self._lock:
            if not self._timeline:
                return None
            # The newest reading that's already been heard.
            chosen = None
            for entry in reversed(self._timeline):
                if entry[0] <= now:
                    chosen = entry
                    break
            if chosen is None:
                chosen = self._timeline[0]
            if now - chosen[0] > 0.5:  # nothing playing any more: the meters fall to rest
                return None
            held_tp = held_sp = None
            psr_peak = None
            for when, tp, sp in self._tp_history:
                if now - TRUE_PEAK_HOLD <= when <= now:
                    held_tp = tp if held_tp is None else [max(a, b) for a, b in zip(held_tp, tp)]
                    held_sp = sp if held_sp is None else [max(a, b) for a, b in zip(held_sp, sp)]
                if now - PSR_WINDOW <= when <= now:
                    psr_peak = max(tp) if psr_peak is None else max(psr_peak, max(tp))
            while self._tp_history and self._tp_history[0][0] < now - TRUE_PEAK_HOLD - 1:
                self._tp_history.popleft()
            momentary, short, whole, spread, measured = self._loudness_now(now)
            max_m, max_s = self.max_momentary, self.max_short_term
        _, vu, ppm, rms = chosen
        channels = len(vu)
        held_tp = held_tp or [0.0] * channels
        held_sp = held_sp or [0.0] * channels
        ref = self.vu_reference()
        psr = None
        if psr_peak is not None and psr_peak > FLOOR and short is not None:
            psr = db(psr_peak) - short
        return Reading(
            vu=tuple(db(v / ref) for v in vu),
            ppm=tuple(db(v) for v in ppm),
            rms=tuple(db(v) + 3.0103 for v in rms),  # AES17: a full-scale sine reads 0 dB
            true_peak=tuple(db(v) for v in held_tp),
            sample_peak=tuple(db(v) for v in held_sp),
            max_true_peak=db(self.max_true_peak),
            momentary=momentary, short_term=short, integrated=whole, loudness_range=spread,
            max_momentary=max_m, max_short_term=max_s, measured=measured, psr=psr,
        )


# -- Auto alignment ----------------------------------------------------------------
def vu_levels(path, cancel: threading.Event | None = None, wait: bool | None = None) -> np.ndarray | None:
    """A file's level in 300 ms slices, the way a VU sees it: the full-wave average of the
    louder channel, as the dBFS of a sine that would read the same. Silence left out.
    None if cancelled. (`wait` as loudness.file_powers.)"""
    levels = []
    with audiofile.open(path, cancel=cancel.is_set if cancel is not None else None, wait=wait) as f:
        window = max(1, int(f.samplerate * 0.3))
        for block in audiofile.blocks(f, window * 30, dtype="float32", always_2d=True):
            if cancel is not None and cancel.is_set():
                return None
            n = len(block) // window
            if not n:
                continue
            mean_abs = np.abs(block[: n * window]).reshape(n, window, -1).mean(axis=1).max(axis=1)
            with np.errstate(divide="ignore"):
                level = 20 * np.log10(mean_abs / (2 / math.pi))
            levels.append(level[level > AUTO_SILENCE])
    return np.concatenate(levels) if levels else np.zeros(0)


def alignment_from_levels(levels: np.ndarray) -> float | None:
    """0 VU for Auto: the level only the loudest 10% of moments exceed, to the whole dB."""
    if len(levels) == 0:
        return None
    value = float(np.percentile(levels, AUTO_PERCENTILE))
    return float(min(max(round(value), AUTO_RANGE[0]), AUTO_RANGE[1]))


def album_alignment(track: Path, on_first=lambda value: None,
                    cancel: threading.Event | None = None) -> float | None:
    """Measure `track`'s whole album for Auto. Calls `on_first` with a provisional figure from
    the track itself as soon as it's known (a second or so), then returns the album's."""
    _, files = album_files(track)
    gathered = []
    for k, path in enumerate(files):
        try:
            levels = vu_levels(path, cancel, wait=False)   # (a network album: only once its songs are here)
        except audiofile.NotCached:
            raise
        except (OSError, RuntimeError):
            continue  # a file that won't decode just doesn't count
        if levels is None:
            return None
        gathered.append(levels)
        if k == 0:
            first = alignment_from_levels(levels)
            if first is not None:
                on_first(first)
    return alignment_from_levels(np.concatenate(gathered)) if gathered else None


# -- Auto (loudness) ------------------------------------------------------------------
@functools.lru_cache(maxsize=32)
def tone_offset(rate: int, channels: int, dbfs: float = -20.0) -> float:
    """How many LU a steady 1 kHz sine reads above its own level in dBFS (the same sine in every
    channel), measured with the player's own loudness code. Measured 04/10/2026: +0.006 for
    stereo at 44.1 kHz (+0.007 at 48k, +0.011 at 96k), -3.005 for mono (one channel carries
    half the power) — i.e. a stereo tone at -18 dBFS reads -18.0 LUFS."""
    seconds = 5
    t = np.arange(rate * seconds) / rate
    x = 10 ** (dbfs / 20) * np.sin(2 * np.pi * TONE_HZ * t)
    meter = Loudness(rate, channels)
    meter.add(np.repeat(x[:, None], channels, axis=1))
    return integrated(meter.powers) - dbfs


def alignment_from_loudness(lufs: float | None, rate: int, channels: int) -> float | None:
    """0 VU for Auto (loudness): the level of a 1 kHz sine as loud as `lufs`, to the whole dB,
    kept within the same range as Auto (per album)."""
    if lufs is None or math.isnan(lufs) or math.isinf(lufs):
        return None
    value = lufs - tone_offset(int(rate), int(channels))
    return float(min(max(round(value), AUTO_RANGE[0]), AUTO_RANGE[1]))


def album_alignment_loudness(track: Path, on_first=lambda value, lufs: None,
                             cancel: threading.Event | None = None) -> tuple[float, float] | None:
    """Auto (loudness) for `track`'s album: (0 VU in dBFS, album integrated LUFS), the album
    measured as one programme (as Now Playing's Album loudness). Calls `on_first(value, lufs)`
    with the track's own figures first. None if cancelled or nothing could be measured."""
    _, files = album_files(track)
    info = audiofile.info(track)
    rate, channels = info.samplerate, info.channels
    gathered = []
    for k, path in enumerate(files):
        try:
            powers = file_powers(path, cancel, wait=False)
        except audiofile.NotCached:
            raise
        except (OSError, RuntimeError):
            continue
        if powers is None:
            return None
        gathered.append(powers)
        if k == 0:
            lufs = integrated(powers)
            value = alignment_from_loudness(lufs, rate, channels)
            if value is not None:
                on_first(value, lufs)
    if not gathered:
        return None
    lufs = integrated(np.concatenate(gathered))
    value = alignment_from_loudness(lufs, rate, channels)
    return None if value is None else (value, lufs)


__all__ = ["MeterEngine", "Reading", "PPM_TYPES", "VU_ALIGN_DBFS", "OVERSAMPLE", "album_alignment",
           "album_alignment_loudness", "alignment_from_loudness", "tone_offset"]

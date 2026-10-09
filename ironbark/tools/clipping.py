"""Inter-sample overs: where a recording's rebuilt wave passes full scale, and what an
ordinary converter does to it. No UI code (the Clipping window is clip_window.py).

A DAC (digital-to-analogue converter) doesn't play the samples as steps: it rebuilds the smooth
wave they stand for, and on loud masters that wave often rises *between* samples above the
highest value a sample can hold ("full scale"). An ordinary converter has no room up there and
chops it flat — heard, if at all, as a slight crackle or edge on the loudest moments.

- `oversample` rebuilds the wave at 8 points per sample (analysis.py's windowed-sinc filter:
  BS.1770-5 says 8x can under-read a peak by at most 0.169 dB).
- `OverFinder` follows a stream block by block and lists every over: a stretch where the wave
  passes full scale; passes closer together than OVER_GAP_S count as one over. The whole-song
  strip (`scan`) and the live view's "overs per second" both use it.
- `simulate_dac` / `listening_versions` make the "hear it" versions (Experimental
  "Hear it"): A = an ordinary DAC (rebuild at 8x, chop at full scale, back down),
  B = the same loop, untouched apart from being turned down just below its true peak, and
  the difference between them (what the chop takes away).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .. import audiofile
from ..analysis import FILE_OVERSAMPLE, TAPS_PER_PHASE, guard_trim, oversampling_phases
from ..audiofile import read_blocks
from .abx import level_of
from .audition import level_gains

FACTOR = FILE_OVERSAMPLE      # 8 rebuilt points per sample
OVER_GAP_S = 0.010            # passes over full scale closer together than this are one over
SCAN_BLOCK_S = 3              # the whole-song pass reads this much at a time
LOOP_S = 4.0                  # Hear it: the loop around the chosen over
DIFFERENCE_BOOST_DB = 20.0    # "just the difference" is played this much louder than the music…
DIFFERENCE_MAX = 0.5          # …but never with a peak above half of full scale
_HALF = TAPS_PER_PHASE // 2  # the filter reaches this many samples either side


def db(level: float) -> float:
    return 20 * math.log10(level) if level > 0 else float("-inf")


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# -- the rebuilt wave ----------------------------------------------------------------------------
def oversample(x: np.ndarray, factor: int = FACTOR, phases=None) -> np.ndarray:
    """The rebuilt wave of `x` (frames × channels, floats, 1.0 = full scale) at `factor` points
    per sample: row j·factor + p is the wave p/factor of a sample after sample j. Silence is
    assumed before and after (give the call a little context either side when that matters)."""
    x = np.asarray(x, dtype=np.float64)
    x = x[:, None] if x.ndim == 1 else x
    phases = phases or oversampling_phases(factor)
    frames, channels = x.shape
    padded = np.concatenate([np.zeros((_HALF - 1, channels)), x, np.zeros((_HALF, channels))])
    out = np.empty((frames, factor, channels))
    for p, phase in enumerate(phases):
        for ch in range(channels):
            out[:, p, ch] = np.convolve(padded[:, ch], phase, mode="valid")
    return out.reshape(frames * factor, channels)


def decimate(y: np.ndarray, factor: int = FACTOR) -> np.ndarray:
    """Back down from `factor` points per sample to one: the same filter as a low-pass (so
    nothing above the original half sample rate folds back), then every factor-th point. The
    inverse of `oversample` for sound that was never changed in between."""
    y = np.asarray(y, dtype=np.float64)
    y = y[:, None] if y.ndim == 1 else y
    phases = oversampling_phases(factor)
    h = np.empty(len(phases) * len(phases[0]))
    for p, phase in enumerate(phases):
        h[p::factor] = phase
    h = h / factor
    taps = len(h)
    frames = len(y) // factor
    half = taps // 2
    # (half − 1 zeros first: then window k is centred on point k exactly.)
    padded = np.concatenate([np.zeros((half - 1, y.shape[1])), y, np.zeros((taps, y.shape[1]))])
    out = np.empty((frames, y.shape[1]))
    kernel = h[::-1]
    for ch in range(y.shape[1]):
        windows = sliding_window_view(padded[:, ch], taps)[: frames * factor: factor]
        out[:, ch] = windows @ kernel
    return out


# -- finding overs -------------------------------------------------------------------------------
@dataclass
class Over:
    """One over: the rebuilt wave above full scale from `start` to `end` (seconds into the
    track), highest at `at`, `peak` (linear: 1.26 = +2.0 dB over)."""
    start: float
    end: float
    peak: float
    at: float

    @property
    def db(self) -> float:
        return db(self.peak)


class OverFinder:
    """Every over in a stream fed in blocks (frames × channels floats), the filter carried
    across blocks. `overs` holds the finished ones; `finish()` closes the last."""

    def __init__(self, channels: int, rate: int, factor: int = FACTOR, gap_s: float = OVER_GAP_S,
                 start_frame: int = 0):
        self.channels, self.rate, self.factor = channels, rate, factor
        self.phases = oversampling_phases(factor)
        self.overlap = TAPS_PER_PHASE - 1
        self.previous = np.zeros((self.overlap, channels))
        self.gap = gap_s * rate * factor          # in rebuilt points
        self.fed = start_frame                    # frames fed so far (the next block's first frame)
        self.overs: list[Over] = []
        self._open: list[float] | None = None     # [start, end, peak, at] in rebuilt points
        self.true_peak = 0.0
        self.highest_sample = 0.0
        self.peak_at = 0.0                        # seconds: where the true peak is
        self.last_peak = self.last_peak_at = self.last_sample = 0.0   # the latest block's

    def _points(self, k) -> np.ndarray | float:
        """Rebuilt-point index → seconds."""
        return k / (self.rate * self.factor)

    def add(self, block: np.ndarray) -> None:
        block = np.asarray(block, dtype=np.float64)
        if not len(block):
            return
        self.highest_sample = max(self.highest_sample, float(np.abs(block).max()))
        x = np.concatenate([self.previous, block])
        n = len(block)
        level = np.zeros((n, self.factor))
        for p, phase in enumerate(self.phases):
            for ch in range(self.channels):
                y = np.abs(np.convolve(x[:, ch], phase, mode="valid"))
                np.maximum(level[:, p], y[:n], out=level[:, p])
        self.previous = x[-self.overlap:]
        # Output j, phase p is the wave at frame (fed + j − HALF) + p/factor (see oversample).
        flat = level.reshape(-1)
        first = (self.fed - _HALF) * self.factor           # rebuilt-point index of flat[0]
        self.fed += n
        top = int(np.argmax(flat))
        # The highest point of this block (the live view shows the loudest moment it's just heard).
        self.last_peak, self.last_peak_at = float(flat[top]), float(self._points(first + top))
        self.last_sample = float(np.abs(block).max())
        if flat[top] > self.true_peak:
            self.true_peak = float(flat[top])
            self.peak_at = max(0.0, self.last_peak_at)
        self._collect(flat, first)

    def _collect(self, flat: np.ndarray, first: int) -> None:
        over = np.flatnonzero(flat > 1.0)
        if not len(over):
            return
        # Split into groups wherever two passes are further apart than the gap.
        breaks = np.flatnonzero(np.diff(over) > self.gap) + 1
        starts = np.concatenate([[0], breaks])
        ends = np.concatenate([breaks, [len(over)]])
        peaks = np.maximum.reduceat(flat[over], starts)
        for s, e, peak in zip(starts, ends, peaks):
            group = over[s:e]
            at = group[int(np.argmax(flat[group]))]
            item = [first + group[0], first + group[-1], float(peak), first + at]
            if self._open is not None and item[0] - self._open[1] <= self.gap:
                self._open[1] = item[1]
                if item[2] > self._open[2]:
                    self._open[2], self._open[3] = item[2], item[3]
            else:
                self._close()
                self._open = item

    def _close(self) -> None:
        if self._open is not None:
            s, e, peak, at = self._open
            self.overs.append(Over(max(0.0, self._points(s)), max(0.0, self._points(e)), peak,
                                   max(0.0, self._points(at))))
            self._open = None

    def settle(self) -> None:
        """Close the over in progress once the stream has moved past it by more than the gap
        (the live view calls this each refresh, so a finished over is counted promptly)."""
        if self._open is not None and (self.fed - _HALF) * self.factor - self._open[1] > self.gap:
            self._close()

    def finish(self) -> None:
        """The end of the stream: the filter's tail (silence after the last sample), then close."""
        self.add(np.zeros((TAPS_PER_PHASE, self.channels)))
        self._close()


@dataclass
class OverScan:
    """The whole song (Clipping window → the strip)."""
    rate: int
    duration: float                   # seconds
    overs: list[Over] = field(default_factory=list)
    true_peak: float = 0.0            # linear
    highest_sample: float = 0.0       # linear
    peak_at: float = 0.0              # seconds
    damaged_at: float | None = None   # the file stopped decoding here

    @property
    def worst(self) -> Over | None:
        return max(self.overs, key=lambda o: o.peak) if self.overs else None


def scan(path, cancel: threading.Event | None = None,
         progress: Callable[[float], None] | None = None) -> OverScan | None:
    """Every over in a file (a CUE track: just its stretch), at 8x. None if cancelled."""
    with audiofile.open(path) as f:
        rate, channels = f.samplerate, f.channels
        finder = OverFinder(channels, rate)
        total = max(1, f.frames)
        done = 0
        damaged: list = []   # a file that stops decoding part-way (or is cut short): scanned up to there
        for block in read_blocks(f, damaged, blocksize=rate * SCAN_BLOCK_S, dtype="float64", always_2d=True):
            if cancel is not None and cancel.is_set():
                return None
            finder.add(block)
            done += len(block)
            if progress:
                progress(done / total)
        finder.finish()
    return OverScan(rate, total / rate if rate else 0.0, finder.overs, finder.true_peak,
                    finder.highest_sample, finder.peak_at, done / rate if damaged else None)


def summary(result: OverScan) -> str:
    """"37 overs, worst +1.9 dB at 2:14" — or that there are none."""
    worst = result.worst
    if worst is None:
        tp = db(result.true_peak)
        level = f"{tp:+.1f} dBTP" if math.isfinite(tp) else "silent"
        return f"No overs: the rebuilt wave stays within full scale (true peak {level})"
    count = len(result.overs)
    return f"{count:,} over{'s' if count != 1 else ''}, worst {worst.db:+.1f} dB at {fmt_clock(worst.at)}"


def overs_between(overs: list[Over], start: float, end: float) -> list[Over]:
    return [o for o in overs if start <= o.at < end]


# -- hearing it ----------------------------------------------------------------------------------
def dac_output(up: np.ndarray) -> np.ndarray:
    """What an ordinary converter (no headroom) makes of a rebuilt wave: everything past full
    scale chopped flat."""
    return np.clip(up, -1.0, 1.0)


def simulate_dac(x: np.ndarray, factor: int = FACTOR) -> tuple[np.ndarray, np.ndarray]:
    """(chopped, clean) for `x`: an ordinary converter with no headroom — the wave rebuilt at
    `factor`x and chopped flat at full scale (`dac_output`) — and the same path without the
    chop, both brought back down to the original rate so the player can send them. Their
    difference is only what the chop did. (Brought back down, the chopped version's own
    rebuilt wave rings a little past the chop again; it's played turned down to the clean
    one's loudness, so the listener's converter never chops it a second time.)"""
    up = oversample(x, factor)
    chopped = decimate(dac_output(up), factor)
    clean = decimate(up, factor)
    return chopped, clean


@dataclass
class Versions:
    """The loop around one over, ready for audition.Switcher: slots A (ordinary DAC), B (clean)
    and D (just the difference), with their gains."""
    rate: int
    start: float                 # seconds into the track where the loop begins
    a: np.ndarray
    b: np.ndarray
    difference: np.ndarray
    gain_a: float
    gain_b: float
    gain_difference: float
    level_a: float | None        # the loudness each is heard at, after its gain
    level_b: float | None
    level_unit: str
    true_peak: float             # the loop's own true peak (linear)
    level_before_db: float       # A was this much louder than B before matching

    @property
    def matched_db(self) -> float:
        """How far apart A and B are as heard (dB; 0 when matched)."""
        if self.level_a is None or self.level_b is None:
            return 0.0
        return self.level_a - self.level_b


def _loudness(x: np.ndarray, rate: int) -> tuple[float | None, str]:
    return level_of(x, rate)


def listening_versions(path, at: float, loop_s: float = LOOP_S) -> Versions:
    """Read `loop_s` seconds around `at` (seconds into the track) and make the three versions.
    B is turned down just below its true peak (as the clipping guard would); A is matched to
    B's loudness (the louder one is turned down, never the quieter up); the difference is
    boosted DIFFERENCE_BOOST_DB but kept well below full scale."""

    with audiofile.open(path) as f:
        rate, total = f.samplerate, f.frames
        length = min(total, int(round(loop_s * rate)))
        begin = int(round(at * rate)) - length // 2
        begin = max(0, min(begin, total - length))
        margin = min(begin, TAPS_PER_PHASE)            # a little sound before, for the filter
        f.seek(begin - margin)
        x = f.read(length + margin + TAPS_PER_PHASE, dtype="float64", always_2d=True)
    chopped, clean = simulate_dac(x)
    keep = slice(margin, margin + length)
    original, a_full, b_full = x[keep], chopped[keep], clean[keep]
    up_peak = float(np.abs(oversample(x)).max()) if len(x) else 0.0
    gain_b = guard_trim(up_peak) if up_peak > 1.0 else 1.0
    b = original
    # The difference against the unchopped path (not the original samples), so the filter's
    # own tiny effect cancels and only the chop is left.
    difference = a_full - b_full
    level_a, unit = _loudness(a_full, rate)
    level_b, _ = _loudness(b * gain_b, rate)
    before = (level_a - level_b) if level_a is not None and level_b is not None else 0.0
    gain_a, down_b = level_gains(-before)   # A is `before` dB louder: A is turned down
    gain_b *= down_b
    loud_a, _ = _loudness(a_full * gain_a, rate)
    loud_b, _ = _loudness(b * gain_b, rate)
    peak_d = float(np.abs(difference).max()) if len(difference) else 0.0
    gain_d = gain_b * 10 ** (DIFFERENCE_BOOST_DB / 20)
    if peak_d * gain_d > DIFFERENCE_MAX:
        gain_d = DIFFERENCE_MAX / peak_d
    return Versions(rate, begin / rate, a_full.astype(np.float32), b.astype(np.float32),
                    difference.astype(np.float32), gain_a, gain_b, gain_d, loud_a, loud_b, unit,
                    up_peak, before)

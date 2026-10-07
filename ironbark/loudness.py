"""Loudness to ITU-R BS.1770-5 and EBU R128 ("EBU Mode": EBU Tech 3341, Tech 3342). No UI code.

- K-weighting: the standard's two filters, a high shelf (the head's effect) then a high-pass,
  with the coefficients BS.1770 publishes for 48 kHz. For other sample rates the filters are
  redesigned: their corner frequency, Q and gains are worked out from the published
  coefficients, then the same filters are built for the new rate.
- Loudness = -0.691 + 10 log10(sum of G_i x the mean square of each K-weighted channel), in
  LUFS. G = 1.0 for left, right and centre, 1.41 for the surrounds; the LFE is left out. In
  other layouts (quad, 6.1, 7.1) G follows where each speaker sits (BS.1770-5 Annex 3, Tables
  4 and 5): 1.41 from 60 to 120 degrees either side of straight ahead, 1.0 elsewhere.
- Momentary: a 400 ms sliding window. Short-term: 3 s. Neither is gated.
- Integrated: 400 ms blocks overlapping by 75%; an absolute gate at -70 LUFS, then a relative
  gate 10 LU below the level of the blocks that passed it.
- Loudness range (LRA, EBU Tech 3342): short-term levels 10 times a second, gated at -70 LUFS
  and then 20 LU below their own gated level; LRA = the 95th minus the 10th percentile.

Everything works from the K-weighted power in 10 ms steps: windows and gating blocks are
whole numbers of steps, so the momentary and short-term readings can be taken 100 times a
second (EBU Mode asks for at least 10).
"""

from __future__ import annotations

import math
import threading
from functools import lru_cache
from typing import Callable, Iterable

import numpy as np

from . import audiofile

PUBLISHED_RATE = 48000
# ITU-R BS.1770-5, Annex 1, Tables 1 and 2: ((b0, b1, b2), (a0, a1, a2)) at 48 kHz.
SHELF_48K = ((1.53512485958697, -2.69169618940638, 1.19839281085285), (1.0, -1.69065929318241, 0.73248077421585))
HIGHPASS_48K = ((1.0, -2.0, 1.0), (1.0, -1.99004745483398, 0.99007225036621))

OFFSET = -0.691            # cancels the K-weighting's gain at 997 Hz
ABSOLUTE_GATE = -70.0      # LUFS
RELATIVE_GATE = -10.0      # LU below the absolute-gated level (integrated)
LRA_RELATIVE_GATE = -20.0  # LU (loudness range)
LRA_PERCENTILES = (10, 95)
SURROUND = 1.41

STEP = 0.010               # seconds of audio per power step
MOMENTARY = 40             # steps: 400 ms
SHORT_TERM = 300           # steps: 3 s
BLOCK_HOP = 10             # gating blocks start every 100 ms (75% overlap)
LRA_HOP = 10               # short-term levels for LRA, 10 a second
LRA_SETTLING = 60.0        # seconds before EBU Mode calls the LRA stable


def channel_weights(channels: int) -> list[float]:
    """G_i in FLAC's channel order for each channel count (RFC 9639, 9.1.3; a WAV's channel
    mask lists these speakers in the same order):
    4 (quad)  L R Ls Rs — the back pair played on the surround speakers, as in 5.0
    5 (5.0)   L R C Ls Rs
    6 (5.1)   L R C LFE Ls Rs
    7 (6.1)   L R C LFE back-centre side-L side-R — back centre (180 degrees) 1.0, sides (90) 1.41
    8 (7.1)   L R C LFE back-L back-R side-L side-R — back pair (135 degrees) 1.0, sides (90) 1.41
    Any other count is taken as front channels (1.0 each)."""
    layouts = {
        4: [1.0, 1.0, SURROUND, SURROUND],
        5: [1.0, 1.0, 1.0, SURROUND, SURROUND],
        6: [1.0, 1.0, 1.0, 0.0, SURROUND, SURROUND],
        7: [1.0, 1.0, 1.0, 0.0, 1.0, SURROUND, SURROUND],
        8: [1.0, 1.0, 1.0, 0.0, 1.0, 1.0, SURROUND, SURROUND],
    }
    return layouts.get(channels, [1.0] * channels)


# -- K-weighting ---------------------------------------------------------------------
def _shape(a1: float, a2: float) -> tuple[float, float]:
    """(K, K/Q) of a 2nd-order section designed with the bilinear transform (K = tan(pi f0 / fs)),
    read back from its denominator."""
    d = 4 / (1 - a1 + a2)
    return math.sqrt((1 + a1 + a2) / (1 - a1 + a2)), (1 - a2) * d / 2


def _denominator(k: float, q: float) -> tuple[float, float, float]:
    d = 1 + k / q + k * k
    return d, 2 * (k * k - 1) / d, (1 - k / q + k * k) / d


@lru_cache(maxsize=16)
def k_weighting(rate: int) -> tuple[tuple[tuple[float, ...], tuple[float, ...]], ...]:
    """The two K-weighting sections for `rate`: exactly the published ones at 48 kHz."""
    (b0, b1, b2), (_, a1, a2) = SHELF_48K
    k, kq = _shape(a1, a2)
    q, f0 = k / kq, math.atan(k) * PUBLISHED_RATE / math.pi
    d = 4 / (1 - a1 + a2)
    high = (b0 - b1 + b2) * d / 4         # gain far above the corner
    mid = (b0 - b2) * d / (2 * kq)        # gain at the corner
    k = math.tan(math.pi * f0 / rate)
    d, a1n, a2n = _denominator(k, q)
    shelf = ((high + mid * k / q + k * k) / d, 2 * (k * k - high) / d, (high - mid * k / q + k * k) / d), \
        (1.0, a1n, a2n)

    (b0, b1, b2), (_, a1, a2) = HIGHPASS_48K
    k, kq = _shape(a1, a2)
    q, f0 = k / kq, math.atan(k) * PUBLISHED_RATE / math.pi
    published_gain = b0 * (4 / (1 - a1 + a2))  # the published section passes high frequencies at b0 * D
    d, a1n, a2n = _denominator(math.tan(math.pi * f0 / rate), q)
    g = published_gain / d  # the same pass-band gain at every rate, so -0.691 still cancels at 997 Hz
    highpass = (g * b0, g * b1, g * b2), (1.0, a1n, a2n)
    return shelf, highpass


def _biquad(x: np.ndarray, b, a) -> np.ndarray:
    y = np.zeros_like(x)
    x1 = x2 = y1 = y2 = 0.0
    for n, v in enumerate(x):
        out = b[0] * v + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        x2, x1, y2, y1 = x1, v, y1, out
        y[n] = out
    return y


@lru_cache(maxsize=8)
def impulse_response(rate: int) -> np.ndarray:
    """The K-weighting's impulse response, long enough that what's cut off is below -200 dB.
    Filtering by convolution with it (FFT) is fast in numpy and matches the filters exactly."""
    sections = k_weighting(rate)
    radius = max(abs(r) for _, a in sections for r in np.roots(a))
    length = int(math.ceil(30 / -math.log(radius)))  # 30 time constants of the slowest pole
    x = np.zeros(length)
    x[0] = 1.0
    for b, a in sections:
        x = _biquad(x, b, a)
    return x


class KWeighting:
    """Streaming K-weighting for one continuous signal (frames x channels)."""

    def __init__(self, rate: int, channels: int):
        self.h = impulse_response(rate)
        self.history = np.zeros((len(self.h) - 1, channels))
        self._spectra: dict[int, np.ndarray] = {}

    def __call__(self, x: np.ndarray) -> np.ndarray:
        n, taps = len(x), len(self.h)
        joined = np.concatenate([self.history, x])
        size = 1 << int(math.ceil(math.log2(len(joined))))
        spectrum = self._spectra.get(size)
        if spectrum is None:
            spectrum = self._spectra[size] = np.fft.rfft(self.h, size)
        y = np.fft.irfft(np.fft.rfft(joined, size, axis=0) * spectrum[:, None], size, axis=0)
        self.history = joined[len(joined) - (taps - 1):]
        return y[taps - 1: taps - 1 + n]


# -- the measures ---------------------------------------------------------------------
def to_lufs(power):
    """Weighted mean-square power -> LUFS (-inf for silence). Works on arrays too."""
    with np.errstate(divide="ignore"):
        return OFFSET + 10 * np.log10(power)


def windows(powers: np.ndarray, steps: int, hop: int = 1) -> np.ndarray:
    """Mean power of every whole window of `steps` steps, starting every `hop` steps."""
    if len(powers) < steps:
        return np.zeros(0)
    total = np.concatenate([[0.0], np.cumsum(powers)])
    return ((total[steps:] - total[:-steps]) / steps)[::hop]


def integrated(powers: np.ndarray) -> float | None:
    """Gated integrated loudness (LUFS) of a stretch of power steps. None until a block passes
    the gates. Blocks that don't fit whole at the end are left out, as Tech 3341 says."""
    blocks = windows(powers, MOMENTARY, BLOCK_HOP)
    return _gated(blocks, to_lufs(blocks))


def _gated(blocks: np.ndarray, levels: np.ndarray) -> float | None:
    """Integrated loudness from the gating blocks' mean powers and their levels (LUFS)."""
    if not len(blocks):
        return None
    loud = levels > ABSOLUTE_GATE
    if not loud.any():
        return None
    relative = float(to_lufs(blocks[loud].mean())) + RELATIVE_GATE
    kept = loud & (levels > relative)
    return float(to_lufs(blocks[kept].mean()))


def loudness_range(powers: np.ndarray) -> float | None:
    """LRA (LU), as EBU Tech 3342's reference implementation computes it."""
    return _spread(to_lufs(windows(powers, SHORT_TERM, LRA_HOP)))


def _spread(levels: np.ndarray) -> float | None:
    """LRA from the short-term levels (LUFS, 10 a second)."""
    levels = levels[levels >= ABSOLUTE_GATE]
    if not len(levels):
        return None
    gated = 10 * math.log10(float(np.mean(10 ** (levels / 10))))
    levels = levels[levels >= gated + LRA_RELATIVE_GATE]
    n = len(levels)
    ranks = [math.floor((n - 1) * p / 100 + 0.5) for p in LRA_PERCENTILES]  # MATLAB's round()
    low, high = np.partition(levels, ranks)[ranks]   # the values a full sort puts at those places
    return float(high - low)


class Loudness:
    """K-weighted power, in 10 ms steps, of one continuous stream. Every measure is read from it.
    The integrated measurement (and LRA) runs from `start`, which `reset` moves to now."""

    def __init__(self, rate: int, channels: int):
        self.rate, self.channels = rate, channels
        self.weights = np.array(channel_weights(channels))
        self.step = max(1, round(rate * STEP))
        self._filter = KWeighting(rate, channels)
        self._carry = np.zeros((0, channels))
        self._powers = np.zeros(4096)
        self.count = 0
        self.start = 0

    @property
    def carried(self) -> int:
        """Samples taken in but not yet making up a whole step."""
        return len(self._carry)

    @property
    def powers(self) -> np.ndarray:
        return self._powers[: self.count]

    def add(self, x: np.ndarray) -> int:
        """Take in more of the stream (frames x channels, full scale = 1.0). Returns the number
        of new steps."""
        y = self._filter(np.asarray(x, dtype=np.float64))
        if len(self._carry):
            y = np.concatenate([self._carry, y])
        n = len(y) // self.step
        self._carry = y[n * self.step:].copy()
        if n:
            p = (y[: n * self.step] ** 2).reshape(n, self.step, self.channels).mean(axis=1) @ self.weights
            if self.count + n > len(self._powers):
                grown = np.zeros(max(2 * len(self._powers), self.count + n))
                grown[: self.count] = self._powers[: self.count]
                self._powers = grown
            self._powers[self.count: self.count + n] = p
            self.count += n
        return n

    def reset(self):
        """Start a new integrated / LRA measurement here. Only the last 3 s are kept, for the
        momentary and short-term windows that reach back past it."""
        keep = self.powers[max(0, self.count - SHORT_TERM):].copy()
        self._powers = np.zeros(max(4096, 2 * len(keep)))
        self._powers[: len(keep)] = keep
        self.count = self.start = len(keep)

    def trailing(self, steps: int, new: int) -> np.ndarray:
        """The window of `steps` steps ending at each of the last `new` steps (LUFS); NaN where
        the stream isn't that long yet."""
        p = self.powers
        out = np.full(new, np.nan)
        span = p[max(0, self.count - new - steps + 1):]
        means = windows(span, steps)
        if len(means):
            out[new - len(means):] = to_lufs(means)
        return out

    def measured(self) -> np.ndarray:
        return self.powers[self.start:]

    def seconds(self) -> float:
        return (self.count - self.start) * self.step / self.rate


class _Growing:
    """A float array that keeps being added to."""

    def __init__(self):
        self._data = np.zeros(1024)
        self.count = 0

    @property
    def values(self) -> np.ndarray:
        return self._data[: self.count]

    def extend(self, values: np.ndarray) -> None:
        if self.count + len(values) > len(self._data):
            grown = np.zeros(max(2 * len(self._data), self.count + len(values)))
            grown[: self.count] = self.values
            self._data = grown
        self._data[self.count: self.count + len(values)] = values
        self.count += len(values)


class Programme:
    """The integrated loudness and loudness range of a measurement that keeps growing (the live
    meters' "this track so far"), kept up to date as its power steps arrive. Each gating block
    and short-term level is worked out once, when its steps are in, so a reading only gates
    the blocks so far instead of going over every step again. Gives exactly what integrated()
    and loudness_range() give for the same steps."""

    def __init__(self):
        self._sums = _Growing()      # running total, as windows() makes it: sums[k] = steps 0..k-1 added
        self._sums.extend(np.zeros(1))
        self._blocks = _Growing()    # gating blocks' mean powers
        self._levels = _Growing()    # …and their LUFS
        self._short = _Growing()     # short-term levels (LUFS) for LRA

    @property
    def steps(self) -> int:
        return self._sums.count - 1

    def add(self, powers: np.ndarray) -> None:
        if not len(powers):
            return
        # Added on from the last total in the same order as one cumsum over them all, so every
        # window below comes out exactly as windows() makes it.
        self._sums.extend(np.cumsum(np.concatenate([self._sums.values[-1:], powers]))[1:])
        blocks = self._new_windows(MOMENTARY, BLOCK_HOP, self._blocks.count)
        self._blocks.extend(blocks)
        self._levels.extend(to_lufs(blocks))
        self._short.extend(to_lufs(self._new_windows(SHORT_TERM, LRA_HOP, self._short.count)))

    def _new_windows(self, steps: int, hop: int, have: int) -> np.ndarray:
        """Mean powers of the windows of `steps` steps (one every `hop`) whole now, after the
        first `have`."""
        whole = (self.steps - steps) // hop + 1 if self.steps >= steps else 0
        starts = np.arange(have, whole) * hop
        total = self._sums.values
        return (total[starts + steps] - total[starts]) / steps

    def integrated(self) -> float | None:
        return _gated(self._blocks.values, self._levels.values)

    def loudness_range(self) -> float | None:
        return _spread(self._short.values)


# -- whole files ----------------------------------------------------------------------
def file_powers(path, cancel: threading.Event | None = None,
                progress: Callable[[float], None] | None = None) -> np.ndarray | None:
    """A file's power steps from start to end (None if cancelled)."""
    with audiofile.open(path) as f:
        meter = Loudness(f.samplerate, f.channels)
        total = max(1, f.frames)
        done = 0
        for block in audiofile.blocks(f, f.samplerate * 5, dtype="float32", always_2d=True):
            if cancel is not None and cancel.is_set():
                return None
            meter.add(block)
            done += len(block)
            if progress:
                progress(done / total)
    return meter.powers.copy()


def album_loudness(paths: Iterable, cancel: threading.Event | None = None,
                   progress: Callable[[float], None] | None = None) -> tuple[float | None, float | None] | None:
    """(integrated LUFS, LRA) of an album played straight through: every track's power steps
    measured together, as one programme. None if cancelled."""
    paths = list(paths)
    gathered = []
    for k, path in enumerate(paths):
        try:
            powers = file_powers(path, cancel, (lambda p, k=k: progress((k + p) / len(paths))) if progress else None)
        except (OSError, RuntimeError):
            continue  # a file that won't decode just doesn't count
        if powers is None:
            return None
        gathered.append(powers)
    if not gathered:
        return None, None
    joined = np.concatenate(gathered)
    return integrated(joined), loudness_range(joined)

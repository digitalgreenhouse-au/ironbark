"""Spectrum maths for the Spectrogram window, and the bitscope's maths. No UI code.

A spectrogram is a picture of the sound: time across, frequency up (on a log scale, as we
hear it), brightness = how strong each frequency is. Each column is one FFT (fast Fourier
transform: the sound split into its frequencies) of a short slice, Kaiser-windowed (very low
side lobes, so a sharp edge — like a lossy encoder's cut-off — stays sharp in the picture).

Levels are in dBFS for a sine: a full-scale sine wave reads 0 dB in its bin.

The bitscope (after Stillwell Audio's free "Bitter" plug-in, whose bit display shows one line
per bit: the shorter the line, the less that bit is used; no line = never used) shows, per
channel, how often each bit of the samples is 1. We split each sample into its sign and its
size (magnitude), so the bars read like a level meter too: loud music lights the top bits,
quiet music only the lower ones, and a 16-bit recording stored as 24-bit leaves the bottom 8
dark. (How Bitter itself splits the bits isn't published; the sign/size split is our choice.)
"""

from __future__ import annotations

import collections
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from . import audiofile, theme
from .analysis import find_cutoff, wall_verdict

LIVE_FFT = 4096
LIVE_COLUMNS_PER_SECOND = 25   # one live slice every 40 ms
TRACK_FFT = 8192
DB_TOP = 0.0
DB_BOTTOM = -130.0
EMPTY = DB_BOTTOM - 10          # "no data": below the bottom of the colour scale (black)
LOW_HZ = 20.0
KAISER_BETA = 20.0


def palette() -> np.ndarray:
    """256 RGB colours, quietest first: theme.SPECTROGRAM_STOPS (dark → blue → magenta → red →
    orange → pale yellow, like most audio spectrograms) blended in between."""
    x = np.linspace(0, 1, 256)
    stops = np.array([a for a, _ in theme.SPECTROGRAM_STOPS])
    rgb = np.array([theme.rgb(c) for _, c in theme.SPECTROGRAM_STOPS], dtype=float)
    return np.stack([np.interp(x, stops, rgb[:, k]) for k in range(3)], axis=1).astype(np.uint8)


PALETTE = palette()


def window(size: int) -> np.ndarray:
    return np.kaiser(size, KAISER_BETA)


def to_float(source: np.ndarray) -> np.ndarray:
    """A decoded block as float64, full scale = 1.0 (int32 left-justified ÷ 2³¹, as meters.py)."""
    if source.dtype == np.int32:
        return source.astype(np.float64) / 2**31
    return source.astype(np.float64)


def power_column(x: np.ndarray, win: np.ndarray) -> np.ndarray:
    """Power per FFT bin of one slice (frames × channels, full scale = 1.0), the channels'
    power averaged, scaled so a full-scale sine reads 1.0 in its bin. Short slices are zero-padded."""
    size = len(win)
    if x.ndim == 1:
        x = x[:, None]
    if len(x) < size:
        x = np.concatenate([x, np.zeros((size - len(x), x.shape[1]))])
    spectrum = np.fft.rfft(x[:size] * win[:, None], axis=0)
    return np.mean(np.abs(spectrum) ** 2, axis=1) * (2 / win.sum()) ** 2


def column(x: np.ndarray, win: np.ndarray) -> np.ndarray:
    """dB per FFT bin of one slice (see power_column)."""
    return 10 * np.log10(power_column(x, win) + 1e-30)


def bin_hz(rate: int, size: int) -> float:
    return rate / size


def log_rows(height: int, rate: int, size: int, low: float = LOW_HZ) -> np.ndarray:
    """For each of `height` picture rows (bottom = low), the first FFT bin it covers, for
    np.maximum.reduceat: a row spanning several bins shows the strongest."""
    edges = np.geomspace(low, rate / 2, height + 1)
    starts = np.rint(edges[:-1] / bin_hz(rate, size)).astype(int)
    return np.clip(starts, 0, size // 2)


def to_rows(db: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """One column's dB (per bin) → per picture row, bottom row first."""
    return np.maximum.reduceat(db, starts)


def colours(levels: np.ndarray) -> np.ndarray:
    """dB values (any shape) → RGB (shape + (3,))."""
    k = np.clip((levels - DB_BOTTOM) / (DB_TOP - DB_BOTTOM), 0, 1)
    return PALETTE[(k * 255).astype(np.uint8)]


def row_of(freq: float, height: int, rate: int, low: float = LOW_HZ) -> float:
    """Picture row (0 = top) where `freq` sits on the log scale."""
    frac = np.log(freq / low) / np.log((rate / 2) / low)
    return (1 - frac) * height


class LiveSpectrogram:
    """The live view's data: blocks of heard samples go in (in order), one column of dB per
    FFT bin comes out every 1/LIVE_COLUMNS_PER_SECOND s of sound, each the FFT of the latest
    LIVE_FFT samples. A new track, sample rate, or a jump (seek) starts the slice afresh."""

    def __init__(self, keep: int = 2000):
        self.columns: collections.deque = collections.deque(maxlen=keep)  # (dB per bin, rate)
        self._win = window(LIVE_FFT)
        self._buffer = np.zeros((0, 1))
        self._key = None        # (path, rate, channels) of the samples in the buffer
        self._next_frame = None  # the frame expected next (to notice a seek)
        self._since = 0          # samples added since the last column

    def add(self, x: np.ndarray, rate: int, path=None, frame: int | None = None) -> int:
        """Add a heard block (frames × channels, float). Returns the number of new columns."""
        key = (path, rate, x.shape[1])
        if key != self._key or (frame is not None and self._next_frame is not None and frame != self._next_frame):
            self._key, self._buffer, self._since = key, np.zeros((0, x.shape[1])), 0
        self._next_frame = frame + len(x) if frame is not None else None
        hop = max(1, rate // LIVE_COLUMNS_PER_SECOND)
        made = 0
        start = 0  # walk the block hop by hop: a column each time a hop's worth has arrived
        while start < len(x):
            take = min(hop - self._since, len(x) - start)
            self._buffer = np.concatenate([self._buffer, x[start:start + take]])[-LIVE_FFT:]
            self._since += take
            start += take
            if self._since >= hop:
                self.columns.append((column(self._buffer, self._win), rate))
                self._since = 0
                made += 1
        return made

    def clear(self):
        self.columns.clear()
        self._key = self._next_frame = None
        self._since = 0


@dataclass
class TrackPicture:
    levels: np.ndarray       # dB, shape (height, columns), row 0 = top (highest frequency)
    rate: int
    duration: float          # seconds
    cutoff_hz: float | None  # where the sound stops (see analysis.find_cutoff)
    cutoff_sharp: bool
    damaged_at: float | None = None  # seconds: the file stopped decoding here (the rest is left dark)

    @property
    def verdict(self) -> str:
        """"lossy", "upsampled" or "" — the same rule as Now Playing → Check."""
        return wall_verdict(self.cutoff_hz, self.cutoff_sharp, self.rate)


def track_picture(path: str | Path, columns: int, height: int,
                  progress: Callable[[float], None] | None = None,
                  cancel: threading.Event | None = None, per_column: int = 2) -> TrackPicture | None:
    """The whole track's spectrogram: `columns` slices across, each the average power of
    `per_column` FFTs spread through that stretch of time. The cut-off is estimated from the
    same slices (Now Playing → Check reads every sample; its figure is preferred when known).
    None if cancelled."""
    win = window(TRACK_FFT)
    damaged_at = None
    with audiofile.open(path) as f:
        rate, frames = f.samplerate, f.frames
        starts = log_rows(height, rate, TRACK_FFT)
        levels = np.full((height, columns), EMPTY)
        total_power = np.zeros(TRACK_FFT // 2 + 1)
        span = max(1, frames - TRACK_FFT)
        for c in range(columns):
            if cancel is not None and cancel.is_set():
                return None
            power = np.zeros(TRACK_FFT // 2 + 1)
            try:
                for k in range(per_column):
                    where = int(span * (c + (k + 0.5) / per_column) / columns)
                    f.seek(where)
                    power += power_column(f.read(TRACK_FFT, dtype="float64", always_2d=True), win)
            except RuntimeError:  # libsndfile: the rest of a damaged file can't be decoded
                damaged_at = c / columns * frames / rate
                break
            total_power += power
            levels[:, c] = to_rows(10 * np.log10(power / per_column + 1e-30), starts)[::-1]
            if progress:
                progress((c + 1) / columns)
    cutoff, sharp = find_cutoff(total_power, rate) if total_power.any() else (None, False)
    return TrackPicture(levels, rate, frames / rate if rate else 0.0, cutoff, sharp, damaged_at)


# -- bitscope ------------------------------------------------------------------------------

def bit_counts(values: np.ndarray, bits: int) -> tuple[np.ndarray, np.ndarray]:
    """Per channel: how many samples are negative, and for each bit of the size (magnitude,
    `bits - 1` bits, most significant first) how many samples have it set. `values` are the
    file's own integers (frames × channels), as pcm_window.to_file_value gives them.
    The most negative value (-2^(bits-1)) is one step bigger than the size bits can hold; it
    counts as just the top size bit (as it is stored: the sign, then all zeros), so it can't
    make a padded file look as if its low bits were used."""
    v = np.asarray(values, dtype=np.int64)
    if v.ndim == 1:
        v = v[:, None]
    negative = (v < 0).sum(axis=0)
    size = np.abs(v)
    size = np.where(size >> (bits - 1) != 0, 1 << (bits - 2), size)
    shifts = np.arange(bits - 2, -1, -1, dtype=np.int64)            # MSB first
    counts = ((size[:, :, None] >> shifts) & 1).sum(axis=0)          # channels × (bits - 1)
    return negative, counts


def bits_used_in_size(values: np.ndarray, bits: int) -> np.ndarray:
    """Per channel, a True/False per size bit (MSB first): set in any sample."""
    _, counts = bit_counts(values, bits)
    return counts > 0


class BitScope:
    """The bitscope's live state: bit counts over the last `seconds` of heard samples, plus
    which bits have been used at all since the track began. Reset on a new track or bit size."""

    def __init__(self, seconds: float = 1.0):
        self.seconds = seconds
        self.recent: collections.deque = collections.deque()  # (frames, negative, counts, rate)
        self.ever: np.ndarray | None = None                    # channels × (bits - 1), bool
        self.ever_negative: np.ndarray | None = None
        self.key = None                                         # (path, bits, channels)

    def add(self, values: np.ndarray, bits: int, rate: int, path=None) -> None:
        key = (path, bits, values.shape[1])
        if key != self.key:
            self.key, self.ever, self.ever_negative = key, None, None
            self.recent.clear()
        negative, counts = bit_counts(values, bits)
        self.recent.append((len(values), negative, counts, rate))
        held = sum(n for n, *_ in self.recent)
        while len(self.recent) > 1 and held - self.recent[0][0] >= self.seconds * rate:
            held -= self.recent.popleft()[0]
        self.ever = counts > 0 if self.ever is None else self.ever | (counts > 0)
        self.ever_negative = negative > 0 if self.ever_negative is None else self.ever_negative | (negative > 0)

    def shares(self) -> tuple[np.ndarray, np.ndarray] | None:
        """(share of samples negative per channel, share with each size bit set: channels ×
        (bits - 1), MSB first), over the recent stretch. None before anything is heard."""
        if not self.recent:
            return None
        frames = sum(n for n, *_ in self.recent)
        negative = sum(neg for _, neg, _, _ in self.recent)
        counts = sum(c for _, _, c, _ in self.recent)
        return negative / frames, counts / frames

    def bits_in_use(self) -> int | None:
        """How many of the bits (sign included) have carried sound since the track began,
        counting down from the top: e.g. 16 for a 16-bit recording stored as 24-bit."""
        if self.ever is None or not self.ever.any():
            return None
        used = self.ever.any(axis=0)
        last = int(np.flatnonzero(used)[-1])   # index from the MSB of the size bits
        return last + 2                         # + the sign bit, + 1 for a count

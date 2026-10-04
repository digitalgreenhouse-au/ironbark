"""The stereo scope's maths (no UI): where the sound sits between the speakers, and how alike
the two channels are.

- Vectorscope / goniometer: each sample pair (L, R) as a dot. Turned 45° the usual way, so
  sound in both speakers equally (mono) is a vertical line, left-only leans to the upper left,
  right-only to the upper right, and opposite channels (one inverted) lie flat. Distance from
  the centre is the level on a dB scale, so quiet music shows as well as loud.
- Correlation: +1 = the same in both channels, 0 = unrelated, -1 = opposite (one channel
  inverted: it cancels when the two are added, e.g. on a mono speaker). The usual meter
  formula, Σ L·R / √(Σ L² · Σ R²), over the last 600 ms — Sound On Sound (Hugh Robjohns): a
  correlation meter's integration time is "typically around 600 milliseconds, to ensure a
  relatively gentle and steady meter movement". No standard fixes it; ours is a plain
  (rectangular) 600 ms window, moved in 10 ms steps.
- Balance: left louder than right, in dB (the two channels' power over the same 600 ms).
- DC offset: each channel's average value since the track began (a constant push away from
  zero), flagged above -40 dBFS like Now Playing → Check.

Like the meters, a thread reads the player's live feed (the file's own samples, before the
volume), and every reading is stamped with when it's heard.
"""

from __future__ import annotations

import collections
import math
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

STEP = 0.010                 # seconds per sums step
CORRELATION_WINDOW = 0.600   # seconds (see above)
SILENT = 1e-10               # mean square below this (-100 dBFS) is treated as silence
DC_WARN_DBFS = -40.0         # the same threshold as Now Playing → Check
FLOOR_DB = -48.0             # the scope's centre: anything quieter sits on it
KEEP_SECONDS = 1.0           # samples kept for the scope picture
SQRT_HALF = math.sqrt(0.5)


def to_float(block: np.ndarray) -> np.ndarray:
    """A feed block as float64 (full scale = 1.0), as two channels: a mono file is shown in
    both, a multichannel file by its first two (front left and right)."""
    x = block.astype(np.float64)
    if block.dtype == np.int32:
        x /= 2.0 ** 31
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return x[:, :2]


def correlation(ll: float, rr: float, lr: float) -> float | None:
    """From the sums Σ L², Σ R², Σ L·R. None in silence. A sound in one channel only reads 0
    (nothing in common)."""
    if ll <= 0 and rr <= 0:
        return None
    if ll <= 0 or rr <= 0:
        return 0.0
    return max(-1.0, min(1.0, lr / math.sqrt(ll * rr)))


def balance_db(ll: float, rr: float) -> float | None:
    """Left louder than right by this many dB (negative: right louder). None in silence or
    when one side is silent (shown as fully one side)."""
    if ll <= 0 or rr <= 0:
        return None
    return 10 * math.log10(ll / rr)


def side_mid(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The scope's axes: across = (R - L)/√2 (side), up = (L + R)/√2 (mid)."""
    left, right = x[:, 0], x[:, 1]
    return (right - left) * SQRT_HALF, (left + right) * SQRT_HALF


def log_radius(side: np.ndarray, mid: np.ndarray, floor_db: float = FLOOR_DB) -> tuple[np.ndarray, np.ndarray]:
    """(across, up) with the same direction but the distance from the centre on a dB scale:
    1.0 = full scale (0 dBFS), 0 = `floor_db` or quieter. A full-scale sine in one channel
    reaches the edge."""
    size = np.hypot(side, mid)
    with np.errstate(divide="ignore"):
        level = 20 * np.log10(np.maximum(size, 1e-12))
    r = np.clip((level - floor_db) / -floor_db, 0.0, 1.0)
    scale = np.divide(r, size, out=np.zeros_like(size), where=size > 0)
    return side * scale, mid * scale


def fold_up(side: np.ndarray, mid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Polar view: a sample and its negative point the same way between the speakers, so the
    lower half is folded onto the upper (iZotope's "Polar Sample" view works the same way)."""
    flip = mid < 0
    return np.where(flip, -side, side), np.where(flip, -mid, mid)


@dataclass
class ScopeReading:
    correlation: float | None         # the last 600 ms
    balance_db: float | None          # the last 600 ms: left louder by this much
    track_correlation: float | None   # this track so far
    dc_offset: tuple[float, float]    # per channel, linear (this track so far)
    one_sided: str = ""               # "left" / "right" when only one channel has sound
    channels: int = 2                 # the file's own channel count


def dc_db(value: float) -> float:
    return 20 * math.log10(abs(value)) if abs(value) > 1e-12 else -240.0


class ScopeEngine:
    """Reads blocks from Player.monitor on its own thread: 10 ms sums for the correlation and
    balance, running sums for the track, and the latest second of samples for the picture."""

    def __init__(self, feed: queue.Queue):
        self.feed = feed
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._steps: collections.deque = collections.deque(maxlen=1000)  # (end time, ll, rr, lr, samples)
        self._carry: np.ndarray | None = None
        self._samples: collections.deque = collections.deque()  # (heard at, rate, x)
        self.current_path = None
        self.channels = 2
        self._reset_track()
        self._thread = threading.Thread(target=self._run, name="stereo-scope", daemon=True)

    def _reset_track(self):
        self._sums = np.zeros(2)      # Σ L, Σ R  (DC offset)
        self._squares = np.zeros(3)   # Σ L², Σ R², Σ L·R
        self._frames = 0

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                item = self.feed.get(timeout=0.1)
            except queue.Empty:
                continue
            self.process(item["source"], item["rate"], item["dac_time"], item.get("path"))

    def process(self, block: np.ndarray, rate: int, heard_at: float, path=None):
        """One block of the file's samples (frames x channels), heard from `heard_at`."""
        channels = block.shape[1] if block.ndim == 2 else 1
        x = to_float(block)
        with self._lock:
            if path != self.current_path:
                self.current_path = path
                self._reset_track()
                self._carry = None
            self.channels = channels
            self._sums += x.sum(axis=0)
            self._squares += ((x[:, 0] ** 2).sum(), (x[:, 1] ** 2).sum(), float(np.dot(x[:, 0], x[:, 1])))
            self._frames += len(x)
            self._samples.append((heard_at, rate, x.astype(np.float32)))
            while self._samples and self._samples[0][0] < heard_at - KEEP_SECONDS:
                self._samples.popleft()
            # 10 ms sums, carrying the remainder into the next block so none is skipped.
            start = heard_at
            if self._carry is not None and len(self._carry):
                start -= len(self._carry) / rate
                x = np.concatenate([self._carry, x])
            step = max(1, int(round(rate * STEP)))
            count = len(x) // step
            self._carry = x[count * step:]
            if count:
                chunks = x[: count * step].reshape(count, step, 2)
                ll = (chunks[:, :, 0] ** 2).sum(axis=1)
                rr = (chunks[:, :, 1] ** 2).sum(axis=1)
                lr = (chunks[:, :, 0] * chunks[:, :, 1]).sum(axis=1)
                ends = start + np.arange(1, count + 1) * step / rate
                self._steps.extend(zip(ends.tolist(), ll.tolist(), rr.tolist(), lr.tolist(), [step] * count))

    # -- reading --------------------------------------------------------------------
    def reading_at(self, now: float | None = None) -> ScopeReading | None:
        """What the scope shows at `now` (time.perf_counter()): the 600 ms heard up to then."""
        now = time.perf_counter() if now is None else now
        with self._lock:
            heard = [s for s in self._steps if now - CORRELATION_WINDOW < s[0] <= now]
            if not heard:
                # Nothing heard lately: the music has stopped (or not reached the speakers yet).
                return None
            squares, sums, frames, channels = self._squares.copy(), self._sums.copy(), self._frames, self.channels
        ll = sum(s[1] for s in heard)
        rr = sum(s[2] for s in heard)
        lr = sum(s[3] for s in heard)
        quiet = SILENT * sum(s[4] for s in heard)  # a channel below -100 dBFS counts as silent
        ll_q, rr_q = (ll if ll > quiet else 0.0), (rr if rr > quiet else 0.0)
        one_sided = ""
        if ll_q > 0 and rr_q == 0:
            one_sided = "left"
        elif rr_q > 0 and ll_q == 0:
            one_sided = "right"
        return ScopeReading(
            correlation=correlation(ll_q, rr_q, lr),
            balance_db=balance_db(ll_q, rr_q),
            track_correlation=correlation(*squares),
            dc_offset=(float(sums[0] / max(1, frames)), float(sums[1] / max(1, frames))),
            one_sided=one_sided,
            channels=channels,
        )

    def samples_between(self, start: float, end: float, limit: int = 8192) -> np.ndarray:
        """The sample pairs heard between `start` and `end` (at most the latest `limit`)."""
        pieces = []
        with self._lock:
            for heard_at, rate, x in self._samples:
                last = heard_at + len(x) / rate
                if last <= start or heard_at > end:
                    continue
                i0 = max(0, int((start - heard_at) * rate))
                i1 = min(len(x), int((end - heard_at) * rate) + 1)
                if i1 > i0:
                    pieces.append(x[i0:i1])
        if not pieces:
            return np.zeros((0, 2), dtype=np.float32)
        joined = np.concatenate(pieces)
        return joined[-limit:]


__all__ = ["ScopeEngine", "ScopeReading", "correlation", "balance_db", "side_mid", "log_radius", "fold_up"]

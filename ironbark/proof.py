"""Proof that the music reaches the sound driver as it should — no UI code in here.

Two checks:

1. What the player sends. Every block handed to the sound driver is fingerprinted
   (CRC-32) in the audio callback. A separate thread decodes the same stretch of the
   file again, independently, applies the same volume the player used, and compares
   fingerprints. So "bit-perfect" is proven block by block, not assumed — and any
   slip (a wrong, repeated or damaged block) is caught, with where it happened.

2. What Windows does with it (shared mode). The player records a few seconds of what
   it sent, while Windows' own recording of the device's output is captured
   (loopback, see loopback.py). `compare_paths` lines the two up — resampling and
   sub-sample delay included — and measures what changed: level, left/right mixing,
   and anything else (effects, other apps' sounds) as a leftover difference.
"""

from __future__ import annotations

import threading
import zlib
from dataclasses import dataclass, field

import numpy as np

from . import audiofile

INT32_MAX = 2**31 - 1
QUIET = 1e-5  # RMS below this (about -100 dBFS) is too quiet to compare
AUDIBLE_CHECK_HZ = 18000  # when Windows resamples, compare below this (see compare_paths)


def apply_gain(block: np.ndarray, gain: float, out: np.ndarray | None = None) -> np.ndarray:
    """What the player sends for `block` at `gain` (the volume and clipping guard together).
    Used by the audio callback and by the checker, so both compute exactly the same thing."""
    if out is None:
        out = np.empty_like(block)
    if gain >= 1.0:
        out[...] = block  # untouched samples
    elif block.dtype == np.float32:
        np.multiply(block, np.float32(gain), out=out)
    else:
        scaled = np.rint(block * gain)
        out[...] = np.clip(scaled, -INT32_MAX - 1, INT32_MAX).astype(np.int32)
    return out


def crc(block: np.ndarray) -> int:
    return zlib.crc32(np.ascontiguousarray(block))


@dataclass
class _Segment:
    """A continuous run of blocks sent from `start` (a frame number); a seek starts a new one.
    Blocks can be any length (gapless playback joins tracks mid-block), so each one's own
    starting frame is kept."""
    start: int
    starts: list = field(default_factory=list)
    crcs: list = field(default_factory=list)
    gains: list = field(default_factory=list)
    frames: list = field(default_factory=list)
    checked: int = 0  # blocks verified so far


@dataclass
class Proof:
    """Where the check has got to, for the window to show."""
    sent: float = 0.0             # seconds of music handed to the driver this track
    checked: float = 0.0          # of which, verified against the file
    mismatch_at: float | None = None  # seconds into the track of the first block that differed
    min_gain: float = 1.0         # lowest gain applied to any block (1.0 = untouched throughout)
    changed_from: float | None = None  # where the first changed (gain < 1) block started

    @property
    def untouched(self) -> bool:
        return self.min_gain >= 1.0

    @property
    def complete(self) -> bool:
        return self.sent > 0 and self.checked >= self.sent - 1e-9 and self.mismatch_at is None


class SentRecord:
    """The fingerprints of what was sent, and a checker that verifies them against the file.

    `add` is called from the audio callback, so it only appends to lists (atomic in
    CPython); the checker thread only reads them."""

    def __init__(self, path, samplerate: int, dtype: str, block_frames: int):
        self.path, self.samplerate, self.dtype, self.block_frames = path, samplerate, dtype, block_frames
        self.segments: list[_Segment] = []
        self._generation = None
        self.mismatch_frame: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- audio thread ---------------------------------------------------------
    def add(self, generation: int, start_frame: int, sent: np.ndarray, gain: float):
        if generation != self._generation or not self.segments:
            self.segments.append(_Segment(start_frame))
            self._generation = generation
        segment = self.segments[-1]
        segment.starts.append(start_frame)
        segment.gains.append(gain)
        segment.frames.append(len(sent))
        segment.crcs.append(crc(sent))  # appended last: the checker reads crcs to know what's ready

    # -- checker --------------------------------------------------------------
    def start_checking(self, interval: float = 0.5):
        self._thread = threading.Thread(target=self._check_loop, args=(interval,), name="bit-perfect-proof",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _check_loop(self, interval: float):
        try:
            with audiofile.open(self.path) as reference:  # the same decode path as playback
                while not self._stop.wait(interval):
                    self.check(reference)
                self.check(reference)  # whatever arrived last
        except (OSError, RuntimeError):
            pass  # the file went away: the proof just stops where it got to

    def check(self, reference):
        """Verify every block sent since last time against an independent decode."""
        for segment in list(self.segments):
            ready = len(segment.crcs)
            while segment.checked < ready:
                k = segment.checked
                frame = segment.starts[k]
                reference.seek(frame)
                block = reference.read(segment.frames[k], dtype=self.dtype, always_2d=True)
                if crc(apply_gain(block, segment.gains[k])) != segment.crcs[k] and self.mismatch_frame is None:
                    self.mismatch_frame = frame
                segment.checked += 1

    def summary(self) -> Proof:
        proof = Proof()
        sent = checked = 0
        for segment in list(self.segments):
            frames, gains, starts = list(segment.frames), list(segment.gains), list(segment.starts)
            n = len(segment.crcs)
            sent += sum(frames[:n])
            checked += sum(frames[:segment.checked])
            for k, gain in enumerate(gains[:n]):
                if gain < proof.min_gain:
                    proof.min_gain = gain
                if gain < 1.0 and proof.changed_from is None:
                    proof.changed_from = starts[k] / self.samplerate
        proof.sent, proof.checked = sent / self.samplerate, checked / self.samplerate
        if self.mismatch_frame is not None:
            proof.mismatch_at = self.mismatch_frame / self.samplerate
        return proof


# -- what Windows did ------------------------------------------------------------
@dataclass
class PathCheck:
    """The result of comparing what the player sent with what Windows played."""
    verdict: str                 # "untouched", "resampled_only", "changed", "too_quiet", "no_match"
    identical: bool = False
    resampled: tuple[int, int] | None = None   # (from, to) rates, if Windows converted
    level_db: float = 0.0        # Windows' level change (0 = none)
    crosstalk_db: float = float("-inf")  # how much of each channel leaks into the other
    difference_db: float = float("-inf")  # everything else that changed, relative to the music
    polarity_flipped: bool = False
    delay_ms: float = 0.0


def resample(x: np.ndarray, rate_in: int, rate_out: int, delay: float = 0.0) -> np.ndarray:
    """Band-limited resampling of a short excerpt (frames x channels) in the frequency
    domain, optionally delayed by `delay` output samples (fractions allowed)."""
    n = len(x)
    m = int(round(n * rate_out / rate_in))
    spectrum = np.fft.rfft(x, axis=0)
    out_bins = m // 2 + 1
    if out_bins <= spectrum.shape[0]:
        spectrum = spectrum[:out_bins]
    else:
        spectrum = np.concatenate([spectrum, np.zeros((out_bins - spectrum.shape[0], x.shape[1]), complex)])
    if delay:
        k = np.arange(out_bins)
        spectrum = spectrum * np.exp(-2j * np.pi * k * delay / m)[:, None]
    return np.fft.irfft(spectrum, n=m, axis=0) * (m / n)


def _find_lag(sent_mono: np.ndarray, got_mono: np.ndarray) -> tuple[int, float, float]:
    """Where `sent` sits inside `got`: (whole-sample offset, fraction, correlation sign)."""
    size = 1 << int(np.ceil(np.log2(len(got_mono) + len(sent_mono))))
    corr = np.fft.irfft(np.fft.rfft(got_mono, size) * np.conj(np.fft.rfft(sent_mono, size)), size)
    valid = corr[: max(1, len(got_mono) - len(sent_mono) + 1)]
    best = int(np.argmax(np.abs(valid)))
    sign = 1.0 if valid[best] >= 0 else -1.0
    fraction = 0.0
    if 0 < best < len(valid) - 1:  # parabola through the peak and its neighbours
        a, b, c = np.abs(valid[best - 1]), np.abs(valid[best]), np.abs(valid[best + 1])
        denominator = a - 2 * b + c
        if denominator:
            fraction = float(np.clip(0.5 * (a - c) / denominator, -0.5, 0.5))
    return best, fraction, sign


def _misfit(x: np.ndarray, y: np.ndarray) -> float:
    """How much of y a mix of x's channels can't explain."""
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    return float(np.sum((y - x @ coef) ** 2))


def _best_delay(cost, low: float, high: float, steps: int = 30) -> float:
    """Golden-section search for the delay with the lowest cost."""
    g = (np.sqrt(5) - 1) / 2
    a, b = low, high
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = cost(c), cost(d)
    for _ in range(steps):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = cost(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = cost(d)
    return (a + b) / 2


def _low_pass(x: np.ndarray, rate: int, cutoff: float) -> np.ndarray:
    spectrum = np.fft.rfft(x, axis=0)
    spectrum[int(cutoff * len(x) / rate) + 1:] = 0
    return np.fft.irfft(spectrum, n=len(x), axis=0)


def _taper(n: int, fade: float = 0.2) -> np.ndarray:
    """1.0 in the middle, fading smoothly (half a cosine) to 0 over `fade`/2 of each end."""
    w = np.ones(n)
    k = int(n * fade / 2)
    if k:
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(k) / k)
        w[:k], w[n - k:] = ramp, ramp[::-1]
    return w


def _db(ratio: float) -> float:
    return 20 * np.log10(ratio) if ratio > 0 else float("-inf")


def compare_paths(sent: np.ndarray, sent_rate: int, got: np.ndarray, got_rate: int) -> PathCheck:
    """`sent`: what the player handed Windows (frames x channels, float). `got`: what
    Windows recorded of the device's output, a little longer so `sent` fits inside it."""
    sent = np.asarray(sent, dtype=np.float64)
    got = np.asarray(got, dtype=np.float64)
    if sent.ndim == 1:
        sent = sent[:, None]
    if got.ndim == 1:
        got = got[:, None]
    channels = min(sent.shape[1], got.shape[1])
    sent, got = sent[:, :channels], got[:, :channels]
    if np.sqrt(np.mean(sent ** 2)) < QUIET:
        return PathCheck("too_quiet")
    resampled = (sent_rate, got_rate) if sent_rate != got_rate else None

    # Resampling a short excerpt in one go "rings" from its hard ends right across it, so
    # when the rates differ both sides are faded in and out the same way first (a Tukey taper).
    taper_in = _taper(len(sent)) if resampled else None
    source = sent * taper_in[:, None] if resampled else sent

    # Line them up: a first pass on whole samples, then again with the fraction, which matters
    # once the rates differ (half a sample out would look like a big change in the treble).
    base = resample(source, sent_rate, got_rate) if resampled else source
    lag, fraction, _ = _find_lag(base.mean(axis=1), got.mean(axis=1))  # (a flipped sign shows in the fit)
    if lag + len(base) > len(got):
        return PathCheck("no_match", resampled=resampled)
    y = got[lag: lag + len(base)]
    if resampled:
        y = y * _taper(len(base))[:, None]
        # The correlation peak only gets the fraction roughly; 0.02 of a sample out still
        # leaves a -45 dB difference in the treble. So home in on the delay that fits best.
        fraction = _best_delay(lambda d: _misfit(resample(source, sent_rate, got_rate, delay=d), y),
                               fraction - 0.6, fraction + 0.6)
        base = resample(source, sent_rate, got_rate, delay=fraction)
    x = base
    if resampled:
        # Every resampler rolls off the top of the band (above ~18 kHz) its own way; that's part
        # of resampling, not an extra change. So the comparison covers the band below it.
        x, y = _low_pass(x, got_rate, AUDIBLE_CHECK_HZ), _low_pass(y, got_rate, AUDIBLE_CHECK_HZ)

    if not resampled and np.array_equal(x.astype(np.float32), y.astype(np.float32)):
        return PathCheck("untouched", identical=True, delay_ms=1000 * lag / got_rate)

    # Fit each recorded channel as a mix of the sent channels: the diagonal is the level,
    # the rest is left/right mixing; what the fit can't explain is "everything else".
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)  # channels x channels
    explained = x @ coef
    residual = y - explained
    music = np.sqrt(np.mean(y ** 2))
    diagonal = np.diag(coef)
    level = float(np.mean(np.abs(diagonal)))
    cross = float(np.max(np.abs(coef - np.diag(diagonal)))) if channels > 1 else 0.0
    result = PathCheck(
        "changed",
        resampled=resampled,
        level_db=_db(level),
        crosstalk_db=_db(cross / level) if level > 0 and cross > 0 else float("-inf"),
        difference_db=_db(np.sqrt(np.mean(residual ** 2)) / music) if music > 0 else float("-inf"),
        polarity_flipped=bool(np.all(diagonal < 0)),
        delay_ms=1000 * (lag + fraction) / got_rate,
    )
    if result.level_db < -20 and result.difference_db > -6:
        return PathCheck("no_match", resampled=resampled)  # the music isn't in there: nothing lined up
    clean = (abs(result.level_db) < 0.05 and result.crosstalk_db < -60 and not result.polarity_flipped
             and result.difference_db < (-50 if resampled else -100))
    if clean:
        result.verdict = "resampled_only" if resampled else "untouched"
    return result


def describe_path(check: PathCheck) -> list[tuple[str, str, str]]:
    """(label, text, tone) lines for the window. tone: "good", "warn" or "plain"."""
    if check.verdict == "too_quiet":
        return [("Result", "Too quiet to check: try again while the music is playing (volume above 0)", "plain")]
    if check.verdict == "no_match":
        return [("Result", "Couldn't find the music in what Windows played: another app may hold the device, "
                           "or Windows' recording isn't available for it", "warn")]
    lines = []
    if check.identical:
        lines.append(("Result", "✓ Untouched: Windows played exactly what the player sent", "good"))
        return lines
    if check.resampled:
        a, b = check.resampled
        lines.append(("Resampled", f"{a / 1000:g} → {b / 1000:g} kHz by the Windows mixer (normal in shared mode; "
                                   "bit-perfect avoids it). The rest is compared below 18 kHz, where "
                                   "resamplers agree", "warn"))
    if abs(check.level_db) >= 0.05:
        lines.append(("Level", f"changed by {check.level_db:+.1f} dB (an app volume in Windows' Volume mixer?)",
                      "warn"))
    if check.polarity_flipped:
        lines.append(("Polarity", "flipped (the waveform is upside down)", "warn"))
    if check.crosstalk_db > -60:
        lines.append(("Left/right", f"mixed together ({check.crosstalk_db:.0f} dB): spatial sound, mono audio "
                                    "or an effect", "warn"))
    if check.difference_db > (-50 if check.resampled else -100):
        lines.append(("Other changes", f"{check.difference_db:.0f} dB of difference beyond the above: audio "
                                       "enhancements/effects, or other apps' sounds playing at the same time", "warn"))
    if check.verdict in ("untouched", "resampled_only"):
        extra = f" (leftover difference {check.difference_db:.0f} dB)" if check.resampled else ""
        lines.insert(0, ("Result", "✓ Nothing else changed: no level change, no effects, no channel mixing" + extra,
                         "good"))
    return lines

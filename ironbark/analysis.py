"""One pass over a file: verify it decodes perfectly, and measure what's really in it.

MD5 check: every FLAC encoder stores an MD5 fingerprint of the original audio.
Decoding the whole file and recomputing it proves the file is intact.

DR score: the "DR meter" method audiophiles compare albums with (Pleasurize
Music Foundation / TT DR Offline Meter style). Per channel, the track is cut
into 3-second blocks; the loudest 20% of blocks give the RMS, the
second-highest block peak gives the peak, and DR = peak/RMS in dB, averaged
over channels. Our figure can differ from the official meter by ±1. An album's
DR is the plain average of its tracks' (the official way; foobar2000's DR Meter
can also weight it by length).

Loudness: integrated loudness and loudness range of the whole track (see loudness.py).

"Is it genuine?" (0.6.0) — clues, never proof:
- Bits used: the lowest bit that ever carries sound. A 24-bit file whose lowest 8 bits are
  always zero is a 16-bit recording stored as 24-bit (padded). This can't see a 16-bit
  recording that was turned up or down on the way to 24-bit: that fills the low bits.
- Where the sound stops: the average spectrum of the whole track. A sharp wall well below the
  top of the range (15–20 kHz) is what lossy encoders (MP3, AAC) leave; a sharp wall at
  20–24 kHz in a 88.2 kHz+ file is what upsampling from CD quality leaves. A gentle fade is
  just the recording (old tape, mellow instruments), so it isn't flagged. Below CD rate (32 or
  22.05 kHz) the converter's own anti-alias filter is a sharp wall near the top of the range,
  so there only a wall well below that counts (`lossy_below`).

Also: clipping (runs of 3+ samples in a row at full scale — Audacity's "Find Clipping"
default), where the highest peak is, true peak and PLR (peak-to-loudness ratio: true peak
minus integrated loudness, after MeterPlugs / ITU-R BS.1770), and the stereo figures
(DC offset, left/right balance, correlation, identical channels).

Format flags (0.9.0, formats.py): HDCD packets and the MQA signal, found in the same pass,
plus pre-emphasis from the file's cue sheet / a .cue file / its tags. Reported only.
"""

from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import soundfile as sf

from . import audiofile
from .formats import FormatFlags, FormatScan
from .loudness import Loudness, integrated, loudness_range

DR_BLOCK_SECONDS = 3
SPECTRUM_SIZE = 8192         # FFT length for the average spectrum (5.4 Hz steps at 44.1 kHz)
CLIP_RUN = 3                 # this many full-scale samples in a row count as clipping
LOSSY_BELOW_HZ = 20000.0     # a sharp wall below this suggests a lossy source…
CONVERTER_WALL = 0.43        # …but below CD rate, only one below this fraction of the sample rate
UPSAMPLED_BELOW_HZ = 24500.0 # …and below this, in a 88.2 kHz+ file, upsampling from CD quality
CUT_SHORT_SLACK = 2304       # frames: decoding may end this little short of the stated length (a margin)
FLOOR_MARGIN_DB = 10.0       # "sound" = at least this far above the spectrum's own floor
SHARP_DB = 25.0              # a drop this big within ~1 kHz is a wall, not a fade
SUBTYPE_BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32,
                "ALAC_16": 16, "ALAC_20": 20, "ALAC_24": 24, "ALAC_32": 32}
FLOAT_SUBTYPES = audiofile.FLOAT_SUBTYPES


@dataclass
class Analysis:
    md5_ok: bool | None   # None = the file has no MD5 to check against
    dr: int | None        # None = too short / silent
    peak_dbfs: float      # highest sample, dB below full scale
    lufs: float | None = None  # integrated loudness (EBU R128 / ITU-R BS.1770), None = silent
    lra: float | None = None   # loudness range, LU
    # 0.6.0
    rate: int = 0
    channels: int = 0
    bits: int | None = None          # the file's sample size; None = lossy or floating point
    float_samples: bool = False
    lossy: bool = False              # an MP3: lossy by design
    bits_used: int | None = None     # how many of `bits` ever carry sound; None = silent / n.a.
    cutoff_hz: float | None = None   # where the sound stops; None = too short / silent
    cutoff_sharp: bool = False       # stops at a wall rather than fading out
    peak_at: float | None = None     # seconds into the track of the highest sample
    true_peak: float | None = None   # linear, 1.0 = full scale (can be above)
    clip_runs: int = 0
    clipped_samples: int = 0
    dc_offset: tuple[float, ...] = ()  # per channel, linear
    balance_db: float | None = None    # left louder than right by this much (stereo only)
    correlation: float | None = None   # +1 same, 0 unrelated, -1 opposite (stereo only)
    identical_channels: bool = False   # left == right, sample for sample (and not silent)
    duration: float = 0.0              # seconds, as the file says
    damaged_at: float | None = None    # seconds: where the file stopped decoding (None = all of it did)
    flags: FormatFlags | None = None   # 0.9.0: HDCD / MQA / pre-emphasis (None = not checked)

    @property
    def true_peak_db(self) -> float | None:
        return 20 * math.log10(self.true_peak) if self.true_peak else None

    @property
    def plr(self) -> float | None:
        """Peak-to-loudness ratio, dB: how far the true peak stands above the integrated loudness."""
        tp = self.true_peak_db
        return tp - self.lufs if tp is not None and self.lufs is not None else None

    @property
    def verdict(self) -> str:
        """"lossy", "upsampled", "padded" or "" — the strongest clue that this isn't what it says."""
        if self.lossy:
            return ""
        wall = wall_verdict(self.cutoff_hz, self.cutoff_sharp, self.rate)
        if wall:
            return wall
        if self.bits and self.bits_used is not None and self.bits_used < self.bits:
            return "padded"
        return ""


def wall_verdict(cutoff_hz: float | None, sharp: bool, rate: int) -> str:
    """"lossy" / "upsampled" / "" from where the sound stops and how (see find_cutoff); shared
    by Now Playing → Check and the Spectrogram window's cut-off line."""
    if not sharp or cutoff_hz is None:
        return ""
    if cutoff_hz < lossy_below(rate):
        return "lossy"
    if rate >= 88200 and cutoff_hz < UPSAMPLED_BELOW_HZ:
        return "upsampled"
    return ""


def lossy_below(rate: int) -> float:
    """A sharp wall below this (Hz) suggests a lossy source. At 32 or 22.05 kHz the converter's
    own anti-alias filter is a sharp wall just under half the sample rate, so there a wall
    only counts well below that."""
    if 0 < rate < 44100:
        return min(LOSSY_BELOW_HZ, CONVERTER_WALL * rate)
    return LOSSY_BELOW_HZ


@dataclass
class AlbumResult:
    lufs: float | None
    lra: float | None
    dr: int | None           # average of the tracks' DR
    dr_low: int | None
    dr_high: int | None
    tracks: int              # tracks measured (one that stops decoding part-way counts up to there)
    skipped: int = 0         # tracks that couldn't be read at all, so aren't in the figures
    partial: int = 0         # of `tracks`, those that stop decoding part-way


def pcm_bytes(block: np.ndarray, bits: int) -> bytes:
    """Left-justified int32 samples -> the little-endian PCM bytes FLAC's MD5 covers."""
    if bits <= 8:
        return (block >> 24).astype("<i1").tobytes()
    if bits <= 16:
        return (block >> 16).astype("<i2").tobytes()
    if bits <= 24:
        raw = (block >> 8).astype("<i4").view(np.uint8).reshape(-1, 4)
        return raw[:, :3].tobytes()
    return block.astype("<i4").tobytes()


def dr_from_blocks(block_rms: np.ndarray, block_peak: np.ndarray) -> float | None:
    """DR for one channel from per-block RMS (already ×√2) and peak values."""
    if len(block_rms) == 0:
        return None
    rms_sorted = np.sort(block_rms)[::-1]
    top = rms_sorted[: max(1, int(len(rms_sorted) * 0.2))]
    rms = math.sqrt(float(np.mean(top ** 2)))
    peaks = np.sort(block_peak)[::-1]
    peak = peaks[1] if len(peaks) > 1 else peaks[0]
    if rms <= 0 or peak <= 0:
        return None
    return 20 * math.log10(peak / rms)


def dr_of(rms_blocks: list[np.ndarray], peak_blocks: list[np.ndarray]) -> float | None:
    """A track's DR (unrounded) from its per-block (RMS×√2, peak) rows, averaged over channels."""
    if not rms_blocks:
        return None
    rms, peaks = np.array(rms_blocks), np.array(peak_blocks)  # shape (blocks, channels)
    per_channel = [dr_from_blocks(rms[:, c], peaks[:, c]) for c in range(rms.shape[1])]
    per_channel = [d for d in per_channel if d is not None]
    return sum(per_channel) / len(per_channel) if per_channel else None


OVERSAMPLE = 4        # the live true-peak meter (meters.py): BS.1770's own 4x
FILE_OVERSAMPLE = 8   # (0.24.0) whole-file measures: Check, the clipping guard, the Clipping window
_TAPS_PER_PHASE = 48
_STRETCH = 2048       # TruePeak works a block out this many points (per phase) at a time
_ROUNDING = 1e-9      # far more than a 48-term sum can be rounded by: keeps TruePeak's skip exact

# 🔎 ITU-R BS.1770-5 (11/2023) Annex 2, Attachment 1 (checked 05/10/2026 against the published PDF):
# the most an n-times oversampled peak can under-read a sine at normalised frequency f (a fraction
# of the sample rate) is 20·log10(cos(π·f/n)) — its table gives 4x: 0.554 dB (f = 0.45) / 0.688 dB
# (f = 0.5), 8x: 0.136 / 0.169 dB. See max_under_read (tested against those figures).


def max_under_read(factor: int, f_norm: float) -> float:
    """dB (positive): the worst an n-times oversampled peak reading can fall short of the real
    peak, for sound up to `f_norm` of the sample rate (BS.1770-5 Annex 2, Attachment 1)."""
    return -20 * math.log10(math.cos(math.pi * f_norm / factor))


# The clipping guard's spare room below full scale: the most 8x can under-read at all (half the
# sample rate, 0.169 dB), rounded up. So a guarded track's rebuilt wave stays under full scale.
GUARD_SPARE_DB = 0.17
GUARD_HEADROOM_CHOICES = (0.0, 1.0, 2.0, 3.0, 3.5)   # Settings → "My DAC has headroom" (0 = off)


def headroom_label(db: float) -> str:
    """Settings' words for a headroom choice: "Off", "+1 dB", "+3.5 dB"."""
    return "Off" if not db else f"+{db:g} dB"


def headroom_index(db) -> int:
    """Which of GUARD_HEADROOM_CHOICES a saved value is (anything unknown = Off)."""
    try:
        return GUARD_HEADROOM_CHOICES.index(float(db or 0.0))
    except (TypeError, ValueError):
        return 0


def guard_trim(peak: float, headroom_db: float = 0.0, spare_db: float = GUARD_SPARE_DB) -> float:
    """The clipping guard's turn-down (a gain ≤ 1.0) for a track whose true peak is `peak`
    (linear, 1.0 = full scale). Only a track that would go past what the converter can take —
    full scale, plus `headroom_db` if the DAC has room above it — is turned down, and only by
    just enough to land `spare_db` under that ceiling."""
    ceiling = 10 ** (max(0.0, headroom_db) / 20)
    if not peak or peak <= ceiling:
        return 1.0
    return ceiling / peak * 10 ** (-spare_db / 20)


def _oversampling_phases(factor: int = OVERSAMPLE) -> list[np.ndarray]:
    """A `factor`x reconstruction filter (windowed sinc, 48 taps per phase), split into its
    phases: what a DAC does to turn samples back into a smooth wave. Phase p gives the wave
    p/factor of a sample after each sample (phase 0 is the samples themselves)."""
    n = np.arange(-_TAPS_PER_PHASE * factor // 2, _TAPS_PER_PHASE * factor // 2)
    h = np.sinc(n / factor) * np.kaiser(len(n), 8.0)
    return [h[p::factor] for p in range(factor)]


class TruePeak:
    """True peak of a stream fed in blocks (the reconstruction above, carried across blocks).
    (0.24.0) 8x for whole files: BS.1770-5's worst under-read drops from 0.69 to 0.17 dB.

    Only the highest point is wanted, so stretches that can't beat it are skipped: a phase's
    output never exceeds the loudest sample it reads times the sum of that phase's tap sizes.
    Each block's loudest stretches are worked out first, and most of the rest then falls
    under the peak they set. The skip is exact (the peak is the very value working out every
    point gives); it saves most on dynamic music, less on loud masters. Call `finish()` at the
    end of the stream."""

    def __init__(self, channels: int, factor: int = FILE_OVERSAMPLE):
        self.phases = _oversampling_phases(factor)
        self.gains = [float(np.abs(phase).sum()) for phase in self.phases]
        self.overlap = _TAPS_PER_PHASE - 1
        self.previous = np.zeros((self.overlap, channels))
        self.peak = 0.0

    def add(self, block: np.ndarray) -> None:
        x = np.concatenate([self.previous, block])
        points = len(x) - self.overlap          # outputs per phase and channel
        if points > 0:
            most = max(self.gains)
            for start, ch, loudest in self._stretches(x, points):
                bound = loudest * (1 + _ROUNDING)
                if bound * most <= self.peak:
                    break                         # loudest first: nothing after this can beat it
                # A view of x itself, so each output is summed exactly as over the whole block.
                stretch = x[start: min(points, start + _STRETCH) + self.overlap, ch]
                for phase, gain in zip(self.phases, self.gains):
                    if bound * gain > self.peak:
                        y = np.convolve(stretch, phase, mode="valid")
                        self.peak = max(self.peak, float(np.abs(y).max()))
        self.previous = x[-self.overlap:]

    def _stretches(self, x: np.ndarray, points: int) -> list[tuple[int, int, float]]:
        """(first output, channel, loudest sample its outputs read) for each _STRETCH outputs of
        the block, loudest first. Output j reads x[j] to x[j + overlap]."""
        count = -(-points // _STRETCH)
        magnitude = np.zeros(((count + 1) * _STRETCH, x.shape[1]))
        magnitude[: len(x)] = np.abs(x)
        own = magnitude[: count * _STRETCH].reshape(count, _STRETCH, -1).max(axis=1)
        after = magnitude[_STRETCH:].reshape(count, _STRETCH, -1)[:, : self.overlap].max(axis=1)
        loudest = np.maximum(own, after)
        channels = x.shape[1]
        return [(int(i) // channels * _STRETCH, int(i) % channels, float(loudest.flat[i]))
                for i in np.argsort(-loudest, axis=None, kind="stable")]

    def finish(self) -> None:
        """The end of the stream: the filter's tail (silence after the last sample), so the
        wave around the last few samples is measured too."""
        self.add(np.zeros((_TAPS_PER_PHASE, self.previous.shape[1])))


def true_peak(path: str | Path, cancel: threading.Event | None = None) -> float | None:
    """The highest point of the reconstructed waveform (8x), as a linear level (1.0 = full
    scale). Loud masters often peak *between* samples above 1.0, and a converter then clips:
    heard as crackle or small pops. None if cancelled. The clipping guard's measurement."""
    with audiofile.open(path) as f:
        meter = TruePeak(f.channels)
        for block in audiofile.blocks(f, f.samplerate * 5, dtype="float32", always_2d=True):
            if cancel is not None and cancel.is_set():
                return None
            meter.add(block)
    meter.finish()
    return meter.peak


class ClipCounter:
    """Runs of at least CLIP_RUN samples in a row at full scale, in one channel, fed in blocks
    (a run may cross from one block into the next)."""

    def __init__(self, min_run: int = CLIP_RUN):
        self.min_run = min_run
        self.carry = 0      # a run still going at the end of the last block
        self.runs = 0
        self.samples = 0

    def _count(self, length: int) -> None:
        if length >= self.min_run:
            self.runs += 1
            self.samples += length

    def add(self, mask: np.ndarray) -> None:
        if not len(mask):
            return
        edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
        starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
        lengths = list(ends - starts)
        if self.carry:
            if len(starts) and starts[0] == 0:
                lengths[0] += self.carry
            else:
                self._count(self.carry)
        self.carry = 0
        if len(ends) and ends[-1] == len(mask):
            self.carry = lengths.pop()
        for length in lengths:
            self._count(int(length))

    def finish(self) -> None:
        self._count(self.carry)
        self.carry = 0


def bits_in_use(ored: int, bits: int) -> int | None:
    """How many of a file's `bits` ever carry sound, from every left-justified int32 sample
    ORed together: everything below the lowest set bit is always zero. None = silent."""
    ored &= 0xFFFFFFFF
    if not ored:
        return None
    zeros = (ored & -ored).bit_length() - 1
    return min(bits, 32 - zeros)


def find_cutoff(power: np.ndarray, rate: int) -> tuple[float | None, bool]:
    """(where the sound stops in Hz, whether it stops at a wall) from an average power spectrum
    (SPECTRUM_SIZE // 2 + 1 bins, 0 Hz to half the sample rate)."""
    if power is None or not np.any(power > 0):
        return None, False
    nyquist = rate / 2
    freqs = np.linspace(0, nyquist, len(power))
    step = freqs[1]
    width = max(1, round(100 / step))                       # smooth over ~100 Hz
    smooth = np.convolve(power, np.ones(width) / width, mode="same")
    db = 10 * np.log10(smooth + 1e-30)
    upper = freqs > 1000
    floor = float(np.percentile(db[upper], 3))
    loud = np.flatnonzero(upper & (db > floor + FLOOR_MARGIN_DB))
    if not len(loud):
        return float(nyquist), False                         # flat to the top: sound all the way
    cutoff = float(freqs[loud[-1]])
    if cutoff + 300 >= nyquist:
        return cutoff, False                                 # reaches the top: no wall to see
    below = db[(freqs >= cutoff - 1500) & (freqs <= cutoff - 500)]
    above = db[(freqs >= cutoff + 200) & (freqs <= min(cutoff + 1200, nyquist))]
    sharp = bool(len(below) and len(above) and np.median(below) - np.median(above) >= SHARP_DB)
    return cutoff, sharp


def cut_short(f: sf.SoundFile, frames_read: int) -> bool:
    """Whether a file whose decoding ended after `frames_read` frames stopped clearly short of
    the length its header states: cut off (an incomplete download, say), not just at its end.
    An MP3 without a length header (Xing / Info / VBRI) has only an estimate made from its
    size, which can be off either way, so it never counts as cut short."""
    if frames_read + CUT_SHORT_SLACK >= f.frames:
        return False
    if audiofile.is_mp3(f):
        try:
            return audiofile.has_length_header(f.name)
        except (OSError, ValueError, IndexError):
            return False
    return True


def _blocks(f: sf.SoundFile, damaged: list, **kwargs):
    """audiofile.blocks() (int32 from a float or lossy file comes out scaled and clipped, as
    playback sends it), except that a file which stops decoding part-way (damaged) ends the loop
    instead of raising, and says so in `damaged`; so does one cut short of its stated length."""
    done = 0
    try:
        for block in audiofile.blocks(f, **kwargs):
            done += len(block)
            yield block
    except RuntimeError:  # libsndfile: the rest can't be decoded
        damaged.append(True)
        return
    if cut_short(f, done):
        damaged.append(True)


def analyse(
    path: str | Path,
    bits: int,
    stored_md5: int,
    progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
) -> Analysis | None:
    """Returns None if cancelled."""
    md5 = hashlib.md5()
    rms_blocks: list[np.ndarray] = []
    peak_blocks: list[np.ndarray] = []
    with audiofile.open(path) as f:
        rate, channels = f.samplerate, f.channels
        file_bits = SUBTYPE_BITS.get(f.subtype)
        is_float = f.subtype in FLOAT_SUBTYPES
        lossy = audiofile.is_lossy(f)
        block_frames = rate * DR_BLOCK_SECONDS
        meter = Loudness(rate, channels)
        tp = TruePeak(channels)
        clips = [ClipCounter() for _ in range(channels)]
        # Full scale in the left-justified int32 samples: the file's own largest values.
        top = (2 ** ((file_bits or 16) - 1) - 1) << (32 - (file_bits or 16))
        # A Kaiser window with very low side lobes, so a deep wall isn't smeared upwards by leakage.
        window = np.kaiser(SPECTRUM_SIZE, 20.0)
        power = np.zeros(SPECTRUM_SIZE // 2 + 1)
        ored = 0
        sums = np.zeros(channels)
        squares = np.zeros(channels)
        cross = 0.0
        same = channels == 2
        best, best_at = -1.0, None
        formats = FormatScan(rate, channels, file_bits, lossy, is_float)
        total = max(1, f.frames)
        done = 0
        damaged: list = []
        # A floating-point file is measured as it is, overs above full scale included (int32 would clip them).
        dtype = "float64" if is_float else "int32"
        for block in _blocks(f, damaged, blocksize=block_frames, dtype=dtype, always_2d=True):
            if cancel is not None and cancel.is_set():
                return None
            if not is_float:   # (no stored MD5 for float files: WAV / AIFF carry none)
                md5.update(pcm_bytes(block, bits))
            formats.add(block)
            x = block if is_float else block.astype(np.float64) / 2**31
            meter.add(x)
            tp.add(x)
            # Skip a short final block, as the DR meter does — unless it's the only one.
            if len(block) == block_frames or not rms_blocks:
                rms_blocks.append(np.sqrt(2 * np.mean(x * x, axis=0)))
                peak_blocks.append(np.max(np.abs(x), axis=0))
            magnitude = np.abs(x)
            where = np.unravel_index(int(np.argmax(magnitude)), magnitude.shape)
            if magnitude[where] > best:
                best, best_at = float(magnitude[where]), (done + int(where[0])) / rate
            if file_bits:
                ored |= int(np.bitwise_or.reduce(block.ravel()).astype(np.int64))
            full = (block >= top) | (block == np.iinfo(np.int32).min) if not is_float else (np.abs(x) >= 1.0)
            for ch in range(channels):
                clips[ch].add(full[:, ch])
            sums += x.sum(axis=0)
            squares += (x * x).sum(axis=0)
            if channels == 2:
                cross += float(np.dot(x[:, 0], x[:, 1]))
                same = same and bool(np.array_equal(block[:, 0], block[:, 1]))
            frames = len(x) // SPECTRUM_SIZE
            if frames:
                chunk = x[: frames * SPECTRUM_SIZE].T.reshape(channels, frames, SPECTRUM_SIZE)
                power += (np.abs(np.fft.rfft(chunk * window, axis=-1)) ** 2).sum(axis=(0, 1))
            done += len(block)
            if progress:
                progress(done / total)
    for counter in clips:
        counter.finish()
    tp.finish()

    md5_ok = None if stored_md5 == 0 else int(md5.hexdigest(), 16) == stored_md5
    if damaged:
        md5_ok = False
    lufs, lra = integrated(meter.powers), loudness_range(meter.powers)
    precise = dr_of(rms_blocks, peak_blocks)
    top_peak = best if best > 0 else 0.0
    cutoff, sharp = find_cutoff(power, rate) if power.any() else (None, False)
    frames_read = max(1, done)
    balance = correlation = None
    if channels == 2 and squares.all():
        balance = 10 * math.log10(squares[0] / squares[1])
        correlation = cross / math.sqrt(squares[0] * squares[1])
    return Analysis(
        md5_ok=md5_ok,
        dr=round(precise) if precise is not None else None,
        peak_dbfs=20 * math.log10(top_peak) if top_peak > 0 else float("-inf"),
        lufs=lufs, lra=lra,
        rate=rate, channels=channels,
        bits=None if lossy else file_bits, float_samples=is_float, lossy=lossy,
        bits_used=bits_in_use(ored, file_bits) if file_bits and not lossy else None,
        cutoff_hz=cutoff, cutoff_sharp=sharp,
        peak_at=best_at if top_peak > 0 else None,
        true_peak=tp.peak or None,
        clip_runs=sum(c.runs for c in clips), clipped_samples=sum(c.samples for c in clips),
        dc_offset=tuple(float(s / frames_read) for s in sums),
        balance_db=balance, correlation=correlation,
        identical_channels=same and bool(squares.any()),
        duration=total / rate if rate else 0.0,
        damaged_at=done / rate if damaged else None,
        flags=_flags(formats, path),
    )


def _flags(formats: FormatScan, path) -> FormatFlags | None:
    """The format flags; a problem reading the tags or a cue sheet never spoils the rest of
    the check."""
    try:
        return formats.finish(path)
    except Exception:  # noqa: BLE001 — flags are extra information, never worth failing for
        return None


def album_measure(paths: Iterable, cancel: threading.Event | None = None,
                  progress: Callable[[float], None] | None = None) -> AlbumResult | None:
    """An album's loudness (all tracks as one programme, as loudness.album_loudness) and DR
    (the plain average of the tracks'), in one read of each file. A track that stops decoding
    part-way counts up to there (as the Check does); one that can't be read at all is left out
    and counted in `skipped`. None if cancelled."""
    paths = list(paths)
    gathered, drs = [], []
    skipped = partial = 0
    for k, path in enumerate(paths):
        damaged: list = []
        try:
            with audiofile.open(path) as f:
                meter = Loudness(f.samplerate, f.channels)
                block_frames = f.samplerate * DR_BLOCK_SECONDS
                total = max(1, f.frames)
                done = 0
                rms_blocks, peak_blocks = [], []
                for block in _blocks(f, damaged, blocksize=block_frames, dtype="float64", always_2d=True):
                    if cancel is not None and cancel.is_set():
                        return None
                    meter.add(block)
                    if len(block) == block_frames or not rms_blocks:
                        rms_blocks.append(np.sqrt(2 * np.mean(block * block, axis=0)))
                        peak_blocks.append(np.max(np.abs(block), axis=0))
                    done += len(block)
                    if progress:
                        progress((k + done / total) / len(paths))
        except (OSError, RuntimeError):
            skipped += 1  # a file that won't open (or isn't a format the player decodes)
            continue
        if not done:
            skipped += 1  # nothing in it could be decoded
            continue
        partial += bool(damaged)
        gathered.append(meter.powers.copy())
        dr = dr_of(rms_blocks, peak_blocks)
        if dr is not None:
            drs.append(dr)
    joined = np.concatenate(gathered) if gathered else np.zeros(0)
    lufs, lra = (integrated(joined), loudness_range(joined)) if gathered else (None, None)
    rounded = [round(d) for d in drs]
    return AlbumResult(lufs, lra, round(sum(drs) / len(drs)) if drs else None,
                       min(rounded) if rounded else None, max(rounded) if rounded else None,
                       len(gathered), skipped, partial)

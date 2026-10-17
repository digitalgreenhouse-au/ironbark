"""Hardware test: test signals, and the maths that measures what comes back. No UI code.

The idea: play a signal whose every sample is known to an output, record it again (from a
recording input wired to the DAC's analogue output, or from Windows' own "loopback" recording
of the output), and see what the chain added. Three signals, each one exactly periodic, so it
loops forever without a join and the analysis can use whole periods:

- **Tone**: a 1 kHz sine, one second per period (a whole number of cycles at any rate). Measured:
  **THD+N** (total harmonic distortion plus noise — everything that isn't the tone, as a share
  of the whole), THD (the harmonics 2–10 alone) and the **noise floor** (what's left once the tone
  and its harmonics are taken out), over 20 Hz – 20 kHz.
- **J-test** (Julian Dunn, Prism Sound, mid-1990s): a tone at exactly a quarter of the sample
  rate (Fs/4) plus an undithered square wave one LSB (least significant bit) high at Fs/192.
  The square wave flips every bit of the sample at once (0x4000 ↔ 0x3FFF), the worst case for a
  digital link's clock — any jitter (timing wobble) it causes shows up as **sidebands**: extra
  lines at Fs/4 ± odd multiples of Fs/192. Measured: each sideband's level relative to the tone
  (dBc), against what the perfect signal itself has there.
- **Sweep**: an exponential sine sweep (10 Hz to just below half the sample rate) followed by
  silence. Measured: the **frequency response** in 1/6-octave bands, relative to 1 kHz.

Sources (checked 04/10/2026, not from memory):
- J-test: "a high-level tone at exactly one-quarter the sample frequency, or Fs/4, to which is
  added a squarewave at the level of the least significant bit (LSB), at Fs/192" (Stereophile's
  measurement notes); the exact sample pattern — C00000 C00000 400000 400000 (×24) then
  BFFFFF BFFFFF 3FFFFF 3FFFFF (×24), a 192-sample cycle — and REW's help: "a square wave at one
  quarter of the sample rate at half full scale amplitude … modulated by a square wave at 1/192
  of the sample rate that alternates between 0 and -1 lsb". Half full scale at Fs/4 sampled at
  45° = a sine peaking at 0.707 = −3.01 dBFS. Audio Precision's own J-test technote (118) is
  behind a login and wasn't read.
- THD+N, THD (Audio Precision, "THD and THD+N: similar but not the same"): "The THD+N Ratio is
  calculated as the bandwidth limited rms level of the residual divided by the rms level of the
  entire signal"; THD = the RSS (root of the sum of squares) of the harmonics ÷ the fundamental.
  The 20 kHz limit is AES17's usual band. The notch here is the FFT's: the bins of the tone's
  main lobe (±10 bins = ±10 Hz) are left out — a "brick-wall" notch, not AES17-2015's
  standard notch filter, so figures can differ slightly from an analyser using that one.
- Jitter from a sideband: a tone f0 whose timing wobbles sinusoidally by ±J seconds is phase-
  modulated by β = 2π·f0·J radians, and for small β each sideband is β/2 of the tone. So
  J = r / (π·f0), r = sideband ÷ tone (amplitudes). Textbook maths, checked by the tests on a
  synthetic jittered tone; shown as "equivalent jitter" since a sideband can also have other
  causes.

dBFS here means relative to a full-scale sine (AES17), so a sine peaking at full scale reads
0 dBFS. Spectra use a Kaiser window (β 20, as spectrum.py), so a hardware recording whose clock
isn't exactly the player's still measures cleanly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

LEVEL_DB = -20.0                 # the default test level (dBFS)
LEVEL_CHOICES = (-80.0, -60.0, -40.0, -30.0, -20.0, -12.0, -6.0, -3.0)
TONE_HZ = 1000.0
BAND = (20.0, 20000.0)           # the measurement band (AES17's usual 20 kHz limit)
HARMONICS = range(2, 11)         # THD: the 2nd to the 10th
KAISER_BETA = 20.0
LOBE = 10                        # bins either side of a line that belong to it (main lobe ±6.4 at β 20)
JTEST_CYCLE = 192                # the J-test's square wave: one period every 192 samples
JTEST_FRAMES = JTEST_CYCLE * 256 # analysis length: Fs/4 and every multiple of Fs/192 land on a bin
JTEST_ORDERS = (1, 3, 5, 7, 9, 11)   # sidebands at Fs/4 ± these multiples of Fs/192
JTEST_BITS = (16, 24)
JTEST_TOP = 20 * math.log10(math.sqrt(2) / 2)   # the J-test at half scale: a sine peaking at -3.01 dBFS
SEEN_DBC = -160.0                # below this the window's own skirt hides things: nothing "found" there
SWEEP_LOW = 10.0
SWEEP_SECONDS = 3.0
SWEEP_SILENCE = 0.5              # after the sweep, so each period ends quietly
SWEEP_FADE = 0.05                # raised-cosine fade at each end of the sweep
BANDS_PER_OCTAVE = 6
SETTLE_S = 0.6                   # recording thrown away at the start (the sound arriving, fades)
SPAN = {"tone": 4, "jtest": 4, "sweep": 1}   # periods analysed
FULL_SINE_POWER = 0.5            # mean square of a full-scale sine: 0 dBFS
QUIET_DB = -200.0                # what "nothing at all" reads as


def db(power_ratio: float) -> float:
    return 10 * math.log10(power_ratio) if power_ratio > 0 else QUIET_DB


# -- the signals ---------------------------------------------------------------------------------
@dataclass
class TestSignal:
    kind: str                    # "tone", "jtest", "sweep"
    rate: int
    period: np.ndarray           # one period, frames (mono; both channels play the same)
    level_db: float              # the tone's / sweep's peak level, dBFS (J-test: as built, 6 dB steps)
    frequency: float = 0.0       # tone: its frequency; J-test: Fs/4; sweep: where it starts
    top: float = 0.0             # sweep: where it ends
    bits: int | None = None      # J-test: 16 or 24

    @property
    def frames(self) -> int:
        return len(self.period)

    def stereo(self) -> np.ndarray:
        return np.repeat(self.period[:, None], 2, axis=1)

    def seconds_needed(self) -> float:
        """How long to record: the settle time, the periods analysed, and a little spare."""
        return SETTLE_S + SPAN[self.kind] * analysis_frames(self) / self.rate + 0.4

    @property
    def name(self) -> str:
        if self.kind == "tone":
            return f"{_hz(self.frequency)} tone at {self.level_db:.1f} dBFS"
        if self.kind == "jtest":
            return (f"J-test, {self.bits}-bit: {self.frequency / 1000:g} kHz tone at {self.level_db:.1f} dBFS "
                    f"+ 1-LSB square wave at {self.rate / JTEST_CYCLE:g} Hz")
        return f"Sweep {_hz(self.frequency)} – {_hz(self.top)} at {self.level_db:.1f} dBFS"


def analysis_frames(signal: TestSignal) -> int:
    """The FFT length used on the recording: one second for the tone (1 Hz bins), JTEST_FRAMES
    for the J-test, one whole period for the sweep."""
    return {"tone": signal.rate, "jtest": JTEST_FRAMES}.get(signal.kind, signal.frames)


def tone(rate: int, level_db: float = LEVEL_DB, frequency: float = TONE_HZ) -> TestSignal:
    """A sine with a whole number of cycles in one second, so it loops without a join."""
    cycles = max(1, round(frequency))                     # per second (1 s period)
    n = np.arange(rate)
    period = 10 ** (level_db / 20) * np.sin(2 * np.pi * cycles * n / rate)
    return TestSignal("tone", rate, period, level_db, float(cycles))


def jtest_level(level_db: float) -> tuple[int, float]:
    """The J-test's tone is a square at Fs/4 of ±2^-k of full scale, so that adding −1 LSB flips
    every bit below it (0x4000 → 0x3FFF at k = 1). Returns k for the loudest level not above
    `level_db`, and the real level: 2^-k × √2 as a sine (k = 1: −3.01 dBFS; steps of 6.02 dB)."""
    k = max(1, math.ceil((JTEST_TOP - level_db) / (20 * math.log10(2)) + 1 - 1e-6))
    return k, 20 * math.log10(math.sqrt(2) * 2.0 ** -k)


def jtest(rate: int, bits: int = 16, level_db: float = LEVEL_DB) -> TestSignal:
    """Julian Dunn's J-test, undithered, in `bits`-bit steps: [-A, -A, +A, +A] repeating (Fs/4),
    plus 0 for 96 samples then −1 LSB for 96 (Fs/192). With A = half scale that's exactly the
    published C000 C000 4000 4000 … BFFF BFFF 3FFF 3FFF cycle (16-bit)."""
    if bits not in JTEST_BITS:
        raise ValueError("the J-test is built at 16 or 24 bits")
    k, actual = jtest_level(level_db)
    if k > bits - 2:
        raise ValueError("too quiet for a J-test at this bit depth")
    a = 2 ** (bits - 1 - k)                               # the Fs/4 square's height, in LSBs
    n = np.arange(JTEST_FRAMES)
    values = np.where(n % 4 < 2, -a, a) - ((n % JTEST_CYCLE) >= JTEST_CYCLE // 2)
    period = values / 2.0 ** (bits - 1)                   # exact in float32 too (≤ 24 significant bits)
    return TestSignal("jtest", rate, period, actual, rate / 4, bits=bits)


def sweep(rate: int, level_db: float = LEVEL_DB, low: float = SWEEP_LOW, high: float | None = None,
          seconds: float = SWEEP_SECONDS) -> TestSignal:
    """An exponential sine sweep (Farina) from `low` to `high`, faded in and out, then silence."""
    high = high or 0.49 * rate
    frames = int(round(seconds * rate))
    t = np.arange(frames) / rate
    rise = seconds / math.log(high / low)
    x = np.sin(2 * np.pi * low * rise * (np.exp(t / rise) - 1))
    fade = int(SWEEP_FADE * rate)
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(fade) / fade)
    x[:fade] *= ramp
    x[-fade:] *= ramp[::-1]
    period = np.concatenate([x, np.zeros(int(round(SWEEP_SILENCE * rate)))]) * 10 ** (level_db / 20)
    return TestSignal("sweep", rate, period, level_db, low, top=high)


def make(kind: str, rate: int, level_db: float = LEVEL_DB, bits: int = 16) -> TestSignal:
    if kind == "tone":
        return tone(rate, level_db)
    if kind == "jtest":
        return jtest(rate, bits, level_db)
    if kind == "sweep":
        return sweep(rate, level_db)
    raise ValueError(kind)


# -- spectra ---------------------------------------------------------------------------------------
@lru_cache(maxsize=8)
def _window(size: int) -> np.ndarray:
    return np.kaiser(size, KAISER_BETA)


def power_spectrum(x: np.ndarray, size: int) -> np.ndarray:
    """Mean-square power per bin (one-sided), averaged over the whole `size`-frame segments of x.
    Scaled so the bins of a sine's main lobe add up to its mean square (A²/2), and all bins add
    up to the signal's mean square — so powers can simply be summed over a band."""
    x = np.asarray(x, dtype=np.float64)
    segments = len(x) // size
    if segments < 1:
        raise ValueError("the recording is shorter than one analysis length")
    w = _window(size)
    blocks = x[: segments * size].reshape(segments, size) * w
    power = np.mean(np.abs(np.fft.rfft(blocks, axis=1)) ** 2, axis=0)
    power *= 2.0 / (size * np.sum(w * w))
    power[0] /= 2
    if size % 2 == 0:
        power[-1] /= 2
    return power


def _enbw(size: int) -> float:
    """The window's equivalent noise bandwidth in bins: per-bin power × this = a sine's level at
    its centre bin (how analysers draw a spectrum: a −80 dBFS tone peaks at −80)."""
    w = _window(size)
    return size * float(np.sum(w * w)) / float(np.sum(w)) ** 2


def _log_points(power: np.ndarray, bin_hz: float, lo: float, hi: float, points: int = 480):
    """(Hz, dBFS) for a picture: the loudest bin in each of `points` log-spaced slices."""
    edges = np.unique(np.clip(np.round(np.geomspace(lo, hi, points + 1) / bin_hz).astype(int), 1, len(power) - 1))
    peaks = np.maximum.reduceat(power, edges[:-1])[: len(edges) - 1]
    centres = np.sqrt(edges[:-1] * np.maximum(edges[1:] - 1, edges[:-1])) * bin_hz   # each slice's middle (log)
    return centres, 10 * np.log10(np.maximum(peaks, 1e-30) / FULL_SINE_POWER)


def _linear_points(power: np.ndarray, start_hz: float, bin_hz: float, points: int = 480):
    """(Hz, dBFS) for a picture on a linear frequency scale: the loudest bin per slice."""
    step = max(1, len(power) // points)
    usable = len(power) // step * step
    peaks = power[:usable].reshape(-1, step).max(axis=1)
    return start_hz + np.arange(len(peaks)) * step * bin_hz, 10 * np.log10(np.maximum(peaks, 1e-30) / FULL_SINE_POWER)


def _lobe(power: np.ndarray, k: int, width: int = LOBE) -> float:
    if k < 0 or k >= len(power):
        return 0.0
    return float(np.sum(power[max(0, k - width): k + width + 1]))


def _peak_bin(power: np.ndarray, near: float, bin_hz: float, spread: float = 0.02) -> int:
    """The strongest bin within ±2% of `near` (a hardware clock may be slightly off)."""
    lo = max(1, int((near * (1 - spread)) / bin_hz))
    hi = min(len(power) - 1, int((near * (1 + spread)) / bin_hz) + 1)
    return lo + int(np.argmax(power[lo: hi + 1]))


def _top(rate: int) -> float:
    return min(BAND[1], 0.5 * rate * 0.98)


# -- tone: THD+N, THD, noise floor ----------------------------------------------------------------
@dataclass
class ToneResult:
    frequency: float             # measured (power-weighted centre of the tone's lobe)
    level_db: float              # the tone, dBFS
    thd_n_db: float              # THD+N ratio, dB (everything but the tone ÷ everything, in the band)
    thd_db: float                # THD ratio, dB (harmonics ÷ tone)
    noise_db: float              # the noise floor, dBFS: the band without the tone and its harmonics
    harmonics: list[tuple[int, float, float]]   # (n, Hz, dBc)
    band: tuple[float, float]
    peak: float                  # the recording's highest sample (1.0 = full scale)
    spectrum: tuple | None = None   # (Hz, dBFS) to draw, log frequency

    @property
    def thd_n_percent(self) -> float:
        return 100 * 10 ** (self.thd_n_db / 20)

    @property
    def silent(self) -> bool:
        return self.level_db < -150


def analyse_tone(x: np.ndarray, rate: int, frequency: float = TONE_HZ, size: int | None = None) -> ToneResult:
    """THD+N, THD and noise floor of a recorded tone (one channel)."""
    size = size or rate
    power = power_spectrum(x, size)
    bin_hz = rate / size
    lo, hi = BAND[0], _top(rate)
    band = slice(int(math.ceil(lo / bin_hz)), int(hi / bin_hz) + 1)
    total = float(np.sum(power[band]))
    k0 = _peak_bin(power, frequency, bin_hz)
    first = max(0, k0 - LOBE)
    lobe = power[first: k0 + LOBE + 1]
    fundamental = float(np.sum(lobe))
    measured = float(np.sum(lobe * np.arange(first, first + len(lobe))) / fundamental) * bin_hz if fundamental else 0.0
    # What's left is added up bin by bin, never taken as a difference of two near-equal sums: that
    # would be rounding noise (one unit in the last place of the total reads as about -157 dB).
    rest = np.zeros(len(power), dtype=bool)
    rest[band] = True
    rest[first: k0 + LOBE + 1] = False
    residual = float(np.sum(power[rest]))
    harmonics, harmonic_power = [], 0.0
    for n in HARMONICS:
        f = n * (measured or frequency)
        if f > hi:
            break
        k = int(round(f / bin_hz))
        p = _lobe(power, k)
        harmonic_power += p
        rest[max(0, k - LOBE): k + LOBE + 1] = False
        harmonics.append((n, f, db(p / fundamental) if fundamental else QUIET_DB))
    noise = float(np.sum(power[rest]))
    return ToneResult(
        frequency=measured, level_db=db(fundamental / FULL_SINE_POWER),
        thd_n_db=db(residual / total) if total else QUIET_DB,
        thd_db=db(harmonic_power / fundamental) if fundamental else QUIET_DB,
        noise_db=db(noise / FULL_SINE_POWER), harmonics=harmonics, band=(lo, hi),
        peak=float(np.max(np.abs(x))) if len(x) else 0.0,
        spectrum=_log_points(power * _enbw(size), bin_hz, lo, 0.5 * rate))


# -- J-test: sidebands -----------------------------------------------------------------------------
def jitter_ps(dbc: float, f0: float) -> float:
    """The peak sinusoidal jitter (picoseconds) that would put a sideband pair `dbc` below a tone
    at f0: J = r / (π f0), r = 10^(dBc/20)."""
    return 10 ** (dbc / 20) / (math.pi * f0) * 1e12


def from_jitter_ps(ps: float, f0: float) -> float:
    """The other way round: each sideband's level (dBc) for ±`ps` of sinusoidal jitter."""
    return 20 * math.log10(math.pi * f0 * ps * 1e-12)


@dataclass
class Sideband:
    order: int                   # k: at Fs/4 ± k·Fs/192
    offset: float                # Hz either side of the tone
    dbc: float                   # the louder of the pair, relative to the tone
    own_dbc: float               # what the perfect J-test itself has there
    found: bool                  # clearly above the noise floor
    added: bool                  # found, and more than 1 dB above the signal's own


@dataclass
class JTestResult:
    tone_hz: float
    level_db: float              # the Fs/4 tone, dBFS
    sidebands: list[Sideband]
    floor_dbc: float             # the noise floor in one line's width, relative to the tone
    spur_dbc: float              # the loudest other line near the tone (± 12·Fs/192), dBc; QUIET_DB if none
    spur_offset: float           # where it is (Hz from the tone)
    peak: float
    spectrum: tuple | None = None   # (Hz, dBFS) to draw: the tone ± 12·Fs/192, linear frequency

    @property
    def worst(self) -> Sideband | None:
        added = [s for s in self.sidebands if s.added]
        return max(added, key=lambda s: s.dbc) if added else None

    @property
    def silent(self) -> bool:
        return self.level_db < -150


def _sideband_levels(x: np.ndarray, rate: int):
    """(tone power, tone Hz, {k: (lower, upper) power}, sliding lobe powers, tone bin, power per
    bin) of a J-test."""
    power = power_spectrum(x, JTEST_FRAMES)
    bin_hz = rate / JTEST_FRAMES
    k0 = _peak_bin(power, rate / 4, bin_hz, spread=0.002)
    tone_power = _lobe(power, k0)
    step = JTEST_FRAMES // JTEST_CYCLE                    # bins between multiples of Fs/192
    levels = {k: (_lobe(power, k0 - k * step), _lobe(power, k0 + k * step)) for k in JTEST_ORDERS}
    sliding = np.convolve(power, np.ones(2 * LOBE + 1), mode="same")
    return tone_power, k0 * bin_hz, levels, sliding, k0, power


@lru_cache(maxsize=8)
def _own_sidebands(rate: int, bits: int, level_db: float) -> dict[int, float]:
    """The perfect J-test's own lines at the sideband places (dBc): the LSB square's odd
    harmonics land exactly there (47·Fs/192 = Fs/4 − Fs/192, and so on)."""
    signal = jtest(rate, bits, level_db)
    tone_power, _, levels, _, _, _ = _sideband_levels(np.tile(signal.period, 2), rate)
    return {k: db(max(pair) / tone_power) for k, pair in levels.items()}


def analyse_jtest(x: np.ndarray, rate: int, bits: int = 16, level_db: float = LEVEL_DB) -> JTestResult:
    """The sidebands around the Fs/4 tone of a recorded J-test (one channel)."""
    tone_power, tone_hz, levels, sliding, k0, power = _sideband_levels(x, rate)
    own = _own_sidebands(rate, bits, jtest_level(level_db)[1])
    step = JTEST_FRAMES // JTEST_CYCLE
    bin_hz = rate / JTEST_FRAMES
    # The noise floor near the tone: the median of the sliding line-width power, lines left out.
    region = np.arange(max(1, k0 - 12 * step), min(len(sliding) - 1, k0 + 12 * step))
    keep = np.abs(region - k0) > 3 * LOBE                 # the tone and its skirt
    for line in [k0 + s * k * step for k in range(1, 13) for s in (-1, 1)]:
        keep &= np.abs(region - line) > 2 * LOBE
    floor = float(np.median(sliding[region[keep]])) if keep.any() and tone_power else 0.0
    sidebands = []
    for k in JTEST_ORDERS:
        level = max(levels[k])
        dbc = db(level / tone_power) if tone_power else QUIET_DB
        found = level > 4 * floor and dbc > SEEN_DBC      # 6 dB clear of the noise in the same width
        sidebands.append(Sideband(k, k * rate / JTEST_CYCLE, dbc, own[k], found, found and dbc > own[k] + 1.0))
    others = region[keep]
    if len(others) and tone_power:
        at = int(others[np.argmax(sliding[others])])
        spur = float(sliding[at])
        at = max(0, at - LOBE) + int(np.argmax(power[max(0, at - LOBE): at + LOBE + 1]))   # the line's own bin
        spur_dbc = db(spur / tone_power)
        if spur <= 10 * floor or spur_dbc <= SEEN_DBC:    # needs 10 dB clear of the noise
            spur_dbc = QUIET_DB
        spur_offset = (at - k0) * bin_hz
    else:
        spur_dbc, spur_offset = QUIET_DB, 0.0
    lo, hi = region[0], region[-1]
    picture = _linear_points(power[lo: hi + 1] * _enbw(JTEST_FRAMES), lo * bin_hz, bin_hz)
    return JTestResult(tone_hz, db(tone_power / FULL_SINE_POWER), sidebands,
                       db(floor / tone_power) if tone_power and floor else QUIET_DB, spur_dbc, spur_offset,
                       float(np.max(np.abs(x))) if len(x) else 0.0, picture)


# -- sweep: frequency response ---------------------------------------------------------------------
@dataclass
class Response:
    freqs: np.ndarray            # band centres, Hz (1/6 octave, through 1 kHz)
    db: np.ndarray               # level in each band relative to the 1 kHz band
    gain_db: float               # the 1 kHz band's level: recorded ÷ played
    low_3db: float | None        # where it's 3 dB down below 1 kHz (None: not within the sweep)
    high_3db: float | None       # … and above
    flat: tuple[float, float]    # lowest and highest level, 20 Hz – 20 kHz (or the sweep's top)
    peak: float

    @property
    def silent(self) -> bool:
        return self.gain_db < -120


def _crossing(freqs, levels) -> float | None:
    """Where `levels` first falls to −3 dB walking away from 1 kHz (log-frequency interpolation)."""
    for i in range(1, len(freqs)):
        if levels[i] <= -3.0:
            a, b = levels[i - 1], levels[i]
            share = (a + 3.0) / (a - b) if a != b else 0.0
            return float(freqs[i - 1] * (freqs[i] / freqs[i - 1]) ** share)
    return None


def analyse_sweep(x: np.ndarray, signal: TestSignal) -> Response:
    """Frequency response from a recorded sweep (one channel). The sweep repeats exactly, so any
    whole period of the recording holds every frequency once: its spectrum ÷ the played one's is
    the response (the delay only turns the phase)."""
    n = signal.frames
    if len(x) < n:
        raise ValueError("the recording is shorter than one sweep")
    got = np.abs(np.fft.rfft(np.asarray(x[-n:], dtype=np.float64))) ** 2
    sent = np.abs(np.fft.rfft(signal.period)) ** 2
    bin_hz = signal.rate / n
    high = signal.top / 1.07                              # the top band's upper edge stays inside the sweep
    low = max(BAND[0], signal.frequency * 2)
    steps = np.arange(math.floor(BANDS_PER_OCTAVE * math.log2(low / 1000)),
                      math.floor(BANDS_PER_OCTAVE * math.log2(high / 1000)) + 1)
    centres = 1000 * 2.0 ** (steps / BANDS_PER_OCTAVE)
    centres = centres[(centres >= low) & (centres <= high)]
    half = 2 ** (1 / (2 * BANDS_PER_OCTAVE))
    levels = []
    for fc in centres:
        a, b = int(round(fc / half / bin_hz)), int(round(fc * half / bin_hz)) + 1
        s = float(np.sum(sent[a:b]))
        levels.append(db(float(np.sum(got[a:b])) / s) if s > 0 else QUIET_DB)
    levels = np.array(levels)
    one_k = int(np.argmin(np.abs(centres - 1000)))
    gain = float(levels[one_k])
    relative = levels - gain
    up, down = relative[one_k:], relative[: one_k + 1][::-1]
    low_3 = _crossing(centres[: one_k + 1][::-1], down)
    high_3 = _crossing(centres[one_k:], up)
    inside = relative[(centres >= BAND[0]) & (centres <= BAND[1])]
    return Response(centres, relative, gain, low_3, high_3,
                    (float(inside.min()), float(inside.max())) if len(inside) else (0.0, 0.0),
                    float(np.max(np.abs(x))) if len(x) else 0.0)


# -- one measurement, both channels ----------------------------------------------------------------
@dataclass
class Measurement:
    signal: TestSignal
    rate: int                    # the recording's rate
    channels: list = field(default_factory=list)   # a ToneResult / JTestResult / Response per channel
    notes: list[str] = field(default_factory=list)


def measure(signal: TestSignal, recording: np.ndarray, rate: int) -> Measurement:
    """Analyse a recording (frames × channels, full scale = 1.0) of `signal` played on a loop."""
    result = Measurement(signal, rate)
    audio = np.asarray(recording, dtype=np.float64)
    audio = audio[:, None] if audio.ndim == 1 else audio
    if rate != signal.rate:
        result.notes.append(f"Recorded at {rate / 1000:g} kHz but played at {signal.rate / 1000:g} kHz — "
                            "the rates must match")
        return result
    size = analysis_frames(signal)
    usable = audio[int(SETTLE_S * rate):]
    span = min(SPAN[signal.kind], len(usable) // size)
    if span < 1:
        result.notes.append("The recording was too short to measure")
        return result
    usable = usable[len(usable) - span * size:]          # the latest whole periods
    for channel in range(min(2, audio.shape[1])):
        x = usable[:, channel]
        if signal.kind == "tone":
            result.channels.append(analyse_tone(x, rate, signal.frequency))
        elif signal.kind == "jtest":
            result.channels.append(analyse_jtest(x, rate, signal.bits, signal.level_db))
        else:
            result.channels.append(analyse_sweep(x, signal))
    return result


# -- wording ---------------------------------------------------------------------------------------
def _per_channel(values: list[str]) -> str:
    if len(values) == 2 and values[0] == values[1]:
        return f"{values[0]} (both channels)"
    return " · ".join(f"{side} {v}" for side, v in zip(("L", "R"), values)) if len(values) > 1 else values[0]


def _hz(f: float | None) -> str:
    if f is None:
        return "—"
    return f"{f / 1000:.3g} kHz" if f >= 1000 else f"{f:.0f} Hz"


def describe(m: Measurement) -> list[tuple[str, str, str]]:
    """(label, text, tone) rows; tone "good", "warn" or "plain"."""
    rows = [("Signal", m.signal.name, "plain")]
    if not m.channels:
        return rows + [("Result", "; ".join(m.notes) or "Nothing measured", "warn")]
    if all(c.silent for c in m.channels):
        return rows + [("Result", "Nothing came back: check the input is connected and turned up (and the "
                                  "output isn't muted)", "warn")]
    if any(c.peak >= 0.999 for c in m.channels):
        rows.append(("Recording", "Reached full scale: the input clipped — turn the recording level down",
                     "warn"))
    if m.signal.kind == "tone":
        thdn = [c.thd_n_db for c in m.channels]
        rows.append(("THD+N", _per_channel([f"{v:.1f} dB ({100 * 10 ** (v / 20):.4g}%)" for v in thdn]),
                     "good" if max(thdn) <= -90 else "plain"))
        rows.append(("THD", _per_channel([f"{c.thd_db:.1f} dB" for c in m.channels]), "plain"))
        rows.append(("Noise floor", _per_channel([_floor(c.noise_db, "dBFS") for c in m.channels]) +
                     f" ({_hz(m.channels[0].band[0])}–{_hz(m.channels[0].band[1])})", "plain"))
        rows.append(("Tone back", _per_channel([f"{c.level_db:.2f} dBFS at {c.frequency:.2f} Hz"
                                                for c in m.channels]), "plain"))
        worst = max(m.channels, key=lambda c: max((h[2] for h in c.harmonics), default=QUIET_DB))
        if worst.harmonics:
            top3 = sorted(worst.harmonics, key=lambda h: -h[2])[:3]
            rows.append(("Harmonics", ", ".join(f"{_ordinal(n)} {d:.1f} dBc" for n, _, d in top3), "plain"))
    elif m.signal.kind == "jtest":
        added = [c.worst for c in m.channels if c.worst is not None]
        if added:
            worst = max(added, key=lambda s: s.dbc)
            ps = jitter_ps(worst.dbc, m.channels[0].tone_hz)
            rows.append(("Sidebands", f"Jitter sidebands up to {worst.dbc:.1f} dBc at ±{worst.offset:.1f} Hz "
                                      f"(the signal's own: {worst.own_dbc:.1f} dBc) — about {_ps(ps)} of jitter",
                         "warn"))
        else:
            rows.append(("Sidebands", "✓ Nothing added: every sideband at Fs/4 ± odd multiples of Fs/192 is at "
                                      "the signal's own level or below the noise", "good"))
        for side, c in zip(("Lines L", "Lines R"), m.channels):
            lines = [f"±{s.offset:.0f} Hz {s.dbc:.1f}" for s in c.sidebands[:3]]
            own = "" if c.worst is not None else " — the signal's own"
            rows.append((side, " · ".join(lines) + " dBc" + own, "plain"))
        spurs = [c for c in m.channels if c.spur_dbc > QUIET_DB]
        if spurs:
            c = max(spurs, key=lambda c: c.spur_dbc)
            rows.append(("Other lines", f"{c.spur_dbc:.1f} dBc at {c.spur_offset:+.1f} Hz from the tone", "warn"))
        rows.append(("Noise floor", _per_channel([_floor(c.floor_dbc, "dBc") for c in m.channels]) +
                     " (in one line's width)", "plain"))
        rows.append(("Tone back", _per_channel([f"{c.level_db:.2f} dBFS" for c in m.channels]), "plain"))
    else:
        flat = [c.flat for c in m.channels]
        spread = max(hi for _, hi in flat) - min(lo for lo, _ in flat)
        top = min(BAND[1], m.channels[0].freqs[-1])
        rows.append(("Response", _per_channel([f"{round(lo, 2) or 0.0:+.2f} / {round(hi, 2) or 0.0:+.2f} dB"
                                               for lo, hi in flat]) +
                     f" from 20 Hz to {_hz(top)}, against 1 kHz", "good" if spread <= 0.2 else "plain"))
        rows.append(("−3 dB at", _per_channel([_corners(c) for c in m.channels]), "plain"))
        rows.append(("Gain", _per_channel([f"{c.gain_db:+.2f} dB at 1 kHz" for c in m.channels]), "plain"))
    for note in m.notes:
        rows.append(("Note", note, "plain"))
    return rows


def _floor(value: float, unit: str) -> str:
    return f"{value:.1f} {unit}" if value > -180 else f"below -180 {unit} (nothing measurable)"


def _corners(c: Response) -> str:
    if c.low_3db is None and c.high_3db is None:
        return f"not reached ({_hz(c.freqs[0])} – {_hz(c.freqs[-1])} measured)"
    low = _hz(c.low_3db) if c.low_3db else f"below {_hz(c.freqs[0])}"
    high = _hz(c.high_3db) if c.high_3db else f"above {_hz(c.freqs[-1])}"
    return f"{low} and {high}"


def _ps(ps: float) -> str:
    return f"{ps / 1000:.2f} ns" if ps >= 1000 else f"{ps:.1f} ps"


def _ordinal(n: int) -> str:
    return f"{n}{'tsnrhtdd'[(n // 10 % 10 != 1) * (n % 10 < 4) * n % 10::4]}"


def plot_points(m: Measurement, channel: int = 0):
    """(Hz, dB, log scale?) to draw: the recording's spectrum in dBFS (tone: log frequency;
    J-test: linear, around the tone) or the response relative to 1 kHz (sweep). None if there's
    nothing to draw."""
    if not m.channels or channel >= len(m.channels):
        return None
    c = m.channels[channel]
    if isinstance(c, Response):
        return c.freqs, c.db, True
    if c.spectrum is None:
        return None
    return c.spectrum[0], c.spectrum[1], isinstance(c, ToneResult)

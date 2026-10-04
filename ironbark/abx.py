"""The ABX blind test: can you really hear the difference between two versions? No UI code.

How an ABX test works: you can listen to A and B as much as you like, knowing which is which.
X is secretly one of them, chosen at random for each trial; you say which. Guessing gets about
half right, so a score only means something once it's clearly better than chance — the
p-value says how often pure guessing would do at least that well.

- `prepare` decodes the same stretch (the "excerpt") of both files into memory and lines B up
  with A to the sample (compare.find_lag, the Compare window's own code). If the sample rates
  differ, the lower-rate one is upsampled to the higher rate so both play on one stream — up,
  never down, so nothing above the lower rate's range is lost from the higher one. Levels are
  measured as integrated loudness (EBU R128 / ITU-R BS.1770, loudness.py): a level difference
  of more than 0.1 dB is flagged, since a level difference alone is audible and gives the game
  away (the louder one tends to sound "better").
- `AbxTest`: the number of trials is fixed before the first one, and every X is drawn up front,
  so you can't stop on a lucky streak; the score stays hidden until the last answer.
- `p_value`: the one-sided binomial tail, P(at least that many right by guessing, 50/50 each).
  Checked against published tables (see tests/test_abx.py).
- The result is saved as a plain text log in %APPDATA%\\Ironbark\\ABX\\.

The playing itself (looping, switching at the same sample, the identical dip) is audition.py.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from . import APP_NAME, __version__, audiofile, compare, loudness, settings
from .proof import resample

TRIAL_CHOICES = (8, 10, 12, 16, 20, 25)
TRIALS = 16
LENGTH_CHOICES = (5, 10, 15, 20, 30)   # excerpt lengths offered, seconds
LENGTH = 10
MAX_LENGTH_S = 30.0       # both versions are held in memory: keep it modest
MARGIN_S = 2.0            # how far B may be out of step with A and still be lined up
LEVEL_WARN_DB = 0.1       # flag level differences above this (audible on its own)
SIGNIFICANT = 0.05        # the usual bar: under 1 chance in 20 of guessing this well


def p_value(correct: int, trials: int) -> float:
    """P(at least `correct` of `trials` right by guessing): Σ C(n, k) / 2^n for k ≥ correct."""
    if trials <= 0:
        return 1.0
    correct = max(0, min(correct, trials))
    return sum(math.comb(trials, k) for k in range(correct, trials + 1)) / 2 ** trials


def point_probability(k: int, trials: int) -> float:
    """P(exactly k right by guessing) — what the printed binomial tables list."""
    return math.comb(trials, k) / 2 ** trials if 0 <= k <= trials else 0.0


def needed(trials: int, bar: float = SIGNIFICANT) -> int:
    """The fewest right answers that beat the bar (e.g. 12 of 16)."""
    return next((k for k in range(trials + 1) if p_value(k, trials) <= bar), trials + 1)


# -- the two excerpts -------------------------------------------------------------------------
@dataclass
class Pair:
    """The same stretch of both files, in memory, lined up and ready to play."""
    path_a: Path
    path_b: Path
    a: np.ndarray            # frames x channels, float32, full scale = 1.0
    b: np.ndarray
    rate: int
    start_s: float           # where the excerpt starts in A
    offset_s: float = 0.0    # B is this much later than A (lined up already)
    lined_up: bool = True    # False: no match found; played from the same time instead
    level_a: float | None = None   # integrated loudness (LUFS), or RMS dBFS when too short/quiet
    level_b: float | None = None
    level_unit: str = "LUFS"
    notes: list[str] = field(default_factory=list)

    @property
    def channels(self) -> int:
        return self.a.shape[1]

    @property
    def seconds(self) -> float:
        return len(self.a) / self.rate

    @property
    def difference_db(self) -> float:
        """How much louder B is than A (dB); 0 when it can't be measured."""
        if self.level_a is None or self.level_b is None:
            return 0.0
        return self.level_b - self.level_a

    @property
    def levels_match(self) -> bool:
        return abs(self.difference_db) <= LEVEL_WARN_DB


def _level(x: np.ndarray, rate: int) -> tuple[float | None, str]:
    meter = loudness.Loudness(rate, x.shape[1])
    meter.add(x)
    value = loudness.integrated(meter.powers)
    if value is not None and math.isfinite(value):
        return float(value), "LUFS"
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return (20 * math.log10(rms) if rms > 0 else None), "dB RMS"


def _to_channels(x: np.ndarray, channels: int) -> np.ndarray:
    return np.repeat(x, channels, axis=1) if x.shape[1] == 1 and channels > 1 else x


def prepare(path_a, path_b, start_s: float, length_s: float = LENGTH,
            cancel: threading.Event | None = None) -> Pair:
    """Decode `length_s` seconds of A from `start_s`, and the matching stretch of B, lined up."""
    a_info, b_info = compare.info(path_a), compare.info(path_b)
    if a_info.channels != b_info.channels and 1 not in (a_info.channels, b_info.channels):
        raise ValueError(f"A has {a_info.channels} channels and B {b_info.channels}: they can't be "
                         "compared by ear on one stream")
    channels = max(a_info.channels, b_info.channels)
    rate = max(a_info.rate, b_info.rate)
    length_s = max(1.0, min(float(length_s), MAX_LENGTH_S, a_info.seconds))
    start_s = max(0.0, min(float(start_s), a_info.seconds - length_s))
    notes = []
    with audiofile.open(path_a) as fa, audiofile.open(path_b) as fb:
        # Both read with a margin either side: resampling rings at an excerpt's hard ends, and B
        # may sit a little earlier or later than A.
        def stretch(f: sf.SoundFile, info: compare.FileInfo) -> np.ndarray:
            margin, n = int(MARGIN_S * info.rate), int(round(length_s * info.rate))
            x = compare._read(f, int(round(start_s * info.rate)) - margin, n + 2 * margin)
            if info.rate != rate:
                x = resample(x, info.rate, rate)
                notes.append(f"{'A' if f is fa else 'B'} was upsampled {info.rate / 1000:g} → {rate / 1000:g} kHz "
                             "to play on one stream with the other")
            return _to_channels(x.astype(np.float32), channels)

        a_region = stretch(fa, a_info)
        if cancel is not None and cancel.is_set():
            raise InterruptedError
        b_region = stretch(fb, b_info)
    margin, n = int(MARGIN_S * rate), int(round(length_s * rate))
    a = a_region[margin: margin + n]
    lag, match = compare.find_lag(compare._mono(a.astype(np.float64)), compare._mono(b_region.astype(np.float64)))
    lined_up = match > 0.5
    if not lined_up:
        lag = margin
        notes.append("B couldn't be lined up with A (a different recording or edit?): both play from the same time")
    b = b_region[lag: lag + n]
    if len(b) < n:
        b = np.concatenate([b, np.zeros((n - len(b), channels), np.float32)])
    pair = Pair(Path(path_a), Path(path_b), np.ascontiguousarray(a), np.ascontiguousarray(b), rate, start_s,
                offset_s=(lag - margin) / rate, lined_up=lined_up, notes=notes)
    pair.level_a, unit_a = _level(pair.a, rate)
    pair.level_b, unit_b = _level(pair.b, rate)
    pair.level_unit = unit_a if unit_a == unit_b else "dB RMS"
    if unit_a != unit_b:
        pair.level_a, pair.level_b = (_level_rms(pair.a), _level_rms(pair.b))
    return pair


def _level_rms(x: np.ndarray) -> float | None:
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return 20 * math.log10(rms) if rms > 0 else None


def describe_levels(pair: Pair, matched: bool = False) -> tuple[str, bool]:
    """(text, fine?) about the two versions' levels. `matched`: the louder one is being turned down."""
    if pair.level_a is None or pair.level_b is None:
        return "Levels: couldn't be measured (silence?)", True
    d = pair.difference_db
    figures = f"A {pair.level_a:.2f}, B {pair.level_b:.2f} {pair.level_unit}"
    if abs(d) <= LEVEL_WARN_DB:
        return f"✓ Levels match: {abs(d):.2f} dB apart ({figures})", True
    louder = "B" if d > 0 else "A"
    if matched:
        return (f"✓ Levels matched: {louder} was {abs(d):.2f} dB louder ({figures}), so it's turned down by that "
                "much while you listen"), True
    return (f"⚠ {louder} is {abs(d):.2f} dB louder ({figures}). A level difference alone is audible "
            "and gives the answer away — match the levels for a fair test"), False


# -- the test ---------------------------------------------------------------------------------
class AbxTest:
    """A fixed number of trials, every X drawn before the first. The score stays hidden until
    the last answer (`score` refuses before then)."""

    def __init__(self, trials: int = TRIALS, rng: random.Random | None = None):
        if trials < 1:
            raise ValueError("at least one trial")
        rng = rng or random.SystemRandom()
        self._secret = [rng.choice("AB") for _ in range(trials)]
        self.guesses: list[str] = []
        self.seconds: list[float] = []    # how long each answer took
        self.started = time.time()
        self.finished: float | None = None
        self._since = time.monotonic()

    @property
    def trials(self) -> int:
        return len(self._secret)

    @property
    def trial(self) -> int:
        """The trial being answered now, 0-based (== trials once done)."""
        return len(self.guesses)

    @property
    def done(self) -> bool:
        return self.trial >= self.trials

    @property
    def x(self) -> str:
        """What X is in this trial — for playing it, never for showing it."""
        if self.done:
            raise RuntimeError("the test is over")
        return self._secret[self.trial]

    def answer(self, guess: str):
        """'A' or 'B'. Says nothing about whether it was right."""
        guess = guess.upper()
        if guess not in ("A", "B"):
            raise ValueError(guess)
        if self.done:
            raise RuntimeError("the test is over")
        now = time.monotonic()
        self.guesses.append(guess)
        self.seconds.append(now - self._since)
        self._since = now
        if self.done:
            self.finished = time.time()

    @property
    def score(self) -> int:
        if not self.done:
            raise RuntimeError("the score stays hidden until the last trial")
        return sum(g == x for g, x in zip(self.guesses, self._secret))

    @property
    def p(self) -> float:
        return p_value(self.score, self.trials)

    def verdict(self) -> tuple[str, bool]:
        """(plain-English result, a difference was heard?)."""
        score, n, p = self.score, self.trials, self.p
        chance = f"{p:.1%}" if p >= 0.001 else "less than 0.1%"
        if p <= SIGNIFICANT:
            return (f"{score} of {n} right. You heard a difference: guessing would do this well only {chance} of "
                    f"the time (p = {p:.4f}; under 0.05 is the usual bar)."), True
        return (f"{score} of {n} right. Not proven: guessing alone would do at least this well {chance} of the "
                f"time (p = {p:.4f}). That doesn't show they sound the same — only that this test didn't show "
                f"a difference ({needed(n)} of {n} would have)."), False

    def rows(self) -> list[tuple[int, str, str, float]]:
        """(trial number, X was, answered, seconds) — only once it's over."""
        if not self.done:
            raise RuntimeError("the answers stay hidden until the last trial")
        return [(i + 1, x, g, s) for i, (x, g, s) in enumerate(zip(self._secret, self.guesses, self.seconds))]


# -- the log ----------------------------------------------------------------------------------
def log_folder() -> Path:
    return settings.settings_path().parent / "ABX"


def log_text(test: AbxTest, pair: Pair, output: str = "", matched: bool = False) -> str:
    when = time.localtime(test.finished or time.time())
    level, _ = describe_levels(pair, matched)
    end = pair.start_s + pair.seconds
    lines = [
        f"{APP_NAME} {__version__} — ABX blind test",
        f"Date: {time.strftime('%d/%m/%Y %H:%M:%S', when)}",
        "",
        f"A: {pair.path_a}",
        f"B: {pair.path_b}",
        f"Excerpt: {compare._clock(pair.start_s)}–{compare._clock(end)} of A ({pair.seconds:.1f} s), "
        f"{pair.rate / 1000:g} kHz, {pair.channels} channel{'s' if pair.channels != 1 else ''}",
        "Lined up: " + (f"B {pair.offset_s * 1000:+.1f} ms against A" if pair.lined_up else "no match found"),
        f"Levels: {level}",
        "Level matching: " + ("on (the louder one turned down)" if matched else "off"),
    ]
    lines += [f"Note: {note}" for note in pair.notes]
    if output:
        lines.append(f"Played on: {output}")
    lines += ["", "Trial  X was  Answered  Right  Seconds"]
    for number, x, guess, seconds in test.rows():
        lines.append(f"{number:5d}  {x:>5}  {guess:>8}  {'yes' if x == guess else 'no':>5}  {seconds:7.1f}")
    verdict, _ = test.verdict()
    lines += ["", f"Score: {test.score} of {test.trials}", f"p-value: {test.p:.6f} (one-sided binomial: the "
              "chance of doing at least this well by guessing)", verdict, ""]
    return "\n".join(lines)


def save_log(text: str, folder: Path | None = None, when: float | None = None) -> Path:
    folder = Path(folder) if folder is not None else log_folder()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when or time.time()))
    path = folder / f"ABX_{stamp}.txt"
    k = 2
    while path.exists():
        path = folder / f"ABX_{stamp}_{k}.txt"
        k += 1
    path.write_text(text, encoding="utf-8")
    return path

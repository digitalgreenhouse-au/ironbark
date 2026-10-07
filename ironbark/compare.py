"""Compare two files: are they the same music, sample for sample? No UI code in here.

1. Sample for sample (both files at the same sample rate and channel count): both are decoded
   and every sample compared — the music, not the file bytes, so a FLAC and a WAV of the same
   rip count as identical, and so does a 16-bit file padded out to 24 bits (the values are the
   same). If they don't line up from the first sample, the offset is found first (cross-
   correlation on whole samples, the way foobar2000's Binary Comparator does it) and the
   comparison runs from there: how many samples differ, the largest difference and the
   difference's level. Samples that match where both have audio aren't called "the same" if
   either file has sound the other hasn't (a copy cut short, say) — only digital silence and a
   moment's difference at the ends are let through.
2. When they differ (or can't be compared sample for sample: another sample rate, say), a
   few seconds are compared the way DeltaWave does, with proof.compare_paths: resampled if
   the rates differ, lined up to a fraction of a sample, the level fitted — so what's left is
   the real change, not just "one is quieter" or "one is a resampled copy".

`find_lag` is shared with the ABX test (abx.py), which lines its two excerpts up the same way.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from . import audiofile
from .proof import QUIET, PathCheck, compare_paths

BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32}
BLOCK = 1 << 16          # frames read at a time
MAX_OFFSET_S = 10.0      # how far apart the two can start and still be lined up (same rate)
MAX_OFFSET_RESAMPLED_S = 2.0  # the same, when the rates differ (only a few seconds are compared then)
PROBE_S = 4.0            # the stretch of A looked for in B, to find the offset
EXCERPT_S = 5.0          # the stretch compared DeltaWave-style when the files differ
PARTIAL_S = 0.5          # unmatched sound at an end longer than this: one file is missing audio
TIE = 1e-6               # correlation peaks this close (relative) match equally well


@dataclass
class FileInfo:
    path: Path
    rate: int
    channels: int
    frames: int
    format: str          # "FLAC", "WAV", "MP3"…
    subtype: str         # "PCM_16", "FLOAT", "MPEG_LAYER_III"…

    @property
    def bits(self) -> int | None:
        """Bits per sample for integer files; None for floating-point and lossy ones."""
        return BITS.get(self.subtype)

    @property
    def seconds(self) -> float:
        return self.frames / self.rate if self.rate else 0.0

    @property
    def describe(self) -> str:
        bits = f"{self.bits}-bit" if self.bits else self.subtype.replace("_", " ").lower()
        rate = f"{self.rate / 1000:g} kHz"
        channels = {1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels} channels")
        return f"{self.format} {bits} / {rate} {channels}, {_clock(self.seconds)}"


def info(path) -> FileInfo:
    with audiofile.open(path) as f:
        # (a CUE track's path is kept as it is, so it's named as the track, not its whole file)
        return FileInfo(path if isinstance(path, Path) else Path(path), f.samplerate, f.channels, f.frames,
                        f.format, f.subtype)


@dataclass
class Comparison:
    """The result of comparing B against A."""
    a: FileInfo
    b: FileInfo
    # "identical", "same_shifted", "partial" (the same where both have audio, but one has more),
    # "nothing_compared" (no overlap), "different", "not_comparable", "cancelled"
    verdict: str = ""
    offset: int = 0                # B's frame number for A's frame 0 (B starts this many frames later)
    offset_found: bool = True      # False: not lined up, so compared from the start
    silent: bool = False           # (when not found) A had no sound to line up on
    missing_from_b: bool = False   # A has sound B hasn't (more than PARTIAL_S at an end)
    extra_in_b: bool = False       # B has sound A hasn't (likewise)
    compared: int = 0              # frames compared (where both have music)
    differing: int = 0             # samples (each channel counts) that differ
    first_difference: int | None = None  # A's frame number of the first difference
    largest: float = 0.0           # largest difference, full scale = 1.0
    difference_db: float = float("-inf")    # RMS of the difference, relative to A's RMS
    difference_dbfs: float = float("-inf")  # RMS of the difference, relative to full scale
    level_db: float = 0.0          # B's level against A's (least-squares fit over everything compared)
    after_level_db: float = float("-inf")   # what's left once that level is matched, relative to A
    inverted: bool = False         # B is A upside down (the fit's gain is negative)
    extra_start: int = 0           # B frames before A's first sample (negative: A has extra)
    extra_end: int = 0             # B frames after A's last sample (negative: A has extra)
    path: PathCheck | None = None  # the DeltaWave-style figure (proof.compare_paths)
    path_at: float = 0.0           # where in A (seconds) that excerpt starts
    notes: list[str] = field(default_factory=list)

    @property
    def same_rate(self) -> bool:
        return self.a.rate == self.b.rate and self.a.channels == self.b.channels


def _clock(seconds: float, millis: bool = False) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:06.3f}" if millis else f"{int(m)}:{int(s):02d}"


def _db(ratio: float) -> float:
    return 20 * math.log10(ratio) if ratio > 0 else float("-inf")


def _mono(x: np.ndarray) -> np.ndarray:
    return x.mean(axis=1) if x.ndim == 2 else x


def _read(f: sf.SoundFile, start: int, frames: int, dtype: str = "float64") -> np.ndarray:
    """frames x channels from `start` (zeros where the file has none: before 0 or past the end)."""
    out = np.zeros((max(0, frames), f.channels), dtype)
    first, last = max(0, start), min(f.frames, start + frames)
    if last > first:
        f.seek(first)
        got = f.read(last - first, dtype=dtype, always_2d=True)
        out[first - start: first - start + len(got)] = got
    return out


def find_lag(probe: np.ndarray, region: np.ndarray, prefer: int | None = None) -> tuple[int, float]:
    """Where `probe` (mono) sits inside `region` (mono, longer): (whole-sample position of its
    first sample, how well it matched 0…1). Cross-correlation by FFT; the sign is ignored, so a
    polarity-flipped copy lines up too. Music that repeats exactly (a loop, a steady tone)
    matches equally well in several places: then the one nearest `prefer` is taken (callers
    pass where "not shifted at all" would be)."""
    n, m = len(probe), len(region)
    if n == 0 or m < n:
        return 0, 0.0
    size = 1 << int(np.ceil(np.log2(n + m)))
    corr = np.fft.irfft(np.fft.rfft(region, size) * np.conj(np.fft.rfft(probe, size)), size)[: m - n + 1]
    strength = np.abs(corr)
    best = int(np.argmax(strength))
    if prefer is not None:
        ties = np.flatnonzero(strength >= strength[best] * (1 - TIE))
        best = int(ties[np.argmin(np.abs(ties - prefer))])
    # How good a match: the correlation at the best spot, normalised by both stretches' energy.
    window = region[best: best + n]
    energy = math.sqrt(float(np.dot(probe, probe)) * float(np.dot(window, window)))
    return best, (abs(float(corr[best])) / energy if energy > 0 else 0.0)


def _probe_starts(frames: int, probe: int) -> list[int]:
    """Where in A to take the probe from: a third of the way in first (past any quiet intro),
    then a few other spots in case that one is silent."""
    room = max(0, frames - probe)
    return sorted({int(room * f) for f in (1 / 3, 1 / 2, 2 / 3, 1 / 6, 5 / 6, 0.0)},
                  key=lambda s: abs(s - room / 3))


def find_offset(fa: sf.SoundFile, fb: sf.SoundFile, max_offset_s: float = MAX_OFFSET_S) -> tuple[int | None, bool]:
    """(B's frame number for A's frame 0 (same sample rate) or None if it can't be found, whether
    A had any sound to line up on). Offsets up to `max_offset_s` either way; when several fit
    equally well (music that repeats exactly), the smallest."""
    rate = fa.samplerate
    probe_len = min(fa.frames, int(PROBE_S * rate))
    reach = int(max_offset_s * rate)
    heard = False
    for start in _probe_starts(fa.frames, probe_len):
        probe = _mono(_read(fa, start, probe_len))
        if np.sqrt(np.mean(probe ** 2)) < QUIET:
            continue  # silence: nothing to match
        heard = True
        region_start = start - reach
        region = _mono(_read(fb, region_start, probe_len + 2 * reach))
        best, match = find_lag(probe, region, prefer=reach)   # region[reach] is "not shifted"
        if match > 0.5:
            return region_start + best - start, True
    return None, heard


def compare_files(path_a, path_b, cancel: threading.Event | None = None, progress=None) -> Comparison:
    """Compare B with A (see the top of this file). `progress(fraction)` is called as it goes."""
    cancel = cancel or threading.Event()
    progress = progress or (lambda fraction: None)
    a, b = info(path_a), info(path_b)
    result = Comparison(a, b)
    with audiofile.open(path_a) as fa, audiofile.open(path_b) as fb:
        if not result.same_rate:
            result.verdict = "not_comparable"
            if a.rate != b.rate:
                result.notes.append(f"Different sample rates ({a.rate / 1000:g} → {b.rate / 1000:g} kHz), so "
                                    "they can't be the same sample for sample; compared as music instead")
            if a.channels != b.channels:
                result.notes.append(f"Different channel counts ({a.channels} and {b.channels}): the first "
                                    f"{min(a.channels, b.channels)} compared")
            _path_check(result, fa, fb, None)
            progress(1.0)
            return result

        offset, heard = find_offset(fa, fb)
        result.offset_found = offset is not None
        result.silent = not heard
        result.offset = offset or 0
        _sample_compare(result, fa, fb, cancel, progress)
        if result.verdict == "cancelled":
            return result
        if result.verdict == "different":
            _path_check(result, fa, fb, result.offset)
    progress(1.0)
    return result


def _sample_compare(result: Comparison, fa: sf.SoundFile, fb: sf.SoundFile, cancel, progress):
    """Every sample of A against B (B shifted by the offset), block by block."""
    d = result.offset
    first, last = max(0, -d), min(fa.frames, fb.frames - d)   # A frames that have a partner in B
    result.extra_start = d
    result.extra_end = (fb.frames - d) - fa.frames
    result.compared = max(0, last - first)
    sum_aa = sum_bb = sum_ab = sum_dd = 0.0
    position = first
    while position < last:
        if cancel.is_set():
            result.verdict = "cancelled"
            return
        n = min(BLOCK, last - position)
        x = _read(fa, position, n)
        y = _read(fb, position + d, n)
        diff = y - x
        differs = diff != 0
        count = int(np.count_nonzero(differs))
        if count:
            if result.first_difference is None:
                result.first_difference = position + int(np.argmax(differs.any(axis=1)))
            result.differing += count
            result.largest = max(result.largest, float(np.max(np.abs(diff))))
        sum_aa += float(np.sum(x * x))
        sum_bb += float(np.sum(y * y))
        sum_ab += float(np.sum(x * y))
        sum_dd += float(np.sum(diff * diff))
        position += n
        progress((position - first) / max(1, last - first))
    samples = max(1, result.compared * fa.channels)
    if result.differing == 0:
        if result.compared == 0:
            result.verdict = "nothing_compared"
            return
        # What each file has beyond the stretch compared, and whether any of it is sound.
        tolerance = int(PARTIAL_S * fa.samplerate)
        result.missing_from_b = any(_has_sound(fa, start, stop, tolerance)
                                    for start, stop in ((0, first), (last, fa.frames)))
        result.extra_in_b = any(_has_sound(fb, start, stop, tolerance)
                                for start, stop in ((0, first + d), (last + d, fb.frames)))
        if result.missing_from_b or result.extra_in_b:
            result.verdict = "partial"
            return
        same_span = d == 0 and result.extra_end == 0
        result.verdict = "identical" if same_span else "same_shifted"
        return
    result.verdict = "different"
    result.difference_dbfs = _db(math.sqrt(sum_dd / samples))
    result.difference_db = _db(math.sqrt(sum_dd / sum_aa)) if sum_aa > 0 else float("-inf")
    if sum_aa > 0:
        gain = sum_ab / sum_aa                        # least-squares level of B against A
        left = max(0.0, sum_bb - 2 * gain * sum_ab + gain * gain * sum_aa)
        result.level_db = _db(abs(gain)) if gain else float("-inf")
        result.inverted = gain < 0
        result.after_level_db = _db(math.sqrt(left / sum_aa))


def _has_sound(f: sf.SoundFile, start: int, stop: int, shortest: int) -> bool:
    """Whether frames start…stop of a file are longer than `shortest` and hold any sound (a
    sample above QUIET; digital silence doesn't count)."""
    if stop - start <= shortest:
        return False
    for position in range(start, stop, BLOCK):
        if np.max(np.abs(_read(f, position, min(BLOCK, stop - position)))) > QUIET:
            return True
    return False


def _path_check(result: Comparison, fa: sf.SoundFile, fb: sf.SoundFile, offset: int | None):
    """A few seconds compared as music (proof.compare_paths): resampling, sub-sample timing and
    level all fitted, so the figure left is what really changed."""
    a_rate, b_rate = fa.samplerate, fb.samplerate
    length = min(fa.frames, int(EXCERPT_S * a_rate))
    margin_s = 0.5 if offset is not None else MAX_OFFSET_RESAMPLED_S
    for start in _probe_starts(fa.frames, length):
        sent = _read(fa, start, length)
        if np.sqrt(np.mean(sent ** 2)) < QUIET:
            continue
        b_start = (start + offset) if offset is not None else int(round(start * b_rate / a_rate))
        margin = int(margin_s * b_rate)
        got_len = int(round(length * b_rate / a_rate)) + 2 * margin
        got = _read(fb, b_start - margin, got_len)
        result.path = compare_paths(sent, a_rate, got, b_rate)
        result.path_at = start / a_rate
        return


# -- wording, for the window -------------------------------------------------------------
def _count(n: int) -> str:
    return f"{n:,}"


def _timing(frames: int, rate: int) -> str:
    ms = 1000 * abs(frames) / rate
    when = f"{ms:.1f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"
    return f"{_count(abs(frames))} sample{'' if abs(frames) == 1 else 's'} ({when})"


def describe(result: Comparison) -> list[tuple[str, str, str]]:
    """(label, text, tone) rows. tone: "good", "warn" or "plain"."""
    a, b = result.a, result.b
    rows = [("A", f"{audiofile.display_name(a.path)} · {a.describe}", "plain"),
            ("B", f"{audiofile.display_name(b.path)} · {b.describe}", "plain")]
    if result.verdict == "cancelled":
        return rows + [("Result", "Stopped", "plain")]
    stored = (a.format, a.subtype) != (b.format, b.subtype)
    if result.verdict == "identical":
        how = (" — stored differently, but the music is the same" if stored else "")
        rows.append(("Result", f"✓ Identical: every sample the same ({_count(result.compared * a.channels)} "
                               f"compared){how}", "good"))
    elif result.verdict == "same_shifted":
        how = "lined up differently" if result.offset else "only the lengths differ"
        rows.append(("Result", f"✓ The same samples, {how} ({_count(result.compared * a.channels)} compared)",
                     "good"))
    elif result.verdict == "partial":
        if result.missing_from_b:
            what = "B is missing some of A's audio" + (", and has audio A hasn't" if result.extra_in_b else "")
        else:
            what = "B has audio A hasn't"
        rows.append(("Result", f"⚠ The same samples where both have audio ({_count(result.compared * a.channels)} "
                               f"compared), but {what}", "warn"))
    elif result.verdict == "nothing_compared":
        rows.append(("Result", "⚠ Nothing to compare: B has no audio alongside A's", "warn"))
    elif result.verdict == "different":
        share = result.differing / max(1, result.compared * a.channels)
        first = (f", the first at {_clock(result.first_difference / a.rate, millis=True)} in A"
                 if result.first_difference is not None else "")
        rows.append(("Result", f"✗ Different: {_count(result.differing)} of {_count(result.compared * a.channels)} "
                               f"samples differ ({share:.2%}){first}", "warn"))
    elif result.verdict == "not_comparable":
        rows.append(("Result", "Can't be identical: " + "; ".join(result.notes), "warn"))

    if result.verdict in ("identical", "same_shifted", "partial", "nothing_compared", "different"):
        if not result.offset_found and result.silent:
            rows.append(("Lined up", "No sound to line up on, so compared from the very start", "plain"))
        elif not result.offset_found:
            rows.append(("Lined up", "A's music couldn't be found in B, so compared from the very start", "warn"))
        elif result.offset:
            later = "later" if result.offset > 0 else "earlier"
            rows.append(("Lined up", f"B starts {_timing(result.offset, a.rate)} {later} than A", "warn"))
        else:
            rows.append(("Lined up", "Both start at the same sample", "plain"))
        ends = []
        for frames, where in ((result.extra_start, "start"), (result.extra_end, "end")):
            if frames > 0:
                ends.append(f"B has {_timing(frames, a.rate)} more at the {where}")
            elif frames < 0:
                ends.append(f"A has {_timing(frames, a.rate)} more at the {where}")
        if ends:
            rows.append(("Length", "; ".join(ends), "warn"))
        if stored and result.verdict != "identical":
            rows.append(("Stored as", f"A {a.format} {a.subtype}, B {b.format} {b.subtype}", "plain"))

    if result.verdict == "different":
        steps = ""
        bits = min(x for x in (a.bits, b.bits, 32) if x)
        if a.bits and b.bits:
            steps = f", {result.largest * 2 ** (bits - 1):,.0f} steps of {bits}-bit"
        rows.append(("Largest", f"{result.largest:.6f} of full scale ({_db(result.largest):.1f} dBFS{steps})",
                     "plain"))
        rows.append(("Difference", f"{result.difference_db:.1f} dB against the music "
                                   f"({result.difference_dbfs:.1f} dBFS RMS)", "warn"))
        if result.inverted:
            rows.append(("Polarity", "B is A upside down (inverted)", "warn"))
        if abs(result.level_db) >= 0.01 and result.after_level_db < result.difference_db - 3:
            louder = "louder" if result.level_db > 0 else "quieter"
            rows.append(("Level", f"B is {abs(result.level_db):.2f} dB {louder}; with the level matched, the "
                                  f"difference is {result.after_level_db:.1f} dB", "warn"))
    if result.path is not None:
        rows += _describe_path(result)
    return rows


def _describe_path(result: Comparison) -> list[tuple[str, str, str]]:
    check = result.path
    where = f"{EXCERPT_S:g} s from {_clock(result.path_at)}"
    if check.verdict == "too_quiet":
        return [("As music", f"Too quiet to compare ({where})", "plain")]
    if check.verdict == "no_match":
        return [("As music", f"B's music couldn't be found lined up with A's ({where}): a different "
                             "recording or edit?", "warn")]
    parts = []
    if check.resampled:
        parts.append(f"resampled {check.resampled[0] / 1000:g} → {check.resampled[1] / 1000:g} kHz, compared "
                     "below 18 kHz")
    if abs(check.level_db) >= 0.01:
        parts.append(f"level {check.level_db:+.2f} dB")
    if check.polarity_flipped:
        parts.append("polarity flipped")
    if check.crosstalk_db > -60:
        parts.append(f"left/right mixed ({check.crosstalk_db:.0f} dB)")
    left = f"{check.difference_db:.1f} dB" if check.difference_db > -200 else "nothing"
    parts.append(f"what's left after fitting: {left}")
    tone = "good" if check.verdict in ("untouched", "resampled_only") else "warn"
    return [("As music", f"{where}, lined up to a fraction of a sample (as DeltaWave does): " + "; ".join(parts),
             tone)]

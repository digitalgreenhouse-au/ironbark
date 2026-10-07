"""When a network song may start, and how fast its source is: pure rules, no I/O, time passed in.

Symbols (seconds unless said):
- S: the file's final size in bytes. H: bytes before the first audio (FLAC metadata, a WAV/AIFF
  header, an MP3's ID3 tag). D: its duration.
- ρ (rho): bytes of file per second of music. Exact for WAV/AIFF; otherwise 1.2 × the average —
  the densest 30 s of real files measured 1.03–1.19 × their average.
- p: where playback starts. L: music left from p to the end.
- A(p): seconds of music from p fully on disk in one unbroken run.
- v: the safe download speed: the smaller of a 10 s moving average and the 20th percentile of
  1-second rates over the last 60 s of transfer (idle time doesn't count).
- m = v / ρ: seconds of music arriving per second of playback.
- gap15: the longest wait in the last 15 minutes with a request outstanding and no bytes coming.
- B0: the "initial buffer" setting (10, 30, 60 or 300 s).

Modes:
- Complete track: only a complete, checked copy plays.
- Fast start: need = min(L, B0).
- Intelligent (the default): bank enough that the download finishes before playback catches up
  (deficit = L × max(0, 1 − m)), plus a reserve for the worst recent outage (1.5 × gap15, 5–120 s),
  and never less than a floor that depends on how good the network is (5 s when excellent, B0/3
  when good, B0 when poor). An unstable network plays the complete track, unless that would take
  over 2 minutes.
- Apple Lossless is always Complete: a short read decodes as loud noise.
A start always needs the decoder's open to find every byte it reads (`open_ready`).

Quality (excellent / good / poor / unstable) drops a class at once and rises only after
QUALITY_RISE_S at the better level (`QualityScore`), so one good minute after an outage doesn't
cut the buffering straight back. After k pauses for the network on a song in the last 5 minutes it
waits for need × 2^k before carrying on (`resume_need`).

Pre-download: "Next track", "Next 2 tracks" or "Entire queue" — songs not heard yet may use at
most half the cache limit (`within_budget`), and when the link can't fetch the rest of this song
and the next before this one ends (`feasible`), only those two are fetched.
"""

from __future__ import annotations

import math
from collections import deque

MIN_START_S = 3.0            # a forced start (play pressed while preparing): the buffer plus the patience
QUICK_RESUME_S = 5.0
STALL_STEP_WINDOW_S = 300
UNSTABLE_COMPLETE_ETA_S = 120
QUIET_WINDOW_S = 0.05
EWMA_TAU_S = 10.0
P20_WINDOW_S = 60
GAP_WINDOW_S = 900
SPEED_KNOWN_S = 1.0          # v is unknown until this much transfer time …
SPEED_KNOWN_BYTES = 1 << 20  # … or this many bytes
TYPICAL_RHO = 120_000        # a 16-bit / 44.1 kHz FLAC, when nothing is playing to measure against
HEAD_ONLY_EXTRA = 64 * 1024  # before the probe: what a format that reads only its head needs at open
RHO_MARGIN = 1.2
QUALITY_RISE_S = 60          # a better quality class counts only after this long at it
STALLS_UNSTABLE_S = 120      # two pauses for the network within this long: unstable
UNHEARD_BUDGET = 0.5         # songs not heard yet may use at most this share of the cache limit

EXCELLENT, GOOD, POOR, UNSTABLE = "excellent", "good", "poor", "unstable"
RANK = {UNSTABLE: 0, POOR: 1, GOOD: 2, EXCELLENT: 3}
EXACT_FORMATS = {"WAV", "AIFF", "WAVEX", "W64", "RF64", "AIFC"}


def rho(S: int, H: int, D: float, fmt: str = "", rate: int = 0, frame_bytes: int = 0) -> float:
    """Bytes of file per second of music (see the module note)."""
    if fmt in EXACT_FORMATS and rate and frame_bytes:
        return float(rate * frame_bytes)
    if D and D > 0 and S:
        return RHO_MARGIN * max(1, S - H) / D
    return float(TYPICAL_RHO)


def byte_at(p: float, H: int, rho_avg: float, fmt: str = "", frame_bytes: int = 0, rate: int = 0,
            flac_scan=None, seek_points=None) -> int:
    """Where second `p` of the music sits in the file (exact for WAV/AIFF, and for FLAC once
    the scanner has seen that far; for FLAC with a SEEKTABLE, the seek point at or before it — the
    frame holding that sample starts no earlier; else the average density)."""
    if fmt in EXACT_FORMATS and rate and frame_bytes:
        return int(H + round(p * rate) * frame_bytes)
    if flac_scan is not None and rate:
        at = flac_scan.byte_at(int(p * rate))
        if at is not None:
            return at
    if seek_points and rate:
        sample = int(max(0.0, p) * rate)
        best = None
        for point_sample, offset in seek_points:   # (in order of sample)
            if point_sample > sample:
                break
            best = offset
        if best is not None:
            return int(H + best)
    return int(H + max(0.0, p) * rho_avg)


def run_seconds(run_end: int, b: int, p: float, *, S: int, H: int, D: float, fmt: str = "", rate: int = 0,
                frame_bytes: int = 0) -> float:
    """Seconds of music from second `p` (at byte `b`) to byte `run_end`, where an island holds
    them: exact for WAV/AIFF, else at the dense end of the file's density (so never more than is
    there). The whole rest of the song if the run reaches the end of the file."""
    if S and run_end >= S:
        return max(0.0, D - p)
    if fmt in EXACT_FORMATS and rate and frame_bytes:
        return max(0, run_end - b) // frame_bytes / rate
    return max(0, run_end - b) / rho(S, H, D, fmt, rate, frame_bytes)


def covered(spans, ranges) -> float:
    """The share of `ranges` ([(start, end), …]) that `spans` (the runs on disk, in order) hold."""
    total = sum(max(0, end - start) for start, end in ranges)
    if total <= 0:
        return 0.0
    have = 0
    for start, end in ranges:
        for s, e in spans:
            have += max(0, min(end, e) - max(start, s))
    return min(1.0, have / total)


def available_seconds(prefix: int, p: float, *, S: int, H: int, D: float, fmt: str = "", rate: int = 0,
                      frame_bytes: int = 0, flac_end_sample: int | None = None) -> float:
    """A(p): seconds of music from p on disk in one unbroken run (v1 downloads from byte 0 in
    order, so it's what the prefix holds past p)."""
    if S and prefix >= S:
        return max(0.0, D - p)
    if fmt in EXACT_FORMATS and rate and frame_bytes:
        held = max(0, prefix - H) // frame_bytes / rate
    elif flac_end_sample is not None and rate:
        held = flac_end_sample / rate
    else:
        held = max(0, prefix - H) / rho(S, H, D, fmt, rate, frame_bytes)
    return max(0.0, held - p)


def need_seconds(mode: str, L: float, B0: float, m: float | None, gap15: float, quality: str,
                 eta_ready: float | None) -> float | None:
    """Seconds of music that must be on disk before starting. None = only a complete copy will do.
    `eta_ready`: seconds until the download finishes at the current speed (None if unknown)."""
    L = max(0.0, L)
    if mode == "complete":
        return None
    if mode == "fast":
        return min(L, B0)
    deficit = L * max(0.0, 1.0 - (1.0 if m is None else m))
    reserve = min(120.0, max(5.0, 1.5 * gap15))
    if quality == UNSTABLE:
        if eta_ready is not None and eta_ready <= UNSTABLE_COMPLETE_ETA_S:
            return None
        return min(L, max(4 * B0, deficit + reserve))
    floor = {EXCELLENT: max(5.0, B0 / 6), GOOD: max(5.0, B0 / 3)}.get(quality, B0)
    return min(L, max(floor, deficit + reserve))


def resume_need(need: float | None, stalls: int, L: float) -> float | None:
    """After k committed stalls on this track in the last 5 minutes, wait for need × 2^k."""
    if need is None:
        return None
    return min(max(0.0, L), need * (2 ** max(0, stalls)))


def startable(*, open_ready: bool, ready: bool, complete: bool, A: float, need: float | None,
              forced: bool = False) -> bool:
    """The start rule. `ready`: a complete copy that may be played (checked, or checks are off);
    `complete`: every byte is here (not yet checked)."""
    if ready:
        return True
    if need is None and not forced:
        return False
    if not open_ready:
        return False
    if forced:
        return complete or A >= MIN_START_S
    return complete or A >= need


def needed_bytes(p: float, need: float | None, *, S: int, H: int, D: float, open_end: int | None,
                 reads_tail: bool, fmt: str = "", frame_bytes: int = 0, rate: int = 0, flac_scan=None) -> int:
    """How many bytes from the start must be here to start (for the progress figure): what the
    decoder's open reads (once the probe knows; before that the head, or the whole file for a
    format whose open reads its end), and enough music from p."""
    if need is None:
        return S
    if open_end is not None:
        opening = open_end
    else:
        opening = S if reads_tail else H + HEAD_ONLY_EXTRA
    dense = rho(S, H, D, fmt, rate, frame_bytes)
    average = dense if fmt in EXACT_FORMATS else dense / RHO_MARGIN
    wanted = max(opening, byte_at(p, H, average, fmt, frame_bytes, rate, flac_scan) + need * dense)
    return int(min(S, wanted)) if S else int(wanted)


def percent(prefix: int, needed: int, complete_mode: bool = False, S: int | None = None) -> int | None:
    """"Preparing track… N%": never 100 before it starts, never going backwards for an in-order
    download. Complete mode counts the whole file."""
    if complete_mode and S:
        return min(99, int(100 * prefix / S))
    if not needed or needed <= 0:
        return None
    return min(99, int(100 * prefix / needed))


def quality(m: float | None, gap15: float, failures15: int, stalls2: int = 0) -> str:
    """How good the network is for a song, as measured right now (no memory: see QualityScore).
    `stalls2`: pauses for the network in the last 2 minutes."""
    if gap15 >= 15 or failures15 >= 3 or stalls2 >= 2:
        return UNSTABLE
    if m is not None and m >= 4 and gap15 < 2 and failures15 == 0:
        return EXCELLENT
    if m is not None and m >= 1.5 and gap15 < 5 and failures15 <= 1:
        return GOOD
    return POOR


def worse(a: str, b: str) -> str:
    return a if RANK.get(a, 1) <= RANK.get(b, 1) else b


class QualityScore:
    """One server's quality class with hysteresis: it drops to a worse class at once, and rises
    only once every measurement for QUALITY_RISE_S has been better (then to the worst of them)."""

    def __init__(self, current: str = "", since: float | None = None):
        self.current = current          # "" until first measured
        self.since = since
        self._rising_since: float | None = None
        self._rising_to = ""

    def update(self, measured: str, now: float) -> str:
        if not self.current or RANK[measured] < RANK[self.current]:
            self.current, self.since = measured, now
            self._rising_since, self._rising_to = None, ""
        elif RANK[measured] == RANK[self.current]:
            self._rising_since, self._rising_to = None, ""
        else:
            if self._rising_since is None:
                self._rising_since, self._rising_to = now, measured
            else:
                self._rising_to = worse(self._rising_to, measured)
            if now - self._rising_since >= QUALITY_RISE_S:
                self.current, self.since = self._rising_to, now
                self._rising_since, self._rising_to = None, ""
        return self.current


def within_budget(sizes, limit: float, share: float = UNHEARD_BUDGET) -> int:
    """How many of the songs ahead (their sizes, in play order; None = not known yet) to fetch so
    that together they use at most `share` of the cache limit. The first is always fetched; a song
    whose size isn't known yet is the last one this time (its size is learnt as it starts)."""
    budget = share * limit
    used, count = 0.0, 0
    for size in sizes:
        used += size or 0
        if count and used > budget:
            break
        count += 1
        if size is None:
            break
    return count


def expired(item, keep: str, now: float, session: set) -> bool:
    """Is this copy past what Keep cached tracks allows (whatever room the cache has)?"""
    return (keep == "none" or (keep == "session" and item.key not in session)
            or (keep == "day" and now - (item.last_used or 0) > 24 * 3600))


def feasible(v: float | None, seconds_left: float, bytes_left_now: int, bytes_left_next: int) -> bool:
    """Can the link fetch the rest of this song and the next before this one ends? (Unknown
    speed: assume so until measured.)"""
    if v is None:
        return True
    return v * max(0.0, seconds_left) >= bytes_left_now + bytes_left_next


def quiet_point(block, rate: int, window_s: float = QUIET_WINDOW_S) -> int:
    """Index k in `block` (frames × channels, integer or float) within its last `window_s` where the
    loudest channel is nearest zero — where to cut before a pause, so the last sample heard is
    a quiet one. The latest such sample, to re-decode as little as possible."""
    import numpy as np
    frames = len(block)
    if frames == 0:
        return 0
    span = max(1, min(frames, int(round(window_s * rate))))
    tail = np.asarray(block[frames - span:])
    level = np.abs(tail.astype(np.float64))
    if level.ndim > 1:
        level = level.max(axis=1)
    lowest = level.min()
    k = int(np.flatnonzero(level == lowest)[-1])
    return frames - span + k


def evict_order(items, protected: set, now: float, keep: str = "limit", in_plan: set | None = None,
                session: set | None = None) -> list:
    """Items that may be removed, first to go first: (1) expired by the keep policy, (2) partial
    downloads no longer wanted — untouched for 7 days first, then the oldest, (3) failed and stale
    ones, (4) complete copies, least recently used first. Never: leased (being read), protected
    (playing, lined up, the look-ahead), pinned, downloading for the current or next song, or in
    the middle of being fetched or checked (an I/O or checking thread has it in hand; it's
    partial or complete moments later, and can go then).

    Keep policies: "none" (don't keep: gone once out of the protected set), "session" (until
    playback finishes: kept while in the playing list or Up next, `session`), "day" (24 hours
    after last played), "limit" (until the cache is full: only the limit removes them)."""
    from .store import DOWNLOADED, DOWNLOADING, FAILED, PARTIAL, PROBING, QUEUED, READY, STALE, VERIFYING
    in_plan = in_plan or set()
    session = in_plan if session is None else session
    week = 7 * 24 * 3600
    groups: list[list] = [[], [], [], [], []]
    for item in items:
        if item.leased() or item.key in protected or item.pinned:
            continue
        if item.state in (PROBING, DOWNLOADING, DOWNLOADED, VERIFYING):
            continue
        if item.priority is not None and item.priority <= 2:   # CURRENT / NEXT / a demand
            continue
        if expired(item, keep, now, session):
            groups[0].append(item)
        elif item.state in (PARTIAL, QUEUED) and item.key not in in_plan:
            groups[1 if now - (item.last_used or 0) > week else 2].append(item)
        elif item.state in (FAILED, STALE):
            groups[3].append(item)
        elif item.state == READY:
            groups[4].append(item)
    order = []
    for group in groups:
        order += sorted(group, key=lambda item: item.last_used or 0)
    return order


class Speed:
    """One host's download speed, gaps and failures, from what its I/O thread reports. Until
    enough is measured, a figure saved within the last hour (`warm`) stands in for the speed."""

    def __init__(self, clock=None):
        import time
        self._clock = clock or time.monotonic
        self._buckets: deque = deque()   # [second, bytes, active seconds]
        self._ewma: float | None = None
        self._active = 0.0
        self._bytes = 0
        self._gaps: deque = deque()      # (when it ended, how long)
        self._failures: deque = deque()
        self._waiting_since: float | None = None
        self._warm: float | None = None
        self._warm_until = 0.0

    def warm(self, speed: float | None, age: float, gap: float = 0.0, failures: int = 0,
             keep: float = 3600.0) -> None:
        """A start from figures saved `age` seconds ago (used only while under `keep` old)."""
        now = self._clock()
        if age < 0 or age >= keep:
            return
        if speed:
            self._warm, self._warm_until = float(speed), now + keep - age
        if age < GAP_WINDOW_S:
            if gap and gap > 0.5:
                self._gaps.append((now - age, float(gap)))
            for _ in range(min(int(failures or 0), 10)):
                self._failures.append(now - age)

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def measured(self) -> bool:
        """Enough has been transferred to know the speed (rather than a saved figure)."""
        return self._ewma is not None and (self._active >= SPEED_KNOWN_S or self._bytes >= SPEED_KNOWN_BYTES)

    def add(self, now: float, nbytes: int, duration: float) -> None:
        duration = max(duration, 1e-4)
        rate = nbytes / duration
        if self._ewma is None:
            self._ewma = rate
        else:
            self._ewma += (1 - math.exp(-duration / EWMA_TAU_S)) * (rate - self._ewma)
        self._active += duration
        self._bytes += nbytes
        second = int(now)
        if self._buckets and self._buckets[-1][0] == second:
            self._buckets[-1][1] += nbytes
            self._buckets[-1][2] += duration
        else:
            self._buckets.append([second, nbytes, duration])
        while len(self._buckets) > P20_WINDOW_S:
            self._buckets.popleft()
        self.arrived(now)

    def value(self) -> float | None:
        """v (bytes per second), or None while too little has been measured (and nothing recent
        was saved)."""
        if not self.measured():
            if self._warm is not None and self._clock() < self._warm_until:
                return self._warm
            return None
        rates = sorted(b[1] / max(b[2], 1e-4) for b in self._buckets)
        p20 = rates[int(0.2 * (len(rates) - 1))] if rates else self._ewma
        return min(self._ewma, p20)

    def asking(self, now: float) -> None:
        """A request is outstanding (bytes are wanted)."""
        if self._waiting_since is None:
            self._waiting_since = now

    def arrived(self, now: float) -> None:
        if self._waiting_since is not None:
            gap = now - self._waiting_since
            if gap > 0.5:
                self._gaps.append((now, gap))
        self._waiting_since = None

    def failed(self, now: float) -> None:
        self._failures.append(now)

    def gap15(self, now: float) -> float:
        while self._gaps and now - self._gaps[0][0] > GAP_WINDOW_S:
            self._gaps.popleft()
        longest = max((g for _, g in self._gaps), default=0.0)
        if self._waiting_since is not None:
            longest = max(longest, now - self._waiting_since)
        return longest

    def failures15(self, now: float) -> int:
        while self._failures and now - self._failures[0] > GAP_WINDOW_S:
            self._failures.popleft()
        return len(self._failures)

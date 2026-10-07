"""The whole-library quality check ("Needs a look"), no UI.

Every song in the library gets the one pass Now Playing's Check makes (analysis.analyse: the MD5
lossless check, the genuine-file clues, clipping, the true peak, the format flags), in the
background, and the result is kept in the library database (LibraryDB.save_check) — keyed by the
song's key (audiofile.key: a CUE track is "file#3") with the size and modified time of the file it
read. So each file is read once: one that changes is checked again, Now Playing shows a stored
result at once (`stored`), and the clipping guard knows a song's true peak before it plays
(`stored_peak`).

**What needs a look** (`findings`, worst first): a file that stops decoding (damaged) or whose MD5
doesn't match; the clues that a file isn't what it says — made from a lossy file, upsampled from
CD quality, a 16-bit recording padded out to 24 bits (clues, never proof: the wording is Now
Playing's); clipping at mastering; a file named as another format. And, for information only,
HDCD, MQA and pre-emphasis. `needs_look` lists them for the Library's view.

**The work** is opt-in (the Library's "Check the whole library…") and kept in the settings as the
checker's `state`: "off", "on" (new and changed music is checked as it arrives) or "paused". One
"library-check" thread hands songs to WORKERS worker processes at below-normal priority — numpy
keeps the GIL, so threads wouldn't run side by side, and processes never take the processor from
the player — one song each at a time, and stores each result as it comes. A pause, a `hold` (a
measurement or a blind test running) or quitting stops the songs in hand part-way (a flag the
workers read between blocks); what's stored stays, so the next run — after a restart too — carries
on where it left off. Songs this player can't decode (AAC) are left out; a file that can't be
reached just now (a network drive asleep, a file in use) is tried again on the next run, never
taken as damaged.

A first check of a big library takes a while: about SPEED times faster than playing it per worker
(measured), so `estimate` says how long before it starts, and `Status.seconds_left` how long is
left, measured as it goes. Nothing here touches Tk: windows read `status` from their own loop.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import multiprocessing
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, BrokenExecutor, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from . import analysis, audiofile, indexer, library_db
from .analysis import Analysis
from .formats import FormatFlags, HdcdResult, MqaResult, PreEmphasis
from .library import Track
from .library_db import LibraryDB, SongHit

log = logging.getLogger(__name__)

# Which version of the check made a stored result: raise it when analysis.analyse (or the findings)
# learn something new, and every song is checked again in the background.
CHECK_FORMAT = 1
# Worker processes: two where the computer has the cores for it (the player keeps the rest).
WORKERS = 2 if (os.cpu_count() or 1) >= 4 else 1
# How much faster than playing it one worker checks CD-rate music. Measured 07/10/2026 on a desktop PC:
# 42–61× for 44.1 kHz FLAC (one process, normal priority); a little under, for the estimate.
SPEED = 40.0
RECHECK_S = 300.0      # while on: how often to see whether the library changed (new music to check)
SETTLE_S = 20.0        # the speed is measured once a run has gone this long…
SETTLE_SONGS = 2       # …and checked this many songs
CRASHES = 2            # a song in hand this many times when a worker process died is taken as undecodable
STATES = ("off", "on", "paused")

# What can need a look, worst first.
FINDINGS = ("damaged", "md5", "lossy", "upsampled", "padded", "clipping", "misnamed", "hdcd", "mqa", "pre_emphasis")
INFORMATION = frozenset({"hdcd", "mqa", "pre_emphasis"})   # said, but nothing wrong with the file
# Each finding counted in the Library's status line ("3 damaged · 12 lossy-source clues").
COUNTED = {"damaged": ("damaged", "damaged"), "md5": ("MD5 mismatch", "MD5 mismatches"),
           "lossy": ("lossy-source clue", "lossy-source clues"), "upsampled": ("upsampling clue", "upsampling clues"),
           "padded": ("padded", "padded"), "clipping": ("clipped", "clipped"),
           "misnamed": ("misnamed", "misnamed"), "hdcd": ("HDCD", "HDCD"), "mqa": ("MQA", "MQA"),
           "pre_emphasis": ("pre-emphasis", "pre-emphasis")}


# -- what a check found ----------------------------------------------------------------------------------
def findings(result: Analysis, misnamed: str = "") -> list[str]:
    """What needs a look in a song's check, worst first (FINDINGS); [] when nothing does."""
    found = set()
    if result.damaged_at is not None:
        found.add("damaged")
    elif result.md5_ok is False:
        found.add("md5")
    if not result.lossy:
        wall = analysis.wall_verdict(result.cutoff_hz, result.cutoff_sharp, result.rate)
        if wall:
            found.add(wall)
        if (result.bits and not result.float_samples and result.bits_used is not None
                and result.bits_used < result.bits):
            found.add("padded")
    if result.clip_runs:
        found.add("clipping")
    if misnamed:
        found.add("misnamed")
    flags = result.flags
    if flags is not None:
        if flags.hdcd is not None and flags.hdcd.detected:
            found.add("hdcd")
        if flags.mqa is not None and flags.mqa.detected:
            found.add("mqa")
        if flags.pre_emphasis.flagged:
            found.add("pre_emphasis")
    return [word for word in FINDINGS if word in found]


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def finding_text(word: str, result: Analysis | None, track: Track | None = None) -> str:
    """One finding in a few words, as Now Playing's Check words it (clues, never proof)."""
    if word == "damaged":
        if result is None or not result.damaged_at:
            return "Damaged: can't be decoded from the start"
        return f"Damaged: stops decoding at {_clock(result.damaged_at)}"
    if word == "md5":
        return "MD5 mismatch: damaged or altered after encoding"
    khz = f"{result.cutoff_hz / 1000:.1f} kHz" if result is not None and result.cutoff_hz else ""
    if word == "lossy":
        return "Suggests a lossy source" + (f" (sound stops sharply at {khz})" if khz else "")
    if word == "upsampled":
        return "Suggests upsampling from CD quality" + (f" (sound stops at {khz})" if khz else "")
    if word == "padded":
        if result is None or not result.bits or result.bits_used is None:
            return "Padded bit depth"
        if result.bits == 24 and result.bits_used <= 16:
            return f"Only {result.bits_used} of {result.bits} bits used: 16-bit stored as 24-bit"
        return f"Only {result.bits_used} of {result.bits} bits used"
    if word == "clipping":
        runs = result.clip_runs if result is not None else 0
        return f"Clipped when mastered ({runs:,} run{'' if runs == 1 else 's'} stuck at full scale)"
    if word == "misnamed":
        if track is not None and track.misnamed:
            return f"Really {track.codec}, but named {track.misnamed}"
        return "Named as another format"
    return {"hdcd": "HDCD-encoded (for information)", "mqa": "MQA-encoded (for information)",
            "pre_emphasis": "Pre-emphasis flagged (for information)"}.get(word, word)


def describe(words: Iterable[str], result: Analysis | None, track: Track | None = None) -> str:
    """Every finding of a song, worst first: "Damaged: stops decoding at 2:13 · Clipped when …"."""
    return " · ".join(finding_text(word, result, track) for word in words)


def summary(counts: dict[str, int]) -> str:
    """How many songs have each finding, worst first: "2 damaged · 30 clipped · 4 HDCD"."""
    parts = []
    for word in FINDINGS:
        n = counts.get(word, 0)
        if n:
            one, many = COUNTED[word]
            parts.append(f"{n:,} {one if n == 1 else many}")
    return " · ".join(parts)


# -- results to and from the database --------------------------------------------------------------------
def to_json(result: Analysis) -> str:
    return json.dumps(dataclasses.asdict(result), separators=(",", ":"))


def _known(cls, data) -> dict:
    """The fields of `cls` in a stored dict (one an older or newer version wrote may have others)."""
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in names} if isinstance(data, dict) else {}


def from_json(text: str) -> Analysis | None:
    """A stored result back as an Analysis; None when there's none, or it can't be read."""
    if not text:
        return None
    try:
        data = json.loads(text)
        flags = data.get("flags")
        result = Analysis(**_known(Analysis, {k: v for k, v in data.items() if k != "flags"}))
        result.dc_offset = tuple(result.dc_offset or ())
        if isinstance(flags, dict):
            pre = _known(PreEmphasis, flags.get("pre_emphasis"))
            result.flags = FormatFlags(
                hdcd=HdcdResult(**_known(HdcdResult, flags["hdcd"])) if flags.get("hdcd") else None,
                mqa=MqaResult(**_known(MqaResult, flags["mqa"])) if flags.get("mqa") else None,
                pre_emphasis=PreEmphasis(**{**pre, "sources": list(pre.get("sources") or [])}))
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _file_now(path) -> tuple[int, int] | None:
    try:
        st = os.stat(audiofile.real_path(path))
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def stored(db: LibraryDB, path) -> tuple[Analysis, float] | None:
    """A song's stored check and when it was made — only when its file hasn't changed since (and
    the check is this version's). Looks at the file: for a worker thread."""
    row = db.check_of(path)
    if row is None or row[2] != CHECK_FORMAT or _file_now(path) != (row[0], row[1]):
        return None
    result = from_json(row[6])
    return (result, row[3]) if result is not None else None


def stored_peak(db: LibraryDB, path) -> float | None:
    """A song's true peak from its stored check (linear), when its file hasn't changed since; None
    when there's none — or when the check stopped at damage, as its peak is then only of the part
    before it (the player plays on past the damage). For the clipping guard, on its worker thread."""
    row = db.check_of(path)
    if (row is None or row[5] is None or row[2] != CHECK_FORMAT or "damaged" in row[4].split()
            or _file_now(path) != (row[0], row[1])):
        return None
    return float(row[5])


def _whole_peak(result: Analysis) -> float | None:
    """The true peak kept for the clipping guard: only one of the whole song, not of a read that
    stopped at damage."""
    return result.true_peak if result.damaged_at is None else None


def remember(db: LibraryDB, track: Track, result: Analysis, file: tuple[int, int] | None) -> None:
    """Keep a check made elsewhere (Now Playing's) as the library check would. `file`: the file's
    (size, modified ns) as it was read."""
    if file is None:
        return
    db.save_check(track.path, file[0], file[1], CHECK_FORMAT, " ".join(findings(result, track.misnamed)),
                  _whole_peak(result), to_json(result))


# -- the songs to check ------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Song:
    """One song to check (plain values: it goes to a worker process). Slotted: a first run holds
    one for every song in the library."""
    key: str
    path: str | Path           # its text (the key itself), or a CuePath for a CUE track: its stretch of the file
    bits: int
    md5: int
    duration: float
    rate: int
    misnamed: str = ""

    @property
    def work(self) -> float:
        """How much reading it takes, as seconds of CD-rate music."""
        return max(0.0, self.duration) * max(self.rate, 1) / 44100


@dataclass
class Plan:
    songs: list[Song]          # still to check, folder by folder (none kept when only counted)
    done: int                  # songs checked already (their files unchanged since)
    gone: list[str]            # stored checks of songs no longer in the library
    music_s: float = 0.0       # how long the songs still to check play for, all together
    count: int = 0             # how many there are still to check
    work: float = 0.0          # the reading they take (Song.work, all together)

    @property
    def total(self) -> int:
        return self.done + self.count


def plan(db: LibraryDB, keep: bool = True) -> Plan:
    """What a run has to check: every song whose stored check is missing, for a file that has
    changed since, or from another version of the check. Songs whose files are missing just now
    are left for later (their checks kept); songs this player can't decode are left out.
    `keep` False: only counted (the question's estimate holds no songs).

    `gone`: stored checks of songs no longer in the library — but none under a library folder
    whose first look hasn't finished (a new folder, or the library being read again after its
    database was remade): its songs aren't all listed yet, and their checks took hours to make."""
    states = db.check_states()
    unread = [os.path.join(os.path.normcase(root), "") for root in db.roots() if not db.built(root)]
    known: set[str] = set()
    songs: list[Song] = []
    done = count = 0
    music = work = 0.0
    for path, bits, md5, duration, rate, misnamed, unsupported, size, mtime, missing in db.check_list():
        key = audiofile.key(path)
        known.add(key)
        if missing or unsupported:
            continue
        if states.get(key) == (size, mtime, CHECK_FORMAT):
            done += 1
            continue
        song = Song(key, path, bits, md5, float(duration or 0.0), rate or 0, misnamed or "")
        count += 1
        music += song.duration
        work += song.work
        if keep:
            songs.append(song)
    gone = [key for key in states
            if key not in known and not (unread and os.path.normcase(key).startswith(tuple(unread)))]
    return Plan(songs, done, gone, music, count, work)


def estimate(songs: Iterable[Song] | float, workers: int = WORKERS, speed: float = SPEED) -> float:
    """Seconds a run takes to check these songs (or this much reading: Plan.work), before it has
    measured anything."""
    work = songs if isinstance(songs, (int, float)) else sum(song.work for song in songs)
    return work / (speed * max(1, workers))


def duration_text(seconds: float) -> str:
    """"about 40 minutes", "about 1 hour 40 minutes", "about 12 hours"."""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"about {minutes} minute{'' if minutes == 1 else 's'}"
    if minutes >= 10 * 60:
        return f"about {round(minutes / 60)} hours"
    hours, minutes = divmod(5 * round(minutes / 5), 60)
    text = f"about {hours} hour{'' if hours == 1 else 's'}"
    return text + (f" {minutes} minutes" if minutes else "")


def music_text(seconds: float) -> str:
    """How long some music plays for: "40 minutes", "49.2 hours"."""
    if seconds < 90 * 60:
        minutes = max(1, round(seconds / 60))
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    return f"{seconds / 3600:,.1f} hours"


# -- checking one song (in a worker process, or on the checker's thread) --------------------------------
@dataclass(frozen=True)
class Outcome:
    state: str                          # "done", "gone" (couldn't be reached: try again later), "stopped"
    file: tuple[int, int] | None = None   # the file's (size, modified ns) as it was read
    result: Analysis | None = None      # None with "done": nothing to keep (this player can't decode it)


def check_song(song: Song, cancel=None) -> Outcome:
    """Check one song (`cancel`: anything with is_set(), looked at between blocks)."""
    real = audiofile.real_path(song.path)
    try:
        st = os.stat(real)
        with open(real, "rb"):
            pass   # (in use by another program, or a network drive gone: not the file's fault)
    except OSError:
        return Outcome("gone")
    file = (st.st_size, st.st_mtime_ns)
    try:
        result = analysis.analyse(song.path, song.bits, song.md5, cancel=cancel)
    except audiofile.Unsupported:
        return Outcome("done", file)
    except (OSError, RuntimeError):
        if _file_now(song.path) is None:
            return Outcome("gone")
        # It won't even open: damaged from the start (as Now Playing's Check says it).
        result = Analysis(md5_ok=False, dr=None, peak_dbfs=float("-inf"), damaged_at=0.0, duration=song.duration)
    if result is None:
        return Outcome("stopped")
    if result.damaged_at is not None and _file_now(song.path) != file:
        return Outcome("gone")   # (it went, or changed, while it was read: not damage)
    return Outcome("done", file, result)


def _undecodable(song: Song) -> Outcome:
    """A song that stops the decoder itself (its worker process died reading it): damaged."""
    file = _file_now(song.path)
    if file is None:
        return Outcome("gone")
    return Outcome("done", file, Analysis(md5_ok=False, dr=None, peak_dbfs=float("-inf"), damaged_at=0.0,
                                          duration=song.duration))


_stop = None   # in a worker process: the run's stop flag (set to stop the song in hand)


def _worker_start(stop) -> None:
    global _stop
    _stop = stop
    indexer.lower_priority()


def _check_in_worker(song: Song) -> Outcome:
    return check_song(song, _stop)


def make_pool(workers: int):
    """Worker processes for the check and the flag that stops the songs they have in hand, or
    (None, None) if they can't be started."""
    try:
        context = multiprocessing.get_context("spawn")
        stop = context.Event()
        return ProcessPoolExecutor(max_workers=workers, mp_context=context, initializer=_worker_start,
                                   initargs=(stop,)), stop
    except (OSError, ValueError, ImportError, NotImplementedError, RuntimeError):
        return None, None


# -- the checker -------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Status:
    """Where the check has got to (replaced whole as it goes: read from any thread)."""
    state: str = "off"           # as chosen: "off", "on" or "paused"
    running: bool = False        # a run is under way
    held: bool = False           # held off for a while (a measurement running)
    done: int = 0                # songs checked, their files unchanged since
    total: int = 0               # songs that can be checked (0: not known yet)
    seconds_left: float | None = None
    error: str = ""              # the last run failed: why

    @property
    def finished(self) -> bool:
        return self.total > 0 and self.done >= self.total and not self.running


class Checker:
    """Checks the library's songs in the background (see the module notes). Its methods are for
    the window's thread; the work happens on a "library-check" thread and worker processes.
    `db_for()`: the library database (asked afresh for each run); `on_state(state)`: the choice
    changed (to keep in the settings)."""

    def __init__(self, db_for: Callable[[], LibraryDB], state: str = "off",
                 on_state: Callable[[str], None] = lambda state: None, workers: int | None = None,
                 pool_factory: Callable = make_pool, in_process: bool = False):
        self.db_for = db_for
        self.on_state = on_state
        self.workers = workers if workers is not None else WORKERS
        self.pool_factory = pool_factory
        self.in_process = in_process        # (tests) check on the checker's own thread, no worker processes
        self.status = Status(state=state if state in STATES else "off")
        self.changes = 0                    # goes up with every result stored (the view re-reads it)
        self._lock = threading.Lock()
        self._status_lock = threading.Lock()   # (taken alone: the window's choices and the run's progress)
        self._wake = threading.Event()
        self._stop: threading.Event | None = None   # the run under way's
        self._holds: set[str] = set()
        self._closed = False
        self._thread: threading.Thread | None = None
        self._generation: int | None = None   # the library as the last run found it
        self._busy = False                    # the thread is taking a wake-up in hand

    @property
    def state(self) -> str:
        return self.status.state

    def _set(self, **changes) -> None:
        with self._status_lock:
            self.status = dataclasses.replace(self.status, **changes)

    def _choose(self, state: str) -> None:
        with self._lock:
            self._set(state=state, error="")
            if state != "on" and self._stop is not None:
                self._stop.set()
        self.on_state(state)
        if state == "on":
            self.kick()

    # -- the choices --
    def start(self) -> None:
        """Check the whole library (and new music as it arrives) from now on."""
        self._choose("on")

    def pause(self) -> None:
        """Stop for now, where it is (the songs in hand are read again on resume)."""
        if self.state == "on":
            self._choose("paused")

    def resume(self) -> None:
        if self.state == "paused":
            self._choose("on")

    def stop(self) -> None:
        """No more checking (what's stored stays: Needs a look still lists it)."""
        self._choose("off")

    def hold(self, reason: str, on: bool = True) -> None:
        """Hold off while something else needs the computer to itself (a measurement, a blind
        test); carries on when the last reason is let go."""
        with self._lock:
            (self._holds.add if on else self._holds.discard)(reason)
            held = bool(self._holds)
            self._set(held=held)
            if held and self._stop is not None:
                self._stop.set()
        if not held:
            self.kick()

    def kick(self) -> None:
        """Look for songs to check now (the library changed, or it's time to carry on). Nothing
        happens unless it's on."""
        if self._closed or self.state != "on":
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="library-check", daemon=True)
                self._thread.start()
        self._wake.set()

    def close(self, timeout: float = 2.0) -> None:
        """Quitting: stop the songs in hand and let the thread go."""
        self._closed = True
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            thread = self._thread
        self._wake.set()
        if thread is not None:
            thread.join(timeout)

    def idle(self, timeout: float = 30.0) -> bool:
        """(Tests) wait until no run is under way and none is about to start."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._wake.is_set() and not self._busy:
                return True
            time.sleep(0.02)
        return False

    # -- the work (the "library-check" thread) --
    def _loop(self) -> None:
        while not self._closed:
            woken = self._wake.wait(RECHECK_S)
            if self._closed:
                return
            with self._lock:
                self._wake.clear()
                if self.state != "on" or self._holds:
                    continue
                stop = self._stop = threading.Event()
                self._busy = True
            db = None
            try:
                db = self.db_for()
                if woken or db.generation != self._generation:   # (else nothing new since the last run)
                    self._set(running=True, error="")
                    self._run(db, stop)
            except Exception as error:   # (said in the Library; tried again at the next kick, or in RECHECK_S)
                if db is not None and not db.note_damage(error):
                    log.warning("the library check stopped: %r", error)
                self._generation = None
                self._set(error=str(error) or type(error).__name__)
            finally:
                with self._lock:
                    self._stop = None
                    self._busy = False
                    self._set(running=False, seconds_left=None)
                if db is not None:
                    db.release()

    def _run(self, db: LibraryDB, stop: threading.Event) -> None:
        self._generation = db.generation
        found = plan(db)
        if found.gone:
            db.forget_checks(found.gone)
        self._set(done=found.done, total=found.total, seconds_left=estimate(found.work, self.workers))
        if found.songs and not stop.is_set():
            self._work(db, found, stop)

    def _work(self, db: LibraryDB, found: Plan, stop: threading.Event) -> None:
        songs = deque(found.songs)
        left = sum(song.work for song in songs)
        started, measured = time.monotonic(), 0.0
        checked = 0
        suspects: dict[str, int] = {}   # songs in hand when a worker process died: how many times
        pool, flag = self._pool()
        in_flight: dict = {}
        try:
            while (songs or in_flight) and not stop.is_set():
                if pool is None:
                    song = songs.popleft()
                    finished = [(song, check_song(song, stop))]
                else:
                    finished = []
                    try:
                        while songs and len(in_flight) < self.workers:   # (one song each: a pause waits for nothing)
                            future = pool.submit(_check_in_worker, songs[0])
                            in_flight[future] = songs.popleft()
                        ready, _rest = wait(in_flight, timeout=0.25, return_when=FIRST_COMPLETED)
                        for future in ready:
                            song = in_flight.pop(future)
                            try:
                                finished.append((song, future.result()))
                            except BrokenExecutor:
                                in_flight[future] = song
                                raise
                            except Exception:   # (that song's own trouble: the rest carry on; tried next run)
                                log.exception("the library check couldn't check %s", song.path)
                    except BrokenExecutor:
                        # A worker process died (a file that crashes the decoder, say): start new ones
                        # and try the songs in hand again — one in hand at CRASHES deaths is damaged.
                        log.warning("a library-check worker process stopped; starting new ones")
                        flag.set()
                        pool.shutdown(wait=False, cancel_futures=True)
                        for song in in_flight.values():
                            suspects[song.key] = suspects.get(song.key, 0) + 1
                            if suspects[song.key] >= CRASHES:
                                finished.append((song, _undecodable(song)))
                            else:
                                songs.appendleft(song)
                        in_flight.clear()
                        pool, flag = self._pool()
                for song, outcome in finished:
                    if outcome.state == "done":
                        self._keep(db, song, outcome)
                        checked += 1
                        measured += song.work
                    if outcome.state != "stopped":
                        left -= song.work
                spent = time.monotonic() - started
                speed = (measured / spent if spent >= SETTLE_S and checked >= SETTLE_SONGS and measured
                         else SPEED * self.workers)
                self._set(done=found.done + checked, seconds_left=max(0.0, left) / speed)
        finally:
            if flag is not None:
                flag.set()   # (the songs in hand stop between blocks, so the workers go at once)
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

    def _pool(self):
        """(worker processes, their stop flag) — (None, None) to check on this thread (tests)."""
        if self.in_process:
            return None, None
        pool, flag = self.pool_factory(self.workers)
        if pool is None:
            raise RuntimeError("the library check's worker processes couldn't be started")
        return pool, flag

    def _keep(self, db: LibraryDB, song: Song, outcome: Outcome) -> None:
        result = outcome.result
        size, mtime = outcome.file
        if result is None:   # (can't be decoded here: kept as checked, with nothing to say)
            db.save_check(song.key, size, mtime, CHECK_FORMAT, "", None, "")
        else:
            db.save_check(song.key, size, mtime, CHECK_FORMAT, " ".join(findings(result, song.misnamed)),
                          _whole_peak(result), to_json(result))
        self.changes += 1


# -- the Library's "Needs a look" view ---------------------------------------------------------------------
@dataclass(frozen=True)
class Look:
    """What one song's check found, for the Library's list."""
    words: tuple[str, ...]
    text: str
    rank: int          # its worst finding's place in FINDINGS (0 = the worst kind)
    checked: float     # when (seconds since 1970)
    changed: bool = False   # the file has changed since it was checked (it's checked again on the next run)

    @property
    def information_only(self) -> bool:
        return all(word in INFORMATION for word in self.words)


CHANGED_TEXT = "the file has changed since: it's checked again on the next run"


def needs_look(db: LibraryDB) -> list[tuple[SongHit, Look]]:
    """Every song in the library whose stored check found something (missing files left out), worst
    first (`worst_first`). A song checked under two keys — its file written two ways, by Now
    Playing say — is listed once, by its own key's check (else the newer). A check of a file the
    library has seen change since (re-ripped, its tags edited) says so (`Look.changed`): what it
    found may no longer hold. Reads the database: for the library's query thread."""
    found = {}
    for key, words, checked, text, size, mtime in db.flagged_checks():
        words = tuple(word for word in words.split() if word in FINDINGS)
        if words:
            found[key] = (words, checked, text, (size, mtime))
    chosen: dict[int, tuple[SongHit, str, bool]] = {}
    for key, hit in db.songs_for_keys(found):
        own = key == audiofile.key(hit.track.path)
        had = chosen.get(hit.id)
        if had is None or (not had[2] and (own or found[key][1] > found[had[1]][1])):
            chosen[hit.id] = (hit, key, own)
    files = db.song_file_states(hit.id for hit, _key, _own in chosen.values())
    listed = []
    for hit, key, own in chosen.values():
        if not own and db.check_of(hit.track.path) is not None:
            continue   # (its own key's check found nothing: that one has the say)
        words, checked, text, state = found[key]
        result = from_json(text)
        changed = files.get(hit.id, state) != state
        said = describe(words, result, hit.track) + (f" · {CHANGED_TEXT}" if changed else "")
        listed.append((hit, Look(words, said, FINDINGS.index(words[0]), checked, changed)))
    listed.sort(key=worst_first)
    return listed


def counts(looks: Iterable[Look]) -> dict[str, int]:
    """How many songs have each finding."""
    found: dict[str, int] = {}
    for look in looks:
        for word in look.words:
            found[word] = found.get(word, 0) + 1
    return found


def worst_first(pair: tuple[SongHit, Look]):
    """Sorting (hit, look) pairs: the worst kind of finding first, then by album and track."""
    track = pair[0].track
    return (pair[1].rank, str(pair[0].album).lower(), track.disc_no or 0,
            track.track_no if track.track_no is not None else math.inf, track.title.lower())

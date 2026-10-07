"""The download manager: what to fetch, in what order, and whether a song may start.

Threads (all daemon; none touches Tk):
- `netcache-scheduler` (one): owns the plan and the priorities; the only writer of netcache.db;
  hands work to the I/O threads; checks hosts; sends finished downloads to be checked; tidies.
  No network I/O.
- `netcache-io-<host>` (at most 4 in all): every call that may wait on the network — `stat`,
  `open`, reads — for one host, one transfer at a time; writes the .part file; feeds the FLAC
  scanner; measures the speed.
- `netcache-probe` (one): opens downloading copies the way the player will (through the waiting
  reader, patience 30 s), to learn the format, length and how far an open reads. Local reads only.
- `netcache-probe-hosts` (one): asks a host that stopped answering whether it's back (a TCP
  connection to the file-sharing port of the server named in the song's own path).
- `netcache-verify` (one): checks finished downloads (verify.py).

Priorities (lower first): DEMAND (a reader waiting for bytes right now), CURRENT (the song
playing or about to), NEXT, ASKED (a whole-file tool asked for another song), FOLLOWING (the song
after next, and "one more" once everything wanted is complete), QUEUE (the rest of Pre-download,
within the budget), GAP (stretches a seek ahead skipped, so the copy can be completed). One
transfer per host, so the song playing gets the whole link; between chunks (sized to take about
250 ms) an I/O thread switches to anything more important on its host.

Within one song, bytes don't only come in order from the start: a reader waiting for bytes past
what's here (the decoder after a seek ahead, the probe reading a file's end) gets an "island" from
that point (64 KiB-aligned) that grows forward until it meets the next one; a format whose open
reads its end fetches its last 256 KiB first; the song playing is fetched from where it plays to
the end, then the stretch behind. When the run from the start reaches an island they're joined.

Host health: a call outstanding over 5 s makes its host SLOW, over 15 s DOWN; while a call is
stuck no further calls go to that host. DOWN hosts are probed with back-off (2, 4, 8 … 60 s). An
I/O thread stuck over 120 s is abandoned (its result thrown away when it returns); at most two
per host, after which the host stays DOWN until they come back. When Windows says the network
changed (netwatch.py), every server not answering is asked again at once.

Each server's speed, longest gap, failures and quality are saved in netcache.db, so the next start
begins from them (if under an hour old) rather than from nothing.

Every method the player calls (`prepare`, `notify_when_startable`, `forget`, `resumable`,
`outlook`, `plan_changed`, `position_hint`, `progress`) is a dictionary look-up under a short
lock: no I/O.
Lock order: the player's lock, then this manager's lock, then an item's `cond`. The manager never
takes the player's lock; it reaches the player only through the `post` callables it was given.
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import shutil
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .. import audiofile, flacframes, mp3frames, settings
from . import netwatch, policy, verify
from .fetch import Changed, Identity, Permanent, ShareFetcher, Transient
from .growing import open_shared
from .sources import Classifier, Kind
from .store import (DOWNLOADED, DOWNLOADING, FAILED, PARTIAL, QUEUED, READY, STALE, VERIFYING, Island,
                    Item, OwnerLock, Store)

log = logging.getLogger(__name__)

DEMAND, CURRENT, NEXT, ASKED, FOLLOWING, QUEUE, GAP = 0, 1, 2, 3, 4, 5, 6

SCHEDULER_TICK_S = 1.0
CHUNK_TARGET_S = 0.25
CHUNK_MIN = 64 * 1024
CHUNK_MAX = 4 * 1024 * 1024
CHUNK_UNKNOWN = 256 * 1024
FSYNC_BYTES = 4 * 1024 * 1024
FSYNC_S = 2.0
DB_CHECKPOINT_S = 2.0
HOST_SLOW_S = 5.0
HOST_DOWN_S = 15.0
WORKER_ABANDON_S = 120.0
MAX_ABANDONED_PER_HOST = 2
PROBE_BACKOFF_MIN_S = 2.0
PROBE_BACKOFF_MAX_S = 60.0
TCP_PROBE_TIMEOUT_S = 1.5
SMB_PORT = 445
MAX_IO_WORKERS = 4
READ_WAIT_S = 30.0
PROBE_PATIENCE_S = 30.0
REVALIDATE_AFTER_S = 600.0
RENAME_RETRY_S = 10.0
SWEEP_S = 60.0
DEMAND_HOLD_S = 5.0
FAILED_FORGET_S = 60.0
TRANSIENT_RETRY_S = 5.0
FREE_FLOOR_MIN = 2 * 1024 ** 3
FREE_FLOOR_FRACTION = 0.05
GIB = 1024 ** 3
COMPLETE_ONLY_EXTS = {"m4a", "mp4", "m4b"}
TAIL_READING_EXTS = {"mp3", "ogg", "oga", "opus", "m4a", "mp4", "m4b"}   # their open reads the end
# Formats whose seek reads near its target (so a seek ahead is worth an island there). MP3 and Ogg
# seeks read every frame from the start, so for them the bytes simply come in order.
SEEKS_DIRECTLY_EXTS = {"flac", "wav", "wave", "aif", "aiff", "aifc", "w64", "rf64"}
ISLAND_ALIGN = 64 * 1024     # an island starts on a multiple of this
ISLAND_LEAD = 64 * 1024      # … this far before the byte wanted
JUMP_SLACK = 1024 * 1024     # wanted bytes this close past what's here just wait for the run to reach them
TAIL_BYTES = 256 * 1024      # fetched first for a format whose open reads its end
QUEUE_DEPTH = 200            # "Entire queue": at most this many songs ahead are looked at
HOST_WARM_S = 3600           # a saved speed under an hour old starts the next run
HOST_SAVE_S = 30.0
DEFAULTS = {name: value for name, value in settings.DEFAULTS.items() if name.startswith("net_")}
PREDOWNLOAD_DEPTH = {"off": 0, "next": 1, "next2": 2, "queue": QUEUE_DEPTH}
BYTES_PER_SAMPLE = {"PCM_S8": 1, "PCM_U8": 1, "PCM_16": 2, "PCM_24": 3, "PCM_32": 4, "FLOAT": 4, "DOUBLE": 8}
UP, SLOW, DOWN = "up", "slow", "down"


@dataclass(frozen=True)
class NotReady:
    """The song can't start yet: `phase` is "starting", "preparing", "downloading", "checking",
    "host-down" or "fetching"; `percent` the progress (None before it's known)."""
    phase: str
    percent: int | None
    reason: str
    detail: str


@dataclass(frozen=True)
class Unavailable:
    """The song can't be played from the cache: `permanent` (gone, refused — skip it) or not (the
    disk is full — stop here). `action` names a setting that would help, if any."""
    reason: str
    permanent: bool
    action: str | None = None


@dataclass(frozen=True)
class Progress:
    state: str
    fraction: float          # of the file on disk
    speed: float | None      # bytes per second, this host
    copy: str                # LocalTrack.copy_state of the current version
    checks: str = ""         # the checks its complete copy passed (Item.verified_by), e.g. "size+identity+decode"
    quality: str = ""        # its server's: "excellent" | "good" | "poor" | "unstable" ("" not known yet)
    spans: tuple = ()        # what of the track is on this computer: ((from, to), …) as fractions of it
    rate: float | None = None   # bytes per second of music this song takes (its average)
    host: str = ""           # its server's name
    host_state: str = "up"   # "up" | "slow" | "down"
    error: str = ""          # why it couldn't be downloaded, if it couldn't


@dataclass(frozen=True)
class NetworkNow:
    """The network as the song playing (or the last one fetched) sees it, for Settings."""
    host: str
    state: str               # "up" | "slow" | "down"
    quality: str             # "" until measured
    speed: float | None      # bytes per second
    song_rate: float | None  # bytes per second the song playing takes (None: no network song playing)


@dataclass(frozen=True)
class CacheView:
    used: int
    songs: int
    ready: int
    hosts: dict


class _Abandoned(Exception):
    """This I/O thread was given up on while its call was stuck: its result is thrown away."""


class _Worker:
    def __init__(self):
        self.thread: threading.Thread | None = None
        self.abandoned = False
        self.call_since: float | None = None


class Host:
    """One server (or "cloud" for OneDrive): its health, speed and the one transfer it's doing."""

    def __init__(self, name: str, kind: str, clock=time.monotonic):
        self.name, self.kind = name, kind
        self.state = UP
        self.speed = policy.Speed(clock)
        self.score = policy.QualityScore()   # the quality class, with hysteresis
        self.stalls: deque = deque()         # when songs from it paused for the network
        self.last_active = 0.0               # when a chunk last arrived
        self.saved_bytes = -1                # speed.total_bytes when its figures were last saved
        self.assigned: Item | None = None
        self.cond = threading.Condition()
        self.worker: _Worker | None = None
        self.stuck: list[_Worker] = []
        self.backoff = PROBE_BACKOFF_MIN_S
        self.next_probe = 0.0
        self.down_since: float | None = None
        self.probing = False
        self.told = False        # the "hasn't answered for a minute" notice was given
        self.calls = 0           # calls started (tests count them)

    def busy(self) -> bool:
        w = self.worker
        return w is not None and w.call_since is not None


def tcp_probe(host: str, timeout: float = TCP_PROBE_TIMEOUT_S) -> bool:
    """Is the server answering? A TCP connection to its file-sharing port, nothing sent."""
    try:
        with socket.create_connection((host, SMB_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _ext_of(text: str) -> str:
    name = text.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


class Manager:
    """See the module note. `player` needs only `upcoming(n)` (lock-free). Everything that reaches
    outside — fetchers, the TCP probe, disk space, clocks — can be replaced for tests."""

    def __init__(self, folder: Path, prefs: dict | None = None, player=None, *, classifier: Classifier | None = None,
                 fetcher=ShareFetcher, probe_host=tcp_probe, disk_usage=shutil.disk_usage,
                 clock=time.monotonic, wall=time.time, owner: OwnerLock | None = None,
                 watch_network=netwatch.start):
        self.folder = Path(folder)
        self.prefs = prefs if prefs is not None else {}
        self.player = player
        self.classifier = classifier or Classifier()
        self.store = Store(self.folder)
        self._fetcher_factory = fetcher
        self._probe_host = probe_host
        self._disk_usage = disk_usage
        self._clock, self._wall = clock, wall
        self._owner = owner
        self._watch_network = watch_network
        self._network_watch = None
        self._net_changed = threading.Event()
        self._lock = threading.RLock()
        self.items: dict[str, Item] = {}
        self.hosts: dict[str, Host] = {}
        self._paths: dict[str, object] = {}         # key -> a path that names it (for the plan)
        self._explicit: dict[str, tuple[str, int]] = {}   # role -> (key, priority) from prepare()
        self._asks: dict[str, int] = {}
        self._demands: dict[str, float] = {}
        self._positions: dict[str, tuple] = {}    # key -> (path, seconds into the track, when told)
        self._ranges: dict[str, dict] = {}        # key -> {role: (gen, start, end, when)}: readers waiting
        self._stall_counts: dict[str, int] = {}   # key -> the pauses for the network the player last reported
        self._stall_times: dict[str, deque] = {}  # key -> when each began
        self._current_key: str | None = None      # the song playing (or about to), if it's a network one
        self._warm: dict[str, dict] = {}          # host -> its saved figures, until it's first used
        self._last_host_save = -1e18
        self._pct_shown: dict[str, tuple] = {}    # key -> (at, percent): "Preparing… N%" never goes back
        self.narrowed = False                     # the link can't fetch this song and the next in time: only they are
        self._waiters: dict[int, tuple] = {}
        self._tokens = itertools.count(1)
        self._fetchers: dict[str, object] = {}
        self._stale: list[tuple[Item, int, Path | None]] = []   # old versions waiting to be deleted
        self._renames: dict[str, float] = {}       # verified, not yet renamed: key -> first try
        self._inbox: queue.SimpleQueue = queue.SimpleQueue()
        self._verify_queue: queue.Queue = queue.Queue()
        self._probe_hosts: queue.Queue = queue.Queue()
        self._probe_wake = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._loaded = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_checkpoint = 0.0
        self._last_sweep = -1e18
        self._sweep_due = False
        self._told_full = False
        self.notices: deque = deque(maxlen=20)
        self.should_yield = lambda: False   # the player's "buffer below half" (set by the engine)
        self.tidied: dict = {}

    # -- settings -----------------------------------------------------------------------------
    def _pref(self, name: str):
        value = self.prefs.get(name, DEFAULTS[name])
        return DEFAULTS[name] if value is None else value

    def _limit(self) -> float:
        gb = self._pref("net_cache_gb")
        return float("inf") if not gb else float(gb) * GIB

    # -- lifetime -----------------------------------------------------------------------------
    def start(self) -> "Manager":
        self.folder.mkdir(parents=True, exist_ok=True)
        if self._watch_network is not None:
            try:
                self._network_watch = self._watch_network(self._network_changed)
            except Exception:   # noqa: BLE001 — only a quicker way back: the back-off still works
                log.warning("music cache: can't hear network changes", exc_info=True)
        for name, target in (("netcache-scheduler", self._run_scheduler), ("netcache-probe", self._run_probe),
                             ("netcache-probe-hosts", self._run_host_probes), ("netcache-verify", self._run_verify)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()
        return self

    def wait_loaded(self, timeout: float | None = None) -> bool:
        return self._loaded.wait(timeout)

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop every thread (an I/O thread finishes the chunk it's on and makes it durable), save
        what's on disk, and let go of the cache."""
        self._stop.set()
        self._wake.set()
        self._probe_wake.set()
        watch, self._network_watch = self._network_watch, None
        if watch is not None:
            watch.close()
        with self._lock:
            hosts = list(self.hosts.values())
        for host in hosts:
            with host.cond:
                host.cond.notify_all()
        for item in list(self.items.values()):
            item.notify()
        for host in hosts:
            w = host.worker
            if w is not None and w.thread is not None:
                w.thread.join(timeout)
        for thread in self._threads:
            thread.join(timeout)
        if not any(thread.is_alive() for thread in self._threads):
            try:
                self._checkpoint(force=True)   # (every writer has stopped: this is the only one now)
            except Exception:   # noqa: BLE001
                log.exception("music cache: saving at shutdown")
            self.store.close()
        for fetcher in list(self._fetchers.values()):
            try:
                fetcher.close()
            except Exception:   # noqa: BLE001
                pass
        if self._owner is not None:
            self._owner.release()

    # -- items --------------------------------------------------------------------------------
    def _describe(self, path) -> tuple[Kind, str, str]:
        """(kind, key, host) for a path: classification only, done outside the lock."""
        kind = self.classifier.classify(path)
        if kind is Kind.LOCAL or kind is Kind.URL:
            return kind, "", ""
        return kind, self.classifier.source_key(path), self.classifier.host_of(path)

    def _get(self, key: str, path, kind: Kind, host: str) -> Item:
        """The item for `key`, made (in memory) if new. Call with the lock held."""
        item = self.items.get(key)
        if item is None:
            real = os.fspath(audiofile.real_path(path))
            source = (self.classifier.unc(path) or real) if kind is Kind.SHARE else real
            item = Item(self.store, key=key, kind=kind.value, source=source, host=host, ext=_ext_of(real))
            self._wire(item)
            self.items[key] = item
        self._paths.setdefault(key, path)
        if host and host not in self.hosts:
            self._add_host(host, kind.value)
        return item

    def _add_host(self, name: str, kind: str) -> Host:
        """A server's record, starting from its saved figures if they're recent. Lock held."""
        host = self.hosts[name] = Host(name, kind, self._clock)
        saved = self._warm.pop(name, None)
        if saved is not None:
            age = self._wall() - (saved.get("measured") or 0)
            host.speed.warm(saved.get("speed"), age, saved.get("gap") or 0.0, saved.get("failures") or 0,
                            keep=HOST_WARM_S)
            if saved.get("quality") in policy.RANK and 0 <= age < HOST_WARM_S:
                host.score = policy.QualityScore(saved["quality"], self._clock() - age)
            host.saved_bytes = 0
        return host

    def _wire(self, item: Item) -> None:
        item.on_demand = self._on_demand

    def _on_demand(self, item: Item, gen: int, start: int, end: int, role: str) -> None:
        """A reader is waiting for bytes (called from the reader's own thread: quick). The player
        and the proof make the song urgent; every reader's wait says where to fetch next."""
        with self._lock:
            now = self._clock()
            if role in ("play", "proof"):
                self._demands[item.key] = now
            self._ranges.setdefault(item.key, {})[role] = (gen, start, end, now)
        self._wake.set()

    def _network_changed(self) -> None:
        """Windows says the network changed (called on a thread of Windows' own: quick, no lock)."""
        self._net_changed.set()
        self._wake.set()

    # -- the preparer interface (no I/O) -------------------------------------------------------
    def prepare(self, path, *, role: str = "play", at: float = 0.0, force: bool = False):
        """A LocalTrack to play now, NotReady (and the download is wanted at this role's
        priority), or Unavailable. `role`: "play" (the song about to play), "next" (the one after
        it, as the decoder lines it up) or "join" (that one, while the song before it is about to
        end: wanted as urgently as a reader waiting for bytes)."""
        kind, key, host = self._describe(path)
        if kind is Kind.LOCAL:
            return audiofile.LocalTrack.plain(path)
        if kind is Kind.URL:
            return Unavailable("it's a web address: playing music from the internet isn't supported yet", True)
        if not self._loaded.is_set():
            return NotReady("starting", None, "starting", "Getting the music cache ready…")
        with self._lock:
            item = self._get(key, path, kind, host)
            self._explicit["play" if role == "play" else "next"] = (key, CURRENT if role == "play" else NEXT)
            if role == "play":
                self._positions[key] = (path, at, self._clock())
            elif role == "join":
                self._demands[key] = self._clock()
            item.last_used = self._wall()
            result = self._evaluate(item, path, at, force)
        self._wake.set()
        return result

    def notify_when_startable(self, path, at: float, post) -> int:
        """Call `post()` once, from the scheduler, when the song may start at `at`."""
        token = next(self._tokens)
        with self._lock:
            self._waiters[token] = (path, at, post)
        self._wake.set()
        return token

    def forget(self, token) -> None:
        with self._lock:
            self._waiters.pop(token, None)

    def resumable(self, local, frame: int, stalls: int, quick: bool = False) -> bool:
        """After a pause for the network: may playback carry on from `frame` (of the track)? The
        start rule there, asking for twice as much after each recent pause (`stalls`). `quick`:
        before the pause is heard: carry on once policy.QUICK_RESUME_S is here (or all of it)."""
        item = getattr(local, "item", None)
        if item is None:
            return True
        with self._lock:
            if not quick:
                self._note_stalls(item, stalls)
            if local.gen != item.gen:
                return local.ready_path() is not None or item.has(local.gen, item.size_of(local.gen))
            rate = item.info.get("rate") or 0
            at = frame / rate if rate else 0.0
            if quick:
                return self._available(item, local.source, at) >= policy.QUICK_RESUME_S
            return self._startable_at(item, local.source, at, stalls=stalls)

    def outlook(self, x, at: float = 0.0):
        """What prepare() would say now about starting (or carrying on) at `at` seconds, with
        nothing asked for: None (it may), NotReady (with its progress) or Unavailable. `x` is a
        path, or the LocalTrack playing (then about that very version of the copy)."""
        local = x if isinstance(x, audiofile.LocalTrack) else None
        if local is not None and not local.remote:
            return None
        path = local.source if local is not None else x
        kind, key, host = self._describe(path)
        if kind is Kind.LOCAL:
            return None
        if kind is Kind.URL:
            return Unavailable("it's a web address: playing music from the internet isn't supported yet", True)
        if not self._loaded.is_set():
            return NotReady("starting", None, "starting", "Getting the music cache ready…")
        with self._lock:
            item = local.item if local is not None else self.items.get(key)
            if item is None:
                return NotReady("preparing", None, "preparing", "Preparing track…")
            if local is not None and local.gen != item.gen:
                if local.ready_path() is not None or item.has(local.gen, item.size_of(local.gen)):
                    return None
                return Unavailable("the file on the drive changed while it was playing (it will play in full "
                                   "next time)", True)
            if item.state == FAILED and item.permanent:
                return Unavailable(item.error or "it couldn't be downloaded", True)
            if item.error == "disk":
                return Unavailable(self._disk_message(item), False)
            ok, need, measured = self._rule(item, path, at)
            if ok:
                return None
            return self._not_ready(item, path, at, need, measured)

    def plan_changed(self) -> None:
        self._wake.set()

    def settings_changed(self) -> None:
        """Network playback settings changed (the cache's size, how long copies are kept): tidy now."""
        self._sweep_due = True
        self._wake.set()

    def position_hint(self, path, seconds: float) -> None:
        kind, key, _ = self._describe(path)
        if kind is Kind.LOCAL or not key:
            return
        with self._lock:
            self._positions[key] = (path, seconds, self._clock())
        self._wake.set()

    def _note_stalls(self, item: Item, stalls: int) -> None:
        """The player counts `stalls` recent pauses for the network on this song: a rise is a new
        pause (it counts toward the server's quality, and a later start of the song waits longer).
        Lock held."""
        before = self._stall_counts.get(item.key, 0)
        self._stall_counts[item.key] = stalls
        if stalls <= before:
            return
        now = self._clock()
        host = self.hosts.get(item.host)
        times = self._stall_times.setdefault(item.key, deque(maxlen=16))
        for _ in range(stalls - before):
            times.append(now)
            if host is not None:
                host.stalls.append(now)

    def _recent_stalls(self, key: str, now: float) -> int:
        return sum(1 for when in self._stall_times.get(key, ()) if now - when <= policy.STALL_STEP_WINDOW_S)

    def progress(self, path) -> Progress | None:
        kind, key, host = self._describe(path)
        if kind is Kind.LOCAL or not key:
            return None
        with self._lock:
            item = self.items.get(key)
            if item is None:
                return None
            h = self.hosts.get(item.host)
            error = item.error if item.state == FAILED or (item.error and item.error != "disk") else ""
            return Progress(item.state, item.fraction(), h.speed.value() if h else None, item.copy_state(item.gen),
                            item.verified_by or "", quality=h.score.current if h else "",
                            spans=self._track_spans(item, path), rate=self._song_rate(item),
                            host=item.host, host_state=h.state if h else UP, error=error or "")

    def _song_rate(self, item: Item | None) -> float | None:
        """Bytes per second of music the song takes, on average (None before it's known)."""
        if item is None:
            return None
        rate, frames = item.info.get("rate"), item.info.get("frames")
        if not item.size or not rate or not frames:
            return None
        return max(1, item.size - int(item.info.get("header_bytes") or 0)) / (frames / rate)

    def _track_spans(self, item: Item, path) -> tuple:
        """What of this track is on disk, as fractions of the track: ((from, to), …). Bytes are
        mapped to time by the file's average density (exactly, for the run a FLAC scan has seen)."""
        if item.state == READY or item.complete:
            return ((0.0, 1.0),)
        S = item.size
        runs = item.spans()
        if not S or not runs:
            return ()
        info = item.info
        rate, frames = info.get("rate"), info.get("frames")
        if not rate or not frames:
            return tuple((a / S, b / S) for a, b in runs)
        D = frames / rate
        H = int(info.get("header_bytes") or 0)
        cue = audiofile.cue_of(path)
        t0 = cue.start / rate if cue is not None and cue.start >= 0 else 0.0
        t1 = min(cue.end, frames) / rate if cue is not None and cue.end is not None else D
        if t1 <= t0:
            return ()
        scanned = item.scanner.end_sample() if item.scanner is not None else None

        def time_of(b: int) -> float:
            if b >= S:
                return D
            return max(0.0, min(D, (b - H) / max(1, S - H) * D))

        spans = []
        for a, b in runs:
            ta = 0.0 if a == 0 else time_of(a)
            tb = scanned / rate if a == 0 and scanned is not None and b < S else time_of(b)
            lo, hi = max(ta, t0), min(tb, t1)
            if hi > lo:
                spans.append(((lo - t0) / (t1 - t0), (hi - t0) / (t1 - t0)))
        return tuple(spans)

    def network_now(self) -> NetworkNow | None:
        """How the network looks for the song playing (or the server last fetched from), for
        Settings: None when no network song has been asked for yet."""
        with self._lock:
            item = self.items.get(self._current_key) if self._current_key else None
            host = self.hosts.get(item.host) if item is not None else None
            if host is None:
                host = max(self.hosts.values(), key=lambda h: h.last_active, default=None)
            if host is None:
                return None
            return NetworkNow(host.name, host.state, host.score.current, host.speed.value(), self._song_rate(item))

    # -- whole-file readers (audiofile's resolver) ---------------------------------------------
    def resolve(self, path, *, cancel=None, wait=None):
        """For audiofile.open of a network path by name: the complete copy (a LocalTrack), None for
        a local path, else NotCached / DownloadFailed. Waits (as an ASKED download) on any thread but
        the window's, up to READ_WAIT_S or until `cancel()`."""
        kind, key, host = self._describe(path)
        if kind is Kind.LOCAL:
            return None
        if kind is Kind.URL:
            raise audiofile.DownloadFailed("web addresses aren't supported yet")
        if wait is None:
            wait = threading.current_thread() is not threading.main_thread()
        deadline = self._clock() + READ_WAIT_S
        if not self._loaded.is_set():
            if not wait or not self._loaded.wait(READ_WAIT_S):
                raise audiofile.NotCached("the music cache is still starting", None, "starting")
        with self._lock:
            item = self._get(key, path, kind, host)
            ready = self._ready_track(item, path)
            if ready is not None:
                return ready
            if item.state == FAILED and item.permanent:
                raise audiofile.DownloadFailed(item.error or "the file can't be downloaded")
            if not wait:
                raise audiofile.NotCached("this network song hasn't been downloaded yet", item.fraction(), "not-cached")
            self._asks[key] = self._asks.get(key, 0) + 1
        self._wake.set()
        try:
            while True:
                if cancel is not None and cancel():
                    raise audiofile.NotCached("no longer wanted", item.fraction(), "cancel")
                with self._lock:
                    ready = self._ready_track(item, path)
                    failed = item.state == FAILED and item.permanent
                if ready is not None:
                    return ready
                if failed:
                    raise audiofile.DownloadFailed(item.error or "the file can't be downloaded")
                left = deadline - self._clock()
                if left <= 0 or self._stop.is_set():
                    raise audiofile.NotCached("it took too long to download", item.fraction(), "timeout")
                with item.cond:   # (woken by every chunk and every change of state)
                    item.cond.wait(min(left, 0.1))
        finally:
            with self._lock:
                count = self._asks.get(key, 0) - 1
                if count > 0:
                    self._asks[key] = count
                else:
                    self._asks.pop(key, None)

    def _ready_track(self, item: Item, path):
        if item.state == READY:
            item.last_used = self._wall()
            return audiofile.LocalTrack(path, item, item.gen)
        return None

    def known_info(self, path) -> audiofile.Info | None:
        """What the probe found about a network song (audiofile.info's shortcut), or None."""
        kind, key, _ = self._describe(path)
        if kind is Kind.LOCAL or not key:
            return None
        with self._lock:
            item = self.items.get(key)
            info = dict(item.info) if item is not None else {}
        if not {"format", "subtype", "rate", "channels", "frames"} <= info.keys():
            return None
        frames = int(info["frames"])
        cue = audiofile.cue_of(path)
        if cue is not None:
            end = frames if cue.end is None else min(cue.end, frames)
            frames = max(0, end - max(0, cue.start))
        return audiofile.Info(info["format"], info["subtype"], int(info["rate"]), int(info["channels"]), frames)

    def invalidate(self, path) -> None:
        """The source was changed through the player (its tags saved): drop the copy."""
        kind, key, _ = self._describe(path)
        if kind is Kind.LOCAL or not key:
            return
        with self._lock:
            item = self.items.get(key)
            if item is not None:
                self._new_generation(item, None)
        self._wake.set()

    def snapshot(self) -> CacheView:
        with self._lock:
            items = list(self.items.values())
            hosts = {name: h.state for name, h in self.hosts.items()}
        used = sum(self._bytes_held(item) for item in items)
        return CacheView(used, len(items), sum(1 for i in items if i.state == READY), hosts)

    def empty(self) -> threading.Event:
        """Remove everything that isn't in use (done on the scheduler thread; the event is set then)."""
        done = threading.Event()
        self._inbox.put(("empty", done))
        self._wake.set()
        return done

    # -- the start rule -------------------------------------------------------------------------
    def _verify_on(self) -> bool:
        return bool(self._pref("net_verify"))

    def _playable_complete(self, item: Item) -> bool:
        """Every byte here and allowed to play as a complete copy (checked, or checks are off)."""
        if item.state == READY:
            return True
        if not (item.complete and item.transfer_ok):
            return False
        return bool(item.verified_by) or not self._verify_on()

    def _measure(self, item: Item, path, at: float):
        """(p, L, D, H, fmt, rate, frame_bytes) for the start rule, or None before the probe."""
        info = item.info
        rate, frames = info.get("rate"), info.get("frames")
        if not rate or frames is None:
            return None
        D = frames / rate
        cue = audiofile.cue_of(path)
        start = cue.start / rate if cue is not None and cue.start >= 0 else 0.0
        end = (min(cue.end, frames) / rate) if cue is not None and cue.end is not None else D
        p = start + max(0.0, at)
        fmt = info.get("format", "")
        width = BYTES_PER_SAMPLE.get(info.get("subtype", ""), 0)
        frame_bytes = width * int(info.get("channels") or 0) if fmt in policy.EXACT_FORMATS else 0
        return p, max(0.0, end - p), D, int(info.get("header_bytes") or 0), fmt, rate, frame_bytes

    def _rule(self, item: Item, path, at: float, stalls: int = 0, force: bool = False):
        """(may start, need, measured) — the start rule of policy.py for this item at `at`."""
        complete_only = item.ext in COMPLETE_ONLY_EXTS or item.info.get("complete_only")
        if self._playable_complete(item):
            # (Apple Lossless is only ever opened by name: not until its copy has its final name.)
            return item.state == READY or not complete_only, (0.0 if item.state == READY or not complete_only
                                                              else None), None
        measured = self._measure(item, path, at)
        if complete_only or item.size is None or measured is None:
            return False, None, measured
        p, L, D, H, fmt, rate, frame_bytes = measured
        S = item.size
        host = self.hosts.get(item.host)
        now = self._clock()
        v = host.speed.value() if host else None
        gap = host.speed.gap15(now) if host else 0.0
        failures = host.speed.failures15(now) if host else 0
        dense = policy.rho(S, H, D, fmt, rate, frame_bytes)
        m = v / dense if v else None
        quality = self._quality(host, m, gap, failures, now)
        eta = (S - item.held()) / v if v else None
        need = policy.need_seconds(self._pref("net_mode"), L, float(self._pref("net_buffer")), m, gap, quality, eta)
        # After pauses for the network on this song (also before a device let go carries on) it
        # waits for twice as much after each one.
        stalls = max(stalls, self._recent_stalls(item.key, now))
        if stalls:
            need = policy.resume_need(need, stalls, L)
        A = self._seconds_at(item, measured)
        ok = policy.startable(open_ready=item.open_ready or item.complete, ready=False, complete=item.complete,
                              A=A, need=need, forced=force)
        return ok, need, measured

    def _quality(self, host: Host | None, m, gap: float, failures: int, now: float) -> str:
        """The quality class for a song from `host`: as measured now for this song, but never
        better than the server's own class (which rises only after a minute at the better level)."""
        stalls2 = sum(1 for when in host.stalls if now - when <= policy.STALLS_UNSTABLE_S) if host else 0
        measured = policy.quality(m, gap, failures, stalls2)
        if host is not None and host.score.current:
            return policy.worse(measured, host.score.current)
        return measured

    def _startable_at(self, item: Item, path, at: float, stalls: int = 0, force: bool = False) -> bool:
        return self._rule(item, path, at, stalls, force)[0]

    def _bytes_of(self, item: Item, measured) -> tuple[int, int]:
        """(no earlier than, no later than): where the frame holding second p starts. Exact for
        WAV/AIFF and for what a FLAC scan has seen; between two SEEKTABLE points for FLAC; else
        the average density."""
        p, L, D, H, fmt, rate, frame_bytes = measured
        S = int(item.size or 0)
        average = max(1, S - H) / D if D else policy.TYPICAL_RHO
        points = item.info.get("seek_points") if fmt == "FLAC" else None
        lo = policy.byte_at(p, H, average, fmt, frame_bytes, rate, item.scanner, points)
        hi = lo
        if points and rate and not (item.scanner is not None and item.scanner.byte_at(int(p * rate)) is not None):
            sample = int(p * rate)
            later = [offset for point, offset in points if point > sample]
            hi = H + later[0] if later else S
        return lo, max(lo, hi)

    def _seconds_at(self, item: Item, measured) -> float:
        """A(p): seconds of music from p on disk in one run — in the run from the start or, for a
        format whose seek reads near its target, in the island holding p."""
        p, L, D, H, fmt, rate, frame_bytes = measured
        flac_end = item.scanner.end_sample() if item.scanner is not None else None
        A = policy.available_seconds(item.visible, p, S=item.size, H=H, D=D, fmt=fmt, rate=rate,
                                     frame_bytes=frame_bytes, flac_end_sample=flac_end)
        if A > 0 or not item.islands or item.ext not in SEEKS_DIRECTLY_EXTS:
            return A
        lo, hi = self._bytes_of(item, measured)
        for island in item.islands:
            if island.start <= lo and hi < island.visible:
                return policy.run_seconds(island.visible, hi, p, S=item.size, H=H, D=D, fmt=fmt, rate=rate,
                                          frame_bytes=frame_bytes)
        return 0.0

    def _available(self, item: Item, path, at: float) -> float:
        """Seconds of music from `at` on disk in one run (all the rest of it: infinity)."""
        if self._playable_complete(item) or item.complete:
            return float("inf")
        measured = self._measure(item, path, at)
        if measured is None or item.size is None or not item.open_ready:
            return 0.0
        A = self._seconds_at(item, measured)
        return float("inf") if A >= measured[1] else A

    def _evaluate(self, item: Item, path, at: float, force: bool):
        """prepare()'s answer for `item` (lock held)."""
        if item.state == FAILED and item.retry_at is not None and self._wall() >= item.retry_at:
            item.state, item.error, item.permanent, item.dirty = QUEUED, None, False, True
        if item.state == FAILED and item.permanent:
            return Unavailable(item.error or "it couldn't be downloaded", True)
        if item.error == "disk":
            return Unavailable(self._disk_message(item), False)
        ok, need, measured = self._rule(item, path, at, force=force)
        if ok:
            return audiofile.LocalTrack(path, item, item.gen)
        return self._not_ready(item, path, at, need, measured)

    def _not_ready(self, item: Item, path, at: float, need, measured) -> NotReady:
        host = self.hosts.get(item.host)
        if host is not None and host.state == DOWN:
            server = item.source[:2 + len(item.host)] if item.source.startswith("\\\\") else item.host
            return NotReady("host-down", None, "host-down",
                            f"waiting for {server} (not answering) · starts by itself when it's back")
        complete_mode = need is None
        if item.state in (DOWNLOADED, VERIFYING) and complete_mode:
            return NotReady("checking", None, "checking", "Checking the download…")
        if item.size is None or not item.size:
            return NotReady("preparing", None, "preparing", "Preparing track…")
        if complete_mode:
            pct = policy.percent(item.held(), item.size, True, item.size)
            return NotReady("downloading", pct, "downloading", f"Downloading track… {pct}%")
        share = policy.covered(item.spans(), self._needed_ranges(item, measured, need))
        pct = min(99, int(100 * share))
        shown = self._pct_shown.get(item.key)
        if shown is not None and shown[0] == (at, item.gen):
            pct = max(pct, shown[1])   # (what's needed can grow once the probe knows more: never shown going back)
        self._pct_shown[item.key] = ((at, item.gen), pct)
        return NotReady("preparing", pct, "preparing", self._slower(item, need) or f"Preparing track… {pct}%")

    def _slower(self, item: Item, need) -> str:
        """While its server answers, but more slowly than the song plays: what the wait is for."""
        host = self.hosts.get(item.host)
        rate = self._song_rate(item)
        v = host.speed.value() if host is not None and host.state != DOWN else None
        if not v or not rate or need is None or v >= rate:
            return ""
        return f"the download is slower than the music: building up {need:.0f} s"

    def _needed_ranges(self, item: Item, measured, need) -> list[tuple[int, int]]:
        """The bytes that must be here to start: what the decoder's open reads (once the probe
        knows; before that the head, and the tail for a format whose open reads its end), and
        `need` seconds of music from p (from the start, for a format whose seek reads from there)."""
        S = int(item.size or 0)
        spans = item.info.get("open_spans")
        if spans:
            ranges = [(int(a), int(b)) for a, b in spans]
        elif item.open_end is not None:
            ranges = [(0, int(item.open_end))]
        else:
            ranges = [(0, min(S, policy.HEAD_ONLY_EXTRA))]
            if item.ext in TAIL_READING_EXTS:
                ranges.append((max(0, S - TAIL_BYTES), S))
        if measured is not None and need is not None:
            p, L, D, H, fmt, rate, frame_bytes = measured
            dense = policy.rho(S, H, D, fmt, rate, frame_bytes)
            lo, hi = self._bytes_of(item, measured)
            start = lo if item.ext in SEEKS_DIRECTLY_EXTS and lo > item.visible + JUMP_SLACK else 0
            ranges.append((start, min(S, int(hi + need * dense))))
        return _union(ranges)

    def _disk_message(self, item: Item) -> str:
        drive = os.path.splitdrive(str(self.folder))[0] or str(self.folder)
        need = max(0, (item.size or 0) - item.visible)
        return (f"Not enough free space on {drive} for the music cache (needs {need / 1e6:.0f} MB): "
                f"free some space or make the cache smaller")

    # -- the scheduler ------------------------------------------------------------------------
    def _run_scheduler(self) -> None:
        try:
            loaded = self.store.open()
            kept, self.tidied = self.store.tidy(loaded)
            warm = self.store.load_hosts()
        except Exception:   # noqa: BLE001 — the cache must never stop the player
            log.exception("couldn't open the music cache's database")
            kept, warm = [], []
        with self._lock:
            self._warm.update({row["host"]: row for row in warm if row.get("host")})   # (used as each is named)
            for item in kept:
                self._wire(item)
                self.items.setdefault(item.key, item)
                if item.host and item.host not in self.hosts:
                    self._add_host(item.host, item.kind)
                if item.state == READY or self._open_bytes_here(item):
                    item.open_ready = True
                if item.state == DOWNLOADED:
                    item.transfer_ok = True
                    self._verify_queue.put((item, item.gen))
        if self.store.set_aside is not None:
            self.notices.append("The music cache's list was damaged, so the cache was emptied "
                                "(songs will download again).")
        if self.tidied:
            log.info("music cache ready: %d item(s); tidied %s", len(kept), self.tidied)
        self._loaded.set()
        try:
            self._sweep(force=True)
        except Exception:   # noqa: BLE001
            log.exception("music cache: the start-up sweep failed")
        while not self._stop.is_set():
            self._wake.wait(SCHEDULER_TICK_S)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._tick()
            except Exception:   # noqa: BLE001 — log and carry on: one bad tick mustn't stop downloads
                log.exception("music cache scheduler")

    def _tick(self) -> None:
        now = self._clock()
        if self._net_changed.is_set():
            self._net_changed.clear()
            self._after_network_change(now)
        if self.classifier.due():
            self.classifier.refresh()
        while True:
            try:
                message = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._handle(message)
        self._replan(now)
        self._score_hosts(now)
        self._watch_hosts(now)
        self._assign(now)
        self._try_renames(now)
        self._checkpoint()
        self._post_waiters()
        if self._sweep_due or now - self._last_sweep >= SWEEP_S:
            self._sweep()

    def _handle(self, message) -> None:
        kind = message[0]
        if kind == "verified":
            self._verified(*message[1:])
        elif kind == "empty":
            self._sweep(everything=True)
            message[1].set()

    # -- the plan ---------------------------------------------------------------------------------
    def _upcoming(self, n: int) -> list:
        upcoming = getattr(self.player, "upcoming", None)
        if upcoming is None:
            return []
        try:
            return list(upcoming(n))
        except Exception:   # noqa: BLE001 — the player's list changed under us: next tick
            log.debug("upcoming() failed", exc_info=True)
            return []

    def _replan(self, now: float) -> None:
        """Every item's priority from the player's plan, prepare()'s roles, tools' asks and readers'
        demands. Pre-download sets how far ahead (Off: only the song playing; the next one is
        asked for at the join), within the budget for songs not heard yet; when the link can't
        fetch this song and the next before this one ends, only those two. Items dropping out of
        the plan keep their bytes (resumable)."""
        depth = PREDOWNLOAD_DEPTH.get(self._pref("net_predownload"), 1)
        upcoming = self._upcoming(depth + 1)
        window: list[tuple] = []   # (position, key, path, kind, host)
        for position, (_, path) in enumerate(upcoming):
            kind, key, host = self._describe(path)
            if kind is Kind.LOCAL or kind is Kind.URL:
                continue
            window.append((position, key, path, kind, host))
        wants: dict[str, int] = {}
        with self._lock:
            for _, key, path, kind, host in window:
                self._get(key, path, kind, host)
            self._current_key = next((key for position, key, *_ in window if position == 0), None)
            ahead = [entry for entry in window if 1 <= entry[0] <= depth]
            # Songs not heard yet use at most half the cache limit (the next one always comes).
            fits = policy.within_budget([self.items[key].size for _, key, *_ in ahead], self._limit())
            self.narrowed = not self._feasible(window, now)
            if self.narrowed:
                fits = min(fits, 1)
            chosen = {key for _, key, *_ in ahead[:fits]}
            extra = []
            for position, key, path, kind, host in window:
                if position == 0 or key in chosen:
                    rank = (CURRENT if position == 0 else NEXT if position == 1 else FOLLOWING if position == 2
                            else QUEUE)
                    wants[key] = min(rank, wants.get(key, rank))
                elif position > depth and depth and not self.narrowed:   # (Off: nothing ahead at all)
                    extra.append(key)
            for role, (key, rank) in self._explicit.items():
                if key in self.items:
                    wants[key] = min(rank, wants.get(key, rank))
            for key in self._asks:
                if key in self.items:
                    wants[key] = min(ASKED, wants.get(key, ASKED))
            for key, roles in list(self._ranges.items()):
                for role, wanted in list(roles.items()):
                    if now - wanted[3] > DEMAND_HOLD_S:
                        del roles[role]
                if not roles:
                    del self._ranges[key]
            for key, since in list(self._demands.items()):
                if now - since > DEMAND_HOLD_S:
                    del self._demands[key]
                elif key in self.items:
                    wants[key] = DEMAND
            # "One more": everything wanted is complete and the links are idle -> the song after.
            if extra and all(self.items[k].state == READY for k in wants) and not any(
                    h.assigned is not None for h in self.hosts.values()):
                wants.setdefault(extra[0], FOLLOWING)
            for item in self.items.values():
                rank = wants.get(item.key)
                if rank is not None and CURRENT <= rank and item.islands and self._only_gaps_left(item):
                    rank = GAP   # what plays from here on is here: the stretch a seek skipped can wait
                item.priority = rank

    def _feasible(self, window, now: float) -> bool:
        """Can the link fetch the rest of the song playing and the next one before it ends? Lock held."""
        current = next((entry for entry in window if entry[0] == 0), None)
        following = next((entry for entry in window if entry[0] == 1), None)
        if current is None or following is None:
            return True
        item, after = self.items[current[1]], self.items[following[1]]
        host = self.hosts.get(item.host)
        rate, frames = item.info.get("rate"), item.info.get("frames")
        if host is None or not rate or not frames or item.size is None or after.size is None:
            return True
        told = self._positions.get(item.key)
        played = (told[1] + (now - told[2])) if told is not None else 0.0
        measured = self._measure(item, current[2], played)
        left = measured[1] if measured is not None else 0.0
        return policy.feasible(host.speed.value(), left, item.size - item.held(), after.size - after.held())

    def _only_gaps_left(self, item: Item) -> bool:
        """Everything from where the song plays to its end is here; only stretches behind are missing."""
        if item.size is None or item.complete:
            return False
        if any(r[0] == item.gen and not item.has_range(r[0], r[1], r[2])
               for r in self._ranges.get(item.key, {}).values()):
            return False
        cursor = self._cursor_byte(item)
        return cursor is not None and cursor > item.visible and item.has_range(item.gen, cursor, item.size)

    def _cursor_byte(self, item: Item) -> int | None:
        """Where the song plays from (the start, or the last seek), in bytes: None before the probe."""
        told = self._positions.get(item.key)
        if told is None:
            return 0
        measured = self._measure(item, told[0], told[1])
        if measured is None or not item.size:
            return None
        return self._bytes_of(item, measured)[0]

    def _needs_bytes(self, item: Item, now: float) -> bool:
        if item.priority is None:
            return False
        if item.state == FAILED:
            if item.retry_at is not None and self._wall() >= item.retry_at:
                item.state, item.error, item.permanent = QUEUED, None, False
            else:
                return False
        if item.state == READY:
            return self._wall() - (item.verified_at or 0) > REVALIDATE_AFTER_S
        if item.state in (DOWNLOADED, VERIFYING, STALE):
            return False
        if item.retry_at is not None and self._wall() < item.retry_at:
            return False
        return item.size is None or item.visible < item.size

    def _assign(self, now: float) -> None:
        with self._lock:
            by_host: dict[str, list[Item]] = {}
            for item in self.items.values():
                if item.host and self._needs_bytes(item, now):
                    by_host.setdefault(item.host, []).append(item)
            for name, host in self.hosts.items():
                best = None
                if host.state == UP:
                    candidates = by_host.get(name, [])
                    for item in sorted(candidates, key=lambda i: (i.priority, -(i.visible or 0))):
                        if item.state == READY or self._room_for(item):
                            if item.error == "disk":
                                item.error = None
                            best = item
                            break
                        if item.priority <= CURRENT:
                            if item.error != "disk":
                                item.error = "disk"
                                self.notices.append(self._disk_message(item))
                        elif not self._told_full:
                            self._told_full = True
                            self.notices.append("The music cache is full: songs further ahead aren't being downloaded")
                with host.cond:
                    if host.assigned is not best:
                        host.assigned = best
                        host.cond.notify_all()
                if best is not None:
                    self._ensure_worker(host)

    def _shortfall(self, item: Item) -> int:
        """Bytes the rest of this download would take below the free-space floor (0: it fits)."""
        wanted = max(0, (item.size or 0) - item.visible)
        if not wanted:
            return 0
        try:
            usage = self._disk_usage(self.folder)
        except OSError:
            return 0
        floor = max(FREE_FLOOR_MIN, FREE_FLOOR_FRACTION * usage.total)
        return int(max(0, wanted + floor - usage.free))

    def _room_for(self, item: Item) -> bool:
        """Is there room on the disk for the rest of this download (after evicting what may go)?
        Scheduler thread only (it may sweep)."""
        short = self._shortfall(item)
        if short:
            self._sweep(need=short)
            short = self._shortfall(item)
        item.room_check = False
        return not short

    def _ensure_worker(self, host: Host) -> None:
        w = host.worker
        if w is not None and w.thread is not None and w.thread.is_alive():
            return
        host.stuck = [s for s in host.stuck if s.thread is not None and s.thread.is_alive()]
        if len(host.stuck) >= MAX_ABANDONED_PER_HOST:
            return
        live = sum(1 for h in self.hosts.values() if h.worker is not None and h.worker.thread is not None
                   and h.worker.thread.is_alive())
        if live >= MAX_IO_WORKERS:
            return
        w = _Worker()
        w.thread = threading.Thread(target=self._run_worker, args=(host, w), name=f"netcache-io-{host.name}",
                                    daemon=True)
        host.worker = w
        w.thread.start()

    def _post_waiters(self) -> None:
        """Tell the player about songs that may now start."""
        due = []
        with self._lock:
            for token, (path, at, post) in list(self._waiters.items()):
                kind, key, _ = self._describe(path)
                item = self.items.get(key)
                if item is None:
                    continue
                if self._startable_at(item, path, at) and item.error != "disk":
                    del self._waiters[token]
                    due.append(post)
        for post in due:
            try:
                post()
            except Exception:   # noqa: BLE001
                log.exception("music cache: telling the player a song can start")

    # -- host health ------------------------------------------------------------------------------
    def _score_hosts(self, now: float) -> None:
        """Each server's quality class, measured against the song playing from it (or a typical
        CD-quality FLAC), with its hysteresis."""
        with self._lock:
            current = self.items.get(self._current_key) if self._current_key else None
            for host in self.hosts.values():
                dense = policy.TYPICAL_RHO
                if current is not None and current.host == host.name:
                    measured = self._measure(current, self._paths.get(current.key, current.source), 0.0)
                    if measured is not None and current.size:
                        p, L, D, H, fmt, rate, frame_bytes = measured
                        dense = policy.rho(current.size, H, D, fmt, rate, frame_bytes)
                while host.stalls and now - host.stalls[0] > policy.STALLS_UNSTABLE_S:
                    host.stalls.popleft()
                if not host.speed.measured() and not host.score.current and host.speed.value() is None:
                    continue   # nothing known about it yet
                v = host.speed.value()
                host.score.update(policy.quality(v / dense if v else None, host.speed.gap15(now),
                                                 host.speed.failures15(now), len(host.stalls)), now)

    def _after_network_change(self, now: float) -> None:
        """Windows says the network changed: look at the drive letters again, and ask every server
        that wasn't answering straight away."""
        log.info("music cache: the network changed; asking servers that weren't answering again")
        self.classifier.refresh(force=True)
        with self._lock:
            for host in self.hosts.values():
                if host.state == DOWN:
                    host.backoff = PROBE_BACKOFF_MIN_S
                    host.next_probe = now

    def _host_down(self, host: Host, now: float) -> None:
        if host.state != DOWN:
            log.warning("music cache: %s isn't answering", host.name)
            host.down_since = now
            host.backoff = PROBE_BACKOFF_MIN_S
        host.state = DOWN
        host.next_probe = now + host.backoff
        host.speed.failed(now)

    def _watch_hosts(self, now: float) -> None:
        with self._lock:
            for host in self.hosts.values():
                w = host.worker
                if w is not None and w.call_since is not None:
                    stuck_for = now - w.call_since
                    if stuck_for > WORKER_ABANDON_S:
                        log.warning("music cache: giving up on a call to %s stuck for %.0f s", host.name, stuck_for)
                        w.abandoned = True
                        host.stuck.append(w)
                        host.worker = None
                        self._host_down(host, now)
                    elif stuck_for > HOST_DOWN_S:
                        if host.state != DOWN:
                            self._host_down(host, now)
                    elif stuck_for > HOST_SLOW_S and host.state == UP:
                        host.state = SLOW
                host.stuck = [s for s in host.stuck if s.thread is not None and s.thread.is_alive()]
                if (host.state == DOWN and not host.probing and now >= host.next_probe and not host.busy()
                        and len(host.stuck) < MAX_ABANDONED_PER_HOST):
                    host.probing = True
                    self._probe_hosts.put(host)
                if (host.state == DOWN and host.down_since is not None and now - host.down_since > 60
                        and not host.told):
                    host.told = True
                    self.notices.append(f"{host.name} hasn't answered for over a minute: its songs will carry on "
                                        f"downloading when it's back")

    def _run_host_probes(self) -> None:
        while not self._stop.is_set():
            try:
                host = self._probe_hosts.get(timeout=0.5)
            except queue.Empty:
                continue
            ok = True if host.kind != "share" else bool(self._probe_host(host.name))
            now = self._clock()
            with self._lock:
                host.probing = False
                if ok:
                    log.info("music cache: %s is answering again", host.name)
                    if host.told:   # (it was said to have stopped answering)
                        self.notices.append(f"{host.name} is answering again: its songs carry on downloading")
                    host.state, host.down_since, host.backoff = UP, None, PROBE_BACKOFF_MIN_S
                    host.told = False
                else:
                    host.backoff = min(PROBE_BACKOFF_MAX_S, host.backoff * 2)
                    host.next_probe = now + host.backoff
            self._wake.set()

    # -- the I/O threads --------------------------------------------------------------------------
    def _call(self, host: Host, w: _Worker, fn, *args):
        """One call that may wait on the network, watched by the scheduler."""
        with self._lock:
            w.call_since = self._clock()
            host.calls += 1
        try:
            result = fn(*args)
        finally:
            with self._lock:
                w.call_since = None
        if w.abandoned:
            raise _Abandoned()
        with self._lock:
            if host.state == SLOW:
                host.state = UP
        return result

    def _chunk(self, host: Host) -> int:
        v = host.speed.value()
        if v is None:
            return CHUNK_UNKNOWN
        return int(min(CHUNK_MAX, max(CHUNK_MIN, v * CHUNK_TARGET_S)))

    def _run_worker(self, host: Host, w: _Worker) -> None:
        while not self._stop.is_set() and not w.abandoned:
            with host.cond:
                item = host.assigned
                if item is None or host.state == DOWN:
                    host.cond.wait(0.5)
                    continue
            try:
                self._transfer(host, w, item)
            except _Abandoned:
                return
            except Permanent as error:
                self._fail(item, str(error), permanent=True)
            except Transient as error:
                now = self._clock()
                with self._lock:
                    self._host_down(host, now)
                    if item.state == DOWNLOADING:
                        item.state, item.dirty = PARTIAL, True
                    item.error = str(error)
                item.notify()
            except Changed as error:
                log.info("music cache: %s changed while downloading (%s)", item.source, error)
                with self._lock:
                    self._new_generation(item, None)
            except Exception:   # noqa: BLE001 — never let one song stop the host's downloads
                log.exception("music cache: downloading %s", item.source)
                with self._lock:
                    if item.state == DOWNLOADING:
                        item.state, item.dirty = PARTIAL, True
                    item.retry_at = self._wall() + TRANSIENT_RETRY_S
            finally:
                with host.cond:
                    if host.assigned is item and (item.state in (READY, DOWNLOADED, VERIFYING, FAILED)
                                                  or item.complete or item.room_check):
                        host.assigned = None
                self._wake.set()

    def _fetcher(self, item: Item):
        fetcher = self._fetchers.get(item.key)
        if fetcher is None:
            fetcher = self._fetchers[item.key] = self._fetcher_factory(item.source)
        return fetcher

    def _close_fetcher(self, item: Item) -> None:
        fetcher = self._fetchers.pop(item.key, None)
        if fetcher is not None:
            try:
                fetcher.close()
            except Exception:   # noqa: BLE001
                pass

    def _transfer(self, host: Host, w: _Worker, item: Item) -> None:
        fetcher = self._fetcher(item)
        identity = self._call(host, w, fetcher.identity)
        with self._lock:
            if item.state == READY:   # a re-check of a complete copy
                if identity.same_file(Identity(item.size or 0, item.mtime_ns)):
                    item.verified_at, item.dirty = self._wall(), True
                else:
                    self.notices.append(f"“{_name(item)}” has changed on the drive: the new version is downloading")
                    self._new_generation(item, identity)
                return
            if item.size is None:
                item.size, item.mtime_ns, item.dirty = identity.size, identity.mtime_ns, True
            elif not identity.same_file(Identity(item.size, item.mtime_ns)):
                self._new_generation(item, identity)
            if item.state != DOWNLOADING:
                if item.visible == 0:
                    log.info("music cache: downloading %s (%d bytes)", item.source, item.size)
                item.state, item.dirty = DOWNLOADING, True
            item.error = None
            item.retry_at = None
            gen = item.gen
            if self._shortfall(item):   # no room for it: the scheduler decides (it may clear some)
                item.room_check = True
                if item.state == DOWNLOADING:
                    item.state = PARTIAL
                return
        self.store.make_folders()
        writer = _Writer(self.store, item, gen, self._clock)
        try:
            if item.ext == "flac" and (item.scanner is None or item.scanner.fed != item.visible):
                item.scanner = _rescan(self.store.part_path(item, gen), item.visible)
            while item.size is not None and not item.complete:
                if self._stop.is_set() or w.abandoned or item.gen != gen:
                    break
                with host.cond:
                    if host.assigned is not item:
                        break   # something more important on this host
                writer.join_runs()
                if item.complete:
                    break
                with self._lock:
                    offset, limit, island = self._next_range(item)
                n = min(self._chunk(host), limit - offset)
                if n <= 0:
                    break
                started = self._clock()
                host.speed.asking(started)
                data = self._call(host, w, fetcher.read, offset, n)
                finished = self._clock()
                if not data:
                    raise Changed("the file on the drive is shorter than it was")
                if not writer.write(island, offset, data):
                    break   # (a new version began meanwhile)
                host.speed.add(finished, len(data), finished - started)
                host.last_active = finished
                writer.sync(finished)
                self._wake.set()
            writer.join_runs()
        finally:
            writer.close()
        if item.gen != gen or item.size is None or not item.complete:
            with self._lock:
                if item.state == DOWNLOADING and item.gen == gen:
                    item.state, item.dirty = PARTIAL, True
            return
        # Every byte is here: the source must end exactly there and still be the same file.
        tail = self._call(host, w, fetcher.read, item.size, 1)
        again = self._call(host, w, fetcher.identity)
        if tail or not again.same_file(identity):
            raise Changed("the file on the drive changed while it downloaded")
        self._close_fetcher(item)
        with self._lock:
            if item.gen != gen:
                return
            item.transfer_ok = True
            item.state, item.dirty, item.completed = DOWNLOADED, True, self._wall()
        log.info("music cache: downloaded %s", item.source)
        item.notify()
        self._verify_queue.put((item, gen))

    def _next_range(self, item: Item) -> tuple[int, int, Island | None]:
        """Where to fetch next for `item`: (from byte, at most up to byte, the island it goes
        into — None: the run from the start). In order: bytes a reader is waiting for; the end of
        a file whose open reads it; the song playing from where it plays; else on from the start.
        Lock held (an island it starts is added to the item here)."""
        S = int(item.size or 0)
        prefix_limit = item.islands[0].start if item.islands else S
        now = self._clock()
        wanted = self._ranges.get(item.key, {})
        for role in ("play", "proof", "probe"):
            r = wanted.get(role)
            if r is not None and r[0] == item.gen and now - r[3] <= DEMAND_HOLD_S \
                    and not item.has_range(r[0], r[1], min(r[2], S)):
                return self._toward(item, r[1], prefix_limit)
        if (item.ext in TAIL_READING_EXTS and item.ext not in COMPLETE_ONLY_EXTS and S > 4 * TAIL_BYTES
                and item.visible < S - TAIL_BYTES and not (item.islands and item.islands[-1].visible >= S)):
            return self._toward(item, S - TAIL_BYTES, prefix_limit, lead=0)
        if item.priority is not None and item.priority <= CURRENT and item.ext in SEEKS_DIRECTLY_EXTS:
            cursor = self._cursor_byte(item)
            if cursor is not None and cursor > item.visible + JUMP_SLACK and not item.has_range(item.gen, cursor, S):
                return self._toward(item, cursor, prefix_limit)
        return item.visible, prefix_limit, None

    def _toward(self, item: Item, at: int, prefix_limit: int, lead: int | None = None):
        """The run that reaches byte `at` soonest: the run from the start if `at` is just past it,
        an island holding (or just short of) it, else a new island from a little before it."""
        S = int(item.size or 0)
        if at <= item.visible + JUMP_SLACK:
            return item.visible, prefix_limit, None
        islands = item.islands
        start = (max(0, at - (ISLAND_LEAD if lead is None else lead)) // ISLAND_ALIGN) * ISLAND_ALIGN
        for i, island in enumerate(islands):
            # One holding `at`, or ending just short of it (or of where a new one would start): it grows on.
            if island.start <= at <= island.visible + JUMP_SLACK or island.start <= start < island.visible:
                limit = islands[i + 1].start if i + 1 < len(islands) else S
                if island.visible < limit:
                    return island.visible, limit, island
                return item.visible, prefix_limit, None   # (it meets the next one: joined before the next read)
        if start <= item.visible:
            return item.visible, prefix_limit, None
        new = Island(start)
        with item.cond:
            islands.append(new)
            islands.sort(key=lambda isl: isl.start)
        following = [isl.start for isl in islands if isl.start > start]
        return start, (following[0] if following else S), new

    def _open_bytes_here(self, item: Item) -> bool:
        """Every byte the decoder's open reads (as the probe recorded) is on disk."""
        spans = item.info.get("open_spans")
        if spans:
            return all(item.has_range(item.gen, int(a), int(b)) for a, b in spans)
        return item.open_end is not None and item.visible >= item.open_end

    def _fail(self, item: Item, reason: str, permanent: bool) -> None:
        with self._lock:
            log.warning("music cache: %s: %s", item.source, reason)
            item.state, item.error, item.permanent, item.dirty = FAILED, reason, permanent, True
            item.retry_at = self._wall() + FAILED_FORGET_S
            self._close_fetcher(item)
        item.notify()

    def _new_generation(self, item: Item, identity: Identity | None) -> None:
        """The source changed (or the copy failed its check): start a new version from byte 0.
        The old one is kept only while someone is reading it. Lock held."""
        with item.cond:
            old = item.gen
            final = self.store.final_path(item) if item.state == READY else None
            if item.leased(old):
                item.old[old] = (item.size or item.visible) if item.state == READY else item.visible
                item.old_sizes[old] = int(item.size or item.visible)
                item.old_islands[old] = list(item.islands)
                if final is not None:
                    item.old_files[old] = final
            self._stale.append((item, old, final))
            item.islands = []
            item.gen += 1
            item.visible = item.durable = item.prefix = 0
            item.transfer_ok = item.open_ready = False
            item.open_end, item.scanner, item.verified_by, item.final_name = None, None, None, None
            item.info = {}
            item.size = identity.size if identity is not None else None
            item.mtime_ns = identity.mtime_ns if identity is not None else None
            item.state, item.error, item.permanent, item.retry_at = QUEUED, None, False, None
            item.dirty = True
            item.cond.notify_all()
        self._close_fetcher(item)
        self._renames.pop(item.key, None)
        self._sweep_due = True

    # -- the probe (local reads only) ----------------------------------------------------------
    def _next_to_probe(self) -> Item | None:
        with self._lock:
            candidates = [item for item in self.items.values()
                          if item.priority is not None and not item.open_ready and item.size and item.visible > 0
                          and item.state not in (FAILED, READY, STALE) and item.ext not in COMPLETE_ONLY_EXTS
                          and not item.info.get("complete_only") and not item.info.get("open_error")]
            return min(candidates, key=lambda i: i.priority) if candidates else None

    def _run_probe(self) -> None:
        while not self._stop.is_set():
            item = self._next_to_probe() if self._loaded.is_set() else None
            if item is None:
                self._probe_wake.wait(0.25)
                self._probe_wake.clear()
                continue
            self._probe(item)

    def _probe(self, item: Item) -> None:
        gen = item.gen
        track = audiofile.LocalTrack(Path(item.source), item, gen)

        def cancel():
            return self._stop.is_set() or item.gen != gen or item.priority is None

        try:
            f = audiofile.open(track, patience=PROBE_PATIENCE_S, cancel=cancel, role="probe")
        except audiofile.Waiting:
            time.sleep(0.05)
            return
        except audiofile.Unsupported as error:
            with self._lock:
                item.info["complete_only"] = True
                item.info["open_note"] = str(error)
                item.dirty = True
            return
        except (RuntimeError, ValueError, OSError) as error:
            with self._lock:
                if item.gen == gen:
                    item.info["open_error"] = str(error)
                    item.dirty = True
            return
        try:
            found = dict(format=f.format, subtype=f.subtype, rate=f.samplerate, channels=f.channels, frames=f.frames)
            furthest = f.gate.furthest
            spans = [[int(a), int(b)] for a, b in f.gate.spans]
        finally:
            f.close()
        header = _header_bytes(item, found["format"])
        points = _seek_points(item) if found["format"] == "FLAC" else []
        with self._lock:
            if item.gen != gen:
                return
            item.info.update(found)
            if header is not None:
                item.info["header_bytes"] = header
            if points:
                item.info["seek_points"] = points
            item.info["open_spans"] = spans
            item.open_end = item.info["open_end"] = furthest
            item.open_ready = True
            item.dirty = True
        self._wake.set()

    # -- checking finished downloads ---------------------------------------------------------------
    def _run_verify(self) -> None:
        while not self._stop.is_set():
            try:
                item, gen = self._verify_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                if item.gen != gen or item.state != DOWNLOADED:
                    continue
                item.state, item.dirty = VERIFYING, True
            part = self.store.part_path(item, gen)
            try:
                result = verify.check(item, gen, part, decode=self._verify_on(), should_yield=self.should_yield,
                                      cancel=lambda: self._stop.is_set() or item.gen != gen)
            except Exception as error:   # noqa: BLE001 — a check that breaks is a failed check
                log.exception("music cache: checking %s", item.source)
                result = verify.Result(False, "", f"the check couldn't run ({error})")
            self._inbox.put(("verified", item, gen, result))
            self._wake.set()

    def _verified(self, item: Item, gen: int, result: verify.Result) -> None:
        with self._lock:
            if item.gen != gen:
                return
            if result.cancelled:
                item.state = DOWNLOADED
                return
            if not result.ok:
                item.fail_count += 1
                log.warning("music cache: %s failed its check: %s", item.source, result.problem)
                self.notices.append(f"The downloaded copy of “{_name(item)}” didn't match the original "
                                    f"({result.problem}). Downloading it again.")
                item.failed_gens.add(gen)
                if item.fail_count >= 2:
                    item.state, item.error, item.permanent, item.dirty = FAILED, result.problem, False, True
                    item.retry_at = self._wall() + FAILED_FORGET_S
                    item.notify()
                else:
                    self._new_generation(item, Identity(item.size or 0, item.mtime_ns))
                return
            item.verified_by, item.verified_at = result.verified_by, self._wall()
            item.info.update({k: v for k, v in result.info.items() if k != "decoded"})
            item.final_name = self.store.final_name_for(item)
            item.dirty = True
            log.info("music cache: checked %s (%s)", item.source, result.verified_by)
        self._checkpoint(force=True)   # the name is saved before the rename (a crash then is promoted at start-up)
        self._renames[item.key] = self._clock()
        self._try_renames(self._clock())

    def _try_renames(self, now: float, sweeping: bool = False) -> None:
        for key, first in list(self._renames.items()):
            item = self.items.get(key)
            if item is None or item.state != VERIFYING or not item.verified_by:
                self._renames.pop(key, None)
                continue
            part, final = self.store.part_path(item), self.store.final_path(item)
            try:
                os.replace(part, final)
            except OSError:
                if now - first > RENAME_RETRY_S and not sweeping:
                    self._renames[key] = float("inf")   # left for the next sweep (still playable meanwhile)
                continue
            self._renames.pop(key, None)
            with self._lock:
                item.state, item.prefix, item.error, item.fail_count = READY, item.size or 0, None, 0
                item.durable = item.visible = item.size or item.visible
                item.open_ready = True
                item.dirty = True
            item.notify()
            self._sweep_due = True
            self._checkpoint(force=True)

    def _checkpoint(self, force: bool = False) -> None:
        """Save what changed: the durable length, states, what the probe and checks found."""
        now = self._clock()
        if not force and now - self._last_checkpoint < DB_CHECKPOINT_S:
            return
        self._last_checkpoint = now
        with self._lock:
            changed = []
            for item in self.items.values():
                if item.state not in (READY,) and item.prefix != item.durable:
                    item.prefix = item.durable
                    item.dirty = True
                claim = item.island_claim()
                if claim != item.saved_islands:
                    item.saved_islands = claim
                    item.dirty = True
                if item.dirty:
                    changed.append(item)
        try:
            self.store.save(changed)
        except Exception:   # noqa: BLE001 — (busy, or the disk full): tried again next time
            log.warning("music cache: couldn't save its list", exc_info=True)
            for item in changed:
                item.dirty = True
        self._save_hosts(now, force)

    def _save_hosts(self, now: float, force: bool = False) -> None:
        """Each server's figures, for a warm start next time (only ones measured since last saved)."""
        if not force and now - self._last_host_save < HOST_SAVE_S:
            return
        self._last_host_save = now
        rows = []
        with self._lock:
            for host in self.hosts.values():
                if not host.speed.measured() or host.speed.total_bytes == host.saved_bytes:
                    continue
                rows.append(dict(host=host.name, speed=host.speed.value(), gap=host.speed.gap15(now),
                                 failures=host.speed.failures15(now), quality=host.score.current or None,
                                 measured=self._wall()))
                host.saved_bytes = host.speed.total_bytes
        try:
            self.store.save_hosts(rows)
        except Exception:   # noqa: BLE001
            log.warning("music cache: couldn't save the servers' speeds", exc_info=True)

    # -- eviction ---------------------------------------------------------------------------------
    def _bytes_held(self, item: Item) -> int:
        return int(item.held() or 0)

    def _session_keys(self) -> set:
        """The songs in the playing list and Up next while something plays (or waits to): what
        "until playback finishes" keeps."""
        listed = getattr(self.player, "session_paths", None)
        if listed is None:
            return set()
        try:
            paths = list(listed())
        except Exception:   # noqa: BLE001 — the list changed under us: keep everything this time
            return {key for key in self.items}
        keys = set()
        for path in paths:
            kind, key, _ = self._describe(path)
            if key:
                keys.add(key)
        return keys

    def _delete_islands(self, item: Item, gen: int) -> bool:
        gone = True
        for seg in self.store.partial.glob(f"{item.id}.g{gen}.seg-*"):
            gone = self.store.delete_file(seg) and gone
        return gone

    def _sweep(self, force: bool = False, everything: bool = False, need: int = 0) -> None:
        """Delete old versions nobody reads, then evict (the keep policy, the limit, the free-space
        floor) until the cache fits. `everything`: empty the cache of all that isn't in use."""
        self._sweep_due = False
        self._last_sweep = self._clock()
        keep = "none" if everything else self._pref("net_keep")
        session = self._session_keys() if keep == "session" else None
        with self._lock:
            for entry in list(self._stale):
                item, gen, final = entry
                if item.leased(gen):
                    continue
                gone = self.store.delete_file(self.store.part_path(item, gen))
                gone = self.store.delete_file(final) and gone
                gone = self._delete_islands(item, gen) and gone
                if gone:
                    self._stale.remove(entry)
                    item.old.pop(gen, None)
                    item.old_files.pop(gen, None)
                    item.old_islands.pop(gen, None)
            for key, first in list(self._renames.items()):
                if first == float("inf"):
                    self._renames[key] = self._clock()
            items = list(self.items.values())
            protected = {item.key for item in items if item.priority is not None}
            used = sum(self._bytes_held(item) for item in items)
            target = self._limit()
            try:
                usage = self._disk_usage(self.folder)
                floor = max(FREE_FLOOR_MIN, FREE_FLOOR_FRACTION * usage.total)
                target = min(target, used + usage.free - floor - need)
            except OSError:
                pass
            if everything:
                target = -1
            now_wall = self._wall()
            order = policy.evict_order(items, protected, now_wall, keep, in_plan=protected, session=session)
            removed, freed = [], 0
            for item in order:
                if used <= target and not everything and not policy.expired(item, keep, now_wall, session or set()):
                    break   # (copies past their keep time come first, and go whatever room there is)
                with item.cond:
                    if item.leased():
                        continue
                ok = self.store.delete_file(self.store.part_path(item))
                ok = self.store.delete_file(self.store.final_path(item)) and ok
                ok = self._delete_islands(item, item.gen) and ok
                if not ok:
                    continue   # still open somewhere (Windows says so): next sweep
                held = self._bytes_held(item)
                used -= held
                freed += held
                removed.append(item)
                del self.items[item.key]
                self._paths.pop(item.key, None)
                self._renames.pop(item.key, None)
        if removed:
            log.info("music cache: removed %d song(s), %.1f MB", len(removed), freed / 1e6)
            try:
                self.store.remove(removed)
            except Exception:   # noqa: BLE001
                log.warning("music cache: couldn't update its list after clearing", exc_info=True)
        self._try_renames(self._clock(), sweeping=True)


def _name(item: Item) -> str:
    return item.source.replace("\\", "/").rsplit("/", 1)[-1]


def _union(ranges) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(r for r in ranges if r[1] > r[0]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


class _Writer:
    """The files one transfer writes for one version of an item: the .part (the run from the
    start) and its islands, kept open between chunks. Each write is flushed (readers see it at
    once); the files are fsynced every FSYNC_BYTES or FSYNC_S (only then may the database claim
    the bytes). `join_runs` appends an island the run before it has reached (a local copy), then
    changes the map, then deletes the island's file — so a reader always finds its bytes."""

    def __init__(self, store: Store, item: Item, gen: int, clock):
        self.store, self.item, self.gen, self._clock = store, item, gen, clock
        self.files: dict = {}        # None (the .part) or an island's start -> its open file
        self.unsynced = 0
        self.synced_at = clock()
        self._open(None)

    def _path(self, island: Island | None) -> Path:
        if island is None:
            return self.store.part_path(self.item, self.gen)
        return self.store.island_path(self.item, island.start, self.gen)

    def _open(self, island: Island | None):
        key = None if island is None else island.start
        f = self.files.get(key)
        if f is None:
            path = self._path(island)
            f = open(path, "r+b" if path.exists() else "wb")
            f.seek(self.item.visible if island is None else island.visible - island.start)
            f.truncate()   # (nothing past what readers were told is there)
            self.files[key] = f
        return f

    def _close(self, key) -> None:
        f = self.files.pop(key, None)
        if f is not None:
            f.close()

    def write(self, island: Island | None, offset: int, data: bytes) -> bool:
        item = self.item
        f = self._open(island)
        f.seek(offset - (0 if island is None else island.start))
        f.write(data)
        f.flush()
        with item.cond:
            if item.gen != self.gen:
                return False
            if island is None:
                item.visible = offset + len(data)
            else:
                island.visible = offset + len(data)
            item.cond.notify_all()
        if island is None and item.scanner is not None:
            item.scanner.feed(data)
        self.unsynced += len(data)
        return True

    def sync(self, now: float | None = None, force: bool = False) -> None:
        now = self._clock() if now is None else now
        if not force and self.unsynced < FSYNC_BYTES and now - self.synced_at < FSYNC_S:
            return
        for f in self.files.values():
            f.flush()
            os.fsync(f.fileno())
        item = self.item
        with item.cond:
            if item.gen == self.gen:
                item.durable = item.visible
                for island in item.islands:
                    island.durable = island.visible
        self.unsynced, self.synced_at = 0, now

    def join_runs(self) -> None:
        item = self.item
        while True:
            with item.cond:
                if item.gen != self.gen or not item.islands:
                    return
                first = item.islands[0]
                if first.start <= item.visible:
                    target, island = None, first
                else:
                    pair = next(((a, b) for a, b in zip(item.islands, item.islands[1:]) if b.start <= a.visible),
                                None)
                    if pair is None:
                        return
                    target, island = pair
                reached = item.visible if target is None else target.visible
                end = island.visible
            out = self._open(target)
            self._close(island.start)
            source = self._path(island)
            if end > reached:
                out.seek(reached - (0 if target is None else target.start))
                with open_shared(source) as f:
                    f.seek(reached - island.start)
                    left = end - reached
                    while left > 0:
                        chunk = f.read(min(left, 1 << 20))
                        if not chunk:
                            raise OSError(f"{source.name} is shorter than recorded")
                        out.write(chunk)
                        if target is None and item.scanner is not None:
                            item.scanner.feed(chunk)
                        left -= len(chunk)
            out.flush()
            os.fsync(out.fileno())
            with item.cond:
                if item.gen != self.gen:
                    return
                if target is None:
                    item.visible = item.durable = max(item.visible, end)
                else:
                    target.visible = target.durable = max(target.visible, end)
                item.islands.remove(island)
                item.cond.notify_all()
            self.store.delete_file(source)   # (a reader still holding it reads on: it was opened to allow this)

    def close(self) -> None:
        try:
            self.sync(force=True)
        except OSError:
            pass
        finally:
            for key in list(self.files):
                try:
                    self._close(key)
                except OSError:
                    pass


def _rescan(part: Path, upto: int):
    """A FLAC scanner fed the bytes already downloaded (after a restart, or a new version)."""
    scanner = flacframes.Scanner()
    if upto <= 0:
        return scanner
    with open(part, "rb") as f:
        left = upto
        while left > 0:
            data = f.read(min(left, 1 << 20))
            if not data:
                break
            scanner.feed(data)
            left -= len(data)
    return scanner


def _seek_points(item: Item) -> list:
    """A FLAC copy's SEEKTABLE (from the bytes already here): [[sample, offset from the first frame], …]."""
    try:
        with item.open_bytes(item.gen) as f:
            head = f.read(min(item.visible, 1 << 20))
    except OSError:
        return []
    return [[sample, offset] for sample, offset in flacframes.seek_points(head)]


def _header_bytes(item: Item, fmt: str) -> int | None:
    """H: bytes before the first audio, from the start of the downloaded copy."""
    if fmt == "FLAC" and item.scanner is not None and item.scanner.first_frame is not None:
        return item.scanner.first_frame
    try:
        with item.open_bytes(item.gen) as f:
            head = f.read(min(item.visible, 1 << 16))
    except OSError:
        return None
    if fmt in ("WAV", "WAVEX", "RF64"):
        at = head.find(b"data", 12)
        return at + 8 if at >= 0 else None
    if fmt in ("AIFF", "AIFC"):
        at = head.find(b"SSND", 12)
        return at + 16 if at >= 0 else None
    if fmt in ("MP3", "MPEG"):
        return mp3frames._skip_id3v2(head)
    return 0

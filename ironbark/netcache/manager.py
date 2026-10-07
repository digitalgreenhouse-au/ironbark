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
after next, and "one more" once everything wanted is complete), QUEUE. One transfer per host, so
the song playing gets the whole link; between chunks (sized to take about 250 ms) an I/O thread
switches to anything more important on its host.

Host health: a call outstanding over 5 s makes its host SLOW, over 15 s DOWN; while a call is
stuck no further calls go to that host. DOWN hosts are probed with back-off (2, 4, 8 … 60 s). An
I/O thread stuck over 120 s is abandoned (its result thrown away when it returns); at most two
per host, after which the host stays DOWN until they come back.

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
from . import policy, verify
from .fetch import Changed, Identity, Permanent, ShareFetcher, Transient
from .sources import Classifier, Kind
from .store import (DOWNLOADED, DOWNLOADING, FAILED, PARTIAL, QUEUED, READY, STALE, VERIFYING, Item,
                    OwnerLock, Store)

log = logging.getLogger(__name__)

DEMAND, CURRENT, NEXT, ASKED, FOLLOWING, QUEUE = 0, 1, 2, 3, 4, 5

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
DEFAULTS = {name: value for name, value in settings.DEFAULTS.items() if name.startswith("net_")}
PREDOWNLOAD_DEPTH = {"off": 0, "next": 1, "next2": 2, "queue": 1000}
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

    def __init__(self, name: str, kind: str):
        self.name, self.kind = name, kind
        self.state = UP
        self.speed = policy.Speed()
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
                 clock=time.monotonic, wall=time.time, owner: OwnerLock | None = None):
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
        self._lock = threading.RLock()
        self.items: dict[str, Item] = {}
        self.hosts: dict[str, Host] = {}
        self._paths: dict[str, object] = {}         # key -> a path that names it (for the plan)
        self._explicit: dict[str, tuple[str, int]] = {}   # role -> (key, priority) from prepare()
        self._asks: dict[str, int] = {}
        self._demands: dict[str, float] = {}
        self._positions: dict[str, float] = {}
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
            self.hosts[host] = Host(host, kind.value)
        return item

    def _wire(self, item: Item) -> None:
        item.on_demand = self._on_demand

    def _on_demand(self, item: Item, gen: int, start: int, end: int, role: str) -> None:
        """A reader is waiting for bytes (called from the reader's own thread: quick)."""
        if role in ("play", "proof"):
            with self._lock:
                self._demands[item.key] = self._clock()
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
                self._positions[key] = at
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

    def position_hint(self, path, seconds: float) -> None:
        kind, key, _ = self._describe(path)
        if kind is Kind.LOCAL or not key:
            return
        with self._lock:
            self._positions[key] = seconds

    def progress(self, path) -> Progress | None:
        kind, key, host = self._describe(path)
        if kind is Kind.LOCAL or not key:
            return None
        with self._lock:
            item = self.items.get(key)
            if item is None:
                return None
            h = self.hosts.get(item.host)
            return Progress(item.state, item.fraction(), h.speed.value() if h else None, item.copy_state(item.gen),
                            item.verified_by or "")

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
        quality = policy.quality(m, gap, failures)
        eta = (S - item.visible) / v if v else None
        need = policy.need_seconds(self._pref("net_mode"), L, float(self._pref("net_buffer")), m, gap, quality, eta)
        if stalls:
            need = policy.resume_need(need, stalls, L)
        flac_end = item.scanner.end_sample() if item.scanner is not None else None
        A = policy.available_seconds(item.visible, p, S=S, H=H, D=D, fmt=fmt, rate=rate,
                                     frame_bytes=frame_bytes, flac_end_sample=flac_end)
        ok = policy.startable(open_ready=item.open_ready or item.complete, ready=False, complete=item.complete,
                              A=A, need=need, forced=force)
        return ok, need, measured

    def _startable_at(self, item: Item, path, at: float, stalls: int = 0, force: bool = False) -> bool:
        return self._rule(item, path, at, stalls, force)[0]

    def _available(self, item: Item, path, at: float) -> float:
        """Seconds of music from `at` on disk in one run (all the rest of it: infinity)."""
        if self._playable_complete(item) or item.complete:
            return float("inf")
        measured = self._measure(item, path, at)
        if measured is None or item.size is None or not item.open_ready:
            return 0.0
        p, L, D, H, fmt, rate, frame_bytes = measured
        flac_end = item.scanner.end_sample() if item.scanner is not None else None
        A = policy.available_seconds(item.visible, p, S=item.size, H=H, D=D, fmt=fmt, rate=rate,
                                     frame_bytes=frame_bytes, flac_end_sample=flac_end)
        return float("inf") if A >= L else A

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
            pct = policy.percent(item.visible, item.size, True, item.size)
            return NotReady("downloading", pct, "downloading", f"Downloading track… {pct}%")
        if measured is None:
            reads_tail = item.ext in TAIL_READING_EXTS
            needed = item.size if reads_tail else min(item.size, policy.HEAD_ONLY_EXTRA)
        else:
            p, L, D, H, fmt, rate, frame_bytes = measured
            needed = policy.needed_bytes(p, need, S=item.size, H=H, D=D, open_end=item.open_end,
                                         reads_tail=item.ext in TAIL_READING_EXTS, fmt=fmt,
                                         frame_bytes=frame_bytes, rate=rate, flac_scan=item.scanner)
        pct = policy.percent(item.visible, needed)
        return NotReady("preparing", pct, "preparing",
                        f"Preparing track… {pct}%" if pct is not None else "Preparing track…")

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
        except Exception:   # noqa: BLE001 — the cache must never stop the player
            log.exception("couldn't open the music cache's database")
            kept = []
        with self._lock:
            for item in kept:
                self._wire(item)
                self.items.setdefault(item.key, item)
                if item.host and item.host not in self.hosts:
                    self.hosts[item.host] = Host(item.host, item.kind)
                if item.state == READY or (item.open_end is not None and item.visible >= item.open_end):
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
        if self.classifier.due():
            self.classifier.refresh()
        while True:
            try:
                message = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._handle(message)
        self._replan(now)
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
        demands. Items dropping out of the plan keep their bytes (resumable)."""
        depth = PREDOWNLOAD_DEPTH.get(self._pref("net_predownload"), 1)
        upcoming = self._upcoming(depth + 1)
        wants: dict[str, int] = {}
        paths: dict[str, object] = {}
        window_keys = []
        for position, (_, path) in enumerate(upcoming):
            kind, key, host = self._describe(path)
            if kind is Kind.LOCAL or kind is Kind.URL:
                continue
            if position > depth:
                window_keys.append((key, path, kind, host, True))
                continue
            rank = CURRENT if position == 0 else NEXT if position == 1 else FOLLOWING if position == 2 else QUEUE
            window_keys.append((key, path, kind, host, False))
            wants[key] = min(rank, wants.get(key, rank))
            paths[key] = path
        with self._lock:
            for key, path, kind, host, _ in window_keys:
                self._get(key, path, kind, host)
            for role, (key, rank) in self._explicit.items():
                if key in self.items:
                    wants[key] = min(rank, wants.get(key, rank))
            for key in self._asks:
                if key in self.items:
                    wants[key] = min(ASKED, wants.get(key, ASKED))
            for key, since in list(self._demands.items()):
                if now - since > DEMAND_HOLD_S:
                    del self._demands[key]
                elif key in self.items:
                    wants[key] = DEMAND
            # "One more": everything wanted is complete and the links are idle -> the song after.
            extra = [entry for entry in window_keys if entry[4]]
            if extra and all(self.items[k].state == READY for k in wants) and not any(
                    h.assigned is not None for h in self.hosts.values()):
                key = extra[0][0]
                wants.setdefault(key, FOLLOWING)
            for item in self.items.values():
                item.priority = wants.get(item.key)

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
        part = self.store.part_path(item, gen)
        with open(part, "r+b" if part.exists() else "wb") as out:
            out.seek(item.visible)
            out.truncate()
            if item.ext == "flac" and (item.scanner is None or item.scanner.fed != item.visible):
                item.scanner = _rescan(part, item.visible)
            unsynced, synced_at = 0, self._clock()
            while item.size is not None and item.visible < item.size:
                if self._stop.is_set() or w.abandoned or item.gen != gen:
                    break
                with host.cond:
                    if host.assigned is not item:
                        break   # something more important on this host
                offset = item.visible
                started = self._clock()
                host.speed.asking(started)
                data = self._call(host, w, fetcher.read, offset, min(self._chunk(host), item.size - offset))
                finished = self._clock()
                if not data:
                    raise Changed("the file on the drive is shorter than it was")
                if item.gen != gen:
                    break
                out.write(data)
                out.flush()
                with item.cond:
                    item.visible += len(data)
                    item.cond.notify_all()
                if item.scanner is not None:
                    item.scanner.feed(data)
                host.speed.add(finished, len(data), finished - started)
                unsynced += len(data)
                if unsynced >= FSYNC_BYTES or finished - synced_at >= FSYNC_S:
                    os.fsync(out.fileno())
                    item.durable = item.visible
                    unsynced, synced_at = 0, finished
                self._wake.set()
            out.flush()
            os.fsync(out.fileno())
            if item.gen == gen:
                item.durable = item.visible
        if item.gen != gen or item.size is None or item.visible < item.size:
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
                if final is not None:
                    item.old_files[old] = final
            self._stale.append((item, old, final))
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
        finally:
            f.close()
        header = _header_bytes(item, found["format"])
        with self._lock:
            if item.gen != gen:
                return
            item.info.update(found)
            if header is not None:
                item.info["header_bytes"] = header
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
                if item.dirty:
                    changed.append(item)
        try:
            self.store.save(changed)
        except Exception:   # noqa: BLE001 — (busy, or the disk full): tried again next time
            log.warning("music cache: couldn't save its list", exc_info=True)
            for item in changed:
                item.dirty = True

    # -- eviction ---------------------------------------------------------------------------------
    def _bytes_held(self, item: Item) -> int:
        if item.state == READY:
            return int(item.size or 0)
        return int(item.visible or 0)

    def _sweep(self, force: bool = False, everything: bool = False, need: int = 0) -> None:
        """Delete old versions nobody reads, then evict (the keep policy, the limit, the free-space
        floor) until the cache fits. `everything`: empty the cache of all that isn't in use."""
        self._sweep_due = False
        self._last_sweep = self._clock()
        with self._lock:
            for entry in list(self._stale):
                item, gen, final = entry
                if item.leased(gen):
                    continue
                gone = self.store.delete_file(self.store.part_path(item, gen))
                gone = self.store.delete_file(final) and gone
                if gone:
                    self._stale.remove(entry)
                    item.old.pop(gen, None)
                    item.old_files.pop(gen, None)
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
            keep = "none" if everything else self._pref("net_keep")
            order = policy.evict_order(items, protected, self._wall(), keep, in_plan=protected)
            removed, freed = [], 0
            for item in order:
                if used <= target and not everything:
                    break
                with item.cond:
                    if item.leased():
                        continue
                ok = self.store.delete_file(self.store.part_path(item))
                ok = self.store.delete_file(self.store.final_path(item)) and ok
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

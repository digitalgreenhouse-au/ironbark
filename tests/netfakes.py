"""Stand-ins for the network playback cache's tests: no real network anywhere.

- `FakeShare`: in-memory files behind the fetcher interface, with scripted speed, stalls,
  errors, a changing file, a byte flipped in one transfer, and calls that block until released.
- `FakePlayer`: just `upcoming(n)`, lock-free, like the real one.
- `FakeClassifier`: classifies by spelling only — never asks Windows about this computer's drives.
- `make_manager`: a Manager on a temp folder with all of the above.
- `FakeCopy` / `FakePreparer`: the player's side: a cached copy whose download the test moves
  on by hand (read through the real waiting reader), and the preparer interface over it — a copy
  whose bytes go missing here (`lose_file`) and the fresh version the cache starts (`renew`) too.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import importlib

from ironbark.netcache import fetch, sources

manager_mod = importlib.import_module("ironbark.netcache.manager")   # (netcache.manager() is a function)

Usage = namedtuple("Usage", "total used free")
HOST = "fakehost"


def unc(name: str) -> Path:
    return Path(f"\\\\{HOST}\\share\\{name}")


class FakeShare:
    def __init__(self, rate: float | None = None):
        self.rate = rate                  # bytes per second (None: as fast as possible)
        self.files: dict[str, list] = {}  # name -> [bytes, mtime_ns]
        self.log: list[tuple] = []        # (name, "identity" | "read", offset, n)
        self.lock = threading.Lock()
        self.open = threading.Event()     # cleared: every call waits until it's set again
        self.open.set()
        self.waiting = 0                  # calls waiting on `open` now
        self.flip_once: dict[str, int] = {}
        self.stall_at: dict[str, tuple[int, float]] = {}
        self.transient: dict[str, int] = {}   # name -> errors still to raise
        self.active = 0                   # reads in progress now, and the most at once
        self.most_active = 0

    def add(self, name: str, data: bytes, mtime_ns: int = 1_000_000_000) -> Path:
        with self.lock:
            self.files[name] = [bytes(data), mtime_ns]
        return unc(name)

    def change(self, name: str, data: bytes | None = None, mtime_ns: int | None = None) -> None:
        with self.lock:
            entry = self.files[name]
            if data is not None:
                entry[0] = bytes(data)
            entry[1] = mtime_ns if mtime_ns is not None else entry[1] + 1_000_000

    def remove(self, name: str) -> None:
        with self.lock:
            self.files.pop(name, None)

    def reads(self, name: str) -> list[int]:
        return [offset for (n, op, offset, _) in self.log if n == name and op == "read"]

    def fetcher(self, source: str) -> "_FakeFetcher":
        return _FakeFetcher(self, source.replace("\\", "/").rsplit("/", 1)[-1])


class _FakeFetcher:
    def __init__(self, share: FakeShare, name: str):
        self.share, self.name = share, name

    def _gate(self):
        share = self.share
        if not share.open.is_set():
            with share.lock:
                share.waiting += 1
            share.open.wait()
            with share.lock:
                share.waiting -= 1

    def identity(self):
        share = self.share
        with share.lock:
            share.log.append((self.name, "identity", 0, 0))
        self._gate()
        with share.lock:
            entry = share.files.get(self.name)
            if entry is None:
                raise fetch.Permanent(f"{self.name} isn't on the share")
            return fetch.Identity(len(entry[0]), entry[1])

    def read(self, offset: int, n: int) -> bytes:
        share = self.share
        with share.lock:
            share.log.append((self.name, "read", offset, n))
            share.active += 1
            share.most_active = max(share.most_active, share.active)
        try:
            return self._read(offset, n)
        finally:
            with share.lock:
                share.active -= 1

    def _read(self, offset: int, n: int) -> bytes:
        share = self.share
        with share.lock:
            left = share.transient.get(self.name, 0)
            if left:
                share.transient[self.name] = left - 1
        self._gate()
        if left:
            raise fetch.Transient("the server didn't answer")
        stall = share.stall_at.get(self.name)
        if stall is not None and offset + n > stall[0]:
            share.stall_at.pop(self.name, None)
            time.sleep(stall[1])
        with share.lock:
            entry = share.files.get(self.name)
            if entry is None:
                raise fetch.Permanent(f"{self.name} isn't on the share")
            data = bytearray(entry[0][offset: offset + n])
            flip = share.flip_once.get(self.name)
            if flip is not None and offset <= flip < offset + len(data):
                data[flip - offset] ^= 0xFF
                share.flip_once.pop(self.name)
        if share.rate:
            time.sleep(len(data) / share.rate)
        return bytes(data)

    def close(self):
        pass


class FakePlayer:
    """The current song is `order[at]`; upcoming(n) is it and up to n after."""

    def __init__(self, paths=()):
        self.order = list(paths)
        self.at = 0
        self.playing = True            # something plays (or waits to): session_paths lists the songs
        self.lock = threading.Lock()   # the player's own lock: the manager must never take it

    def upcoming(self, n: int):
        order, at = self.order, self.at
        return [(i, order[i]) for i in range(at, min(len(order), at + n + 1))]

    def session_paths(self):
        return list(self.order) if self.playing else []


class FakeClassifier(sources.Classifier):
    def refresh(self, force: bool = False) -> None:
        pass

    def due(self) -> bool:
        return False

    def _type_of(self, letter: str) -> int:
        return 3   # a fixed disk

    def _is_cloud(self, text: str) -> bool:
        return False


def make_manager(folder: Path, share: FakeShare, player=None, prefs=None, *, probe=None, free=10 ** 12,
                 total=10 ** 12, start=True, **options):
    probes = probe if probe is not None else []

    def probe_host(name):
        probes.append(name)
        return True

    disk = options.pop("disk_usage", lambda path: Usage(total, total - free, free))
    options.setdefault("watch_network", None)   # (never Windows' own network notifications in tests)
    made = manager_mod.Manager(folder, {"net_verify": False, **(prefs or {})}, player, classifier=FakeClassifier(),
                               fetcher=share.fetcher, probe_host=options.pop("probe_host", probe_host),
                               disk_usage=disk, **options)
    if start:
        made.start()
        assert made.wait_loaded(10)
    return made


def wait_until(condition, timeout: float = 10.0, step: float = 0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(step)
    return condition()


# -- the player's side: a copy whose download the test moves on by hand ------------------------------
class FakeCopy:
    """One song's cached copy with every byte on disk, of which readers (the real GrowingFile) may
    see only the first `visible`: the test moves that on (`arrive`), finishes it (`ready`) or fails
    it. `source` is the song's network path (never opened: the bytes are here already)."""

    def __init__(self, folder: Path, name: str, data: bytes, *, rate: int = 44100, frame_bytes: int = 4,
                 header: int | None = None):
        from ironbark.netcache.store import DOWNLOADING, Item
        folder.mkdir(parents=True, exist_ok=True)
        self.folder, self.name, self.data = folder, name, bytes(data)
        self.part = folder / f"{name}.part"
        self.part.write_bytes(self.data)
        self.source = unc(name)
        self.rate, self.frame_bytes = rate, frame_bytes
        self.header = header if header is not None else self.data.index(b"data") + 8
        store = SimpleNamespace(part_path=lambda item, gen=None: self.part, tracks=folder,
                                final_path=lambda item: folder / item.final_name if item.final_name else None)
        self.item = Item(store, key=f"smb:{name}", kind="share", source=str(self.source), host=HOST,
                         ext=name.rsplit(".", 1)[-1], size=len(self.data))
        self.item.state = DOWNLOADING
        self.item.visible = 0

    @property
    def size(self) -> int:
        return len(self.data)

    def local(self):
        from ironbark import audiofile
        return audiofile.LocalTrack(self.source, self.item, self.item.gen)

    def byte_at(self, seconds: float) -> int:
        return min(self.size, self.header + int(round(max(0.0, seconds) * self.rate)) * self.frame_bytes)

    def arrive(self, upto: int | None = None, seconds: float | None = None) -> None:
        if upto is None:
            upto = self.size if seconds is None else self.byte_at(seconds)
        with self.item.cond:
            self.item.visible = max(self.item.visible, min(self.size, upto))
            self.item.cond.notify_all()

    def ready(self) -> None:
        """Downloaded and checked: renamed to its final name (opened by name from now on)."""
        from ironbark.netcache.store import READY
        stem, ext = self.name.rsplit(".", 1)
        final = f"{stem} [ready].{ext}"
        (self.folder / final).write_bytes(self.data)
        with self.item.cond:
            self.item.visible = self.size
            self.item.final_name, self.item.state = final, READY
            self.item.cond.notify_all()

    def lose_file(self) -> None:
        """The .part is deleted behind the cache's back (its bytes are still recorded as here)."""
        self.part.unlink()

    def renew(self) -> None:
        """The cache gave that version up and starts a fresh one from nothing (the test moves it on)."""
        with self.item.cond:
            self.item.gen += 1
            self.item.visible = 0
            self.item.cond.notify_all()
        self.part.write_bytes(self.data)

    def fail(self, reason: str = "it's no longer on the share") -> None:
        from ironbark.netcache.store import FAILED
        with self.item.cond:
            self.item.state, self.item.permanent, self.item.error = FAILED, True, reason
            self.item.cond.notify_all()

    @property
    def complete(self) -> bool:
        return self.item.visible >= self.size


class FakePreparer:
    """The preparer interface the player uses (as netcache.Manager offers it) over FakeCopy songs:
    a song may start (or carry on) once `need` seconds from that spot are visible, or all of it.
    Songs it hasn't been given are local files. `tick()` tells the player about songs that may
    start now (the real one's scheduler does that by itself)."""

    def __init__(self, need: float = 1.0):
        self.copies: dict = {}
        self.gone: dict = {}
        self.need = need
        self.waiters: dict = {}
        self.calls: list = []
        self._tokens = itertools.count(1)
        self.lock = threading.Lock()

    def add(self, copy: FakeCopy) -> FakeCopy:
        self.copies[copy.source] = copy
        return copy

    def _ok(self, copy: FakeCopy, at: float, need: float, gen: int | None = None) -> bool:
        if (copy.item.gen if gen is None else gen) in copy.item.lost_gens:
            return False
        return copy.complete or copy.item.visible >= copy.byte_at(at + need)

    def _not_ready(self, copy: FakeCopy, at: float):
        needed = copy.byte_at(at + self.need)
        pct = min(99, int(100 * copy.item.visible / needed)) if needed else None
        return manager_mod.NotReady("preparing", pct, "preparing", f"Preparing track… {pct}%")

    def prepare(self, path, *, role: str = "play", at: float = 0.0, force: bool = False):
        from ironbark import audiofile
        from ironbark.netcache import policy
        self.calls.append(("prepare", path, role, at, force))
        copy = self.copies.get(path)
        if copy is None:
            return audiofile.LocalTrack.plain(path)
        if path in self.gone:
            return manager_mod.Unavailable(self.gone[path], True)
        if self._ok(copy, at, policy.MIN_START_S if force else self.need):
            return copy.local()
        return self._not_ready(copy, at)

    def notify_when_startable(self, path, at: float, post) -> int:
        token = next(self._tokens)
        with self.lock:
            self.waiters[token] = (path, at, post)
        return token

    def forget(self, token) -> None:
        with self.lock:
            self.waiters.pop(token, None)

    def tick(self) -> int:
        due = []
        with self.lock:
            for token, (path, at, post) in list(self.waiters.items()):
                copy = self.copies.get(path)
                if copy is not None and path not in self.gone and self._ok(copy, at, self.need):
                    del self.waiters[token]
                    due.append(post)
        for post in due:
            post()
        return len(due)

    def resumable(self, local, frame: int, stalls: int, quick: bool = False) -> bool:
        from ironbark.netcache import policy
        copy = self.copies.get(local.source)
        if copy is None:
            return True
        need = policy.QUICK_RESUME_S if quick else self.need * 2 ** stalls
        self.calls.append(("resumable", local.source, stalls, quick))
        return self._ok(copy, frame / copy.rate, need, local.gen)

    def note_stall(self, local) -> None:
        self.calls.append(("stall", local.source))

    def outlook(self, x, at: float = 0.0):
        path = getattr(x, "source", x)
        copy = self.copies.get(path)
        if copy is None:
            return None
        if path in self.gone:
            return manager_mod.Unavailable(self.gone[path], True)
        if getattr(x, "gen", None) in copy.item.lost_gens:
            return manager_mod.NotReady("lost", None, "lost", "The copy on this computer went missing")
        if self._ok(copy, at, self.need):
            return None
        return self._not_ready(copy, at)

    def plan_changed(self) -> None:
        self.calls.append(("plan",))

    def position_hint(self, path, seconds: float) -> None:
        self.calls.append(("hint", path, seconds))

    def progress(self, path):
        copy = self.copies.get(path)
        if copy is None:
            return None
        item = copy.item
        return manager_mod.Progress(item.state, item.fraction(), None, item.copy_state(item.gen))

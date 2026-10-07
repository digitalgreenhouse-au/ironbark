"""Stand-ins for the network playback cache's tests: no real network anywhere.

- `FakeShare`: in-memory files behind the fetcher interface, with scripted speed, stalls,
  errors, a changing file, a byte flipped in one transfer, and calls that block until released.
- `FakePlayer`: just `upcoming(n)`, lock-free, like the real one.
- `FakeClassifier`: classifies by spelling only — never asks Windows about this computer's drives.
- `make_manager`: a Manager on a temp folder with all of the above.
"""
from __future__ import annotations

import threading
import time
from collections import namedtuple
from pathlib import Path

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
        self.lock = threading.Lock()   # the player's own lock: the manager must never take it

    def upcoming(self, n: int):
        order, at = self.order, self.at
        return [(i, order[i]) for i in range(at, min(len(order), at + n + 1))]


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

"""The network playback cache when a server stops answering mid-read, and when its own files are
cut short behind its back:

- a share fetcher's close never waits for a read stuck on the network (Windows' C runtime holds a
  file's lock for the whole of a read, so a close from another thread used to wait for it — with
  the cache's lock held: a tag save froze the window, quitting hung);
- an I/O thread given up on lets go of its song: the replacement opens a file of its own instead
  of waiting on the stuck one, and the song is no longer "downloading" (so it's fetched again, or
  evicted);
- joining a downloaded stretch onto a file cut short (or from a stretch that has gone) is refused
  as a copy gone missing, never filled with zeros that play as silence.

A stuck read is a read from an empty local pipe (through the C runtime, as a share's is). No real
network, no sound device."""
from __future__ import annotations

import importlib
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from netfakes import FakeClassifier, FakePlayer, Usage, unc, wait_until

from ironbark.netcache import fetch, policy
from ironbark.netcache.fetch import Changed, Identity, ShareFetcher
from ironbark.netcache.store import DOWNLOADING, PARTIAL, Island, Item, Store

manager_mod = importlib.import_module("ironbark.netcache.manager")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


class StuckFile:
    """A share's open file whose reads wait until `release` (a server that stopped answering): its
    read sits in the C runtime's read of an empty pipe, holding that file's lock, as a stuck SMB
    read does."""

    made: list[StuckFile] = []

    def __init__(self):
        r, self._w = os.pipe()
        self._f = open(r, "rb", buffering=0)
        self.closed = False
        StuckFile.made.append(self)

    def seek(self, offset):
        return offset

    def read(self, n):
        return self._f.read(1) * n

    def release(self):
        try:
            os.write(self._w, b"x" * 64)
        except OSError:
            pass

    def close(self):
        self._f.close()   # (waits while a read holds the file's lock)
        self.closed = True
        os.close(self._w)


@pytest.fixture
def stuck_files(monkeypatch):
    StuckFile.made = []
    monkeypatch.setattr(fetch, "open_shared", lambda path: StuckFile())
    yield StuckFile.made
    for f in list(StuckFile.made):
        f.release()


class StuckFetcher(ShareFetcher):
    """The real ShareFetcher (its reads and closes), with a server that answers a stat."""

    made: list[StuckFetcher] = []

    def __init__(self, path):
        super().__init__(path)
        StuckFetcher.made.append(self)

    def identity(self):
        return Identity(1_000_000, 123)


def _timed(fn, wait: float) -> bool:
    """Whether fn() returns within `wait` seconds (it runs on a thread of its own)."""
    done = threading.Event()
    threading.Thread(target=lambda: (fn(), done.set()), daemon=True).start()
    return done.wait(wait)


def test_a_fetchers_close_never_waits_for_a_stuck_read(stuck_files):
    fetcher = ShareFetcher(r"\\server\music\01.flac")
    got = []
    reader = threading.Thread(target=lambda: got.append(fetcher.read(0, 4)), daemon=True)
    reader.start()
    assert wait_until(lambda: stuck_files and fetcher._reading == 1, 2)
    time.sleep(0.2)   # (the read is now inside the C runtime, holding the file's lock)

    assert _timed(fetcher.close, 1.0), "close() waited for the stuck read"
    assert not stuck_files[0].closed          # (let go of once the read returns, not under it)
    stuck_files[0].release()
    reader.join(2)
    assert got == [b"xxxx"] and stuck_files[0].closed and fetcher._file is None

    # Read again later (a download resumed): the file is opened afresh.
    def release_the_new_one():
        if wait_until(lambda: len(stuck_files) == 2, 2):
            stuck_files[1].release()

    threading.Thread(target=release_the_new_one, daemon=True).start()
    assert fetcher.read(0, 2) == b"xx" and len(stuck_files) == 2


@pytest.fixture
def stuck_manager(tmp_path, stuck_files, monkeypatch):
    monkeypatch.setattr(manager_mod, "SCHEDULER_TICK_S", 0.05)
    StuckFetcher.made = []
    managers = []

    def make(**timings):
        for name, value in timings.items():
            monkeypatch.setattr(manager_mod, name, value)
        player = FakePlayer([unc("01.bin")])
        m = manager_mod.Manager(tmp_path / "NetCache", {"net_verify": False}, player, classifier=FakeClassifier(),
                                fetcher=StuckFetcher, probe_host=lambda name: True,
                                disk_usage=lambda p: Usage(10 ** 12, 0, 10 ** 12), watch_network=None)
        m.start()
        assert m.wait_loaded(10)
        managers.append(m)
        return m

    yield make
    for f in list(stuck_files):
        f.release()
    for m in managers:
        m.shutdown(timeout=1)


def test_a_tag_save_and_the_window_dont_wait_for_a_stuck_read_nor_does_quitting(stuck_manager, stuck_files):
    m = stuck_manager()
    path = unc("01.bin")
    m.prepare(path, role="play")
    assert wait_until(lambda: stuck_files and StuckFetcher.made and StuckFetcher.made[0]._reading, 10)
    time.sleep(0.2)   # (the I/O thread is inside its read)

    # Tags saved for the song (the window's thread): the copy is dropped at once; the window's
    # status poll (progress) carries on meanwhile.
    assert _timed(lambda: m.invalidate(path), 1.0), "invalidate() waited for the stuck read"
    assert _timed(lambda: m.progress(path), 1.0)

    for f in list(stuck_files):
        f.release()
    assert wait_until(lambda: len(stuck_files) >= 2 and StuckFetcher.made[-1]._reading, 10)   # (the new version)
    time.sleep(0.2)
    assert _timed(lambda: m.shutdown(timeout=0.5), 3.0), "quitting waited for the stuck read"


def test_a_thread_given_up_on_lets_go_of_its_song_and_its_file(stuck_manager, stuck_files):
    m = stuck_manager(WORKER_ABANDON_S=0.6, HOST_DOWN_S=0.4, HOST_SLOW_S=0.2, PROBE_BACKOFF_MIN_S=0.1)
    path = unc("01.bin")
    m.prepare(path, role="play")
    key = m.classifier.source_key(path)
    host = lambda: m.hosts.get("fakehost")   # noqa: E731
    assert wait_until(lambda: host() is not None and len(host().stuck) >= 1, 10)
    item = m.items[key]
    with m._lock:
        assert item.state != DOWNLOADING      # (given up on: not left "downloading" for ever)
    # The server is asked again, answers, and a new thread reads through a file of its own.
    assert wait_until(lambda: len(StuckFetcher.made) >= 2 and StuckFetcher.made[1]._reading, 10)
    first, second = StuckFetcher.made[:2]
    assert first is not second
    assert m._fetchers.get(key) is not first
    # Given up on too: the song is partial, and can be evicted once nothing wants it.
    assert wait_until(lambda: len(host().stuck) >= 2, 10)
    with m._lock:
        assert item.state == PARTIAL
        item.priority = None
    assert item in policy.evict_order([item], set(), time.time(), "none")


# -- joining stretches onto a file cut short behind the cache's back ------------------------------------
def _writer_with_island(tmp_path, island_bytes: bytes | None):
    store = Store(tmp_path / "NetCache")
    store.make_folders()
    item = Item(store, key=r"smb:\\srv\sh\b.wav", kind="share", source=r"\\srv\sh\b.wav", host="srv", ext="wav")
    item.size, item.state = 1_000_000, DOWNLOADING
    store.part_path(item).write_bytes(b"\1" * 65536)
    item.visible = item.durable = item.prefix = 65536
    island = Island(65536, 65536 + 4096, 65536 + 4096)
    if island_bytes is not None:
        store.island_path(item, island.start).write_bytes(island_bytes)
    item.islands = [island]
    return store, item, manager_mod._Writer(store, item, item.gen, time.monotonic)


def test_a_part_cut_short_since_it_was_opened_is_never_joined_with_zeros(tmp_path):
    store, item, writer = _writer_with_island(tmp_path, b"\2" * 4096)
    with open(store.part_path(item), "r+b") as f:
        f.truncate(1000)                       # (cut short behind the cache's back)
    try:
        with pytest.raises(Changed):
            writer.join_runs()
    finally:
        writer.close()
    data = store.part_path(item).read_bytes()
    assert data.count(0) == 0 and len(data) == 1000   # nothing filled in
    assert item.visible == 65536 and item.islands      # nothing counted as downloaded


@pytest.mark.parametrize("island_bytes", [None, b"\2" * 100], ids=["gone", "short"])
def test_a_stretch_gone_or_short_is_a_copy_gone_missing_not_an_endless_retry(tmp_path, island_bytes):
    _store, item, writer = _writer_with_island(tmp_path, island_bytes)
    try:
        with pytest.raises(Changed):
            writer.join_runs()
    finally:
        writer.close()
    assert item.gen in item.lost_gens   # (its readers are told the copy went missing: it's fetched afresh)
    assert item.visible == 65536


def test_joining_still_joins(tmp_path):
    store, item, writer = _writer_with_island(tmp_path, b"\2" * 4096)
    try:
        writer.join_runs()
    finally:
        writer.close()
    data = store.part_path(item).read_bytes()
    assert data == b"\1" * 65536 + b"\2" * 4096 and item.visible == 65536 + 4096 and not item.islands
    assert not Path(store.island_path(item, 65536)).exists()

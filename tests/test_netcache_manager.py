"""The download manager, against fake shares (no real network): what it fetches in what order,
resuming, a source that changes, hosts that stop answering, eviction, and the preparer contract
the player uses."""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from ironbark import audiofile, netcache
import importlib

from ironbark.netcache import policy
from ironbark.netcache.manager import CURRENT, NEXT, NotReady, Unavailable
from ironbark.netcache.store import FAILED, PARTIAL, READY, STALE, Item

from netfakes import FakePlayer, FakeShare, Usage, make_manager, unc, wait_until

manager_mod = importlib.import_module("ironbark.netcache.manager")   # (netcache.manager() is a function)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Small chunks and a quick scheduler, so small files still take several chunks."""
    monkeypatch.setattr(manager_mod, "CHUNK_MIN", 16 * 1024)
    monkeypatch.setattr(manager_mod, "CHUNK_MAX", 64 * 1024)
    monkeypatch.setattr(manager_mod, "CHUNK_UNKNOWN", 32 * 1024)
    monkeypatch.setattr(manager_mod, "SCHEDULER_TICK_S", 0.05)
    monkeypatch.setattr(manager_mod, "FSYNC_BYTES", 64 * 1024)


@pytest.fixture
def managers():
    made = []
    yield made
    for m in made:
        m.shutdown()


def first_reads(share: FakeShare) -> list[str]:
    order = []
    for name, op, _, _ in share.log:
        if op == "read" and name not in order:
            order.append(name)
    return order


def key_of(m, name: str) -> str:
    return m.classifier.source_key(unc(name))


def has(m, name: str, test=lambda item: True) -> bool:
    item = m.items.get(key_of(m, name))
    return item is not None and test(item)


def flac_bytes(tmp_path: Path, seconds: float = 3.0, name: str = "src.flac") -> bytes:
    t = np.arange(int(44100 * seconds)) / 44100
    x = 0.3 * np.sin(2 * np.pi * 330 * t)[:, None] * np.ones((1, 2))
    sf.write(tmp_path / name, np.rint(x * 32767).astype(np.int16), 44100, subtype="PCM_16")
    return (tmp_path / name).read_bytes()


# -- 22. priorities ---------------------------------------------------------------------------------------
def test_current_then_next_then_one_more_and_never_song_20(tmp_path, managers):
    share = FakeShare(rate=4_000_000)
    names = [f"{k:02d}.bin" for k in range(1, 21)]
    for name in names:
        share.add(name, os.urandom(200_000))
    player = FakePlayer([unc(n) for n in names])
    m = make_manager(tmp_path / "NetCache", share, player)
    managers.append(m)
    with player.lock:   # the manager never takes the player's lock
        assert wait_until(lambda: all(m.items.get(key_of(m, n)) is not None
                                      and m.items[key_of(m, n)].state == READY for n in names[:3]))
    time.sleep(0.3)
    assert first_reads(share) == names[:3]            # current, next, then "one more"
    reads = [name for name, op, _, _ in share.log if op == "read"]
    assert reads.index("02.bin") > max(i for i, n in enumerate(reads) if n == "01.bin")
    assert share.most_active == 1                      # one transfer per host
    assert "20.bin" not in reads and "04.bin" not in reads


def test_asked_comes_after_current_and_next_but_before_following(tmp_path, managers):
    share = FakeShare(rate=3_000_000)
    names = [f"{k:02d}.bin" for k in range(1, 6)]
    for name in names:
        share.add(name, os.urandom(150_000))
    player = FakePlayer([unc(n) for n in names[:3]])
    m = make_manager(tmp_path / "NetCache", share, player)
    managers.append(m)
    got = {}
    asker = threading.Thread(target=lambda: got.setdefault("t", m.resolve(unc("05.bin"))))
    asker.start()
    asker.join(20)
    assert isinstance(got["t"], audiofile.LocalTrack) and got["t"].ready_path().is_file()
    assert wait_until(lambda: has(m, "03.bin", lambda i: i.state == READY))
    assert first_reads(share) == ["01.bin", "02.bin", "05.bin", "03.bin"]


def test_a_reader_waiting_takes_the_link_within_one_chunk(tmp_path, managers):
    share = FakeShare(rate=1_000_000)
    share.add("01.bin", os.urandom(40_000))
    share.add("02.bin", os.urandom(600_000))
    share.add("06.bin", os.urandom(100_000))
    player = FakePlayer([unc("01.bin"), unc("02.bin")])
    m = make_manager(tmp_path / "NetCache", share, player)
    managers.append(m)
    with pytest.raises(audiofile.NotCached):
        m.resolve(unc("06.bin"), wait=False)           # known, but not wanted
    item = m.items[key_of(m, "06.bin")]
    assert wait_until(lambda: has(m, "02.bin", lambda i: i.visible > 100_000))
    mark = len(share.log)
    item.demand(item.gen, 0, 1000, "play")             # as a waiting decoder does
    assert wait_until(lambda: item.state == READY)
    later = share.log[mark:]
    first = next(i for i, entry in enumerate(later) if entry[0] == "06.bin")
    assert sum(1 for entry in later[:first] if entry[1] == "read") <= 1
    assert wait_until(lambda: m.items[key_of(m, "02.bin")].state == READY)
    offsets = share.reads("02.bin")
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)   # resumed, never restarted


def test_up_next_changes_replan_at_once(tmp_path, managers):
    share = FakeShare(rate=1_000_000)
    for name, size in (("01.bin", 30_000), ("02.bin", 500_000), ("09.bin", 100_000)):
        share.add(name, os.urandom(size))
    player = FakePlayer([unc("01.bin"), unc("02.bin")])
    m = make_manager(tmp_path / "NetCache", share, player)
    managers.append(m)
    assert wait_until(lambda: has(m, "02.bin", lambda i: i.visible > 50_000))
    player.order = [unc("01.bin"), unc("09.bin"), unc("02.bin")]   # something queued as Up next
    m.plan_changed()
    assert wait_until(lambda: has(m, "09.bin", lambda i: i.state == READY))
    assert m.items[key_of(m, "02.bin")].state != READY       # pre-empted part-way
    assert wait_until(lambda: m.items[key_of(m, "02.bin")].state == READY)   # ("one more" picks it up)


# -- 23. resuming --------------------------------------------------------------------------------------------
def test_a_download_resumes_from_its_durable_length_after_a_restart(tmp_path, managers):
    share = FakeShare(rate=400_000)
    data = os.urandom(900_000)
    share.add("big.bin", data)
    player = FakePlayer([unc("big.bin")])
    folder = tmp_path / "NetCache"
    m = make_manager(folder, share, player)
    key = key_of(m, "big.bin")
    assert wait_until(lambda: key in m.items and m.items[key].durable >= 200_000)
    m.shutdown()
    saved = m.items[key].durable
    assert 0 < saved < len(data)
    share.log.clear()
    m2 = make_manager(folder, share, player)
    managers.append(m2)
    item = m2.items[key]
    assert item.prefix == saved and item.state == PARTIAL
    assert wait_until(lambda: item.state == READY)
    assert share.reads("big.bin")[0] == saved
    assert item.ready_path(item.gen, unc("big.bin")).read_bytes() == data


def test_a_changed_source_starts_a_new_version_and_the_old_one_lives_while_read(tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "REVALIDATE_AFTER_S", 0.0)
    share = FakeShare()
    old, new = os.urandom(70_000), os.urandom(80_000)
    share.add("song.bin", old)
    player = FakePlayer([unc("song.bin")])
    m = make_manager(tmp_path / "NetCache", share, player)
    managers.append(m)
    key = key_of(m, "song.bin")
    assert wait_until(lambda: key in m.items and m.items[key].state == READY)
    item = m.items[key]
    first = item.gen
    old_copy = m.store.final_path(item)
    item.acquire(first)                       # a play is reading the old copy
    share.change("song.bin", new)
    assert wait_until(lambda: item.gen == first + 1 and item.state == READY)
    assert item.copy_state(first) == "stale" and item.has(first, len(old))
    with item.open_bytes(first) as f:
        assert f.read() == old                # the old version is still there to finish playing
    assert item.ready_path(item.gen, unc("song.bin")).read_bytes() == new
    assert any("has changed on the drive" in n for n in m.notices)
    item.release(first)
    m._sweep_due = True
    m.plan_changed()
    assert wait_until(lambda: not old_copy.exists())


def test_a_source_that_changes_mid_download_restarts_cleanly(tmp_path, managers):
    share = FakeShare(rate=500_000)
    share.add("song.bin", os.urandom(300_000))
    player = FakePlayer([unc("song.bin")])
    m = make_manager(tmp_path / "NetCache", share, player)
    managers.append(m)
    key = key_of(m, "song.bin")
    assert wait_until(lambda: key in m.items and m.items[key].visible > 60_000)
    first = m.items[key].gen
    replacement = os.urandom(250_000)
    share.change("song.bin", replacement)
    item = m.items[key]
    assert wait_until(lambda: item.state == READY)
    assert item.gen > first and item.ready_path(item.gen, unc("song.bin")).read_bytes() == replacement


# -- 24. hosts that stop answering -----------------------------------------------------------------------------
def test_a_stuck_host_gets_no_more_calls_is_probed_and_comes_back(tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "HOST_SLOW_S", 0.15)
    monkeypatch.setattr(manager_mod, "HOST_DOWN_S", 0.3)
    monkeypatch.setattr(manager_mod, "WORKER_ABANDON_S", 0.6)
    monkeypatch.setattr(manager_mod, "PROBE_BACKOFF_MIN_S", 0.05)
    monkeypatch.setattr(manager_mod, "PROBE_BACKOFF_MAX_S", 0.2)
    share = FakeShare()
    data = os.urandom(100_000)
    share.add("a.bin", data)
    share.open.clear()                        # every call hangs, as a dropped SMB session does
    answers, probes = [False], []

    def probe(name):
        probes.append(name)
        return answers[0]

    player = FakePlayer([unc("a.bin")])
    m = make_manager(tmp_path / "NetCache", share, player, probe_host=probe)
    managers.append(m)
    assert wait_until(lambda: "fakehost" in m.hosts)
    host = m.hosts["fakehost"]
    states = []
    assert wait_until(lambda: states.append(host.state) or host.state == manager_mod.DOWN)
    assert manager_mod.SLOW in states and host.calls == 1     # nothing more while the call is stuck
    assert wait_until(lambda: len(host.stuck) == 1)           # given up on after the limit
    assert wait_until(lambda: len(probes) >= 2)
    assert set(probes) == {"fakehost"}                         # only the server in the song's own path
    answers[0] = True                                          # it answers: a new I/O thread tries again
    assert wait_until(lambda: len(host.stuck) == 2)            # … and gets stuck too
    calls = host.calls
    time.sleep(0.8)
    assert host.calls == calls == 2 and host.state == manager_mod.DOWN   # at most two given up on
    share.open.set()                                           # the server comes back
    item = m.items[key_of(m, "a.bin")]
    assert wait_until(lambda: item.state == READY)
    assert item.ready_path(item.gen, unc("a.bin")).read_bytes() == data
    assert host.state == manager_mod.UP


def test_a_transient_error_marks_the_host_down_and_the_download_resumes(tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "PROBE_BACKOFF_MIN_S", 0.05)
    share = FakeShare(rate=1_000_000)
    data = os.urandom(200_000)
    share.add("a.bin", data)
    share.transient["a.bin"] = 0
    share.stall_at["a.bin"] = (80_000, 0.0)
    player = FakePlayer([unc("a.bin")])
    m = make_manager(tmp_path / "NetCache", share, player)
    managers.append(m)
    key = key_of(m, "a.bin")
    assert wait_until(lambda: key in m.items and m.items[key].visible > 40_000)
    share.transient["a.bin"] = 1
    item = m.items[key]
    assert wait_until(lambda: item.state == READY)
    offsets = share.reads("a.bin")
    assert offsets == sorted(offsets)   # (the failed read is asked again from the same place)
    assert item.ready_path(item.gen, unc("a.bin")).read_bytes() == data


def test_a_file_that_has_gone_is_unavailable(tmp_path, managers):
    share = FakeShare()
    player = FakePlayer([unc("gone.flac")])
    m = make_manager(tmp_path / "NetCache", share, player)
    managers.append(m)
    m.prepare(unc("gone.flac"))
    item = m.items[key_of(m, "gone.flac")]
    assert wait_until(lambda: item.state == FAILED)
    result = m.prepare(unc("gone.flac"))
    assert isinstance(result, Unavailable) and result.permanent
    with pytest.raises(audiofile.DownloadFailed):
        m.resolve(unc("gone.flac"), wait=False)


# -- 25. eviction ------------------------------------------------------------------------------------------------
def _ready(m, name: str, size: int, last_used: float) -> Item:
    key = f"smb:\\\\nas\\s\\{name}"
    item = Item(m.store, key=key, kind="share", source="\\\\nas\\s\\" + name, host="nas", ext="bin",
                size=size, state=READY, last_used=last_used)
    item.final_name = m.store.final_name_for(item)
    m.store.final_path(item).write_bytes(b"x" * size)
    item.visible = item.durable = size
    m.items[key] = item
    return item


def test_evict_order_and_what_is_never_evicted():
    def it(key, state, last_used, **extra):
        item = Item.__new__(Item)
        item.key, item.state, item.last_used = key, state, last_used
        item.pinned, item.priority, item.leases = extra.get("pinned", 0), extra.get("priority"), extra.get("leases", {})
        return item

    now = 10 * 24 * 3600.0
    items = [it("ready-old", READY, 1.0), it("ready-new", READY, now - 10), it("partial-week", PARTIAL, 1.0),
             it("partial-new", PARTIAL, now - 5), it("failed", FAILED, now), it("stale", STALE, now),
             it("leased", READY, 0.5, leases={1: 1}), it("pinned", READY, 0.5, pinned=1),
             it("current", PARTIAL, 0.5, priority=CURRENT), it("next", READY, 0.5, priority=NEXT),
             it("protected", READY, 0.5)]
    order = [i.key for i in policy.evict_order(items, {"protected"}, now, "limit")]
    assert order == ["partial-week", "partial-new", "failed", "stale", "ready-old", "ready-new"]
    assert [i.key for i in policy.evict_order(items, {"protected"}, now, "day")][:1] == ["ready-old"]
    assert set(i.key for i in policy.evict_order(items, {"protected"}, now, "none")) == set(order)


def test_the_limit_evicts_least_recently_used_and_skips_files_in_use(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "GIB", 1000)       # a 1 GB setting means 1000 bytes here
    m = make_manager(tmp_path / "NetCache", FakeShare(), prefs={"net_cache_gb": 1.3}, start=False)
    m.store.open()
    a, b, c, d = (_ready(m, f"{n}.bin", 400, t) for n, t in (("a", 1.0), ("b", 2.0), ("c", 3.0), ("d", 4.0)))
    m._sweep()
    assert set(m.items) == {b.key, c.key, d.key} and not m.store.final_path(a).exists()   # the oldest went
    m.prefs["net_cache_gb"] = 0.5
    b.priority = CURRENT                                  # playing: never evicted
    real_delete = m.store.delete_file
    refused = {m.store.final_path(c)}

    def delete(path):
        if path in refused:           # Windows: "being used by another process"
            refused.discard(path)
            return False
        return real_delete(path)

    monkeypatch.setattr(m.store, "delete_file", delete)
    m._sweep()
    assert set(m.items) == {b.key, c.key}                 # c couldn't go yet, so d went
    m._sweep()
    assert set(m.items) == {b.key} and not m.store.final_path(c).exists()   # tried again, gone
    m.store.close()


def test_the_free_space_floor_shrinks_the_cache(tmp_path):
    usage = Usage(total=100 * 1024 ** 3, used=0, free=5 * 1024 ** 3 - 300)   # 300 bytes under the floor (5%)
    m = make_manager(tmp_path / "NetCache", FakeShare(), start=False, disk_usage=lambda p: usage)
    m.store.open()
    items = [_ready(m, f"{n}.bin", 400, t) for n, t in (("a", 1.0), ("b", 2.0), ("c", 3.0))]
    m._sweep()
    assert set(m.items) == {items[1].key, items[2].key}   # 400 bytes had to go to stay above the floor
    m.store.close()


def test_empty_the_cache_keeps_only_what_is_in_use(tmp_path, managers):
    m = make_manager(tmp_path / "NetCache", FakeShare())
    managers.append(m)
    with m._lock:
        a, b = _ready(m, "a.bin", 100, 1.0), _ready(m, "b.bin", 100, 2.0)
    a.acquire(a.gen)
    assert m.empty().wait(5)
    assert set(m.items) == {a.key} and not m.store.final_path(b).exists()
    a.release(a.gen)


def test_no_room_for_the_current_song_is_unavailable_not_a_direct_read(tmp_path, managers):
    share = FakeShare()
    share.add("big.bin", os.urandom(50_000))
    usage = Usage(total=100 * 1024 ** 3, used=0, free=5 * 1024 ** 3)   # exactly at the floor
    player = FakePlayer([unc("big.bin")])
    m = make_manager(tmp_path / "NetCache", share, player, disk_usage=lambda p: usage)
    managers.append(m)
    m.prepare(unc("big.bin"))
    item = m.items[key_of(m, "big.bin")]
    assert wait_until(lambda: item.error == "disk", 5)
    assert not share.reads("big.bin")                     # nothing was fetched, and never read directly
    result = m.prepare(unc("big.bin"))
    assert isinstance(result, Unavailable) and not result.permanent and "free space" in result.reason
    assert any("free space" in n for n in m.notices)


# -- the preparer contract, end to end ---------------------------------------------------------------------------
def test_prepare_says_preparing_then_posts_and_the_copy_plays_bit_identically(tmp_path, managers):
    data = flac_bytes(tmp_path, seconds=6)
    share = FakeShare(rate=400_000)
    path = share.add("song.flac", data)
    player = FakePlayer([path])
    m = make_manager(tmp_path / "NetCache", share, player, prefs={"net_verify": True})
    managers.append(m)
    assert isinstance(m.prepare(tmp_path / "local.flac"), audiofile.LocalTrack)   # local: today's behaviour
    first = m.prepare(path, role="play")
    assert isinstance(first, NotReady) and first.phase in ("preparing", "starting")
    posted = threading.Event()
    m.notify_when_startable(path, 0.0, posted.set)
    assert posted.wait(20)
    local = m.prepare(path, role="play")
    assert isinstance(local, audiofile.LocalTrack) and local.remote
    item = local.item
    assert item.open_ready and item.info["format"] == "FLAC"
    with audiofile.open(local, patience=5) as f:
        got = np.concatenate(list(audiofile.blocks(f, 4096, "int32", always_2d=True)))
    with audiofile.open(tmp_path / "src.flac") as f:
        want = np.concatenate(list(audiofile.blocks(f, 4096, "int32", always_2d=True)))
    assert np.array_equal(got, want)
    assert wait_until(lambda: item.state == READY)
    assert "flac-md5" in item.verified_by and local.copy_state() == "verified"
    assert m.known_info(path).frames == len(want)    # (from what the probe found: no reading)


def test_a_byte_damaged_in_transit_fails_the_check_and_downloads_again(tmp_path, managers):
    data = flac_bytes(tmp_path, seconds=4)
    share = FakeShare(rate=2_000_000)
    path = share.add("song.flac", data)
    share.flip_once["song.flac"] = len(data) // 2          # one byte wrong, the first time only
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]), prefs={"net_verify": True})
    managers.append(m)
    assert wait_until(lambda: has(m, "song.flac"))
    item = m.items[key_of(m, "song.flac")]
    first = item.gen
    played = audiofile.LocalTrack(path, item, first)       # a play pinned to the first copy
    played.acquire()
    assert wait_until(lambda: item.state == READY and item.gen == first + 1, 20)
    assert played.copy_state() == "failed"                  # (the proof can warn about what was played)
    assert item.ready_path(item.gen, path).read_bytes() == data
    assert any("didn't match the original" in n for n in m.notices)
    played.release()


def test_apple_lossless_waits_for_the_complete_checked_copy(tmp_path, managers):
    import mp4_maker
    sound = (np.random.default_rng(2).standard_normal((44100 * 2, 2)) * 3000).astype(np.int16)
    data = mp4_maker.write_m4a(tmp_path / "src.m4a", sound, moov_first=True).read_bytes()
    share = FakeShare(rate=300_000)
    path = share.add("song.m4a", data)
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]), prefs={"net_verify": True})
    managers.append(m)
    seen = []
    assert wait_until(lambda: seen.append(m.prepare(path)) or isinstance(seen[-1], audiofile.LocalTrack), 20)
    item = m.items[key_of(m, "song.m4a")]
    assert item.state == READY
    assert all(isinstance(r, NotReady) and r.phase in ("starting", "downloading", "checking", "preparing")
               for r in seen[:-1])
    assert isinstance(m.prepare(path, force=True), audiofile.LocalTrack)


def test_whole_file_readers_get_the_complete_copy_and_never_wait_on_the_window_thread(tmp_path):
    data = flac_bytes(tmp_path, seconds=2)
    share = FakeShare(rate=300_000)
    path = share.add("song.flac", data)
    from netfakes import FakeClassifier
    m = netcache.install({"net_verify": False}, None, where=tmp_path / "NetCache", classifier=FakeClassifier(),
                         fetcher=share.fetcher, probe_host=lambda name: True)
    try:
        assert m.wait_loaded(10)
        started = time.perf_counter()
        with pytest.raises(audiofile.NotCached):
            audiofile.open(path)                       # the main (window) thread never waits
        assert time.perf_counter() - started < 0.05
        assert audiofile.local_copy(path) is None
        result = {}

        def tool():
            with audiofile.open(path) as f:            # a worker thread waits for the copy
                result["frames"] = f.frames

        thread = threading.Thread(target=tool)
        thread.start()
        thread.join(20)
        assert result["frames"] == 88200
        assert audiofile.local_copy(path).is_file()
    finally:
        netcache.uninstall()

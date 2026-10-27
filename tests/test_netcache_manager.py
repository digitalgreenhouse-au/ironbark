"""The download manager, against fake shares (no real network): what it fetches in what order,
resuming, a source that changes, hosts that stop answering, eviction, and the preparer contract
the player uses."""
from __future__ import annotations

import importlib
import os
import socket
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from netfakes import FakePlayer, FakeShare, Usage, make_manager, unc, wait_until

from ironbark import audiofile, netcache
from ironbark.netcache import policy
from ironbark.netcache.manager import CURRENT, NEXT, NotReady, Unavailable
from ironbark.netcache.store import FAILED, PARTIAL, READY, STALE, Item

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


def _known(m, path):
    """_get's arguments for `path`: (key, path, kind, host)."""
    kind, key, host = m._describe(path)
    return key, path, kind, host


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


# -- priorities ---------------------------------------------------------------------------------------
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
    with m._lock:                                      # known, but not wanted
        item = m._get(*_known(m, unc("06.bin")))
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


# -- resuming --------------------------------------------------------------------------------------------
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


# -- hosts that stop answering -----------------------------------------------------------------------------
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


# -- eviction ------------------------------------------------------------------------------------------------
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
    monkeypatch.setattr(manager_mod, "GB", 1000)       # a 1 GB setting means 1000 bytes here
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
    usage = Usage(total=100 * 1024 ** 3, used=0, free=2 * 1024 ** 3 - 300)   # 300 bytes under the floor (2 GB)
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
    usage = Usage(total=100 * 1024 ** 3, used=0, free=2 * 1024 ** 3)   # exactly at the floor
    player = FakePlayer([unc("big.bin")])
    m = make_manager(tmp_path / "NetCache", share, player, disk_usage=lambda p: usage)
    managers.append(m)
    m.prepare(unc("big.bin"))
    item = m.items[key_of(m, "big.bin")]
    assert wait_until(lambda: item.error == "disk", 5)
    assert not share.reads("big.bin")                     # nothing was fetched, and never read directly
    result = m.prepare(unc("big.bin"))
    assert isinstance(result, Unavailable) and not result.permanent and "free space" in result.reason
    assert "plus 2 GB kept free" in result.reason        # says what it keeps back, not just the song's size
    assert any("free space" in n for n in m.notices)


def test_a_big_drive_with_plenty_free_is_room_whatever_its_size(tmp_path, managers):
    # A 952 GB drive with 27 GB free: the cache's size is the user's setting, never a share of the drive.
    share = FakeShare()
    share.add("song.bin", os.urandom(50_000))
    usage = Usage(total=1022 * 1000 ** 3, used=993 * 1000 ** 3, free=29 * 1000 ** 3)
    player = FakePlayer([unc("song.bin")])
    m = make_manager(tmp_path / "NetCache", share, player, disk_usage=lambda p: usage)
    managers.append(m)
    m.prepare(unc("song.bin"))
    item = m.items[key_of(m, "song.bin")]
    assert wait_until(lambda: share.reads("song.bin"), 5)
    assert item.error != "disk"
    assert not any("free space" in n for n in m.notices)


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
    assert any("failed its check" in n for n in m.notices)
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


# -- a download complete but not confirmed, checks that fail, the files behind the cache's back --------------------
def _failing_at_the_end(share: FakeShare, size: int):
    """The share's fetcher, except that the read past the last byte (the one that confirms the file
    ends there) fails once, as if the network went at that moment."""
    made, tripped = share.fetcher, []

    def fetcher(source):
        f = made(source)
        real = f.read

        def read(offset, n):
            if offset >= size and not tripped:
                tripped.append(offset)
                raise manager_mod.Transient("the server didn't answer")
            return real(offset, n)
        f.read = read
        return f
    share.fetcher = fetcher
    return tripped


def test_a_download_whose_last_read_failed_is_still_checked_and_put_in_place(tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "PROBE_BACKOFF_MIN_S", 0.05)
    data = flac_bytes(tmp_path, seconds=3)
    share = FakeShare()
    path = share.add("song.flac", data)
    tripped = _failing_at_the_end(share, len(data))
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]),
                     prefs={"net_verify": True, "net_mode": "complete"})
    managers.append(m)
    key = key_of(m, "song.flac")
    assert wait_until(lambda: key in m.items and m.items[key].state == READY, 10)
    item = m.items[key]
    assert tripped and "decode" in item.verified_by
    assert item.ready_path(item.gen, path).read_bytes() == data
    assert isinstance(m.prepare(path), audiofile.LocalTrack)   # (Complete track: it starts)


def test_a_complete_download_not_confirmed_when_the_player_closed_is_finished_off_after_a_restart(
        tmp_path, managers):
    data = os.urandom(150_000)
    share = FakeShare()
    path = share.add("a.bin", data)
    folder = tmp_path / "NetCache"
    m = make_manager(folder, share, start=False)
    key = m.classifier.source_key(path)
    store = m.store
    store.open()
    item = Item(store, key=key, kind="share", source=str(path), host="fakehost", ext="bin", size=len(data),
                mtime_ns=1_000_000_000, state=PARTIAL, prefix=len(data))
    store.part_path(item).write_bytes(data)
    store.save([item])
    store.close()
    m = make_manager(folder, share, FakePlayer([path]))
    managers.append(m)
    assert wait_until(lambda: key in m.items and m.items[key].state == READY, 10)
    assert share.reads("a.bin") == [len(data)]            # only the read that confirms where it ends
    assert m.items[key].ready_path(m.items[key].gen, path).read_bytes() == data


def test_a_copy_that_fails_its_check_twice_is_never_played_and_is_skipped_until_asked_again(
        tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "FAILED_FORGET_S", 0.5)
    data = flac_bytes(tmp_path, seconds=3)
    share = FakeShare(rate=2_000_000)
    path = share.add("bad.flac", data)
    share.flip_each["bad.flac"] = [len(data) * k // 4 for k in (3, 2, 1)]   # a different byte wrong in each transfer
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]), prefs={"net_verify": True})
    managers.append(m)
    key = key_of(m, "bad.flac")
    assert wait_until(lambda: key in m.items and m.items[key].state == FAILED, 20)
    item = m.items[key]
    result = m.prepare(path)
    assert isinstance(result, Unavailable) and result.permanent and "twice" in result.reason
    assert item.visible == 0 and item.gen not in item.failed_gens   # (the bytes that failed are gone)
    assert "Downloading it again" in m.notices[0] and m.notices[-1].endswith("so the song is skipped for now.")
    reads = len(share.reads("bad.flac"))
    time.sleep(0.6)
    seen = []
    assert wait_until(lambda: seen.append(m.prepare(path)) or len(share.reads("bad.flac")) > reads, 10)
    assert not any(isinstance(r, audiofile.LocalTrack) for r in seen)   # asked again: a fresh download


def test_a_complete_copy_whose_source_has_gone_keeps_playing_from_the_cache(tmp_path, managers):
    data = os.urandom(120_000)
    share = FakeShare()
    path = share.add("a.bin", data)
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]))
    managers.append(m)
    key = key_of(m, "a.bin")
    assert wait_until(lambda: key in m.items and m.items[key].state == READY)
    item = m.items[key]
    share.remove("a.bin")
    calls = len(share.log)
    item.verified_at = 0.0                                # due to be looked at again
    m._wake.set()
    assert wait_until(lambda: any(e[1] == "identity" for e in share.log[calls:]), 5)
    assert wait_until(lambda: item.verified_at > 0.0, 5)
    assert item.state == READY and isinstance(m.prepare(path), audiofile.LocalTrack)


def test_the_writer_never_fills_a_missing_part_with_zeros(tmp_path):
    m = make_manager(tmp_path / "NetCache", FakeShare(), start=False)
    m.store.make_folders()
    item = Item(m.store, key="smb:x", kind="share", source="\\\\h\\s\\x.wav", host="h", ext="wav", size=100_000)
    item.visible = item.durable = 60_000                  # (as recorded, but the .part was deleted meanwhile)
    with pytest.raises(manager_mod.Changed):
        manager_mod._Writer(m.store, item, item.gen, time.monotonic)
    m.store.part_path(item).write_bytes(b"\1" * 1000)     # ... or cut short
    with pytest.raises(manager_mod.Changed):
        manager_mod._Writer(m.store, item, item.gen, time.monotonic)
    assert m.store.part_path(item).stat().st_size == 1000


def test_a_new_version_begun_while_a_copy_is_put_in_place_keeps_it_out_of_use(tmp_path, monkeypatch):
    m = make_manager(tmp_path / "NetCache", FakeShare(), start=False)
    m.store.make_folders()
    path = unc("a.bin")
    key = m.classifier.source_key(path)
    item = Item(m.store, key=key, kind="share", source=str(path), host="fakehost", ext="bin", size=1000,
                state="verifying", verified_by="size+identity")
    item.visible = item.durable = 1000
    item.final_name = m.store.final_name_for(item)
    m.store.part_path(item).write_bytes(b"\2" * 1000)
    m.items[key] = item
    m._paths[key] = path
    m._renames[key] = m._clock()
    real_replace = os.replace

    def replace(src, dst):
        m.invalidate(path)                                # (its tags saved through the player, say)
        real_replace(src, dst)
    monkeypatch.setattr(manager_mod.os, "replace", replace)
    m._try_renames(m._clock())
    assert item.gen == 2 and item.state != READY and item.final_name is None
    assert not isinstance(m._evaluate(item, path, 0.0, False), audiofile.LocalTrack)
    monkeypatch.setattr(manager_mod.os, "replace", real_replace)
    m._sweep()
    assert not list(m.store.tracks.iterdir())             # the copy of the old version went with it


def test_clearing_the_cache_never_deletes_files_under_the_lock_the_player_asks(tmp_path, managers, monkeypatch):
    m = make_manager(tmp_path / "NetCache", FakeShare())
    managers.append(m)
    with m._lock:
        items = [_ready(m, f"{n}.bin", 100, float(n)) for n in range(1, 4)]
    real_delete = m.store.delete_file
    held = []

    def delete(target):
        held.append(m._lock._is_owned())
        time.sleep(0.2)                                   # (a slow disk, or a busy one)
        return real_delete(target)
    monkeypatch.setattr(m.store, "delete_file", delete)
    done = m.empty()
    assert wait_until(lambda: held, 5)
    started = time.perf_counter()
    m.progress(unc("1.bin"))                              # as the window does, every moment
    assert time.perf_counter() - started < 0.1
    assert done.wait(10)
    assert held and not any(held)
    assert not m.items and not any(m.store.final_path(item).exists() for item in items)


def test_songs_only_asked_about_arent_counted_or_kept(tmp_path, managers):
    share = FakeShare()
    path = share.add("a.bin", os.urandom(1000))
    m = make_manager(tmp_path / "NetCache", share)
    managers.append(m)
    with pytest.raises(audiofile.NotCached):
        m.resolve(path, wait=False)                      # (an album's loudness: only what's here)
    assert not m.items
    with m._lock:
        m._get(*_known(m, path))
    assert m.snapshot().songs == 0


def test_nothing_being_fetched_or_checked_is_ever_cleared_away():
    from ironbark.netcache.store import DOWNLOADED, DOWNLOADING, VERIFYING

    def it(key, state):
        item = Item.__new__(Item)
        item.key, item.state, item.last_used, item.pinned, item.priority, item.leases = key, state, 1.0, 0, None, {}
        return item

    items = [it("fetching", DOWNLOADING), it("downloaded", DOWNLOADED), it("checking", VERIFYING),
             it("partial", PARTIAL), it("ready", READY)]
    for keep in ("none", "day", "limit", "session"):
        order = {i.key for i in policy.evict_order(items, set(), 10 * 24 * 3600.0, keep, session=set())}
        assert order == {"partial", "ready"}


# -- a file damaged on the drive itself, files left open, copies gone, servers that answer oddly ----------
@pytest.mark.parametrize("damage", ["md5", "frame"])
def test_a_flac_damaged_on_the_drive_itself_plays_from_the_cache_as_it_would_directly(tmp_path, managers, damage):
    data = bytearray(flac_bytes(tmp_path, seconds=3))
    if damage == "md5":
        data[data.find(b"fLaC") + 30] ^= 0xFF             # its own MD5 fingerprint is wrong on the drive
    else:
        data[len(data) // 2] ^= 0xFF                       # one of its frames is damaged on the drive
    share = FakeShare(rate=2_000_000)
    path = share.add("bad.flac", bytes(data))
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]), prefs={"net_verify": True})
    managers.append(m)
    key = key_of(m, "bad.flac")
    assert wait_until(lambda: key in m.items and m.items[key].state == READY, 20)
    item = m.items[key]
    assert item.ready_path(item.gen, path).read_bytes() == bytes(data)   # exactly what's on the drive
    assert share.reads("bad.flac").count(0) == 2           # downloaded twice, the same both times: then kept
    assert "flac-md5" not in item.verified_by and item.info.get("source_problem")
    local = m.prepare(path)
    assert isinstance(local, audiofile.LocalTrack) and local.copy_state() == "verified"
    assert "the file on the drive itself" in m.notices[-1]
    assert not any("didn't match the original" in n for n in m.notices)


class _Holding(FakeShare):
    """A share that remembers which of its files a fetcher holds open (from its first read until closed)."""

    def __init__(self, rate=None):
        super().__init__(rate)
        self.held: set = set()

    def fetcher(self, source):
        inner, share = super().fetcher(source), self

        class Fetcher:
            def identity(self):
                return inner.identity()

            def read(self, offset, n):
                share.held.add(inner.name)
                return inner.read(offset, n)

            def close(self):
                share.held.discard(inner.name)

        return Fetcher()


def test_a_download_that_stops_part_way_lets_go_of_the_file_on_the_share(tmp_path, managers):
    share = _Holding(rate=300_000)
    a = share.add("a.bin", os.urandom(600_000))
    b = share.add("b.bin", os.urandom(60_000))
    player = FakePlayer([a])
    m = make_manager(tmp_path / "NetCache", share, player)
    managers.append(m)
    key = key_of(m, "a.bin")
    assert wait_until(lambda: key in m.items and m.items[key].visible > 50_000)
    player.order = [b]                                     # skipped part-way: it leaves the plan
    m.plan_changed()
    assert wait_until(lambda: has(m, "b.bin", lambda item: item.state == READY))
    assert wait_until(lambda: "a.bin" not in share.held, 3)   # (renaming or deleting it on the share works again)
    assert m.items[key].state == PARTIAL


def test_the_share_fetcher_never_stops_the_file_being_renamed_or_deleted(tmp_path):
    from ironbark.netcache.fetch import ShareFetcher
    source = tmp_path / "song.flac"
    source.write_bytes(os.urandom(100_000))
    fetcher = ShareFetcher(source)
    assert fetcher.read(0, 1000) == source.read_bytes()[:1000]   # (held open between chunks)
    os.replace(source, tmp_path / "renamed.flac")          # renamed or moved in Explorer, say
    (tmp_path / "renamed.flac").unlink()                   # … or deleted
    fetcher.close()


def test_a_complete_copy_deleted_behind_the_caches_back_is_fetched_again_not_skipped(tmp_path, managers):
    data = flac_bytes(tmp_path, seconds=2)
    share = FakeShare(rate=2_000_000)
    path = share.add("song.flac", data)
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]))
    managers.append(m)
    key = key_of(m, "song.flac")
    assert wait_until(lambda: key in m.items and m.items[key].state == READY)
    item = m.items[key]
    local = m.prepare(path)
    local.ready_path().unlink()                            # cleared away by hand from "Open folder", say
    with pytest.raises(audiofile.Waiting):                 # the player waits ("Preparing…"), never skips it
        audiofile.open(local)
    again = m.prepare(path)
    assert not isinstance(again, audiofile.LocalTrack) or again.gen > local.gen
    assert wait_until(lambda: item.state == READY and item.gen > local.gen, 10)
    fresh = m.prepare(path)
    assert isinstance(fresh, audiofile.LocalTrack)
    with audiofile.open(fresh) as f:
        assert f.frames == 2 * 44100


def test_a_whole_file_reader_waits_for_a_fresh_copy_when_the_complete_one_was_deleted(tmp_path):
    data = flac_bytes(tmp_path, seconds=2)
    share = FakeShare(rate=2_000_000)
    path = share.add("song.flac", data)
    from netfakes import FakeClassifier
    m = netcache.install({"net_verify": False}, None, where=tmp_path / "NetCache", classifier=FakeClassifier(),
                         fetcher=share.fetcher, probe_host=lambda name: True, watch_network=None)
    try:
        assert m.wait_loaded(10)
        result = {}

        def read():
            try:
                with audiofile.open(path) as f:
                    result["frames"] = f.frames
            except Exception as error:   # noqa: BLE001
                result["error"] = error

        reader = threading.Thread(target=read)
        reader.start()
        reader.join(15)
        item = m.items[key_of(m, "song.flac")]
        assert item.state == READY and result.pop("frames") == 2 * 44100
        gen = item.gen
        item.ready_path(gen, path).unlink()                # gone from this computer behind the cache's back
        reader = threading.Thread(target=read)
        reader.start()
        reader.join(15)
        assert "error" not in result, result.get("error")
        assert result["frames"] == 2 * 44100 and item.gen > gen
    finally:
        netcache.uninstall()


def test_a_server_the_file_sharing_probe_cant_reach_still_comes_back(tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "PROBE_BACKOFF_MIN_S", 0.05)
    share = FakeShare(rate=1_000_000)
    data = os.urandom(200_000)
    share.add("a.bin", data)
    path = Path("\\\\nas.local@SSL\\DavWWWRoot\\a.bin")    # a WebDAV drive: no file-sharing port to try
    probes = []
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]),
                     probe_host=lambda name: probes.append(name) or False)
    managers.append(m)
    key = m.classifier.source_key(path)
    assert wait_until(lambda: key in m.items and m.items[key].visible > 40_000)
    share.transient["a.bin"] = 1                           # one dropped read
    item = m.items[key]
    assert wait_until(lambda: item.state == READY, 10)
    assert item.ready_path(item.gen, path).read_bytes() == data
    assert m.hosts["nas.local@ssl"].state == manager_mod.UP and not probes


def test_a_server_that_never_answers_on_the_file_sharing_port_still_comes_back(tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "PROBE_BACKOFF_MIN_S", 0.05)
    monkeypatch.setattr(manager_mod, "PROBE_BACKOFF_MAX_S", 0.1)
    monkeypatch.setattr(manager_mod, "ASK_FILES_AFTER_S", 0.5)
    share = FakeShare(rate=1_000_000)
    data = os.urandom(200_000)
    path = share.add("a.bin", data)
    probes = []
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]),
                     probe_host=lambda name: probes.append(name) or False)   # (an NFS-only server, say)
    managers.append(m)
    key = key_of(m, "a.bin")
    assert wait_until(lambda: key in m.items and m.items[key].visible > 40_000)
    share.transient["a.bin"] = 1
    item = m.items[key]
    assert wait_until(lambda: item.state == READY, 10)
    assert m.hosts["fakehost"].state == manager_mod.UP and probes


def test_a_file_that_fails_every_read_is_skipped_and_its_server_stays_up(tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "PROBE_BACKOFF_MIN_S", 0.05)
    share = FakeShare()
    bad = share.add("bad.flac", os.urandom(100_000))
    good = share.add("good.flac", os.urandom(100_000))
    share.transient["bad.flac"] = 10 ** 6                  # an unreadable spot on the server's disk
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([bad, good]))
    managers.append(m)
    key = key_of(m, "bad.flac")
    assert wait_until(lambda: key in m.items and m.items[key].state == FAILED, 10)
    result = m.prepare(bad)
    assert isinstance(result, Unavailable) and result.permanent
    assert result.reason == "it couldn't be read from the server"
    assert wait_until(lambda: has(m, "good.flac", lambda i: i.state == READY), 10)
    host = m.hosts["fakehost"]
    reads = len(share.reads("bad.flac"))
    time.sleep(0.5)
    assert host.state == manager_mod.UP and len(share.reads("bad.flac")) == reads   # no endless retries


def test_why_a_song_couldnt_be_read_is_said_in_plain_words(tmp_path):
    from ironbark.netcache import fetch
    song = "\\\\nas\\Shared\\Music\\Album\\song.flac"
    gone = fetch._classify(FileNotFoundError(2, "The system cannot find the file specified", song))
    assert isinstance(gone, fetch.Permanent) and str(gone) == "it's no longer on \\\\nas\\Shared"
    assert "cannot find" in gone.detail
    refused = fetch._classify(PermissionError(13, "Permission denied", song))
    assert isinstance(refused, fetch.Permanent)
    assert str(refused) == "the server won't let Ironbark read it (it may be open in another program)"
    assert str(fetch._classify(FileNotFoundError(2, "No such file", "Z:\\Music\\song.flac"))) == (
        "it's no longer on Z:")
    if os.name == "nt":
        busy = fetch._classify(OSError(0, "used by another process", song, 32))
        assert isinstance(busy, fetch.Permanent) and str(busy).startswith("the server won't let")
        assert isinstance(fetch._classify(OSError(0, "The network name is no longer available", song, 64)),
                          fetch.Transient)
    with pytest.raises(fetch.Permanent, match="^it's no longer on "):
        fetch.ShareFetcher(tmp_path / "gone.flac").identity()


def test_the_cache_limit_is_counted_in_the_gigabytes_settings_shows(tmp_path):
    m = make_manager(tmp_path / "NetCache", FakeShare(), prefs={"net_cache_gb": 10}, start=False)
    assert m._limit() == 10e9                              # Settings shows "10 GB" as used / 1e9

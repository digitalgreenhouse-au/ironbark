"""How the network playback cache plans: the quality class and its hysteresis, servers' figures
saved for the next start, pauses for the network remembered (also across a device let go),
Pre-download (Off / Next track / Next 2 tracks / Entire queue) within its budget, narrowing when
the link can't keep up, Keep cached tracks and the cache size, and hearing network changes.
Fake shares, a fake clock where time matters; no real network."""
from __future__ import annotations

import importlib
import socket
import sys
import threading
import time

import numpy as np
import pytest

from ironbark import flacframes
from ironbark.netcache import netwatch, policy
from ironbark.netcache.policy import EXCELLENT, GOOD, POOR, UNSTABLE, QualityScore
from ironbark.netcache.store import READY, Item

from netfakes import FakePlayer, FakeShare, make_manager, unc, wait_until

manager_mod = importlib.import_module("ironbark.netcache.manager")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(manager_mod, "CHUNK_MIN", 16 * 1024)
    monkeypatch.setattr(manager_mod, "CHUNK_MAX", 64 * 1024)
    monkeypatch.setattr(manager_mod, "CHUNK_UNKNOWN", 32 * 1024)
    monkeypatch.setattr(manager_mod, "SCHEDULER_TICK_S", 0.05)


@pytest.fixture
def managers():
    made = []
    yield made
    for m in made:
        m.shutdown()


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def names_read(share: FakeShare) -> set[str]:
    return {name for (name, op, _, _) in share.log if op == "read"}


# -- the quality class ---------------------------------------------------------------------------------
def test_quality_drops_at_once_and_rises_only_after_a_minute_at_the_better_level():
    score = QualityScore()
    assert score.update(EXCELLENT, 0) == EXCELLENT
    assert score.update(POOR, 1) == POOR                      # down at once
    for t in range(2, 61):
        assert score.update(EXCELLENT, t) == POOR             # better, but not for a minute yet
    assert score.update(GOOD, 61) == POOR
    assert score.update(EXCELLENT, 62) == GOOD                # a minute better: up to the worst of that minute
    assert score.update(UNSTABLE, 63) == UNSTABLE
    assert score.update(EXCELLENT, 100) == UNSTABLE
    assert score.update(UNSTABLE, 110) == UNSTABLE            # a relapse starts the minute again
    assert score.update(EXCELLENT, 160) == UNSTABLE
    assert score.update(EXCELLENT, 219) == UNSTABLE
    assert score.update(EXCELLENT, 220) == EXCELLENT


def test_two_pauses_for_the_network_in_two_minutes_are_unstable():
    assert policy.quality(10.0, 0.0, 0, stalls2=1) == EXCELLENT
    assert policy.quality(10.0, 0.0, 0, stalls2=2) == UNSTABLE
    assert policy.worse(GOOD, POOR) == POOR and policy.worse(EXCELLENT, UNSTABLE) == UNSTABLE


def test_the_rule_never_uses_a_better_class_than_the_servers_own(tmp_path, managers):
    clock = Clock()
    m = make_manager(tmp_path / "NetCache", FakeShare(), clock=clock)
    managers.append(m)
    host = m._add_host("nas", "share")
    for second in range(30):                                  # 4 MB/s steady: excellent for a CD-quality FLAC
        clock.now += 1
        host.speed.add(clock.now, 4_000_000, 1.0)
    assert m._quality(host, 30.0, 0.0, 0, clock.now) == EXCELLENT
    host.score.update(POOR, clock.now)                        # the server was poor a moment ago
    assert m._quality(host, 30.0, 0.0, 0, clock.now) == POOR
    host.stalls.extend([clock.now - 10, clock.now - 5])
    assert m._quality(host, 30.0, 0.0, 0, clock.now) == UNSTABLE


# -- saved figures: a warm start ------------------------------------------------------------------------
def test_a_servers_figures_are_saved_and_start_the_next_run_if_under_an_hour_old(tmp_path, managers):
    share = FakeShare(rate=3_000_000)
    path = share.add("a.bin", bytes(2_000_000))
    first = make_manager(tmp_path / "NetCache", share, FakePlayer([path]))
    assert wait_until(lambda: first.items and next(iter(first.items.values())).state == READY, 30)
    measured = first.hosts["fakehost"].speed.value()
    assert measured and first.hosts["fakehost"].speed.measured()
    first.shutdown()

    again = make_manager(tmp_path / "NetCache", FakeShare())
    managers.append(again)
    host = again.hosts["fakehost"]                            # (named by the copy kept from the first run)
    assert host.speed.value() == pytest.approx(measured) and not host.speed.measured()
    assert host.score.current == EXCELLENT

    rows = again.store.load_hosts()
    old = make_manager(tmp_path / "Other", FakeShare(), wall=lambda: time.time() + 2 * 3600)
    managers.append(old)
    old._warm = {row["host"]: row for row in rows}
    assert old._add_host("fakehost", "share").speed.value() is None     # two hours old: not used


def test_a_warm_start_carries_recent_gaps_and_failures():
    clock = Clock(5000.0)
    speed = policy.Speed(clock)
    speed.warm(2_000_000, age=120, gap=8.0, failures=2)
    assert speed.value() == 2_000_000 and speed.gap15(clock.now) == 8.0 and speed.failures15(clock.now) == 2
    clock.now += 3600
    assert speed.value() is None                              # past an hour since it was measured
    stale = policy.Speed(clock)
    stale.warm(2_000_000, age=4000)
    assert stale.value() is None


# -- pauses for the network remembered -----------------------------------------------------------------
def test_pauses_on_a_song_make_its_next_start_wait_longer_even_after_the_device_was_let_go(tmp_path, managers):
    clock = Clock()
    m = make_manager(tmp_path / "NetCache", FakeShare(), clock=clock)
    managers.append(m)
    host = m._add_host("nas", "share")
    item = Item(m.store, key="k", kind="share", source="\\\\nas\\s\\a.flac", host="nas", ext="flac", size=30_000_000)
    item.info.update(format="FLAC", subtype="PCM_16", rate=44100, channels=2, frames=44100 * 300, header_bytes=8000)
    item.open_ready = True
    m.items["k"] = item
    path = unc("a.flac")
    need0 = m._rule(item, path, 10.0)[1]
    local = type("L", (), {"item": item, "gen": item.gen, "source": path})()
    m.note_stall(local)                                        # the player: one pause on this song
    m.resumable(local, 44100 * 10, 1)
    m.resumable(local, 44100 * 10, 1)                          # (asked again: still one)
    assert len(host.stalls) == 1
    assert m._rule(item, path, 10.0)[1] == pytest.approx(min(290.0, need0 * 2))   # a fresh start (after a release) too
    clock.now += policy.STALLS_UNSTABLE_S + 10                 # (two pauses within two minutes would be unstable)
    m.note_stall(local)                                        # a second pause, in a new session of the same song
    assert len(host.stalls) == 2
    assert m._rule(item, path, 10.0)[1] == pytest.approx(min(290.0, need0 * 4))
    clock.now += policy.STALL_STEP_WINDOW_S + 1
    assert m._rule(item, path, 10.0)[1] == pytest.approx(need0)


# -- Pre-download and its budget ----------------------------------------------------------------------
@pytest.mark.parametrize("choice, fetched", [("off", 1), ("next", 2), ("next2", 3), ("queue", 6)])
def test_pre_download_fetches_as_far_ahead_as_chosen(tmp_path, managers, choice, fetched):
    share = FakeShare(rate=8_000_000)
    paths = [share.add(f"{k}.bin", bytes(100_000)) for k in range(6)]
    m = make_manager(tmp_path / "NetCache", share, FakePlayer(paths), prefs={"net_predownload": choice})
    managers.append(m)
    want = {f"{k}.bin" for k in range(fetched)}
    assert wait_until(lambda: want <= names_read(share) and all(
        i.state == READY for i in m.items.values() if i.source.rsplit("\\", 1)[-1] in want), 20)
    time.sleep(0.4)
    read = names_read(share)
    extra = read - want
    # (with everything wanted complete and the link idle, "one more" may start the song after — not when Off)
    assert len(extra) <= (1 if choice in ("next", "next2") else 0) and (not extra or extra == {f"{fetched}.bin"})


def test_songs_not_heard_yet_use_at_most_half_the_cache_limit():
    assert policy.within_budget([100, 100, 100, 100], limit=500) == 2
    assert policy.within_budget([400], limit=500) == 1                  # the next song always comes
    assert policy.within_budget([None, None, 100], limit=500) == 1       # a size not known yet: learnt first
    assert policy.within_budget([100, None, 100], limit=500) == 2
    assert policy.within_budget([100] * 50, limit=float("inf")) == 50


def test_entire_queue_stops_at_the_budget(tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "GIB", 1_000_000)        # "1 GB" is 1 MB here: half of it for songs ahead
    share = FakeShare(rate=8_000_000)
    paths = [share.add(f"{k}.bin", bytes(200_000)) for k in range(8)]
    m = make_manager(tmp_path / "NetCache", share, FakePlayer(paths),
                     prefs={"net_predownload": "queue", "net_cache_gb": 1})
    managers.append(m)
    assert wait_until(lambda: {"0.bin", "1.bin", "2.bin"} <= names_read(share), 20)
    time.sleep(0.5)
    assert names_read(share) <= {"0.bin", "1.bin", "2.bin", "3.bin"}   # (3.bin: "one more" at most)


def test_when_the_link_cant_keep_up_only_this_song_and_the_next_are_fetched(tmp_path, managers):
    assert policy.feasible(None, 10, 10 ** 9, 10 ** 9)                  # not measured yet: assume it can
    assert policy.feasible(1_000_000, 100, 40_000_000, 50_000_000)
    assert not policy.feasible(1_000_000, 60, 40_000_000, 50_000_000)
    clock = Clock()
    m = make_manager(tmp_path / "NetCache", FakeShare(), clock=clock, start=False)
    host = m._add_host("nas", "share")
    for _ in range(5):
        clock.now += 1
        host.speed.add(clock.now, 500_000, 1.0)                # 0.5 MB/s
    window = []
    for position, name in enumerate(("now.flac", "next.flac")):
        item = Item(m.store, key=name, kind="share", source=f"\\\\nas\\s\\{name}", host="nas", ext="flac",
                    size=40_000_000)
        item.info.update(format="FLAC", subtype="PCM_16", rate=44100, channels=2, frames=44100 * 120)
        m.items[name] = item
        window.append((position, name, unc(name), None, "nas"))
    m._positions["now.flac"] = (unc("now.flac"), 0.0, clock.now)
    assert not m._feasible(window, clock.now)                  # 80 MB in 2 minutes at 0.5 MB/s: no
    m.items["next.flac"].visible = 39_000_000
    m.items["now.flac"].visible = 30_000_000
    assert m._feasible(window, clock.now)


def _flac_item(m, name: str, size: int = 30_000_000, seconds: int = 300) -> Item:
    item = Item(m.store, key=name, kind="share", source=str(unc(name)), host="nas", ext="flac", size=size)
    item.info.update(format="FLAC", subtype="PCM_16", rate=44100, channels=2, frames=44100 * seconds,
                     header_bytes=8000)
    m.items[name] = item
    m._paths[name] = unc(name)
    return item


def test_once_what_plays_from_the_seek_on_is_here_the_stretch_behind_waits_its_turn(tmp_path):
    from ironbark.netcache.store import Island
    clock = Clock()
    player = FakePlayer([unc("now.flac"), unc("next.flac")])
    m = make_manager(tmp_path / "NetCache", FakeShare(), player, clock=clock, start=False)
    m.classifier.source_key = lambda path: path.name          # (keys as the items above were made)
    m.classifier.host_of = lambda path: "nas"
    m._add_host("nas", "share")
    now, following = _flac_item(m, "now.flac"), _flac_item(m, "next.flac")
    now.visible = 2_000_000
    m._positions["now.flac"] = (unc("now.flac"), 200.0, clock.now)   # sought to 3:20
    m._replan(clock.now)
    assert (now.priority, following.priority) == (manager_mod.CURRENT, manager_mod.NEXT)
    with m._lock:
        offset, limit, island = m._next_range(now)               # fetched from where it plays
    assert island is not None and 19_000_000 < offset < 20_008_000 and limit == now.size
    island.visible = now.size                                    # … to the end
    m._replan(clock.now)
    assert now.priority == manager_mod.GAP                       # the stretch behind now waits for the next song
    with m._lock:
        assert m._next_range(now) == (2_000_000, island.start, None)


def test_when_the_link_cant_keep_up_nothing_past_the_next_song_is_wanted(tmp_path):
    clock = Clock()
    names = ["a.flac", "b.flac", "c.flac", "d.flac"]
    m = make_manager(tmp_path / "NetCache", FakeShare(), FakePlayer([unc(n) for n in names]), clock=clock,
                     start=False, prefs={"net_predownload": "queue"})
    m.classifier.source_key = lambda path: path.name
    m.classifier.host_of = lambda path: "nas"
    host = m._add_host("nas", "share")
    items = [_flac_item(m, name, size=40_000_000, seconds=120) for name in names]
    m._replan(clock.now)
    assert not m.narrowed and [i.priority for i in items] == [manager_mod.CURRENT, manager_mod.NEXT,
                                                              manager_mod.FOLLOWING, manager_mod.QUEUE]
    for _ in range(5):
        clock.now += 1
        host.speed.add(clock.now, 500_000, 1.0)                  # 0.5 MB/s: 80 MB can't come in 2 minutes
    m._replan(clock.now)
    assert m.narrowed and [i.priority for i in items] == [manager_mod.CURRENT, manager_mod.NEXT, None, None]


def test_preparing_counts_what_the_open_reads_and_never_goes_back(tmp_path):
    from ironbark.netcache.store import Island
    m = make_manager(tmp_path / "NetCache", FakeShare(), start=False)
    m._loaded.set()
    item = Item(m.store, key="k", kind="share", source=str(unc("a.mp3")), host="nas", ext="mp3", size=4_000_000)
    m.items["k"] = item
    m._add_host("nas", "share")
    item.visible = 32 * 1024
    item.islands = [Island(4_000_000 - manager_mod.TAIL_BYTES, 4_000_000)]     # the tail came first
    before = m._not_ready(item, unc("a.mp3"), 0.0, 10.0, None)
    assert before.percent == int(100 * (32 * 1024 + manager_mod.TAIL_BYTES) / (64 * 1024 + manager_mod.TAIL_BYTES))
    item.info.update(open_spans=[[0, 40_000], [3_990_000, 4_000_000]], format="MP3", subtype="MPEG_LAYER_III",
                     rate=44100, channels=2, frames=44100 * 200)
    after = m._not_ready(item, unc("a.mp3"), 0.0, 10.0, m._measure(item, unc("a.mp3"), 0.0))
    assert after.percent >= before.percent                       # (more is known to be needed: never shown going back)


# -- keep cached tracks, the cache size ------------------------------------------------------------------
def test_keep_policies_choose_what_goes_first(tmp_path):
    from ironbark.netcache.store import Store
    store = Store(tmp_path)
    now = 100_000.0
    items = []
    for name in ("listed", "unlisted", "yesterday"):
        item = Item(store, key=name, kind="share", source=name, host="nas", ext="bin", size=10)
        item.state = READY
        item.last_used = now - (30 * 3600 if name == "yesterday" else 60)
        items.append(item)
    session = {"listed"}

    def order(keep):
        return [i.key for i in policy.evict_order(items, set(), now, keep, session=session)]

    assert order("none") == ["yesterday", "listed", "unlisted"]           # all past their time
    assert order("session") == ["yesterday", "unlisted", "listed"]        # off the list first
    assert order("day") == ["yesterday", "listed", "unlisted"]
    assert order("limit") == ["yesterday", "listed", "unlisted"]          # least recently used first
    assert [policy.expired(i, "session", now, session) for i in items] == [False, True, True]
    assert [policy.expired(i, "day", now, session) for i in items] == [False, False, True]
    assert not any(policy.expired(i, "limit", now, session) for i in items)


def test_until_playback_finishes_deletes_copies_once_off_the_list_or_stopped(tmp_path, managers):
    share = FakeShare(rate=8_000_000)
    paths = [share.add(f"{k}.bin", bytes(50_000)) for k in range(3)]
    player = FakePlayer(paths)
    m = make_manager(tmp_path / "NetCache", share, player, prefs={"net_keep": "session", "net_predownload": "next2"})
    managers.append(m)
    assert wait_until(lambda: len(m.items) == 3 and all(i.state == READY for i in m.items.values()), 20)
    player.order = paths[:1]                                   # the list replaced: two songs gone from it
    m.settings_changed()
    assert wait_until(lambda: len(m.items) == 1, 10)
    player.playing = False                                     # playback stopped: nothing is kept
    player.order = []
    m.settings_changed()
    assert wait_until(lambda: len(m.items) == 0, 10)


def test_a_smaller_cache_limit_evicts_at_once(tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "GIB", 100_000)
    share = FakeShare(rate=8_000_000)
    paths = [share.add(f"{k}.bin", bytes(60_000)) for k in range(3)]
    player = FakePlayer(paths)
    prefs = {"net_predownload": "next2", "net_cache_gb": 0}
    m = make_manager(tmp_path / "NetCache", share, player, prefs=prefs)
    managers.append(m)
    assert wait_until(lambda: len(m.items) == 3 and all(i.state == READY for i in m.items.values()), 20)
    player.order = []                                          # nothing protected any more
    m.prefs["net_cache_gb"] = 1                                # "1 GB" = 100 kB here
    m.settings_changed()
    assert wait_until(lambda: sum(i.held() for i in m.items.values()) <= 100_000, 10)


# -- hearing network changes ----------------------------------------------------------------------------
def test_a_network_change_asks_a_server_that_stopped_answering_at_once(tmp_path, managers):
    heard = {}
    probes = []

    def watch(on_change):
        heard["call"] = on_change
        return type("W", (), {"close": lambda self: heard.setdefault("closed", True)})()

    m = make_manager(tmp_path / "NetCache", FakeShare(), watch_network=watch, probe=probes)
    host = m._add_host("nas", "share")
    with m._lock:
        m._host_down(host, m._clock())
        host.next_probe = m._clock() + 3600                    # its back-off says: not for an hour
        host.down_since = m._clock() - 120
        host.told = True                                       # ("hasn't answered for over a minute" was said)
    time.sleep(0.3)
    assert "nas" not in probes
    heard["call"]()                                            # Windows: the network changed
    assert wait_until(lambda: "nas" in probes, 5)
    assert wait_until(lambda: host.state == manager_mod.UP, 5)
    assert any("answering again" in notice for notice in m.notices)
    m.shutdown()
    assert heard.get("closed")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' network notifications")
def test_windows_network_notifications_start_and_stop_cleanly():
    calls = []
    watch = netwatch.start(lambda: calls.append(1))
    assert watch is not None
    watch.close()
    watch.close()                                              # (twice is fine)


# -- FLAC seek tables -------------------------------------------------------------------------------------
def test_a_flac_seektable_is_read_from_the_first_bytes():
    streaminfo = bytes([0x00]) + (34).to_bytes(3, "big") + bytes(34)
    points = [(0, 0), (441000, 900_000), (0xFFFFFFFFFFFFFFFF, 0), (882000, 1_800_000)]
    body = b"".join(s.to_bytes(8, "big") + o.to_bytes(8, "big") + (4096).to_bytes(2, "big") for s, o in points)
    seektable = bytes([0x83]) + len(body).to_bytes(3, "big") + body
    head = b"fLaC" + streaminfo + seektable
    assert flacframes.seek_points(head) == [(0, 0), (441000, 900_000), (882000, 1_800_000)]
    assert flacframes.seek_points(head[:-5]) == []             # cut short: not trusted
    assert flacframes.seek_points(b"fLaC" + bytes([0x80]) + (34).to_bytes(3, "big") + bytes(34)) == []
    # Where 15 s sits: no earlier than the 10 s point, no later than the 20 s one.
    assert policy.byte_at(15.0, 8000, 100_000.0, "FLAC", rate=44100,
                          seek_points=[(0, 0), (441000, 900_000), (882000, 1_800_000)]) == 908_000


def test_seconds_in_an_island_and_how_much_of_what_is_needed_is_here():
    assert policy.run_seconds(1_000_000, 500_000, 10.0, S=10_000_000, H=0, D=100.0) == pytest.approx(
        500_000 / (1.2 * 10_000_000 / 100))
    assert policy.run_seconds(10_000_000, 500_000, 10.0, S=10_000_000, H=0, D=100.0) == 90.0   # to the end
    assert policy.run_seconds(1_088_244, 44, 0.0, S=2_000_044, H=44, D=10.0, fmt="WAV", rate=44100,
                              frame_bytes=4) == pytest.approx(1_088_200 // 4 / 44100)
    assert policy.covered([(0, 100), (500, 700)], [(0, 200), (600, 800)]) == pytest.approx(0.5)
    assert policy.covered([], [(0, 10)]) == 0.0


def test_a_wait_while_the_server_is_just_slow_says_so(tmp_path, managers):
    from ironbark.details import network_line
    from ironbark.engine import NetworkView
    clock = Clock()
    m = make_manager(tmp_path / "NetCache", FakeShare(), clock=clock)
    managers.append(m)
    host = m._add_host("nas", "share")
    item = Item(m.store, key="k", kind="share", source=str(unc("a.flac")), host="nas", ext="flac", size=30_000_000)
    item.info.update(format="FLAC", subtype="PCM_16", rate=44100, channels=2, frames=44100 * 300, header_bytes=8000)
    m.items["k"] = item
    for _ in range(5):                                         # 50 kB/s: slower than the song's 100 kB/s
        clock.now += 1
        host.speed.add(clock.now, 50_000, 1.0)
    assert m._slower(item, 45.0) == "the download is slower than the music: building up 45 s"
    with m._lock:
        m._host_down(host, clock.now)
    assert m._slower(item, 45.0) == ""                         # not answering at all: said another way
    slow = NetworkView("waiting", True, None, m._slower(item, 45.0) or "the download is slower than the music: "
                       "building up 45 s", at=134.0)
    assert network_line(slow) == ("Waiting for the network · paused at 2:14 · the download is slower than the "
                                  "music: building up 45 s", "warn")

"""The network playback cache's two probe threads survive an error: the one that opens each
downloading copy (to learn whether playback may start early) and the one that asks a server
that stopped answering whether it's back. Either one ending used to be for the whole session:
no song started before its download finished, or a server stayed "down" for good.

No real network, no sound device."""
from __future__ import annotations

import importlib
import socket
import types

import pytest
from netfakes import FakeClassifier, FakePlayer, Usage, unc, wait_until

manager_mod = importlib.import_module("ironbark.netcache.manager")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def manager(tmp_path):
    made = []

    def make(probe_host=lambda name: True, before_start=None):
        m = manager_mod.Manager(tmp_path / "NetCache", {"net_verify": False}, FakePlayer([unc("01.bin")]),
                                classifier=FakeClassifier(), probe_host=probe_host,
                                disk_usage=lambda p: Usage(10 ** 12, 0, 10 ** 12), watch_network=None)
        if before_start is not None:
            before_start(m)
        m.start()
        assert m.wait_loaded(10)
        made.append(m)
        return m

    yield make
    for m in made:
        m.shutdown(timeout=5)
        assert not [t.name for t in m._threads if t.is_alive()]   # (its own threads end: none was lost)


def _thread(m, name):
    return next(t for t in m._threads if t.name == name)


def test_an_error_while_looking_inside_a_copy_doesnt_end_the_probe_thread(manager):
    first = types.SimpleNamespace(info={}, source=str(unc("01.bin")), dirty=False)
    second = types.SimpleNamespace(info={}, source=str(unc("02.bin")), dirty=False)
    waiting, probed = [first, second], []

    def setup(m):
        m._next_to_probe = lambda: waiting.pop(0) if waiting else None

        def probe(item):
            if item is first:
                raise AttributeError("'SoundFile' object has no attribute 'gate'")
            probed.append(item)
        m._probe = probe

    m = manager(before_start=setup)
    m._probe_wake.set()
    assert wait_until(lambda: probed == [second], 5), "the probe thread stopped after one error"
    assert _thread(m, "netcache-probe").is_alive()
    assert "gate" in first.info["open_error"] and first.dirty   # (not picked again and again)


def test_a_copy_already_in_place_is_left_alone(manager, monkeypatch):
    m = manager()
    closed = []
    finished = types.SimpleNamespace(close=lambda: closed.append(True))   # an ordinary file: no gate
    monkeypatch.setattr(manager_mod.audiofile, "LocalTrack", lambda *args: None)
    monkeypatch.setattr(manager_mod.audiofile, "open", lambda *args, **kwargs: finished)
    item = types.SimpleNamespace(gen=1, source=str(unc("01.bin")), priority=0, info={}, open_ready=False, dirty=False)
    m._probe(item)
    assert closed == [True] and not item.open_ready and item.info == {}


def test_an_error_asking_a_server_doesnt_end_that_thread_and_it_is_asked_again(manager):
    asked = []

    def probe_host(name):
        asked.append(name)
        if len(asked) == 1:
            raise RuntimeError("something unexpected")
        return True

    m = manager(probe_host=probe_host)
    host = manager_mod.Host("nas", "share")
    host.state, host.probing = manager_mod.DOWN, True
    m._probe_hosts.put(host)
    assert wait_until(lambda: asked and not host.probing, 5)
    assert host.state == manager_mod.DOWN and _thread(m, "netcache-probe-hosts").is_alive()
    host.probing = True
    m._probe_hosts.put(host)
    assert wait_until(lambda: host.state == manager_mod.UP, 5), "a server asked once with an error was never asked again"


def test_a_song_back_after_an_eviction_never_takes_the_names_of_an_old_version_still_waiting(tmp_path):
    # Its old version couldn't be deleted (a program had the file open), and the song was evicted
    # meanwhile; downloaded again, the new copy must not share that version's file names, or the
    # next tidy would delete the new copy.
    m = manager_mod.Manager(tmp_path / "NetCache", {"net_verify": False}, FakePlayer([unc("01.bin")]),
                            classifier=FakeClassifier(), probe_host=lambda name: True,
                            disk_usage=lambda p: Usage(10 ** 12, 0, 10 ** 12), watch_network=None)
    path = unc("01.bin")
    kind, key, host = m._describe(path)
    with m._lock:
        old = m._get(key, path, kind, host)
        m._stale.append((old, old.gen, m.store.final_path(old)))
        del m.items[key]                      # evicted
        new = m._get(key, path, kind, host)   # wanted again
    assert new is not old and new.gen > old.gen
    assert m.store.part_path(new) != m.store.part_path(old, old.gen)
    assert m.store.final_name_for(new) != m.store.final_name_for(old)

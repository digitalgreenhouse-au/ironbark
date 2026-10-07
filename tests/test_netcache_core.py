"""The network playback cache's parts that don't need the download manager running:
classification (no network calls), the start rules, the store (crash recovery, a damaged
database, the owner lock) and the checks on a finished download."""
from __future__ import annotations

import os
import re
import socket
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import audiofile, netcache
from ironbark.netcache import policy, sources, verify
from ironbark.netcache import store as store_mod
from ironbark.netcache.manager import Manager
from ironbark.netcache.store import (DOWNLOADING, PARTIAL, QUEUED, READY, STALE, VERIFYING, Item, OwnerLock,
                                     Store)

from netfakes import FakeClassifier, FakeShare


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


# -- 1. classification -------------------------------------------------------------------------------
@pytest.fixture
def windows(monkeypatch):
    """Windows' drive calls, replaced and recorded."""
    calls = []
    types = {"C:\\": 3, "Z:\\": 4, "S:\\": 3}

    def drive_type(root):
        calls.append(("GetDriveTypeW", root))
        return types.get(root.upper(), 3)

    def connection(letter):
        calls.append(("WNetGetConnectionW", letter))
        return {"Z:": "\\\\192.168.80.173\\Shared"}.get(letter)

    def dos_device(letter):
        calls.append(("QueryDosDeviceW", letter))
        return {"S:": "\\\\192.168.80.173\\Shared\\music"}.get(letter)

    def stat(path):
        calls.append(("stat", path))
        return SimpleNamespace(st_file_attributes=0x00400020 if "online" in path else 0x20)

    monkeypatch.setattr(sources, "_drive_type", drive_type)
    monkeypatch.setattr(sources, "_connection", connection)
    monkeypatch.setattr(sources, "_dos_device", dos_device)
    monkeypatch.setattr(sources, "_logical_drives", lambda: ["C:", "S:", "Z:"])
    monkeypatch.setattr(sources, "_stat", stat)
    real_stat = os.stat

    def guarded_stat(path, *args, **kwargs):
        text = os.fspath(path) if not isinstance(path, int) else ""
        assert not text.replace("/", "\\").startswith("\\\\"), f"stat of a server path: {text}"
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", guarded_stat)
    monkeypatch.setenv("OneDrive", r"C:\Users\Test\OneDrive")
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    return calls


def test_classify_by_spelling_and_tables_without_network_calls(windows):
    c = sources.Classifier()
    K = sources.Kind
    table = [
        ("\\\\?\\UNC\\server\\share\\a.flac", K.SHARE), ("\\\\?\\C:\\Music\\a.flac", K.LOCAL),
        ("\\\\.\\C:", K.LOCAL), ("\\\\server\\share\\a.flac", K.SHARE), ("//server/share/a.flac", K.SHARE),
        ("Z:\\Music\\a.flac", K.SHARE), ("C:\\Music\\a.flac", K.LOCAL),
        ("C:\\Users\\Test\\OneDrive\\Music\\online.flac", K.CLOUD),
        ("C:\\Users\\Test\\OneDrive\\Music\\here.flac", K.LOCAL),
        ("C:\\Other\\online.flac", K.LOCAL),
    ]
    for text, kind in table:
        assert c.classify(Path(text) if not text.startswith("\\\\.\\") else text) is kind, text
    assert c.classify(audiofile.UrlPath.parse("https://example.com/a.flac")) is K.URL
    # Nothing about a server path was ever asked of Windows; attributes only under OneDrive.
    assert all(not (name == "GetDriveTypeW" and arg.startswith("\\\\")) for name, arg in windows)
    assert [arg for name, arg in windows if name == "stat"] == [
        "C:\\Users\\Test\\OneDrive\\Music\\online.flac", "C:\\Users\\Test\\OneDrive\\Music\\here.flac"]
    assert not any(name in ("WNetGetConnectionW", "QueryDosDeviceW") for name, _ in windows)  # (refresh only)
    source = Path(sources.__file__).read_text(encoding="utf-8")
    assert "PathIsNetworkPath" not in source.replace("Never `PathIsNetworkPathW`", "")


def test_mapped_and_subst_letters_name_the_same_copy_as_the_server_path(windows):
    c = sources.Classifier()
    c.refresh(force=True)
    assert c.classify(Path("S:\\a.flac")) is sources.Kind.SHARE      # a subst onto the share
    nas = c.source_key(Path("\\\\192.168.80.173\\Shared\\x\\a.flac"))
    assert c.source_key(Path("Z:\\x\\a.flac")) == nas
    assert c.source_key(Path("S:\\a.flac")) == c.source_key(Path("\\\\192.168.80.173\\Shared\\music\\a.flac"))
    assert c.host_of(Path("Z:\\x\\a.flac")) == "192.168.80.173"
    assert c.host_of(Path("C:\\Users\\Test\\OneDrive\\online.flac")) == "cloud"
    # A cue track of a file is the same item as the file.
    cue = audiofile.CuePath("\\\\nas\\s\\image.flac", cue=audiofile.CueRange("", 3, 10, 20))
    assert c.source_key(cue) == c.source_key(Path("\\\\nas\\s\\image.flac"))


# -- 6. the start rules -----------------------------------------------------------------------------------
def test_intelligent_rule_matches_the_worked_examples():
    MB = 1_000_000
    # 1. NAS 16/44.1 FLAC, 30 MB, 5:00, 4 MB/s, steady.
    rho = policy.rho(30 * MB, int(0.1 * MB), 300)
    m = 4 * MB / rho
    q = policy.quality(m, 1.0, 0)
    assert q == policy.EXCELLENT and policy.need_seconds("intelligent", 300, 30, m, 1.0, q, None) == 5
    # 4. Internet 0.5 MB/s, 24/192 FLAC, 250 MB, 5:00.
    rho = policy.rho(250 * MB, 0, 300)
    m = 0.5 * MB / rho
    q = policy.quality(m, 0.0, 0)
    assert q == policy.POOR
    assert policy.need_seconds("intelligent", 300, 30, m, 0.0, q, None) == pytest.approx(155)
    # 5. The plan's example: play 4 MB/min, download 5 MB/min -> poor -> the 30 s buffer.
    rho = 1.2 * 4 * MB / 60
    for per_minute, want in ((5, 30), (30, 5)):
        m = per_minute * MB / 60 / rho
        q = policy.quality(m, 0.0, 0)
        assert policy.need_seconds("intelligent", 240, 30, m, 0.0, q, None) == pytest.approx(want)


def test_other_modes_and_edges():
    assert policy.need_seconds("fast", 200, 30, 10, 0, policy.EXCELLENT, None) == 30
    assert policy.need_seconds("fast", 12, 30, 10, 0, policy.EXCELLENT, None) == 12
    assert policy.need_seconds("complete", 200, 30, 10, 0, policy.EXCELLENT, None) is None
    assert policy.need_seconds("intelligent", 200, 30, None, 0, policy.POOR, None) == 30   # speed unknown
    assert policy.need_seconds("intelligent", 3, 30, 50, 0, policy.EXCELLENT, None) == 3    # L below the floor
    assert policy.need_seconds("intelligent", 200, 30, 2, 0, policy.GOOD, None) == 10
    assert policy.need_seconds("intelligent", 200, 30, 1, 20, policy.UNSTABLE, 60) is None    # finishes soon
    assert policy.need_seconds("intelligent", 200, 30, 1, 20, policy.UNSTABLE, 500) == 120
    assert policy.quality(10, 16, 0) == policy.UNSTABLE and policy.quality(10, 0, 3) == policy.UNSTABLE
    assert policy.quality(10, 0, 0, stalls2=2) == policy.UNSTABLE and policy.quality(None, 0, 0) == policy.POOR
    assert [policy.resume_need(5, k, 100) for k in range(5)] == [5, 10, 20, 40, 80]
    assert policy.resume_need(5, 6, 100) == 100
    assert not policy.startable(open_ready=False, ready=False, complete=False, A=100, need=5)
    assert policy.startable(open_ready=True, ready=False, complete=False, A=5, need=5)
    assert not policy.startable(open_ready=True, ready=False, complete=False, A=50, need=None)
    assert policy.startable(open_ready=True, ready=False, complete=False, A=3, need=None, forced=True)
    assert not policy.startable(open_ready=True, ready=False, complete=False, A=2.9, need=50, forced=True)
    assert policy.startable(open_ready=False, ready=True, complete=False, A=0, need=None)


def test_available_seconds_and_progress():
    # WAV: exact from the header and the frame size.
    assert policy.available_seconds(44 + 4 * 44100 * 10, 4.0, S=44 + 4 * 44100 * 60, H=44, D=60, fmt="WAV",
                                    rate=44100, frame_bytes=4) == pytest.approx(6.0)
    # FLAC: the scanner's last complete frame.
    assert policy.available_seconds(1000, 1.0, S=10 ** 6, H=0, D=60, fmt="FLAC", rate=44100,
                                    flac_end_sample=44100 * 3) == pytest.approx(2.0)
    assert policy.available_seconds(10 ** 6, 10.0, S=10 ** 6, H=0, D=60) == 50   # complete
    needed = policy.needed_bytes(0.0, 5, S=30_000_000, H=100_000, D=300, open_end=200_000, reads_tail=False)
    previous = -1
    for prefix in range(0, 30_000_000, 250_000):
        pct = policy.percent(prefix, needed)
        assert previous <= pct <= 99
        previous = pct
    assert policy.percent(10, 100, complete_mode=True, S=1000) == 1


def test_quiet_point_finds_the_sample_nearest_zero():
    rate = 1000
    for dtype in (np.int32, np.float32):
        block = np.full((200, 2), 1000 if dtype is np.int32 else 0.5, dtype=dtype)
        block[190, 0] = 0
        block[190, 1] = 3 if dtype is np.int32 else 0.001
        block[100, :] = 0     # quieter, but outside the last 50 ms
        assert policy.quiet_point(block, rate) == 190


def test_speed_is_unknown_at_first_then_the_safer_of_two_measures():
    speed = policy.Speed()
    speed.add(100.0, 100_000, 0.2)
    assert speed.value() is None
    for k in range(40):                  # (a 10 s moving average: settled after 40 s)
        speed.add(101.0 + k, 1_000_000, 1.0)
    assert speed.value() == pytest.approx(1_000_000, rel=0.05)
    speed.add(142.0, 100_000, 1.0)       # one slow second pulls the 20th percentile down
    assert speed.value() <= 1_000_000
    speed.asking(200.0)
    assert speed.gap15(203.0) == pytest.approx(3.0)


# -- 8. the store ---------------------------------------------------------------------------------------------
def _item(store, name, **saved):
    key = "smb:\\\\nas\\s\\" + name
    return Item(store, key=key, kind="share", source="\\\\nas\\s\\" + name, host="nas", ext="flac", **saved)


def test_the_database_never_claims_more_than_is_on_disk(tmp_path):
    made = Manager(tmp_path / "NetCache", {}, classifier=FakeClassifier())
    made.store.open()
    item = _item(made.store, "a.flac", size=5000, state=DOWNLOADING)
    made.items[item.key] = item
    item.visible, item.durable = 3000, 1200      # flushed, but only 1200 bytes fsynced
    made._checkpoint(force=True)
    made.store.close()
    again = Store(tmp_path / "NetCache").open()
    assert [(i.prefix, i.state) for i in again] == [(1200, DOWNLOADING)]


def test_start_up_tidy_after_a_crash(tmp_path):
    store = Store(tmp_path / "NetCache")
    store.open()
    partial = _item(store, "partial.flac", size=1000, state=DOWNLOADING, prefix=100)
    store.part_path(partial).write_bytes(b"x" * 300)            # more than was made durable
    renamed = _item(store, "renamed.flac", size=50, state=VERIFYING, prefix=50, verified_by="size+identity")
    renamed.final_name = store.final_name_for(renamed)
    store.final_path(renamed).write_bytes(b"y" * 50)            # renamed, but the row wasn't updated
    lost = _item(store, "lost.flac", size=10, state=READY, prefix=10)
    lost.final_name = store.final_name_for(lost)                 # its file is gone
    nothing = _item(store, "nothing.flac", size=10, state=DOWNLOADING, prefix=10)   # no .part at all
    stale = _item(store, "stale.flac", size=10, state=STALE, prefix=10)
    store.part_path(stale).write_bytes(b"z" * 10)
    store.save([partial, renamed, lost, nothing, stale])
    (store.partial / "0123456789abcdef.g1.part").write_bytes(b"orphan")
    (store.tracks / "Old song [0123456789abcdef].flac").write_bytes(b"orphan")
    (store.tracks / "half.tmp").write_bytes(b"tmp")
    store.close()

    store = Store(tmp_path / "NetCache")
    kept, done = store.tidy(store.open())
    by_name = {Path(i.source).name: i for i in kept}
    assert set(by_name) == {"partial.flac", "renamed.flac", "nothing.flac"}
    assert by_name["partial.flac"].state == PARTIAL and store.part_path(by_name["partial.flac"]).stat().st_size == 100
    assert by_name["renamed.flac"].state == READY
    assert by_name["nothing.flac"].state == QUEUED and by_name["nothing.flac"].prefix == 0
    assert sorted(p.name for p in store.partial.iterdir()) == [store.part_path(by_name["partial.flac"]).name]
    assert [p.name for p in store.tracks.iterdir()] == [by_name["renamed.flac"].final_name]
    assert done["promoted"] == 1 and done["truncated"] == 1 and done["stale"] == 1 and done["dropped"] == 1
    store.close()
    reloaded = {Path(i.source).name: i.state for i in Store(tmp_path / "NetCache").open()}
    assert reloaded == {"partial.flac": PARTIAL, "renamed.flac": READY, "nothing.flac": QUEUED}


def test_a_damaged_database_is_set_aside_and_the_cache_emptied(tmp_path):
    folder = tmp_path / "NetCache"
    store = Store(folder)
    store.make_folders()
    (folder / "netcache.db").write_bytes(b"this is not a database" * 100)
    (store.tracks / "Song [0123456789abcdef].flac").write_bytes(b"old")
    items = store.open()
    assert items == [] and store.set_aside is not None
    assert re.fullmatch(r"netcache\.db\.damaged-\d\d-\d\d-\d{4}", store.set_aside.name)
    assert list(store.tracks.iterdir()) == []
    item = _item(store, "a.flac", size=1)
    store.save([item])
    store.close()
    assert len(Store(folder).open()) == 1


def test_busy_is_not_damage(tmp_path):
    error = sqlite3.OperationalError("database is locked")
    assert not store_mod.is_damage(error)


def test_one_owner_at_a_time(tmp_path):
    first = OwnerLock(tmp_path / "owner.lock")
    second = OwnerLock(tmp_path / "owner.lock")
    assert first.acquire()
    assert not second.acquire()
    assert netcache.install({}, None, where=tmp_path) is None     # another copy owns this cache
    first.release()
    assert second.acquire()
    second.release()


def test_install_and_uninstall_put_audiofile_back_as_it_was(tmp_path):
    class Player:
        preparer = None

        def set_preparer(self, p):
            self.preparer = p

    player = Player()
    made = netcache.install({}, player, where=tmp_path / "NetCache", classifier=FakeClassifier(),
                            fetcher=FakeShare().fetcher)
    try:
        assert made is not None and netcache.manager() is made and player.preparer is made
        assert audiofile._resolver == made.resolve
        assert made.wait_loaded(10)
    finally:
        netcache.uninstall()
    assert audiofile._resolver is None and audiofile._info_hook is None and player.preparer is netcache.DIRECT
    assert netcache.manager() is None
    assert not any(t.name.startswith("netcache-") and t.is_alive() for t in threading.enumerate())


def test_network_playback_settings_fall_back_when_wrong(tmp_path, monkeypatch):
    import json
    from ironbark import settings
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps(
        {"net_mode": "turbo", "net_buffer": True, "net_cache_gb": 25, "net_verify": "yes",
         "net_predownload": "next", "net_keep": "day"}), encoding="utf-8")
    prefs = settings.load()
    assert (prefs["net_mode"], prefs["net_buffer"], prefs["net_cache_gb"]) == ("intelligent", 30, 25)
    assert prefs["net_verify"] is True and prefs["net_keep"] == "day" and prefs["net_predownload"] == "next"
    assert Manager(tmp_path / "c", {}, classifier=FakeClassifier())._pref("net_buffer") == 30


def test_the_direct_preparer_is_todays_behaviour(tmp_path):
    path = tmp_path / "a.flac"
    local = netcache.DIRECT.prepare(path, role="play")
    assert isinstance(local, audiofile.LocalTrack) and local.source == path and not local.remote
    assert local.ready_path() == path and local.copy_state() == ""


# -- 9. checking a finished download ------------------------------------------------------------------------------
def _flac(path: Path, seconds: float = 3.0) -> Path:
    rng = np.random.default_rng(4)
    t = np.arange(int(44100 * seconds)) / 44100
    x = 0.3 * np.sin(2 * np.pi * 440 * t)[:, None] * np.ones((1, 2)) + 0.01 * rng.standard_normal((len(t), 2))
    sf.write(path, np.clip(np.rint(x * 32767), -32768, 32767).astype(np.int16), 44100, subtype="PCM_16")
    return path


def _downloaded(tmp_path, data: bytes, ext="flac") -> tuple[Item, Path]:
    store = Store(tmp_path / "NetCache")
    store.make_folders()
    item = Item(store, key="smb:x", kind="share", source=str(tmp_path / f"a.{ext}"), host="h", ext=ext,
                size=len(data), state="verifying")
    part = store.part_path(item)
    part.write_bytes(data)
    item.visible = item.durable = len(data)
    item.transfer_ok = True
    return item, part


def test_a_good_flac_copy_passes_every_check(tmp_path):
    data = _flac(tmp_path / "src.flac").read_bytes()
    assert verify.flac_md5(data)
    item, part = _downloaded(tmp_path, data)
    result = verify.check(item, item.gen, part)
    assert result.ok, result.problem
    assert result.verified_by == "size+identity+readable+decode+flac-md5"
    assert not item.leased()


def test_a_flipped_byte_fails_the_check(tmp_path):
    data = bytearray(_flac(tmp_path / "src.flac").read_bytes())
    data[len(data) // 2] ^= 0x01
    item, part = _downloaded(tmp_path, bytes(data))
    result = verify.check(item, item.gen, part)
    assert not result.ok and result.problem


def test_a_short_or_unfinished_copy_fails_before_decoding(tmp_path):
    data = _flac(tmp_path / "src.flac").read_bytes()
    item, part = _downloaded(tmp_path, data)
    item.transfer_ok = False
    assert "didn't finish" in verify.check(item, item.gen, part).problem
    item.transfer_ok = True
    part.write_bytes(data[:-10])
    assert "bytes, not" in verify.check(item, item.gen, part).problem


def test_checking_waits_while_the_players_buffer_is_low(tmp_path):
    data = _flac(tmp_path / "src.flac", seconds=12).read_bytes()
    item, part = _downloaded(tmp_path, data)
    started = time.monotonic()
    asked = []

    def low():
        asked.append(1)
        return time.monotonic() - started < 0.3

    result = verify.check(item, item.gen, part, should_yield=low)
    assert result.ok and time.monotonic() - started >= 0.3 and len(asked) > 2

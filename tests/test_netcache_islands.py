"""Islands: bytes of a network song fetched ahead of the unbroken run from its start — where a
reader is waiting (a seek ahead, the probe reading a file's end), the last 256 KiB of a format
whose open reads its end, and the song playing from where it plays. Their files, the database's
claims and the start-up tidy; the reader across files while islands are joined; and the
manager fetching them (fake shares only: no real network)."""
from __future__ import annotations

import importlib
import socket
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from ironbark import audiofile
from ironbark.netcache.growing import GrowingFile
from ironbark.netcache.store import DOWNLOADING, PARTIAL, READY, Island, Item, Store

from netfakes import FakePlayer, FakeShare, make_manager, unc, wait_until

manager_mod = importlib.import_module("ironbark.netcache.manager")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def small(monkeypatch):
    """Small chunks, islands and jumps, so files of a few hundred KB show the behaviour."""
    monkeypatch.setattr(manager_mod, "CHUNK_MIN", 8 * 1024)
    monkeypatch.setattr(manager_mod, "CHUNK_MAX", 16 * 1024)
    monkeypatch.setattr(manager_mod, "CHUNK_UNKNOWN", 16 * 1024)
    monkeypatch.setattr(manager_mod, "SCHEDULER_TICK_S", 0.05)
    monkeypatch.setattr(manager_mod, "FSYNC_BYTES", 32 * 1024)
    monkeypatch.setattr(manager_mod, "ISLAND_ALIGN", 4 * 1024)
    monkeypatch.setattr(manager_mod, "ISLAND_LEAD", 4 * 1024)
    monkeypatch.setattr(manager_mod, "JUMP_SLACK", 32 * 1024)
    monkeypatch.setattr(manager_mod, "TAIL_BYTES", 16 * 1024)


@pytest.fixture
def managers():
    made = []
    yield made
    for m in made:
        m.shutdown()


def _item(store: Store, name: str, data_size: int, **saved) -> Item:
    key = "smb:\\\\nas\\s\\" + name
    return Item(store, key=key, kind="share", source="\\\\nas\\s\\" + name, host="nas", ext=name.rsplit(".", 1)[-1],
                size=data_size, **saved)


def _gate(patience: float = 0.3):
    gate = audiofile.Gate(patience=patience)
    gate.start_call()
    return gate


def _read(item: Item, at: int, n: int, patience: float = 0.3) -> tuple[bytes, str]:
    reader = GrowingFile(item, item.gen, _gate(patience))
    try:
        reader.seek(at)
        data = reader.read(n)
        return data, reader.gate.gave_up
    finally:
        reader.close()


def _seg_files(store: Store) -> list[str]:
    return sorted(p.name for p in store.partial.glob("*.seg-*"))


# -- the store: reading across files, joining, the database and the tidy ---------------------------------
def test_a_reader_reads_from_the_part_and_from_islands_and_waits_for_the_gap_between(tmp_path):
    data = bytes(np.random.default_rng(1).integers(0, 256, 4000, dtype=np.uint8))
    store = Store(tmp_path / "NetCache")
    store.make_folders()
    item = _item(store, "a.flac", len(data), state=DOWNLOADING)
    store.part_path(item).write_bytes(data[:1000])
    item.visible = 1000
    store.island_path(item, 2000).write_bytes(data[2000:3000])
    item.islands = [Island(2000, 3000)]
    assert item.has_range(item.gen, 2100, 2900) and item.has_range(item.gen, 0, 1000)
    assert not item.has_range(item.gen, 900, 2100)
    assert _read(item, 2100, 800) == (data[2100:2900], "")      # inside the island
    assert _read(item, 500, 400) == (data[500:900], "")          # inside the part
    got, why = _read(item, 900, 300)                              # across the gap: waits, then gives up
    assert got == b"" and why == "patience"
    assert item.locate(item.gen, 2500) == (2000, 3000) and item.locate(item.gen, 1500) is None
    assert item.held() == 2000 and item.spans() == [(0, 1000), (2000, 3000)]


def test_joining_an_island_appends_it_to_the_part_and_a_reader_holding_it_reads_on(tmp_path):
    data = bytes(np.random.default_rng(2).integers(0, 256, 6000, dtype=np.uint8))
    store = Store(tmp_path / "NetCache")
    store.make_folders()
    item = _item(store, "b.flac", len(data), state=DOWNLOADING)
    writer = manager_mod._Writer(store, item, item.gen, time.monotonic)
    try:
        assert writer.write(None, 0, data[:1000])
        far = Island(3000)
        item.islands = [far]
        assert writer.write(far, 3000, data[3000:4000])
        near = Island(1500)
        item.islands = [near, far]
        assert writer.write(near, 1500, data[1500:2500])
        # A reader in the far island, its file open, while everything is joined up.
        holding = GrowingFile(item, item.gen, _gate())
        holding.seek(3100)
        assert holding.read(100) == data[3100:3200]
        assert writer.write(near, 2500, data[2500:3000])       # the near island reaches the far one
        writer.join_runs()
        assert [(i.start, i.visible) for i in item.islands] == [(1500, 4000)]
        assert writer.write(None, 1000, data[1000:1500])        # the part reaches the island
        writer.join_runs()
        assert item.islands == [] and item.visible == item.durable == 4000
        assert store.part_path(item).read_bytes() == data[:4000]
        assert _seg_files(store) == []
        assert holding.read(500) == data[3200:3700]             # it reads on (now from the part)
        holding.close()
        assert _read(item, 0, 4000) == (data[:4000], "")
    finally:
        writer.close()


def test_the_database_claims_only_fsynced_island_bytes_and_the_tidy_cuts_back(tmp_path):
    from ironbark.netcache.manager import Manager
    from netfakes import FakeClassifier
    made = Manager(tmp_path / "NetCache", {}, classifier=FakeClassifier(), watch_network=None)
    store = made.store
    store.open()
    item = _item(store, "c.flac", 100_000, state=DOWNLOADING)
    made.items[item.key] = item
    store.part_path(item).write_bytes(b"p" * 500)
    item.visible = item.durable = 500
    store.island_path(item, 40_000).write_bytes(b"i" * 3000)    # 3000 written, 1000 fsynced
    item.islands = [Island(40_000, 43_000, 41_000)]
    store.island_path(item, 60_000).write_bytes(b"j" * 10)      # its file goes missing below
    item.islands.append(Island(60_000, 60_010, 60_010))
    made._checkpoint(force=True)
    store.island_path(item, 60_000).unlink()
    (store.partial / f"{item.id}.g1.seg-90000").write_bytes(b"orphan")
    store.close()

    store = Store(tmp_path / "NetCache")
    kept, done = store.tidy(store.open())
    (again,) = kept
    assert [(i.start, i.visible, i.durable) for i in again.islands] == [(40_000, 41_000, 41_000)]
    assert store.island_path(again, 40_000).stat().st_size == 1000
    assert again.state == PARTIAL and again.visible == 500
    assert _seg_files(store) == [store.island_path(again, 40_000).name]
    assert done["truncated"] >= 1 and done["orphans"] == 1
    store.close()
    (reloaded,) = Store(tmp_path / "NetCache").open()
    assert reloaded.island_claim() == [[40_000, 41_000]]


def test_the_tidy_drops_an_island_the_part_already_reached(tmp_path):
    store = Store(tmp_path / "NetCache")
    store.open()
    item = _item(store, "d.flac", 10_000, state=DOWNLOADING, prefix=5000, islands="[[4000, 6000], [8000, 9000]]")
    store.part_path(item).write_bytes(b"p" * 5000)
    store.island_path(item, 4000).write_bytes(b"x" * 2000)
    store.island_path(item, 8000).write_bytes(b"y" * 1000)
    store.save([item])
    store.close()
    store = Store(tmp_path / "NetCache")
    (kept,), _ = store.tidy(store.open())
    assert [(i.start, i.visible) for i in kept.islands] == [(8000, 9000)]
    assert not store.island_path(kept, 4000).exists()


# -- the manager: where it fetches next ----------------------------------------------------------------------
def _wav_bytes(tmp_path: Path, seconds: float, rate: int = 22050, name: str = "src.wav") -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    x = 0.3 * np.sin(2 * np.pi * 440 * t) + 0.05 * np.sin(2 * np.pi * 37 * t)
    sf.write(tmp_path / name, np.rint(x * 32767).astype(np.int16), rate, subtype="PCM_16")
    return (tmp_path / name).read_bytes()


def _first_index(share: FakeShare, name: str, test) -> int | None:
    reads = [(offset, n) for (who, op, offset, n) in share.log if who == name and op == "read"]
    return next((i for i, (offset, n) in enumerate(reads) if test(offset, n)), None), reads


def test_a_reader_waiting_past_the_download_gets_an_island_there(tmp_path, managers):
    data = bytes(np.random.default_rng(3).integers(0, 256, 600_000, dtype=np.uint8))
    share = FakeShare(rate=150_000)
    path = share.add("far.bin", data)
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]))
    managers.append(m)
    assert wait_until(lambda: m.items and next(iter(m.items.values())).visible > 0)
    item = next(iter(m.items.values()))
    started = time.monotonic()
    got, why = _read(item, 500_000, 20_000, patience=5.0)
    took = time.monotonic() - started
    assert why == "" and got == data[500_000:520_000]
    assert item.visible < 400_000 and took < 3.0       # it didn't wait for the run from the start
    index, reads = _first_index(share, "far.bin", lambda offset, n: offset >= 400_000)
    assert index is not None and max(offset for offset, _ in reads[:index]) < 400_000
    assert wait_until(lambda: item.state == READY, 30)
    assert m.store.final_path(item).read_bytes() == data   # joined back in order, byte for byte
    assert _seg_files(m.store) == []


def test_a_format_whose_open_reads_its_end_fetches_its_last_bytes_first(tmp_path, managers):
    data = bytes(np.random.default_rng(4).integers(0, 256, 200_000, dtype=np.uint8))
    share = FakeShare(rate=2_000_000)
    path = share.add("song.mp3", data)
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]))
    managers.append(m)
    assert wait_until(lambda: any(op == "read" for (_, op, _, _) in share.log))
    first = next(offset for (_, op, offset, _) in share.log if op == "read")
    align = manager_mod.ISLAND_ALIGN
    assert first == (len(data) - manager_mod.TAIL_BYTES) // align * align
    item = next(iter(m.items.values()))
    assert wait_until(lambda: item.state == READY, 30)
    assert m.store.final_path(item).read_bytes() == data


def test_the_song_playing_is_fetched_from_where_it_starts_then_the_stretch_behind(tmp_path, managers):
    data = _wav_bytes(tmp_path, 30.0)                   # 22.05 kHz mono 16-bit: 44,100 bytes a second
    share = FakeShare(rate=220_000)                     # five seconds of music a second: excellent
    path = share.add("long.wav", data)
    m = make_manager(tmp_path / "NetCache", share, FakePlayer([path]))
    managers.append(m)
    assert isinstance(m.prepare(path, role="play", at=20.0), manager_mod.NotReady)
    posted = threading.Event()
    m.notify_when_startable(path, 20.0, posted.set)
    assert posted.wait(20)
    local = m.prepare(path, role="play", at=20.0)
    assert isinstance(local, audiofile.LocalTrack)
    item = local.item
    at20 = item.info["header_bytes"] + 20 * 22050 * 2
    assert item.visible < at20                           # it can start long before the run from the start gets there
    index, reads = _first_index(share, "long.wav", lambda offset, n: offset >= at20 - 16_384)
    assert index is not None and max(offset for offset, _ in reads[:index]) < at20 // 2
    with audiofile.open(local, patience=10) as f:      # it plays bit for bit from 20 s
        f.seek(20 * 22050)
        got = f.read(22050, dtype="int16")
    with sf.SoundFile(tmp_path / "src.wav") as f:
        f.seek(20 * 22050)
        want = f.read(22050, dtype="int16")
    assert np.array_equal(got, want)
    progress = m.progress(path)
    spans = progress.spans                               # the seek bar's band: the start, and from 20 s on
    assert spans[0][0] == 0.0 and (len(spans) == 1 or spans[1][0] == pytest.approx(20 / 30, abs=0.01))
    assert wait_until(lambda: item.state == READY, 30)  # then the stretch behind is filled in
    assert m.store.final_path(item).read_bytes() == data and _seg_files(m.store) == []

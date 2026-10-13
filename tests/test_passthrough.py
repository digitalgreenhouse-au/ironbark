"""0.49.0: encoded streams (DTS music CDs, HDCD) are never turned down by the clipping guard or
levelled — any change of level breaks them (passthrough.py)."""

import math
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from test_formats import hdcd_packet_a, noise16, with_hdcd

from ironbark import levelling, library_check, passthrough
from ironbark.analysis import Analysis, guard_trim, true_peak
from ironbark.engine import OutputInfo, Player
from ironbark.formats import DtsScanner, scan
from ironbark.levelling import Levelling
from ironbark.library import Track
from ironbark.statustext import output_line
from ironbark.tools.details import describe_flags

RATE = 44100
FRAME = 512   # stereo frames from one DTS sync word to the next (any fixed frame length behaves the same)


def db(value):
    return 10 ** (value / 20)


def dts_stream(seconds=6, packing="14-bit", swapped=False, seed=3):
    """A stand-in for a DTS stream as a stereo 16-bit file's samples: random data (a coded stream
    looks like noise) — 14-bit words sign-extended to 16 bits as on a DTS music CD, or full 16-bit
    words — with a frame sync word every FRAME stereo frames. `swapped`: the other byte order."""
    rng = np.random.default_rng(seed)
    words = 2 * int(seconds * RATE)
    if packing == "14-bit":
        data = rng.integers(-8192, 8192, size=words)
        sync = [0x1FFF, 0xE800, 0x07F3]                 # bytes FF 1F 00 E8 F3 07
    else:
        data = rng.integers(-32768, 32768, size=words)
        sync = [0x7FFE, 0x8001]                         # bytes FE 7F 01 80
    data = data.astype(np.uint16)
    for start in range(0, words - len(sync), 2 * FRAME):
        data[start:start + len(sync)] = sync
    if swapped:
        data = (data >> 8) | ((data & 0xFF) << 8)
    return data.view(np.int16).reshape(-1, 2)


def loud_hdcd(seconds=6):
    """A loud HDCD master: a tone at a quarter of the sample rate whose samples sit just under
    full scale while the wave between them goes about 3 dB over (inter-sample peaks), carrying
    HDCD packets in its lowest bits."""
    n = np.arange(int(seconds * RATE))
    wave = (32000 / math.sqrt(0.5) * np.sin(np.pi / 2 * n + np.pi / 4)).clip(-32767, 32767)
    return with_hdcd(np.column_stack([wave, wave]).astype(np.int16), hdcd_packet_a(0x14))


def flac16(path, data):
    sf.write(path, data, RATE, format="FLAC", subtype="PCM_16")
    return path


def as_blocks(data, size=RATE):
    """16-bit samples as the analysis pass's left-justified int32 blocks."""
    wide = data.astype(np.int32) << 16
    return [wide[i:i + size] for i in range(0, len(wide), size)]


# -- the problem: what the guard and levelling would do to these tracks ------------------------------
def test_without_the_exception_the_guard_and_levelling_would_change_these_tracks(tmp_path):
    # Measured as the player does (8x true peak; BS.1770 loudness). A DTS music CD's 14-bit packing
    # keeps it about 12 dB down, so the guard alone leaves it; levelling still turns it down. Full
    # 16-bit DTS and a loud HDCD master go over full scale between samples: the guard turns them down.
    cd = flac16(tmp_path / "dts-cd.flac", dts_stream())
    raw = flac16(tmp_path / "dts16.flac", dts_stream(packing="16-bit"))
    hdcd = flac16(tmp_path / "hdcd.flac", loud_hdcd())
    assert guard_trim(true_peak(cd)) == 1.0
    assert guard_trim(true_peak(raw)) < db(-3.0)
    assert guard_trim(true_peak(hdcd)) < db(-2.0)
    for path in (cd, raw, hdcd):
        peak, lufs = levelling.measure(path)
        assert levelling.level_for("track", None, lufs, None, peak).db < -5.0


# -- finding DTS ------------------------------------------------------------------------------------
@pytest.mark.parametrize("packing,swapped", [("14-bit", False), ("14-bit", True), ("16-bit", False), ("16-bit", True)])
def test_a_dts_stream_is_found_in_either_packing_and_byte_order(packing, swapped):
    scanner = DtsScanner()
    for block in as_blocks(dts_stream(2, packing, swapped)):
        scanner.add(block)
    result = scanner.result()
    assert result.detected and result.packing == packing and result.frame_words == 2 * FRAME


def test_dts_syncs_split_across_block_edges_are_found_once():
    data = dts_stream(2)
    scanner = DtsScanner()
    for block in as_blocks(data, size=FRAME // 2 + 1):   # (block edges fall inside sync words)
        scanner.add(block)
        if scanner.detected:
            break
    assert scanner.detected
    found = scanner.hits[0]
    assert found == sorted(set(found)) and set(np.diff(found)) == {2 * FRAME}


def test_music_and_stray_sync_words_are_not_dts():
    scanner = DtsScanner()
    for block in as_blocks(noise16(5)):
        scanner.add(block)
    assert not scanner.result().detected
    # The sync word now and then, at no steady distance (music can hold one by chance): not a stream.
    data = noise16(5).astype(np.uint16).reshape(-1)
    rng = np.random.default_rng(9)
    for start in np.sort(rng.choice(len(data) - 2, size=20, replace=False)):
        data[start:start + 2] = [0x7FFE, 0x8001]
    scanner = DtsScanner()
    for block in as_blocks(data.view(np.int16).reshape(-1, 2)):
        scanner.add(block)
    assert not scanner.result().detected
    # Opposite full-scale DC on the two channels repeats the 16-bit sync every two words: far closer
    # together than a DTS frame can be.
    dc = np.tile(np.array([[0x7FFE, 0x8001 - 0x10000]], dtype=np.int16), (RATE, 1))
    scanner = DtsScanner()
    for block in as_blocks(dc):
        scanner.add(block)
    assert not scanner.result().detected


def test_the_check_reports_dts_and_keeps_it_through_the_library(tmp_path):
    flags = scan(flac16(tmp_path / "dts-cd.flac", dts_stream()))
    assert flags.dts.detected and flags.dts.packing == "14-bit" and flags.any
    rows = dict(describe_flags(flags))
    assert rows["DTS"].startswith("⚠ A DTS bitstream (a DTS music CD)") and "leave it alone" in rows["DTS"]
    stored = library_check.from_json(library_check.to_json(Analysis(md5_ok=None, dr=None, peak_dbfs=-1.0, flags=flags)))
    assert stored.flags.dts == flags.dts
    # A check stored before DTS was looked for still reads (DTS: not checked).
    old = library_check.to_json(Analysis(md5_ok=None, dr=None, peak_dbfs=-1.0, flags=flags)).replace('"dts":', '"was_dts":')
    assert library_check.from_json(old).flags.dts is None
    assert scan(flac16(tmp_path / "plain.flac", noise16(3))).dts.detected is False


# -- passthrough: what each file is -----------------------------------------------------------------
def test_detect_names_dts_and_hdcd_and_nothing_else(tmp_path):
    assert passthrough.detect(flac16(tmp_path / "dts-cd.flac", dts_stream())) == passthrough.DTS
    assert passthrough.detect(flac16(tmp_path / "dts16.flac", dts_stream(packing="16-bit"))) == passthrough.DTS
    assert passthrough.detect(flac16(tmp_path / "hdcd.flac", loud_hdcd())) == passthrough.HDCD
    assert passthrough.detect(flac16(tmp_path / "plain.flac", noise16(3))) == ""
    hi_res = tmp_path / "24.flac"
    sf.write(hi_res, np.zeros((RATE, 2)), RATE, format="FLAC", subtype="PCM_24")
    assert passthrough.detect(hi_res) == ""                     # (only 16-bit files carry either)
    assert passthrough.detect(tmp_path / "dts-cd.flac", cancel=lambda: True) is None


def test_the_detector_remembers_and_survives_unreadable_files(tmp_path):
    detector = passthrough.Detector()
    path = flac16(tmp_path / "dts-cd.flac", dts_stream())
    assert not detector.known(path) and detector.kind(path) == ""
    assert detector.check(path, lambda: True) is None and not detector.known(path)   # cancelled: not kept
    assert detector.check(path) == passthrough.DTS and detector.kind(path) == passthrough.DTS
    path.unlink()
    assert detector.check(path) == passthrough.DTS                                   # (remembered)
    assert detector.check(tmp_path / "missing.flac") == "" and detector.known(tmp_path / "missing.flac")


# -- the engine plays an encoded stream at its own level --------------------------------------------
def test_the_engine_gives_an_encoded_stream_no_gain_of_its_own():
    player = Player()
    a, b = Track(Path("dts.flac"), "dts"), Track(Path("b.flac"), "b")
    player.set_tracks([a, b])
    player.set_volume(1.0)
    player.set_trim(a.path, db(-5.7))
    player.set_levelling(a.path, Levelling(db(-7.3), "track", "measured"))
    player.set_encoded(a.path, passthrough.DTS)
    player.index = 0
    assert player.trim_for(a.path) == 1.0 and player._gain() == 1.0
    status = player.status()
    assert status.encoded == "DTS" and status.guard == 1.0 and status.levelling is None and status.trim == 1.0
    player.set_encoded(a.path, "")                                   # unmarked: as before
    assert player.trim_for(a.path) == pytest.approx(db(-7.3))
    player.set_trim(b.path, db(-1.0))
    assert player.trim_for(b.path) == pytest.approx(db(-1.0))        # (other files untouched)


EXCLUSIVE = OutputInfo("DAC", 44100, 44100, True, True, 16)
SHARED = OutputInfo("Speakers", 44100, 48000, False)


def test_the_output_line_says_an_encoded_stream_is_left_alone_and_what_it_needs():
    text, tone = output_line(EXCLUSIVE, 1.0, bits=16, encoded="DTS")
    assert tone == "good" and "bit-perfect ✓" in text and text.endswith("DTS stream: clipping guard and levelling kept off")
    text, tone = output_line(EXCLUSIVE, 0.5, bits=16, encoded="DTS")
    assert tone == "warn" and "this DTS stream needs volume at 100%, or the receiver gets noise" in text
    text, tone = output_line(SHARED, 1.0, bits=16, encoded="HDCD")
    assert tone == "warn" and "this HDCD stream needs exclusive mode, or the decoder loses its codes" in text
    text, tone = output_line(EXCLUSIVE, 1.0, bits=16, encoded="HDCD", fading=True)
    assert tone == "warn" and "needs the samples exactly" in text
    assert output_line(EXCLUSIVE, 1.0, bits=16) == output_line(EXCLUSIVE, 1.0, bits=16, encoded="")


# -- the window: the guard and levelling leave encoded streams alone --------------------------------
def fake_window(player, setting="off"):
    return SimpleNamespace(
        player=player, clip_guard=SimpleNamespace(get=lambda: True), _guard_for=None, _peaks={},
        _peak_jobs=queue.Queue(), _peak_wanted=set(), _headroom_db=lambda: 0.0, _encoded=passthrough.Detector(),
        _stored_peak=lambda path: None, _gain_tags={}, _lufs={}, _level_wanted=set(), _level_set_with={},
        _album_lufs={}, _levelling_setting=lambda: setting, _preamp_db=lambda: 0.0, playing_tab=None, tabs=[])


def run_worker(window, until):
    from ironbark.ui import PlayerWindow
    window._measure_for_levelling = PlayerWindow._measure_for_levelling.__get__(window)
    threading.Thread(target=PlayerWindow._measure_peaks, args=(window,), daemon=True).start()
    deadline = time.monotonic() + 60
    while not until() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert until()


def test_the_guard_turns_down_a_loud_master_but_never_an_encoded_stream(tmp_path):
    from ironbark.ui import PlayerWindow
    dts16 = flac16(tmp_path / "dts16.flac", dts_stream(packing="16-bit"))
    hdcd = flac16(tmp_path / "hdcd.flac", loud_hdcd())
    plain = flac16(tmp_path / "plain.flac", (loud_hdcd() & ~1))   # the same master without HDCD's codes
    player = Player()
    player.set_tracks([Track(p, p.stem) for p in (dts16, hdcd, plain)])
    w = fake_window(player)
    w._peak_wanted = {dts16, hdcd, plain}
    for path in (dts16, hdcd, plain):
        w._peak_jobs.put(path)
    run_worker(w, lambda: len(w._peaks) == 3)
    assert w._encoded.kind(dts16) == "DTS" and w._encoded.kind(hdcd) == "HDCD" and w._encoded.kind(plain) == ""
    for index, track in enumerate(player.tracks):
        player.index = index
        PlayerWindow._apply_guard(w, track, index)
    assert player.trim_for(dts16) == 1.0 and player.encoded_for(dts16) == "DTS"
    assert player.trim_for(hdcd) == 1.0 and player.encoded_for(hdcd) == "HDCD"
    assert player.trim_for(plain) == pytest.approx(guard_trim(w._peaks[plain])) and player.trim_for(plain) < db(-2.0)


def test_the_guard_waits_until_it_knows_the_file_is_not_encoded(tmp_path):
    from ironbark.ui import PlayerWindow
    path = flac16(tmp_path / "hdcd.flac", loud_hdcd())
    player = Player()
    player.set_tracks([Track(path, "hdcd")])
    w = fake_window(player)
    w._peaks[path] = 1.4                       # (a true peak known — from the library check, say)
    PlayerWindow._apply_guard(w, player.tracks[0], 0)
    assert player.guard_for(path) == 1.0       # not turned down before the look
    w._encoded.check(path)
    PlayerWindow._apply_guard(w, player.tracks[0], 0)
    assert player.guard_for(path) == 1.0 and player.encoded_for(path) == "HDCD"


def test_levelling_leaves_an_encoded_stream_alone(tmp_path):
    from ironbark.ui import PlayerWindow
    cd = flac16(tmp_path / "dts-cd.flac", dts_stream())
    plain = flac16(tmp_path / "plain.flac", noise16(4))
    player = Player()
    player.set_tracks([Track(cd, "cd"), Track(plain, "plain")])
    w = fake_window(player, setting="track")
    status = SimpleNamespace(index=0)
    PlayerWindow._apply_levelling(w, player.tracks[0], status)     # asks the worker for both
    assert w._level_wanted == {cd, plain} and player.levelling_for(cd) is None
    run_worker(w, lambda: cd in w._lufs and plain in w._lufs)
    PlayerWindow._apply_levelling(w, player.tracks[0], status)
    assert player.levelling_for(cd) is None and player.encoded_for(cd) == "DTS" and player.trim_for(cd) == 1.0
    assert player.levelling_for(plain) is not None and player.levelling_for(plain).db != 0.0

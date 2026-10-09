"""The offline check against a rip log's own checksums (EAC / XLD): parsing the per-track figures
in the layouts real logs use (EAC 0.99 and 1.x, a localised EAC log, an EAC range rip, XLD),
then the comparison on synthetic discs — every track matches, a changed sample shows, a track the
log lacks is "not in the log", silent samples counted or not as the log says. Nothing goes online."""
import gc
import socket
import time
import tkinter as tk
import zlib

import numpy as np
import pytest
import soundfile as sf

from ironbark import network, ripcheck, theme
from ironbark.ripcheck import SECTOR, check_log, checksums, describe_log, find_rip, parse_log

RNG = np.random.default_rng(41)
SECTORS = (20, 23, 21)


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to go online")
    monkeypatch.setattr(network, "_urlopen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def disc_audio():
    """Three tracks of noise with silent stretches in them (so "skip zero" CRCs differ)."""
    tracks = []
    for n in SECTORS:
        t = RNG.integers(-32768, 32768, size=(n * SECTOR, 2)).astype(np.int16)
        t[:300] = 0
        t[1000:1100, 0] = 0
        tracks.append(t)
    return tracks


def crc(track, skip_zero=False):
    data = track.astype("<i2")
    if skip_zero:
        flat = data.reshape(-1)
        return zlib.crc32(flat[flat != 0].tobytes())
    return zlib.crc32(data.tobytes())


def toc_table(sectors=SECTORS, xld=False):
    rows, at = [], 0
    for k, n in enumerate(sectors):
        start = "00:00:00" if xld else "0:00.00"
        rows.append(f"       {k + 1}  | {start} | {start} |    {at:6d}    |   {at + n - 1:6d}   ")
        at += n
    return ("TOC of the extracted CD\n\n     Track |   Start  |  Length  | Start sector | End sector \n"
            "    ---------------------------------------------------------\n" + "\n".join(rows) + "\n")


def eac_log(tracks, sums, null_samples="Yes", version="V1.8 from 15. July 2024", ar_tag="  (AR v2)",
            skip=(), tamper=None):
    """An EAC log laid out as EAC writes one (settings at the left edge, a section per track)."""
    parts = [f"Exact Audio Copy {version}\n\nEAC extraction logfile from 7. October 2026, 9:00\n\n"
             "Read offset correction                      : 6\n"
             "Fill up missing offset samples with silence : Yes\n"
             f"Null samples used in CRC calculations       : {null_samples}\n"
             "Gap handling                                : Appended to previous track\n\n", toc_table(), "\n"]
    for k, t in enumerate(tracks):
        if k + 1 in skip:
            continue
        value = crc(t, skip_zero=null_samples == "No")
        if tamper == k + 1:
            value ^= 1
        ar = sums.ar_v2[k] if "v2" in ar_tag else sums.ar_v1[k]
        parts.append(f"Track {k + 1:2d}\n\n     Filename C:\\Rips\\[DEADBEEF] {k + 1:02d} Track.wav\n\n"
                     f"     Peak level 98.2 %\n     Test CRC {value:08X}\n     Copy CRC {value:08X}\n"
                     f"     Accurately ripped (confidence 12)  [{ar:08X}]{ar_tag}\n     Copy OK\n\n")
    parts.append("All tracks accurately ripped\n\nNo errors occurred\n\nEnd of status report\n")
    return "".join(parts)


def xld_log(tracks, sums):
    parts = ["X Lossless Decoder version 20250302 (157.2)\n\nXLD extraction logfile from 2026-10-07\n\n",
             toc_table(xld=True), "\nAccurateRip Summary (DiscID: 0)\n    Track 01 : OK (v1+v2, confidence 2/2)\n\n"
             "All Tracks\n    Album gain               : -8.41 dB\n    Peak                     : 0.999969\n\n"]
    for k, t in enumerate(tracks):
        parts.append(f"Track {k + 1:02d}\n    Filename : /Volumes/Rips/{k + 1:02d}.flac\n"
                     f"    CRC32 hash (test run)    : {crc(t):08X}\n    CRC32 hash               : {crc(t):08X}\n"
                     f"    CRC32 hash (skip zero)   : {crc(t, True):08X}\n"
                     f"    AccurateRip v1 signature : {sums.ar_v1[k]:08X}\n"
                     f"    AccurateRip v2 signature : {sums.ar_v2[k]:08X}\n"
                     f"        ->Accurately ripped (v1+v2, confidence 2/2)\n\n")
    return "".join(parts)


@pytest.fixture
def disc(tmp_path):
    tracks = disc_audio()
    files = []
    for k, t in enumerate(tracks):
        path = tmp_path / f"{k + 1:02d} Track.flac"
        sf.write(path, t, 44100, format="FLAC", subtype="PCM_16")
        files.append(path)
    rip = find_rip(files[0])
    return tracks, files, checksums(rip)


# -- parsing ---------------------------------------------------------------------------------------
def test_eac_log_figures(disc):
    tracks, _, sums = disc
    log = parse_log(eac_log(tracks, sums))
    assert log.ripper == "EAC" and log.null_samples is True and len(log.tocs) == 1
    assert log.tocs[0].audio_starts == [0, 20, 43]
    first = log.tracks[1]
    assert first.copy_crc == first.test_crc == crc(tracks[0]) and first.ar_v2 == sums.ar_v2[0] and first.ar_v1 is None
    # The file name holds "[DEADBEEF]": never taken for a checksum.
    assert all(t.ar_v2 != 0xDEADBEEF for t in log.tracks.values())


def test_older_eac_names_no_version_and_its_value_is_v1(disc):
    tracks, _, sums = disc
    log = parse_log(eac_log(tracks, sums, version="V0.99 prebeta 4 from 23. January 2008", ar_tag=""))
    assert log.tracks[2].ar_v1 == sums.ar_v1[1] and log.tracks[2].ar_v2 is None


def test_cannot_be_verified_lines_give_the_rips_own_checksum():
    text = ("Exact Audio Copy V1.1 from 23. June 2015\n\nTrack  1\n\n     Copy CRC B988F24B\n"
            "     Cannot be verified as accurate (confidence 15)  [C1F8AAFE], AccurateRip returned [9D26CDE4]  (AR v2)\n"
            "     Copy OK\n\nTrack  2\n\n     Copy CRC BEBC1809\n     Track not present in AccurateRip database\n")
    log = parse_log(text)
    assert log.tracks[1].ar_v2 == 0xC1F8AAFE and log.tracks[1].copy_crc == 0xB988F24B and log.tracks[1].test_crc is None
    assert log.tracks[2].copy_crc == 0xBEBC1809 and log.tracks[2].ar_v2 is None and log.null_samples is None


def test_a_localised_log_by_layout():
    """EAC writes the log in the user's language (here Russian, as UTF-16 the way EAC 1.x saves it):
    tracks and CRCs are found by layout, test before copy."""
    text = ("Exact Audio Copy V1.0 beta 3\n\nОтчёт EAC об извлечении\n\n"
            "При вычислениях CRC использовались нулевые сэмплы : Да\n\n"
            "Трек  1\n\n     Имя файла C:\\Музыка\\01.wav\n\n     CRC теста 1681E02C\n     CRC копии 1681E02D\n"
            "     Точно извлечено (достоверность 3)  [0BDE42E4]  (AR v2)\n\n"
            "Трек  2\n\n     CRC копии 2F2896C4\n")
    from ironbark import cuesheet
    log = parse_log(cuesheet.decode("\ufeff".encode("utf-16-le") + text.encode("utf-16-le")))
    assert log.tracks[1].test_crc == 0x1681E02C and log.tracks[1].copy_crc == 0x1681E02D
    assert log.tracks[1].ar_v2 == 0x0BDE42E4 and log.tracks[2].copy_crc == 0x2F2896C4
    assert log.null_samples is None                  # (said, but not in English)


def test_a_range_rip_has_one_crc_for_the_whole_disc():
    text = ("Exact Audio Copy V0.99 prebeta 5 from 4. May 2009\n\n"
            "Null samples used in CRC calculations       : Yes\n\nRange status and errors\n\nSelected range\n\n"
            "     Filename G:\\Rips\\Disc.wav\n\n     Peak level 97.3 %\n     Range quality 100.0 %\n"
            "     Test CRC 6FF0FD61\n     Copy CRC 6FF0FD61\n     Copy OK\n\nAccurateRip summary\n\n"
            "Track  1  not present in database\nTrack  2  not present in database\n")
    log = parse_log(text)
    assert log.range_copy_crc == log.range_test_crc == 0x6FF0FD61 and not log.tracks and log.has_crcs


def test_xld_figures(disc):
    tracks, _, sums = disc
    log = parse_log(xld_log(tracks, sums))
    assert log.ripper == "XLD" and set(log.tracks) == {1, 2, 3}
    t = log.tracks[3]
    assert (t.copy_crc, t.test_crc, t.skip_zero_crc) == (crc(tracks[2]), crc(tracks[2]), crc(tracks[2], True))
    assert (t.ar_v1, t.ar_v2) == (sums.ar_v1[2], sums.ar_v2[2])


def test_other_logs_are_not_rip_logs():
    assert parse_log("Tape capture meter log\nPeak : 0DEADBEE\n") is None
    assert not parse_log("Exact Audio Copy V1.8\n\nTrack  1\n\n     Copy OK\n").has_crcs


# -- the check ------------------------------------------------------------------------------------------
def write_log(folder, text, name="Rip.log", archive=False, encoding="utf-8"):
    where = folder / "Archive" if archive else folder
    where.mkdir(exist_ok=True)
    (where / name).write_bytes(text.encode(encoding))


def test_every_track_matches_an_eac_log_in_an_archive_folder(disc, tmp_path):
    tracks, files, sums = disc
    write_log(tmp_path, eac_log(tracks, sums), archive=True)
    result = check_log(files[1])
    assert result.problem == "" and result.log_name in ("Archive\\Rip.log", "Archive/Rip.log")
    assert [t.verdict for t in result.tracks] == ["matches"] * 3
    assert result.tracks[0].matched == ["Copy CRC", "AccurateRip v2"]
    rows = describe_log(result)
    assert rows[0] == ("Rip log", f"✓ All 3 tracks match the EAC log ({result.log_name})", "good")
    assert ("Track 2", "matches the rip log ✓ (Copy CRC, AccurateRip v2)", "good") in rows
    assert rows[-1][2] == "muted" and "nothing was sent" in rows[-1][1]


def test_xld_skip_zero_and_both_accuraterip_versions_match(disc, tmp_path):
    tracks, files, sums = disc
    write_log(tmp_path, xld_log(tracks, sums))
    result = check_log(files[0])
    assert all(t.matched == ["CRC32", "CRC32 skip zero", "AccurateRip v2", "AccurateRip v1"] for t in result.tracks)


def test_a_changed_sample_shows_as_that_track_differing(disc, tmp_path):
    tracks, files, sums = disc
    write_log(tmp_path, eac_log(tracks, sums))
    changed = tracks[1].copy()
    changed[5000, 1] ^= 1                                 # one bit of one sample, in track 2
    sf.write(files[1], changed, 44100, format="FLAC", subtype="PCM_16")
    result = check_log(files[0])
    assert [t.verdict for t in result.tracks] == ["matches", "differs", "matches"]
    assert result.tracks[1].differed == ["Copy CRC", "AccurateRip v2"]
    rows = describe_log(result)
    assert rows[0][0] == "Rip log" and rows[0][1].startswith("✗ Track 2 differs from what was ripped") and rows[0][2] == "warn"
    assert ("Track 2", "differs from the rip log ✗ (Copy CRC, AccurateRip v2)", "warn") in rows


def test_a_track_the_log_lacks_and_a_tampered_crc(disc, tmp_path):
    tracks, files, sums = disc
    write_log(tmp_path, eac_log(tracks, sums, skip=(3,), tamper=1))
    result = check_log(files[0])
    assert [t.verdict for t in result.tracks] == ["differs", "matches", "absent"]
    assert result.tracks[0].differed == ["Copy CRC"] and result.tracks[0].matched == ["AccurateRip v2"]
    rows = describe_log(result)
    assert ("Track 3", "not in the log", "muted") in rows
    assert ("Track 1", "differs from the rip log ✗ (Copy CRC; agrees: AccurateRip v2)", "warn") in rows


def test_silent_samples_counted_or_not_as_the_log_says(disc, tmp_path):
    tracks, files, sums = disc
    write_log(tmp_path, eac_log(tracks, sums, null_samples="No"))
    assert [t.verdict for t in check_log(files[0]).tracks] == ["matches"] * 3
    # The same skip-zero CRCs in a log that says null samples were used: they differ.
    write_log(tmp_path, eac_log(tracks, sums, null_samples="No").replace(": No", ": Yes"))
    assert [t.verdict for t in check_log(files[0]).tracks] == ["differs"] * 3


def test_a_range_rip_against_the_whole_disc(disc, tmp_path):
    tracks, files, sums = disc
    whole = crc(np.concatenate(tracks))
    text = ("Exact Audio Copy V0.99 prebeta 5 from 4. May 2009\n\n"
            "Null samples used in CRC calculations       : Yes\n\n" + toc_table() +
            f"\nRange status and errors\n\nSelected range\n\n     Copy CRC {whole:08X}\n     Copy OK\n")
    write_log(tmp_path, text)
    result = check_log(files[2])
    assert result.whole == "matches" and all(t.verdict == "absent" for t in result.tracks)
    assert describe_log(result)[0] == ("Rip log", "✓ The whole disc matches the CRC in the EAC log (Rip.log)", "good")
    write_log(tmp_path, text.replace(f"{whole:08X}", f"{whole ^ 1:08X}"))
    assert check_log(files[2]).whole == "differs"


def test_nothing_to_check_against(disc, tmp_path):
    tracks, files, sums = disc
    result = check_log(files[0])
    assert result.problem == "there's no EAC or XLD log with checksums beside this album"
    assert describe_log(result) == [("Rip log", "Can't check against the rip log: there's no EAC or XLD log with "
                                                "checksums beside this album", "warn")]
    other = eac_log(tracks, sums).replace(toc_table(), toc_table((20, 24, 21)))   # another disc's layout
    write_log(tmp_path, other)
    assert "different track layout" in check_log(files[0]).problem


def test_cancelled(disc, tmp_path):
    tracks, files, sums = disc
    write_log(tmp_path, eac_log(tracks, sums))
    import threading
    stop = threading.Event()
    stop.set()
    assert check_log(files[0], cancel=stop) is None


# -- Now Playing: the button works with the network off ---------------------------------------------------
@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    r.withdraw()
    theme.apply(r)
    yield r
    r.destroy()
    del r
    gc.collect()   # on the main thread: a Tk interpreter freed by another thread aborts


class FakePlayer:
    def __init__(self, track):
        from ironbark.engine import State, Status
        self._status = Status(state=State.STOPPED, index=0, track=track, position=0.0, duration=track.duration)

    def status(self):
        return self._status


def test_now_playing_checks_against_the_log_with_the_network_off(root, disc, tmp_path):
    from ironbark.details import NowPlayingWindow
    from ironbark.library import read_track
    tracks, files, sums = disc
    write_log(tmp_path, eac_log(tracks, sums))
    prefs = {"allow_network": False}
    win = NowPlayingWindow(root, FakePlayer(read_track(files[0])), prefs=prefs)
    win.win.update()
    assert str(win.log_button.cget("state")) == "normal"          # no network needed
    assert str(win.rip_button.cget("state")) == "disabled"        # (the online check still is)
    win.log_button.invoke()
    deadline = time.time() + 20
    while files[0] not in NowPlayingWindow._log_cache and time.time() < deadline:
        win.win.update()
        time.sleep(0.05)
    for _ in range(10):
        win.win.update()
        time.sleep(0.05)
    texts = [w.cget("text") for w in win.check_frame.grid_slaves(column=1) if "text" in w.keys()]
    assert any(t.startswith("✓ All 3 tracks match the EAC log") for t in texts)
    assert texts.count("matches the rip log ✓ (Copy CRC, AccurateRip v2)") == 3
    assert all(NowPlayingWindow._log_cache.get(f) is NowPlayingWindow._log_cache[files[0]] for f in files)
    for f in files:
        NowPlayingWindow._log_cache.pop(f, None)
    win.close()

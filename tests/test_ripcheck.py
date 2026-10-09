"""Rip verification: TOC maths, checksums, finding the layout, and the wording. (0.38.0: the online
look-ups are gone; the rip-log check, test_riplog.py, is the offline check.)"""

import gc
import io
import socket
import struct
import threading
import time
import tkinter as tk
import zlib

import numpy as np
import pytest
import soundfile as sf
from ironbark.tagio.flac import FLACFile

from ironbark import settings, theme
from ironbark.tools import ripcheck
from ironbark.tools.ripcheck import (AR_SKIP, CTDB_SKIP, SECTOR, Checksums, NotARip, Toc, checksums,
                                 find_rip, toc_from_cdtoc, toc_from_lengths, tocs_from_log)

RNG = np.random.default_rng(6)


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    """Belt and braces: anything that tries to open a connection fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to go online")
    monkeypatch.setattr(socket.socket, "connect", refuse)


# -- TOC maths: known answers from real discs (layouts only) --------------------------
# Dire Straits, Brothers in Arms: EAC's log TOC. Its .cue says REM DISCID 8D0CF009 and EAC's
# CUETools DB plug-in logged "CTDB TOCID: 1koFvvTHId9ctbwwSZ6867GpvZs-".
BIA = Toc([33, 23445, 61420, 80360, 109898, 148253, 179608, 200620, 217180], [True] * 9, 248465)
# Eva Cassidy, Songbird: the ripper tagged ACCURATERIPDISCID 010-001038e7-008111a9-910a130a.
SONGBIRD = Toc([0, 21187, 39347, 60437, 80450, 97230, 116465, 134447, 149200, 170940], [True] * 10, 193440)
# Wu-Tang Clan, Wu-Tang Forever CD1: an enhanced CD (11 audio tracks + data), its CDTOC tag.
FOREVER_CDTOC = "B+96+7792+D5D9+1201E+15566+18CD4+1BB16+2100A+24683+279E1+2D067+3483D+477C2"


def test_cddb_and_ctdb_ids_match_what_eac_wrote():
    assert f"{BIA.cddb_id():08X}" == "8D0CF009"
    assert BIA.ctdb_tocid() == "1koFvvTHId9ctbwwSZ6867GpvZs-"
    assert BIA.ctdb_toc() == "33:23445:61420:80360:109898:148253:179608:200620:217180:248465"


def test_accuraterip_ids_match_the_rippers_tag():
    assert SONGBIRD.accuraterip_id() == "010-001038e7-008111a9-910a130a"


def test_cdtoc_tag_with_a_data_track():
    toc = toc_from_cdtoc(FOREVER_CDTOC)
    assert toc.audio_count == 11 and toc.has_data and toc.starts[0] == 0
    assert toc.starts[-1] == 0x3483D - 150 and toc.leadout == 0x477C2 - 150
    assert toc.audio_end == toc.starts[-1] - 11400 == 203551   # EAC's log: track 11 ends at 203550
    assert toc.ctdb_toc().endswith(":-214951:292652")
    # AccurateRip IDs count audio starts only, but the whole disc's leadout; CDDB counts all 12
    id1, id2, cddb = toc.accuraterip_ids()
    assert id1 == sum(toc.starts[:11]) + toc.leadout
    assert cddb & 0xFF == 12


def test_cdtoc_tag_with_a_leading_data_track_and_bad_tags():
    toc = toc_from_cdtoc("2+2EE+4E20+7530+X96")      # data track 1 at 0, audio at 600 and 19850
    assert toc.audio == [False, True, True] and toc.starts == [0, 600, 19850] and toc.leadout == 29850
    for bad in ("", "zz+1", "3+96+5DC", "2+5DC+96+9C4"):
        assert toc_from_cdtoc(bad) is None


EAC_LOG = """Exact Audio Copy V1.8

TOC of the extracted CD

     Track |   Start  |  Length  | Start sector | End sector
    ---------------------------------------------------------
        1  |  0:00.00 |  0:20.00 |         0    |     1499
        2  |  0:20.00 |  0:30.00 |      1500    |     3749
        3  |  1:02.00 |  0:10.00 |     15150    |     15899
"""


def test_log_toc_and_an_enhanced_cds_data_track():
    (toc,) = tocs_from_log(EAC_LOG)
    assert toc.starts == [0, 1500, 15150] and toc.leadout == 15900
    assert toc.audio == [True, True, False]               # 15150 = 3749 + 1 + 11400
    assert toc.audio_lengths == [1500, 2250]
    two = tocs_from_log(EAC_LOG + EAC_LOG.replace("15150", "3750").replace("15899", "4499"))
    assert len(two) == 2 and two[1].audio == [True] * 3


def test_toc_from_lengths_is_marked_less_certain():
    toc = toc_from_lengths([SECTOR * 10, SECTOR * 20])
    assert toc.starts == [0, 10] and toc.leadout == 30 and not toc.certain
    assert toc_from_lengths([SECTOR * 10 + 1]) is None   # not cut on CD sectors


# -- checksums ------------------------------------------------------------------------
def reference(tracks: list[np.ndarray]) -> Checksums:
    """Straight from the documented algorithms, one sample at a time (whipper's loop for
    AccurateRip; zlib over explicit byte ranges for CTDB)."""
    v1, v2, crcs = [], [], []
    count = len(tracks)
    total = sum(len(t) for t in tracks)
    tail = CTDB_SKIP + total % CTDB_SKIP
    for k, t in enumerate(tracks):
        lo, hi = (AR_SKIP if k == 0 else 0), len(t) - (AR_SKIP if k == count - 1 else 0)
        s_lo = s_hi = 0
        for i in range(len(t)):
            pos = i + 1
            if (k == 0 and pos < AR_SKIP) or (k == count - 1 and pos > len(t) - AR_SKIP):
                continue
            word = (int(t[i, 0]) & 0xFFFF) | ((int(t[i, 1]) & 0xFFFF) << 16)
            product = word * pos
            s_lo += product & 0xFFFFFFFF
            s_hi += product >> 32
        assert lo < hi
        v1.append(s_lo & 0xFFFFFFFF)
        v2.append((s_lo + s_hi) & 0xFFFFFFFF)
        a = CTDB_SKIP if k == 0 else 0
        b = len(t) - (tail if k == count - 1 else 0)
        crcs.append(zlib.crc32(t[a:b].astype("<i2").tobytes()))
    whole = np.concatenate(tracks)
    disc = zlib.crc32(whole[CTDB_SKIP:total - tail].astype("<i2").tobytes())
    return Checksums(v1, v2, crcs, disc)


def disc_audio(sectors=(20, 23, 21)) -> list[np.ndarray]:
    return [RNG.integers(-32768, 32768, size=(n * SECTOR, 2)).astype(np.int16) for n in sectors]


def write(path, audio):
    sf.write(str(path), audio, 44100, format="FLAC" if path.suffix == ".flac" else "WAV", subtype="PCM_16")
    return path


def test_checksums_match_the_reference(tmp_path, monkeypatch):
    tracks = disc_audio()
    files = [write(tmp_path / f"{k + 1:02d} t.flac", t) for k, t in enumerate(tracks)]
    monkeypatch.setattr(ripcheck, "BLOCK", 5000)                   # blocks that straddle tracks
    rip = find_rip(files[1])
    assert rip.toc.source == "rebuilt from the track lengths"
    got = checksums(rip)
    assert got == reference(tracks)
    # A middle track's CTDB CRC is the plain CRC-32 of the whole track — EAC's "Copy CRC"
    assert got.ctdb_tracks[1] == zlib.crc32(tracks[1].astype("<i2").tobytes())


def test_one_file_image_gives_the_same_checksums(tmp_path):
    tracks = disc_audio()
    write(tmp_path / "image.wav", np.concatenate(tracks))
    (tmp_path / "image.cue").write_text(
        'FILE "image.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
        '  TRACK 02 AUDIO\n    INDEX 01 00:00:20\n  TRACK 03 AUDIO\n    INDEX 01 00:00:43\n')
    rip = find_rip(tmp_path / "image.wav")
    assert rip.toc.source == "the cue sheet (image.cue)" and rip.toc.starts == [0, 20, 43]
    assert checksums(rip) == reference(tracks)


def test_hidden_gap_before_track_one(tmp_path):
    """Like Brothers in Arms: a 33-sector gap before track 1 that isn't in the files (PREGAP),
    or that is (INDEX 00 at the start of the image): either way track 1 starts at sector 33 and
    the gap is in no checksum."""
    tracks = disc_audio()
    gap = np.zeros((33 * SECTOR, 2), np.int16)
    split = tmp_path / "split"
    split.mkdir()
    for k, t in enumerate(tracks):
        write(split / f"{k + 1:02d}.flac", t)
    (split / "Archive").mkdir()
    (split / "Archive" / "disc.cue").write_text(
        'FILE "01.flac" WAVE\n  TRACK 01 AUDIO\n    PREGAP 00:00:33\n    INDEX 01 00:00:00\n'
        '  TRACK 02 AUDIO\n    INDEX 00 00:00:18\nFILE "02.flac" WAVE\n    INDEX 01 00:00:00\n'
        '  TRACK 03 AUDIO\nFILE "03.flac" WAVE\n    INDEX 01 00:00:00\n')
    rip = find_rip(split / "02.flac")
    assert rip.toc.source == r"the cue sheet (Archive\disc.cue)"
    assert rip.toc.starts == [33, 53, 76] and rip.toc.leadout == 97 and rip.origin == 33
    image = tmp_path / "image"
    image.mkdir()
    write(image / "image.flac", np.concatenate([gap] + tracks))
    (image / "image.cue").write_text(
        'FILE "image.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 00 00:00:00\n    INDEX 01 00:00:33\n'
        '  TRACK 02 AUDIO\n    INDEX 01 00:00:53\n  TRACK 03 AUDIO\n    INDEX 01 00:01:01\n')
    rip2 = find_rip(image / "image.flac")
    assert rip2.toc.starts == [33, 53, 76] and rip2.origin == 0
    assert checksums(rip) == checksums(rip2) == reference(tracks)
    assert rip.toc.ctdb_tocid() == rip2.toc.ctdb_tocid()


def test_layout_sources_in_order(tmp_path):
    tracks = disc_audio((20, 23))
    files = [write(tmp_path / f"{k + 1:02d}.flac", t) for k, t in enumerate(tracks)]
    # stale file names in the .cue: matched in order, and said so
    (tmp_path / "x.cue").write_text('FILE "Track01.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
                                    'FILE "Track02.wav" WAVE\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n')
    rip = find_rip(files[0])
    assert rip.toc.source == "the cue sheet (x.cue)" and any("matched in order" in n for n in rip.notes)
    # a log beats the .cue (it has the real disc's TOC, data tracks included)
    (tmp_path / "rip.log").write_text(
        "     Track |   Start  |  Length  | Start sector | End sector\n"
        "        1  |  0:00.00 |  0:00.20 |         0    |       19\n"
        "        2  |  0:00.20 |  0:00.23 |        20    |       42\n", encoding="utf-16")
    assert find_rip(files[0]).toc.source == "the rip log (rip.log)"
    # a CDTOC tag beats both
    tags = FLACFile(str(files[0]))
    tags["CDTOC"] = "2+96+AA+C1"
    tags.save()
    assert find_rip(files[1]).toc.source == "the CDTOC tag"


def test_a_layout_that_doesnt_fit_is_skipped_and_said(tmp_path):
    tracks = disc_audio((20, 23))
    files = [write(tmp_path / f"{k + 1:02d}.flac", t) for k, t in enumerate(tracks)]
    (tmp_path / "rip.log").write_text(
        "        1  |  0:00.00 |  0:00.20 |         0    |       19\n"
        "        2  |  0:00.20 |  0:00.30 |        20    |       49\n")    # 7 sectors longer
    rip = find_rip(files[0])
    assert rip.toc.source == "rebuilt from the track lengths"
    assert any("7 sectors" in n and "shorter" in n for n in rip.notes)


def test_enhanced_cd_cue_without_a_log(tmp_path):
    tracks = disc_audio((20, 23))
    files = [write(tmp_path / f"{k + 1:02d}.flac", t) for k, t in enumerate(tracks)]
    (tmp_path / "x.cue").write_text(
        'FILE "01.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\nFILE "02.flac" WAVE\n'
        '  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n  TRACK 03 MODE1/2352\n    INDEX 00 00:00:23\n')
    rip = find_rip(files[0])
    assert not rip.toc.certain and any("data track" in n for n in rip.notes)


def test_not_cd_audio(tmp_path):
    path = tmp_path / "hires.flac"
    sf.write(str(path), np.zeros((SECTOR * 20, 2)), 96000, format="FLAC", subtype="PCM_24")
    with pytest.raises(NotARip, match="isn't CD audio"):
        find_rip(path)


def test_cancel(tmp_path):
    import threading
    files = [write(tmp_path / f"{k + 1:02d}.flac", t) for k, t in enumerate(disc_audio())]
    stop = threading.Event()
    stop.set()
    assert checksums(find_rip(files[0]), stop) is None


# -- the lookups (mocked) -------------------------------------------------------------
class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ar_bin(pressings):
    """AccurateRip's .bin: per pressing count, id1, id2, cddb, then (confidence, CRC, CRC450)."""
    out = b""
    for ids, tracks in pressings:
        out += struct.pack("<BIII", len(tracks), *ids)
        for conf, crc in tracks:
            out += struct.pack("<BII", conf, crc, 0)
    return out


def ctdb_xml(entries):
    rows = "".join(f'<entry id="{i}" crc32="{crc:08x}" confidence="{conf}" npar="8" stride="5880" '
                   f'hasparity="x" toc="{toc}" trackcrcs="{" ".join(f"{c:08x}" for c in tc)}"/>'
                   for i, (crc, conf, toc, tc) in enumerate(entries))
    return f'<?xml version="1.0"?><ctdb xmlns="http://db.cuetools.net/ns/mmd-1.0#">{rows}</ctdb>'.encode()


@pytest.fixture
def disc(tmp_path):
    files = [write(tmp_path / f"{k + 1:02d}.flac", t) for k, t in enumerate(disc_audio())]
    rip = find_rip(files[0])
    return files, rip, checksums(rip)


# -- folders that hold more than one disc, or more than the rip ------------------------
def tag(path, **values):
    tags = FLACFile(str(path))
    for key, value in values.items():
        tags[key.upper()] = value
    tags.save()


@pytest.mark.parametrize("tagged", [True, False])
def test_a_folder_with_two_discs_checks_only_the_tracks_own_disc(tmp_path, tagged):
    one, two = disc_audio((20, 23, 21)), disc_audio((22, 24))
    discs = [[write(tmp_path / f"{d}-{k + 1:02d}.flac", t) for k, t in enumerate(audio)]
             for d, audio in ((1, one), (2, two))]
    if tagged:   # DISCNUMBER tags, and names that don't say the disc
        for d, files in enumerate(discs, start=1):
            for k, path in enumerate(files):
                tag(path, discnumber=str(d), tracknumber=str(k + 1))
                path.rename(path.with_name(f"{'ab'[d - 1]}{k + 1:02d}.flac"))
        discs = [[p.with_name(f"{'ab'[d]}{k + 1:02d}.flac") for k, p in enumerate(files)]
                 for d, files in enumerate(discs)]
    rip = find_rip(discs[1][1])
    assert rip.files == discs[1] and rip.toc.audio_count == 2
    assert any("more than one disc" in note for note in rip.notes)
    assert checksums(rip) == reference(two)
    assert ripcheck.disc_files(discs[0][0]) == discs[0]


def test_a_cue_sheet_for_the_other_disc_isnt_used(tmp_path):
    one, two = disc_audio((20, 23, 21)), disc_audio((22, 24, 19))
    for d, audio in ((1, one), (2, two)):
        files = [write(tmp_path / f"{d}-{k + 1:02d}.flac", t) for k, t in enumerate(audio)]
        (tmp_path / f"Disc {d}.cue").write_text("".join(
            f'FILE "{f.name}" WAVE\n  TRACK {k + 1:02d} AUDIO\n    INDEX 01 00:00:00\n' for k, f in enumerate(files)))
    rip = find_rip(tmp_path / "2-02.flac")
    assert rip.toc.source == "the cue sheet (Disc 2.cue)"
    assert [f.name for f in rip.files] == ["2-01.flac", "2-02.flac", "2-03.flac"]
    assert any("Disc 1.cue" in note and "other files" in note for note in rip.notes)


def test_more_files_than_a_cd_can_hold_are_refused(tmp_path, monkeypatch):
    for k in range(ripcheck.CD_MAX_TRACKS + 1):
        write(tmp_path / f"{k + 1:03d}.flac", np.zeros((SECTOR, 2), np.int16))
    with pytest.raises(NotARip, match="more than the 99 tracks"):
        find_rip(tmp_path / "001.flac")


def test_a_rebuilt_layout_longer_than_a_cd_is_refused(tmp_path, monkeypatch):
    files = [write(tmp_path / f"{k + 1:02d}.flac", t) for k, t in enumerate(disc_audio((20, 23)))]
    monkeypatch.setattr(ripcheck, "CD_MAX_SECTORS", 40)    # (a real one would need 80 minutes of files)
    with pytest.raises(NotARip, match="more than a CD can hold"):
        find_rip(files[0])


def test_mp3_copies_and_a_hires_bonus_beside_the_rip_are_left_out(tmp_path):
    tracks = disc_audio((20, 23))
    files = [write(tmp_path / f"{k + 1:02d} t.flac", t) for k, t in enumerate(tracks)]
    sf.write(str(tmp_path / "01 t.mp3"), tracks[0].astype(np.float32) / 32768, 44100, format="MP3")
    sf.write(str(tmp_path / "99 Bonus 24-96.flac"), np.zeros((SECTOR * 20, 2)), 96000, format="FLAC",
             subtype="PCM_24")
    rip = find_rip(files[1])
    assert rip.files == files and checksums(rip) == reference(tracks)
    assert any("left out" in note and "01 t.mp3" in note and "99 Bonus 24-96.flac" in note for note in rip.notes)
    with pytest.raises(NotARip, match="MP3 files can't be checked"):
        find_rip(tmp_path / "01 t.mp3")     # the chosen track itself still has to be CD audio


def test_a_huge_log_isnt_read(tmp_path, monkeypatch):
    files = [write(tmp_path / f"{k + 1:02d}.flac", t) for k, t in enumerate(disc_audio((20, 23)))]
    (tmp_path / "rip.log").write_text(
        "        1  |  0:00.00 |  0:00.20 |         0    |       19\n"
        "        2  |  0:00.20 |  0:00.23 |        20    |       42\n" + "x" * 500)
    monkeypatch.setattr(ripcheck, "LOG_MAX", 400)    # (bigger than any rip log: not one)
    assert find_rip(files[0]).toc.source == "rebuilt from the track lengths"
    monkeypatch.setattr(ripcheck, "LOG_MAX", 4 << 20)
    assert find_rip(files[0]).toc.source == "the rip log (rip.log)"


# -- answers that aren't what they seem ------------------------------------------------
class Trickle:
    """An answer that never finishes: a byte at a time."""

    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def read1(self, size=-1):
        time.sleep(0.01)
        return b"x"

    read = read1


# -- the Now Playing button -------------------------------------------------------------
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
    gc.collect()   # here, on the main thread: a Tk interpreter freed by another thread's collection aborts


class FakePlayer:
    def __init__(self, track):
        from ironbark.engine import State, Status
        self._status = Status(state=State.STOPPED, index=0, track=track, position=0.0, duration=track.duration)

    def status(self):
        return self._status



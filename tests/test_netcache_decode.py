"""The decode path for the network playback cache, and the fixes that came first.

- A truncated .m4a raises "damaged" instead of playing noise; playlists keep web addresses exactly;
  local Opus keeps libsndfile's seek (the proof agrees with playback after a seek), while a cached
  copy reads forward.
- GrowingFile: the final size, reads that wait, give-ups (cancel, patience, a failed copy) that
  return 0 and never raise, never short before the end, readable across the rename to complete.
- _Guarded: a give-up throws that call's output away; Waiting carries the last good frame (the
  track's frame inside a CUE range); the handle stays dead; a decoder error during a give-up is
  Waiting too.
- Every format decoded through the growing path with random stalls and give-ups, rejoined across
  fresh handles, is bit-identical to a local decode.
- flacframes.Scanner, fed in pieces, equals build_index (fixed and variable block sizes).
"""
from __future__ import annotations

import io
import os
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import mp4_maker
import numpy as np
import pytest
import soundfile as sf

from ironbark import audiofile, flacframes, playlist_view, playlists, settings
from ironbark.audiofile import CuePath, CueRange, Gate, LocalTrack
from ironbark.library import Track
from ironbark.netcache.growing import GrowingFile
from ironbark.netcache.store import FAILED, READY, Item, Store

RNG = np.random.default_rng(23)


def music(frames: int, channels: int = 2, seed: int = 1) -> np.ndarray:
    """Integer samples (formats are compared as integers): tones plus a little noise."""
    rng = np.random.default_rng(seed)
    t = np.arange(frames) / 44100
    x = sum(0.2 * np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) for f in (220, 331, 1250, 4400))
    x = x[:, None] * np.ones((1, channels)) + 0.02 * rng.standard_normal((frames, channels))
    return np.clip(np.rint(x * 32767), -32768, 32767).astype(np.int16)


def write(path: Path, data: np.ndarray, rate: int, fmt: str, subtype: str) -> Path:
    with sf.SoundFile(path, "w", rate, data.shape[1], format=fmt, subtype=subtype) as f:
        for k in range(0, len(data), 4096):   # (one big write crashes libsndfile's Vorbis writer)
            f.write(data[k:k + 4096])
    return path


def reference(path) -> np.ndarray:
    with audiofile.open(path) as f:
        return np.concatenate(list(audiofile.blocks(f, 4096, "int32", always_2d=True)))


class Growing:
    """A cached copy that arrives when the test says so (in order from byte 0, as v1 downloads)."""

    def __init__(self, folder: Path, source: Path):
        self.data = source.read_bytes()
        self.store = Store(folder / "NetCache")
        self.store.make_folders()
        self.item = Item(self.store, key="smb:" + str(source), kind="share", source=str(source), host="h",
                         ext=source.suffix.lstrip(".").lower())
        self.item.size = len(self.data)
        self.part = self.store.part_path(self.item)
        self.part.write_bytes(b"")

    def allow(self, upto: int) -> None:
        upto = int(min(len(self.data), upto))
        if upto <= self.item.visible:
            return
        with open(self.part, "r+b") as f:
            f.seek(self.item.visible)
            f.write(self.data[self.item.visible:upto])
        with self.item.cond:
            self.item.visible = upto
            self.item.cond.notify_all()

    def track(self, source=None) -> LocalTrack:
        return LocalTrack(source if source is not None else Path(self.item.source), self.item, self.item.gen)


# -- PATCH (a): a truncated .m4a ----------------------------------------------------------------
def test_a_truncated_m4a_says_damaged_and_never_hands_on_noise(tmp_path):
    sound = (RNG.standard_normal((44100 * 3, 2)) * 3000).astype(np.int16)
    whole = mp4_maker.write_m4a(tmp_path / "whole.m4a", sound, moov_first=True)
    ref = reference(whole)
    cut = tmp_path / "cut.m4a"
    cut.write_bytes(whole.read_bytes()[: int(whole.stat().st_size * 0.4)])
    got = []
    with pytest.raises(RuntimeError, match="damaged"):
        with audiofile.open(cut) as f:
            for block in audiofile.blocks(f, 4096, "int32", always_2d=True):
                got.append(block)
    got = np.concatenate(got) if got else np.zeros((0, 2), np.int32)
    assert 0 < len(got) < len(ref)
    assert np.array_equal(got, ref[:len(got)])   # everything handed on is real audio


# -- PATCH (b): web addresses in playlists ----------------------------------------------------------
def test_playlist_web_addresses_are_kept_exactly(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "settings.json")
    lines = ["https://Music.Example.com/Kind%20of%20Blue/01%20So%20What.flac?sig=Abc&x=1",
             "HTTP://host:8080/a/b.mp3"]
    local = tmp_path / "x.flac"
    source = tmp_path / "in.m3u8"
    source.write_text("#EXTM3U\n" + "\n".join(lines) + f"\n{local}\n", encoding="utf-8")
    songs = playlists.read(source)
    assert [str(s) for s in songs[:2]] == lines
    assert isinstance(songs[0], audiofile.UrlPath) and songs[0].name == "01 So What.flac"
    assert songs[0].suffix == ".flac" and not songs[0].is_file() and not songs[0].exists()
    assert songs[2] == local
    with pytest.raises(audiofile.Unsupported, match="web address"):
        audiofile.open(songs[0])
    out = tmp_path / "out.m3u8"
    playlists.write(out, [Track(s, "t") for s in songs])
    written = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] in written and lines[1] in written
    assert playlists.read(out) == songs
    # Listed as missing, with the reason.
    view = SimpleNamespace(_tracks=[Track(songs[0], "So What", missing=True)], flat=True, columns=(), shown=(),
                           is_favourite=lambda track: False)
    assert playlist_view.PlaylistView._title(view, 0, False).endswith("(web address — not supported yet)")


# -- PATCH (c): Opus seeks ------------------------------------------------------------------------------
def test_local_opus_keeps_its_seek_and_the_proof_agrees_with_playback(tmp_path):
    path = write(tmp_path / "a.opus", music(48000 * 4), 48000, "OGG", "OPUS")
    with audiofile.open(path) as f:
        assert not isinstance(f, audiofile._ExactSeek)
    for start in (0, 37_013, 101_777):
        with audiofile.open(path) as play, audiofile.open(path) as proof:
            play.seek(start)
            frame = start
            while True:
                block = audiofile.read(play, 4096, "int32")
                if not len(block):
                    break
                proof.seek(frame)
                assert np.array_equal(audiofile.read(proof, len(block), "int32"), block)
                frame += len(block)


def test_a_cached_opus_copy_reads_forward(tmp_path):
    path = write(tmp_path / "a.opus", music(48000 * 2), 48000, "OGG", "OPUS")
    g = Growing(tmp_path, path)
    g.allow(len(g.data))
    with audiofile.open(g.track(), patience=1) as f:
        assert isinstance(f._inner, audiofile._ExactSeek)


# -- GrowingFile --------------------------------------------------------------------------------------
@pytest.fixture
def growing(tmp_path):
    path = write(tmp_path / "a.flac", music(44100 * 2), 44100, "FLAC", "PCM_16")
    return Growing(tmp_path, path)


def test_growing_file_size_and_reads_that_wait(growing):
    g = growing
    g.allow(1000)
    gate = Gate(patience=2)
    f = GrowingFile(g.item, g.item.gen, gate)
    assert f.seek(0, io.SEEK_END) == len(g.data)
    f.seek(0)
    gate.start_call()
    assert f.read(1000) == g.data[:1000]
    threading.Timer(0.05, g.allow, args=(5000,)).start()
    started = time.monotonic()
    assert f.read(3000) == g.data[1000:4000]     # waited for all of it, never short
    assert 0.03 < time.monotonic() - started < 1.5
    assert not gate.gave_up
    f.close()
    assert not g.item.leased()


def test_growing_file_gives_up_on_cancel_quickly(growing):
    g = growing
    best = 1.0
    for _ in range(3):
        stop = [False]
        gate = Gate(patience=30, cancel=lambda: stop[0])
        gate.start_call()
        f = GrowingFile(g.item, g.item.gen, gate)
        returned = {}

        def read():
            returned["n"] = f.readinto(bytearray(100))
            returned["at"] = time.perf_counter()

        thread = threading.Thread(target=read)
        thread.start()
        time.sleep(0.05)
        stop[0] = True
        asked = time.perf_counter()
        gate.wake()
        thread.join(2)
        assert returned["n"] == 0 and gate.gave_up == "cancel"
        best = min(best, returned["at"] - asked)
        f.close()
    assert best < 0.005


def test_growing_file_gives_up_on_patience_and_failure_and_never_raises(growing, monkeypatch):
    g = growing
    g.allow(100)
    gate = Gate(patience=0.05)
    f = GrowingFile(g.item, g.item.gen, gate)
    gate.start_call()
    started = time.monotonic()
    assert f.readinto(bytearray(200)) == 0
    assert gate.gave_up == "patience" and time.monotonic() - started >= 0.045
    assert f.readinto(bytearray(10)) == 0          # a gate that gave up stays shut
    f.close()

    gate = Gate(patience=5)
    f = GrowingFile(g.item, g.item.gen, gate)
    gate.start_call()
    g.item.state = FAILED
    assert f.readinto(bytearray(200)) == 0 and gate.gave_up == "failed"
    f.close()
    g.item.state = "downloading"

    gate = Gate(patience=5)
    f = GrowingFile(g.item, g.item.gen, gate)
    gate.start_call()
    monkeypatch.setattr(g.item, "open_bytes", lambda gen: (_ for _ in ()).throw(OSError("gone")))
    assert f.readinto(bytearray(50)) == 0 and gate.gave_up == "failed"   # (no exception escaped)
    f.close()


def test_growing_file_reads_to_the_end_and_across_the_rename(growing):
    g = growing
    g.allow(len(g.data))
    gate = Gate(patience=1)
    f = GrowingFile(g.item, g.item.gen, gate)
    gate.start_call()
    half = len(g.data) // 2
    assert f.read(half) == g.data[:half]
    g.item.final_name = "a [x].flac"
    os.replace(g.part, g.store.tracks / g.item.final_name)   # (works while it's open for reading)
    g.item.state = READY
    rest = f.read(len(g.data))
    assert rest == g.data[half:] and f.read(10) == b"" and not gate.gave_up
    f.close()


# -- _Guarded --------------------------------------------------------------------------------------------
def test_a_give_up_discards_the_call_and_the_handle_stays_dead(growing):
    g = growing
    g.allow(len(g.data) // 2)
    f = audiofile.open(g.track(), patience=0.02)
    good = 0
    with pytest.raises(audiofile.Waiting) as waiting:
        while True:
            good += len(audiofile.read(f, 4096, "int32"))
    assert waiting.value.frame == good and waiting.value.reason == "patience"
    assert not isinstance(waiting.value, (OSError, RuntimeError, ValueError))
    g.allow(len(g.data))
    for call in (lambda: f.read(10), lambda: f.seek(0)):
        with pytest.raises(audiofile.Waiting) as again:
            call()
        assert again.value.frame == good
    f.close()
    # A fresh handle carries on exactly.
    with audiofile.open(g.track(), patience=1) as fresh:
        fresh.seek(good)
        rest = np.concatenate(list(audiofile.blocks(fresh, 4096, "int32", always_2d=True)))
    whole = reference(Path(g.item.source))
    assert np.array_equal(rest, whole[good:])


def test_waiting_frames_are_the_tracks_own_inside_a_cue_range(growing):
    g = growing
    g.allow(len(g.data) * 2 // 3)
    cue_track = CuePath(g.item.source, cue=CueRange("", 2, 22050, 80000))
    f = audiofile.open(g.track(cue_track), patience=0.02)
    good = 0
    with pytest.raises(audiofile.Waiting) as waiting:
        while True:
            block = audiofile.read(f, 4096, "int32")
            if not len(block):
                break
            good += len(block)
    assert waiting.value.frame == good < 80000 - 22050
    f.close()


def test_a_decoder_error_during_a_give_up_is_waiting():
    gate = Gate()

    class Inner:
        frames, samplerate, channels = 100, 44100, 2

        def tell(self):
            return 7

        def read(self, *args, **kwargs):
            gate.gave_up = "patience"
            raise RuntimeError("Internal psf_fseek() failed.")

        def close(self):
            pass

    guarded = audiofile._Guarded(Inner(), gate)
    with pytest.raises(audiofile.Waiting) as waiting:
        guarded.read(10)
    assert waiting.value.frame == 7 and isinstance(waiting.value.__cause__, RuntimeError)
    gate2 = Gate()
    inner = Inner()
    inner.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("really damaged"))
    with pytest.raises(RuntimeError, match="really damaged"):
        audiofile._Guarded(inner, gate2).read(10)


def test_apple_lossless_is_never_opened_while_downloading(tmp_path):
    sound = (RNG.standard_normal((44100, 2)) * 3000).astype(np.int16)
    path = mp4_maker.write_m4a(tmp_path / "a.m4a", sound, moov_first=True)
    g = Growing(tmp_path, path)
    g.allow(len(g.data))
    with pytest.raises(audiofile.Unsupported, match="Apple Lossless"):
        audiofile.open(g.track(), patience=1)


def test_a_part_downloaded_file_is_never_opened_by_name(growing):
    g = growing
    g.allow(len(g.data))
    audiofile.set_resolver(lambda path, cancel=None, wait=None: None, partial_folder=g.store.partial)
    try:
        with pytest.raises(audiofile.Unsupported, match="part-downloaded"):
            audiofile.open(g.part)
    finally:
        audiofile.set_resolver(None)
    assert audiofile._resolver is None and audiofile._partial_folder == ""


# -- the decode-equivalence matrix ---------------------------------------------------------------------------
FORMATS = [("flac16.flac", 44100, "FLAC", "PCM_16"), ("flac24.flac", 44100, "FLAC", "PCM_24"),
           ("wav16.wav", 44100, "WAV", "PCM_16"), ("aiff16.aiff", 44100, "AIFF", "PCM_16"),
           ("mp3.mp3", 44100, "MP3", "MPEG_LAYER_III"), ("vorbis.ogg", 44100, "OGG", "VORBIS"),
           ("opus.opus", 48000, "OGG", "OPUS")]


def decode_with_stalls(g: Growing, track: LocalTrack, rng, patience: float = 0.001) -> tuple[np.ndarray, int]:
    out, frame, f, give_ups = [], 0, None, 0
    while True:
        if f is None:
            try:
                f = audiofile.open(track, patience=patience)
                if frame:
                    f.seek(frame)
            except audiofile.Waiting:
                if f is not None:
                    f.close()
                    f = None
                give_ups += 1
                g.allow(g.item.visible + int(rng.integers(2000, 60000)))
                continue
        audiofile.set_patience(f, patience)
        try:
            block = audiofile.read(f, 4096, "int32", always_2d=True)
        except audiofile.Waiting as waiting:
            assert waiting.frame == frame
            give_ups += 1
            f.close()
            f = None
            g.allow(g.item.visible + int(rng.integers(2000, 60000)))
            continue
        if not len(block):
            break
        out.append(block)
        frame += len(block)
    f.close()
    return np.concatenate(out), give_ups


@pytest.mark.parametrize("name,rate,fmt,subtype", FORMATS)
def test_every_format_decodes_bit_identically_through_stalls(tmp_path, name, rate, fmt, subtype):
    path = write(tmp_path / name, music(int(rate * 2.5), seed=len(name)), rate, fmt, subtype)
    ref = reference(path)
    total_give_ups = 0
    for seed in range(3):
        g = Growing(tmp_path / f"run{seed}", path)
        g.allow(int(np.random.default_rng(seed).integers(100, 5000)))
        got, give_ups = decode_with_stalls(g, g.track(), np.random.default_rng(seed))
        total_give_ups += give_ups
        assert got.shape == ref.shape and np.array_equal(got, ref), f"{name}, run {seed}"
    assert total_give_ups > 0


def test_a_cue_track_decodes_bit_identically_through_stalls(tmp_path):
    path = write(tmp_path / "image.flac", music(44100 * 4), 44100, "FLAC", "PCM_16")
    cue_track = CuePath(path, cue=CueRange("", 2, 44100, 44100 * 3))
    ref = reference(cue_track)
    g = Growing(tmp_path / "c", path)
    g.allow(3000)
    got, give_ups = decode_with_stalls(g, g.track(cue_track), np.random.default_rng(5))
    assert give_ups and np.array_equal(got, ref)


# -- flacframes.Scanner ------------------------------------------------------------------------------------------
def _utf8_number(value: int) -> bytes:
    if value < 0x80:
        return bytes([value])
    for count, limit in ((2, 0x800), (3, 0x10000), (4, 0x200000), (5, 0x4000000), (6, 0x80000000)):  # noqa: B007 (read after the loop)
        if value < limit:
            break
    else:
        count = 7
    tail = [0x80 | ((value >> (6 * k)) & 0x3F) for k in range(count - 1)][::-1]
    lead = ((0xFF << (8 - count)) & 0xFF) | (value >> (6 * (count - 1)))
    return bytes([lead & 0xFF] + tail)


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def variable_block_flac(path: Path, sizes: list[int], rate: int = 44100) -> Path:
    """A mono 16-bit FLAC of verbatim frames with variable block sizes (sync 0xFFF9)."""
    total = sum(sizes)
    info = (struct.pack(">HH", min(sizes), max(sizes)) + b"\x00\x00\x00\x00\x00\x00"
            + ((rate << 44) | (0 << 41) | (15 << 36) | total).to_bytes(8, "big") + bytes(16))
    out = bytearray(b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info)
    sample = 0
    rng = np.random.default_rng(3)
    for size in sizes:
        header = bytearray(b"\xff\xf9" + bytes([0x70, 0x08]) + _utf8_number(sample) + struct.pack(">H", size - 1))
        header.append(flacframes.crc8(header))
        body = bytes([0x02]) + rng.integers(-2000, 2000, size).astype(">i2").tobytes()
        frame = bytes(header) + body
        out += frame + struct.pack(">H", _crc16(frame))
        sample += size
    path.write_bytes(bytes(out))
    return path


@pytest.mark.parametrize("kind", ["fixed", "variable"])
def test_the_scanner_fed_in_pieces_equals_build_index(tmp_path, kind):
    if kind == "fixed":
        path = write(tmp_path / "a.flac", music(44100 * 3), 44100, "FLAC", "PCM_16")
    else:
        path = variable_block_flac(tmp_path / "v.flac", [int(x) for x in RNG.integers(100, 5000, 120)])
    want = flacframes.build_index(path)
    data = path.read_bytes()
    for seed in range(3):
        rng = np.random.default_rng(seed)
        scanner = flacframes.Scanner()
        at = 0
        seen_end = []
        while at < len(data):
            n = int(rng.integers(1, 40000))
            scanner.feed(data[at: at + n])
            at += n
            seen_end.append(scanner.end_sample())
        got = scanner.finish(len(data))
        assert np.array_equal(got.samples, want.samples) and np.array_equal(got.offsets, want.offsets)
        assert got.sample_rate == want.sample_rate
        assert scanner.first_frame == int(want.offsets[0])
        ends = [e for e in seen_end if e is not None]
        assert ends == sorted(ends) and ends[-1] <= want.samples[-1]
    assert len(scanner._buf) < 64   # only a few bytes kept once past the metadata


def test_an_album_scan_reads_only_headers_and_never_fetches_a_song_into_the_cache(tmp_path, monkeypatch):
    from ironbark import library
    album = tmp_path / "Artist - Album"
    album.mkdir()
    x = (np.random.default_rng(5).standard_normal((44100, 2)) * 3000).astype(np.int16)
    sf.write(album / "01 One.flac", x, 44100, subtype="PCM_16")
    sf.write(album / "02 Two.flac", x, 44100, subtype="PCM_16")
    asked = []

    def resolver(path, cancel=None, wait=None):   # (the cache: it would fetch the whole song, or wait 30 s)
        asked.append(path)
        raise audiofile.NotCached("not downloaded yet")
    monkeypatch.setattr(audiofile, "_resolver", resolver)
    monkeypatch.setattr(audiofile, "_info_hook", lambda path: None)
    found = library.find_albums(tmp_path)
    assert [(a.name, a.sample_rate, a.bits) for a in found] == [("Artist - Album", 44100, 16)]
    assert not asked
    with pytest.raises(audiofile.NotCached):
        audiofile.info(album / "01 One.flac")       # (a whole-file reader's info still goes through the cache)

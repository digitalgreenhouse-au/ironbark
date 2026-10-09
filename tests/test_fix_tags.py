"""Tag reading and writing on hostile, unusual or busy files: bounded work on crafted tags (zlib
bombs, NUL floods, big Ogg covers), nothing in a tag silently dropped by a rewrite (a misread
frame walk refuses the write instead), two saves of one file never sharing a copy, a small tag
history, and the edit windows' slow work off the Tk thread. All on files made in a temp folder."""
import base64
import gc
import json
import os
import struct
import threading
import time
import tkinter as tk
import tracemalloc
import zlib
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mp4_maker import write_m4a
from test_tag_editor import FORMATS, make, picture

from ironbark import artwork, library, settings, tag_editor, tagio, theme
from ironbark.tag_editor import Edit, Job, TagSave
from ironbark.tagio import id3, ogg
from ironbark.tagio.flac import FLACFile
from ironbark.tagio.mpeg import MP3File
from ironbark.tagio.vorbis import Picture, VComment

RNG = np.random.default_rng(41)


@pytest.fixture(autouse=True)
def temp_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")


def frame24(frame_id: str, body: bytes, flags: bytes = b"\x00\x00") -> bytes:
    return frame_id.encode() + id3.to_synchsafe(len(body)) + flags + body


def frame23(frame_id: str, body: bytes, flags: bytes = b"\x00\x00") -> bytes:
    return frame_id.encode() + struct.pack(">I", len(body)) + flags + body


def tag(major: int, frames: bytes, flags: int = 0) -> bytes:
    return b"ID3" + bytes([major, 0, flags]) + id3.to_synchsafe(len(frames)) + frames


def mp3_with(path: Path, tag_bytes: bytes) -> Path:
    """An MP3 (libsndfile's LAME) with these exact ID3v2 tag bytes in front."""
    sf.write(path, (RNG.standard_normal((20000, 2)) * 0.1).astype("float32"), 44100, format="MP3")
    path.write_bytes(tag_bytes + path.read_bytes())
    return path


# -- Ogg: header pages read in one pass ------------------------------------------------------------
def opus_file(path: Path, comments, extra_packets=(), trailing: bytes = b"") -> Path:
    """A small Ogg Opus stream built page by page: OpusHead, OpusTags (+ `extra_packets` sharing
    its last page), five audio pages, then `trailing` bytes."""
    serial = 1234
    head = b"OpusHead" + bytes([1, 2]) + struct.pack("<HIhB", 312, 48000, 0, 0)
    first = ogg.paginate([head], serial, 0)
    first[0].flags = 0x02
    pages = first + ogg.paginate([b"OpusTags" + VComment("test", comments).render(), *extra_packets], serial, 1)
    for i in range(5):
        page = ogg.Page()
        page.flags, page.granule, page.serial, page.sequence = 0, 48000 * (i + 1), serial, len(pages)
        page.lacing, page.data = bytes([100]), bytes(RNG.integers(0, 256, 100, np.uint8))
        pages.append(page)
    path.write_bytes(b"".join(p.render() for p in pages) + trailing)
    return path


def cover_comment(size: int) -> tuple[str, str]:
    block = Picture(type=3, mime="image/jpeg", data=bytes(RNG.integers(0, 256, size, np.uint8))).render()
    return "METADATA_BLOCK_PICTURE", base64.b64encode(block).decode("ascii")


def test_an_ogg_file_with_a_big_cover_opens_and_saves_quickly(tmp_path):
    path = opus_file(tmp_path / "big.opus", [("TITLE", "x"), cover_comment(2_000_000)])
    started = time.perf_counter()
    audio = tagio.open_file(path, "opus")
    opened = time.perf_counter() - started
    assert len(audio.pages) > 30 and audio["title"] == ["x"]
    audio["title"] = "y" * 70_000                     # one more page: every later page renumbered
    started = time.perf_counter()
    audio.save()
    saved = time.perf_counter() - started
    assert tagio.open_file(path, "opus")["title"] == ["y" * 70_000]
    assert opened < 1.5 and saved < 3, (opened, saved)


def test_ogg_crc_is_the_specs_and_fast():
    data = bytes(RNG.integers(0, 256, 3_000_000, np.uint8))
    started = time.perf_counter()
    fast = ogg.crc32(data)
    took = time.perf_counter() - started

    def slow(chunk: bytes) -> int:   # RFC 3533 §6 bit by bit: polynomial 0x04C11DB7, initial 0, not reflected
        crc = 0
        for byte in chunk:
            crc ^= byte << 24
            for _ in range(8):
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        return crc

    assert ogg.crc32(data[:5000]) == slow(data[:5000]) and ogg.crc32(b"") == 0
    assert fast == ogg.crc32(memoryview(data)) and took < 0.5


@pytest.mark.parametrize("extra", [[b"\x55" * 50], [b"\x66" * 300], [b"\x77" * (255 * 260)]])
def test_an_ogg_header_page_that_also_holds_audio_is_never_rewritten(tmp_path, extra):
    path = opus_file(tmp_path / "shared.opus", [("TITLE", "x")], extra_packets=extra)
    before = path.read_bytes()
    audio = tagio.open_file(path, "opus")
    assert audio["title"] == ["x"]
    audio["title"] = "changed"
    with pytest.raises(tagio.TagError, match="sound"):
        audio.save()
    assert path.read_bytes() == before


def test_bytes_after_the_last_ogg_page_are_kept_when_the_pages_are_renumbered(tmp_path):
    id3v1 = b"TAG" + b"Old title".ljust(30, b"\x00") + bytes(95)
    path = opus_file(tmp_path / "tail.opus", [("TITLE", "x")], trailing=id3v1)
    audio = tagio.open_file(path, "opus")
    audio.tags.set("METADATA_BLOCK_PICTURE", [cover_comment(150_000)[1]])   # more pages: renumbered
    audio.save()
    again = tagio.open_file(path, "opus")
    assert len(again.pages) > 2 and path.read_bytes().endswith(id3v1) and again.length == 5.0 - 312 / 48000


# -- ID3: the frame walk ---------------------------------------------------------------------------
def test_id3v24_plain_sizes_below_0x80_are_followed_frame_by_frame(tmp_path):
    def plain(frame_id: bytes, body: bytes) -> bytes:   # a buggy writer's plain 32-bit size
        return frame_id + struct.pack(">I", len(body)) + b"\x00\x00" + body
    title = b"\x00" + b"T" * 299                          # 300 = 00 00 01 2C: every byte below 0x80
    cover = b"\x00image/jpeg\x00\x03\x00" + b"\xff\xd8" + b"\x11" * 1000
    frames = plain(b"TIT2", title) + plain(b"TPE1", b"\x00Artist") + plain(b"APIC", cover) + plain(b"TALB", b"\x00Album")
    parsed = id3.ID3Tag.parse(tag(4, frames))
    assert [f.id for f in parsed.frames] == ["TIT2", "TPE1", "APIC", "TALB"]
    assert parsed.text("TIT2") == ["T" * 299] and parsed.problem == ""
    path = mp3_with(tmp_path / "plain.mp3", tag(4, frames))
    audio = MP3File(path)
    assert not audio.damaged_tag and audio.tags.text("TALB") == ["Album"]


@pytest.mark.parametrize("junk", [b"\xffGARBAGE-AFTER-A-FRAME" * 3, b"TPE1\x00\x00\x7f\x7f\x00\x00\x00Cut"])
def test_an_id3_frame_walk_that_stops_early_marks_the_tag_unwritable(tmp_path, junk):
    good = frame24("TIT2", b"\x00Title") + frame24("APIC", b"\x00image/png\x00\x03\x00" + picture())
    parsed = id3.ID3Tag.parse(tag(4, good + junk))
    assert [f.id for f in parsed.frames] == ["TIT2", "APIC"] and parsed.problem
    path = mp3_with(tmp_path / "junk.mp3", tag(4, good + junk))
    before = path.read_bytes()
    audio = MP3File(path)
    assert audio.damaged_tag and audio.tags.text("TIT2") == ["Title"]   # still read, never rewritten
    with pytest.raises(tagio.TagError):
        audio.save(4)
    assert path.read_bytes() == before
    track = library.read_track(path)
    save = TagSave(tag_editor.edit_jobs([track], Edit({"album": "New"})), "x")
    save.run()
    assert save.failed and path.read_bytes() == before


def test_padding_after_the_frames_is_fine():
    parsed = id3.ID3Tag.parse(tag(4, frame24("TIT2", b"\x00Title") + bytes(500)))
    assert parsed.text("TIT2") == ["Title"] and parsed.problem == ""


# -- ID3: bounded work on crafted frames -----------------------------------------------------------
def test_a_compressed_id3_frame_is_never_inflated_past_its_limit():
    raw = bytes(64 * 1024 * 1024)
    packed = zlib.compress(raw, 9)
    del raw
    body = struct.pack(">I", 64 * 1024 * 1024) + packed               # v2.3: decompressed size, then zlib
    frame = frame23("PRIV", body, b"\x00\x80")
    tracemalloc.start()
    try:
        parsed = id3.ID3Tag.parse(tag(3, frame23("TIT2", b"\x00Title") + frame))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 48 * 1024 * 1024, peak
    assert parsed.text("TIT2") == ["Title"] and parsed.problem == ""
    again = id3.ID3Tag.parse(parsed.render(3))                          # kept as it was, flags and all
    assert frame in parsed.render(3) and [f.id for f in again.frames] == ["TIT2", "PRIV"]


def test_a_compressed_frame_must_match_its_declared_size():
    body = b"\x00" + b"Zipped"
    packed = zlib.compress(body)
    lying = frame24("TIT2", id3.to_synchsafe(len(body) + 50) + packed, b"\x00\x09")
    parsed = id3.ID3Tag.parse(tag(4, lying))
    assert parsed.text("TIT2") == [] and lying in parsed.render(4)    # not believed, but kept as it was
    honest = frame24("TIT2", id3.to_synchsafe(len(body)) + packed, b"\x00\x09")
    assert id3.ID3Tag.parse(tag(4, honest)).text("TIT2") == ["Zipped"]


def test_compressed_frames_share_one_budget_per_tag(monkeypatch):
    monkeypatch.setattr(id3, "MAX_INFLATED", 4000)
    body = b"\x00" + b"x" * 1500
    packed = zlib.compress(body)
    one = frame23("TIT2", struct.pack(">I", len(body)) + packed, b"\x00\x80")
    frames = one + one.replace(b"TIT2", b"TPE1") + one.replace(b"TIT2", b"TALB")
    parsed = id3.ID3Tag.parse(tag(3, frames))
    assert parsed.text("TIT2") == ["x" * 1500] and parsed.text("TPE1") == ["x" * 1500]
    assert parsed.text("TALB") == [] and parsed.render(3).count(packed) == 1   # over budget: kept unread


@pytest.mark.parametrize("encoding,nul", [(0, b"\x00"), (1, b"\x00\x00")])
def test_a_text_frame_full_of_nuls_is_read_in_linear_time(encoding, nul):
    body = bytes([encoding]) + (b"x" if encoding == 0 else b"\xff\xfex\x00") + nul * 200_000
    started = time.perf_counter()
    parsed = id3.ID3Tag.parse(tag(4, frame24("TIT2", body)))
    took = time.perf_counter() - started
    assert took < 0.5, took
    assert parsed.text("TIT2") == [] or parsed.text("TIT2")[0] == "x"
    assert parsed.render(4).endswith(body)                              # kept byte for byte either way


def test_utf16_values_still_split_on_aligned_terminators():
    body = b"\x01" + b"\xff\xfeA\x00" + b"\x00\x00" + b"\xff\xfe\x00\x01" + b"\x00\x00"   # "A", "Ā"
    assert id3.ID3Tag.parse(tag(4, frame24("TIT2", body))).text("TIT2") == ["A", "Ā"]
    body = b"\x00a\x00\x00b\x00"                                                          # Latin-1
    assert id3.ID3Tag.parse(tag(4, frame24("TIT2", body))).text("TIT2") == ["a", "", "b"]


# -- ID3: nothing dropped, nothing escaping --------------------------------------------------------
def test_an_encrypted_tory_frame_is_kept_and_does_not_crash():
    encrypted = frame23("TORY", b"\x80" + b"secret", b"\x00\x40")
    parsed = id3.ID3Tag.parse(tag(3, frame23("TIT2", b"\x00Title") + encrypted))
    assert parsed.text("TIT2") == ["Title"] and encrypted in parsed.render(3)


def test_odd_genre_codes_do_not_crash():
    assert id3.genres(["²"]) == ["²"] and id3.genres(["(²)"]) == ["²"]
    assert id3.genres(["(" + "9" * 5000 + ")"]) == ["9" * 5000] and id3.genres(["٣"]) == ["٣"]


def test_v23_dates_that_cannot_merge_are_kept():
    frames = (frame23("TYER", b"\x001999") + frame23("TIME", b"\x001020") + frame23("TRDA", b"\x00June 1999")
              + frame23("TDAT", b"\x00May"))
    parsed = id3.ID3Tag.parse(tag(3, frames))
    assert parsed.text("TDRC") == ["1999"]
    again = id3.ID3Tag.parse(parsed.render(3))
    assert again.text("TRDA") == ["June 1999"] and again.text("TIME") == ["1020"] and again.text("TDAT") == ["May"]
    assert again.text("TDRC") == ["1999"]


def test_id3v22_frames_with_no_v23_equivalent_make_the_tag_unwritable():
    def frame22(frame_id, body):
        return frame_id.encode() + len(body).to_bytes(3, "big") + body
    parsed = id3.ID3Tag.parse(tag(2, frame22("CNT", b"\x00\x00\x00\x07") + frame22("TT2", b"\x00Song")))
    assert parsed.text("TIT2") == ["Song"] and parsed.problem    # read on past it, but never rewritten


@pytest.mark.parametrize("kind,name", FORMATS)
def test_damaged_files_only_ever_raise_tag_or_os_errors(tmp_path, kind, name):
    clean = make(tmp_path / name, kind).read_bytes()
    rng = np.random.default_rng(len(name) * 7 + len(kind))
    path = tmp_path / ("damaged" + Path(name).suffix)
    for _ in range(40):
        data = bytearray(clean)
        for _ in range(int(rng.integers(1, 12))):
            where = int(rng.integers(0, min(len(data), 6000))) if rng.random() < 0.8 else int(rng.integers(0, len(data)))
            data[where] = int(rng.integers(0, 256))
        if rng.random() < 0.2:
            del data[int(rng.integers(len(data) // 2, len(data))):]
        path.write_bytes(bytes(data))
        try:
            audio = tagio.open_file(path, kind)
            tagio.text_values(audio)
            tagio.pictures(audio)
            if isinstance(audio.tags, id3.ID3Tag):
                audio.tags.genres()
            tag_editor.read_fields(path, kind)
            tag_editor.read_cover(path, kind)
            tag_editor.snapshot(path, kind)
        except (tagio.TagError, OSError):
            pass


# -- saving: two saves of one file, errors, links --------------------------------------------------
def _job(path: Path, kind: str, fields: dict) -> Job:
    edit = Edit(fields)
    return Job(path, kind, lambda audio: tag_editor.apply_edit(audio, kind, edit), tag_editor._field_check(kind, edit))


def test_a_second_save_of_a_waiting_file_is_refused_not_mixed_up(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    first = TagSave([_job(path, "flac", {"album": "Album ONE"})], "first")
    first.run(busy=lambda p: True)
    second = TagSave([_job(path, "flac", {"artist": "Artist TWO"})], "second")
    second.run(busy=lambda p: True)
    assert first.waiting == [path] and second.failed and "waiting" in second.failed[0].reason
    assert first.retry() == [path]
    fields = tag_editor.read_fields(path, "flac")
    assert fields["album"] == "Album ONE" and fields["artist"] == "Old artist"
    assert second.retry() == []
    third = TagSave([_job(path, "flac", {"artist": "Artist THREE"})], "third")     # free again
    third.run()
    assert third.saved == [path] and tag_editor.read_fields(path, "flac")["artist"] == "Artist THREE"
    states = {e["summary"]: e["files"][0]["state"] for e in tag_editor.history()}
    assert states == {"first": "saved", "second": "failed", "third": "saved"}


def test_a_changed_copy_is_never_swapped_in(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    save = TagSave([_job(path, "flac", {"album": "Later"})], "x")
    save.run(busy=lambda p: True)
    copy = tag_editor.temp_path(path)
    with open(copy, "r+b") as f:     # something else writes to the checked copy while it waits
        f.seek(0, 2)
        f.write(b"\x00" * 10)
    assert save.retry() == [] and save.failed and tag_editor.read_fields(path, "flac")["album"] == "Old album"
    assert not copy.exists()


def test_a_history_write_failing_mid_save_is_reported_and_leaves_no_copy(tmp_path, monkeypatch):
    paths = [make(tmp_path / f"{i}.flac", "flac") for i in range(3)]
    real, writes = tag_editor._write_json, []

    def failing(path, data):
        writes.append(path)
        if len(writes) > 2:
            raise OSError(28, "No space left on device")
        real(path, data)

    monkeypatch.setattr(tag_editor, "_write_json", failing)
    save = TagSave([_job(p, "flac", {"album": "New"}) for p in paths], "x")
    results = save.run()
    assert all(r.state in ("saved", "failed") for r in results) and save.failed
    assert "history" in save.failed[0].reason
    assert not any(tag_editor.temp_path(p).exists() for p in paths)
    monkeypatch.setattr(tag_editor, "_write_json", real)
    entry = tag_editor.history()[0]
    swapped = [p for p in paths if tag_editor.read_fields(p, "flac")["album"] == "New"]
    listed = [Path(f["path"]) for f in entry["files"] if f.get("saved")]
    assert set(swapped) <= set(listed)          # every file swapped in can still be undone


def test_an_unexpected_error_while_saving_fails_that_file_only(tmp_path):
    paths = [make(tmp_path / f"{i}.flac", "flac") for i in range(2)]

    def broken(audio):
        raise TypeError("a bug")
    save = TagSave([Job(paths[0], "flac", broken, None), _job(paths[1], "flac", {"album": "New"})], "x")
    save.run()
    assert [r.state for r in save.results] == ["failed", "saved"] and "a bug" in save.results[0].reason
    assert not tag_editor.temp_path(paths[0]).exists()


def test_the_first_history_write_failing_saves_nothing(tmp_path, monkeypatch):
    path = make(tmp_path / "a.flac", "flac")
    before = path.read_bytes()

    def failing(path, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(tag_editor, "_write_json", failing)
    save = TagSave([_job(path, "flac", {"album": "New"})], "x")
    save.run()
    assert save.failed and path.read_bytes() == before and not tag_editor.temp_path(path).exists()


def test_hard_links_and_symbolic_links_are_refused(tmp_path, monkeypatch):
    path = make(tmp_path / "a.flac", "flac")
    linked = tmp_path / "b.flac"
    os.link(path, linked)
    before = path.read_bytes()
    save = TagSave([_job(linked, "flac", {"album": "New"})], "x")
    save.run()
    assert save.failed and "link" in save.failed[0].reason and path.read_bytes() == before
    os.unlink(linked)
    symbolic = tmp_path / "c.flac"
    try:
        os.symlink(path, symbolic)
    except (OSError, NotImplementedError):   # Windows without the right to make links: pretend
        symbolic = path
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == path)
    save = TagSave([_job(symbolic, "flac", {"album": "New"})], "x")
    save.run()
    assert save.failed and "link" in save.failed[0].reason and path.read_bytes() == before


# -- the tag history stays small ---------------------------------------------------------------------
def _album(folder: Path, count: int, cover: bytes) -> list[Path]:
    paths = []
    for i in range(count):
        path = folder / f"{i:02d}.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(path, (RNG.standard_normal((4410, 2)) * 2000).astype(np.int16), 44100, format="FLAC")
        audio = FLACFile(path)
        audio["title"] = f"Song {i}"
        audio.add_picture(Picture(type=3, mime="image/jpeg", data=cover))
        audio.save()
        paths.append(path)
    return paths


def _history_bytes() -> int:
    return sum(p.stat().st_size for p in tag_editor.history_folder().rglob("*") if p.is_file())


def _tagged(path: Path, kind: str, title: str, cover: bytes) -> Path:
    """A short file of `kind` with a title and this (front) cover."""
    sound = (RNG.standard_normal((20000, 2)) * 2000).astype(np.int16)
    if kind == "mp3":
        audio = MP3File(mp3_with(path, b""))
        audio.add_tags().add(id3.TextFrame("TIT2", [title]))
        audio.tags.add(id3.PictureFrame("image/jpeg", 3, "", cover))
        audio.save(3)
    elif kind == "mp4":
        write_m4a(path, sound, bits=16)
        audio = tagio.MP4File(path)
        audio["\xa9nam"], audio["covr"] = [title], [tagio.Cover(cover, 13)]
        audio.save()
    else:   # opus
        sf.write(path, sound / 32768, 48000, format="OGG", subtype="OPUS")
        audio = tagio.open_file(path, "opus")
        block = Picture(type=3, mime="image/jpeg", data=cover).render()
        audio["title"], audio["metadata_block_picture"] = title, [base64.b64encode(block).decode("ascii")]
        audio.save()
    return path


@pytest.mark.parametrize("kind", ["flac", "mp3", "mp4", "opus"])
def test_the_history_stores_a_shared_cover_once(tmp_path, monkeypatch, kind):
    cover = bytes(RNG.integers(0, 256, 300_000, np.uint8))
    if kind == "flac":
        paths = _album(tmp_path / "Album", 6, cover)
    else:
        (tmp_path / "Album").mkdir()
        suffix = {"mp3": ".mp3", "mp4": ".m4a", "opus": ".opus"}[kind]
        paths = [_tagged(tmp_path / "Album" / f"{i:02d}{suffix}", kind, f"Song {i}", cover) for i in range(6)]
    written = []
    real = tag_editor._write_json
    monkeypatch.setattr(tag_editor, "_write_json", lambda path, data: (written.append(len(json.dumps(data))),
                                                                       real(path, data)))
    tracks = [library.read_track(p) for p in paths]
    save = TagSave(tag_editor.edit_jobs(tracks, Edit({"genre": "Jazz"})), "genre")
    save.run()
    assert save.saved == paths
    assert _history_bytes() < 2 * len(cover), _history_bytes()        # was ~12 copies, in base64
    assert sum(written) < len(cover), written                           # each rewrite small
    entry = tag_editor.history()[0]
    jobs, changed, gone = tag_editor.undo_jobs(entry)
    assert changed == [] and gone == []
    TagSave(jobs, "undo", undo_of=entry).run()
    for i, path in enumerate(paths):
        assert tag_editor.read_fields(path, kind)["genre"] == "" and tag_editor.read_cover(path, kind) == cover
        assert tag_editor.read_fields(path, kind)["title"] == f"Song {i}"


def test_old_history_entries_are_pruned_with_their_pictures(tmp_path, monkeypatch):
    monkeypatch.setattr(tag_editor, "HISTORY_KEEP", 2)
    monkeypatch.setattr(tag_editor, "BLOB_GRACE_SECONDS", 0)
    covers = [bytes(RNG.integers(0, 256, 20_000, np.uint8)) for _ in range(4)]
    for i, cover in enumerate(covers):
        path = _album(tmp_path / f"A{i}", 1, cover)[0]
        TagSave(tag_editor.edit_jobs([library.read_track(path)], Edit({"album": f"Edit {i}"})), f"edit {i}").run()
    entries = tag_editor.history()
    assert [e["summary"] for e in entries] == ["edit 3", "edit 2"]
    assert _history_bytes() < 2.5 * len(covers[0]) + 20_000
    jobs, _changed, _gone = tag_editor.undo_jobs(entries[1])
    TagSave(jobs, "undo", undo_of=entries[1]).run()
    assert tag_editor.read_fields(tmp_path / "A2" / "00.flac", "flac")["album"] == ""


def test_history_written_before_the_blob_store_still_undoes(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    before = tag_editor.snapshot(path, "flac")
    TagSave(tag_editor.edit_jobs([library.read_track(path)], Edit({"album": "New"})), "x").run()
    entry = tag_editor.history()[0]
    old_style = dict(entry, files=[dict(entry["files"][0], before=before,
                                        after=tag_editor.snapshot(path, "flac"))])
    old_style.pop("file")
    old_style.pop("id")
    (tag_editor.history_folder() / "TagEdit_20200101_000000_000000.json").write_text(
        json.dumps(dict(old_style, id="20200101_000000_000000")), encoding="utf-8")
    old = [e for e in tag_editor.history() if e["id"] == "20200101_000000_000000"][0]
    jobs, changed, _gone = tag_editor.undo_jobs(old)
    assert changed == []
    TagSave(jobs, "undo", undo_of=old).run()
    assert tag_editor.read_fields(path, "flac")["album"] == "Old album"


# -- what the editor writes ----------------------------------------------------------------------------
def test_replacing_the_cover_keeps_the_other_pictures_when_none_is_a_front_cover(tmp_path):
    path = tmp_path / "a.flac"
    sf.write(path, (RNG.standard_normal((4410, 2)) * 2000).astype(np.int16), 44100, format="FLAC")
    audio = FLACFile(path)
    shown, booklet = picture("PNG", (1, 2, 3)), picture("PNG", (4, 5, 6))
    audio.add_picture(Picture(type=0, mime="image/png", data=shown))
    audio.add_picture(Picture(type=0, mime="image/png", data=booklet))
    audio.save()
    new = picture("PNG", (9, 9, 9))
    image = tmp_path / "new.png"
    image.write_bytes(new)
    save = TagSave(tag_editor.edit_jobs([library.read_track(path)], Edit(cover=tag_editor.load_cover(image))), "x")
    save.run()
    assert save.saved == [path]
    assert sorted(tagio.pictures(FLACFile(path))) == sorted([(3, new), (0, booklet)])


def test_numbered_genres_and_year_month_dates_on_id3(tmp_path):
    assert tag_editor.validate({"year": "2004-05"}, kinds={"mp3"})
    assert not tag_editor.validate({"year": "2004-05"}, kinds={"flac"})
    assert not tag_editor.validate({"year": "2004-05-03"}, kinds={"mp3"})
    path = make(tmp_path / "a.mp3", "mp3")
    for typed, shown in (("17", "Rock"), ("(Live) Rock", "(Live) Rock"), ("Jazz; Blues", "Jazz; Blues")):
        save = TagSave(tag_editor.edit_jobs([library.read_track(path)], Edit({"genre": typed})), "x")
        save.run()
        assert save.saved == [path], save.results[0].reason
        assert tag_editor.read_fields(path, "mp3")["genre"] == shown


def test_cover_thumbnails_are_not_cached_by_the_image_bytes(tmp_path):
    artwork._thumbnail_cache.clear()
    path = tmp_path / "a.flac"
    sf.write(path, np.zeros((441, 2), "float32"), 44100, format="FLAC")
    audio = FLACFile(path)
    audio.add_picture(Picture(type=3, mime="image/png", data=picture("PNG", (0, 255, 0), (40, 40))))
    audio.save()
    first = artwork.cover_for(path, 24)
    assert artwork.cover_for(path, 24) is first and first.getpixel((12, 12)) == (0, 255, 0)
    assert not any(isinstance(part, (bytes, bytearray)) for key in artwork._thumbnail_cache for part in key)


# -- the windows ---------------------------------------------------------------------------------------
@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    root.withdraw()
    theme.apply(root)
    yield root
    root.destroy()
    del root
    gc.collect()  # on this thread (Tk objects freed on another thread abort Python)


def _wait(root, until, seconds=20):
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    assert until()


def test_edit_tags_keeps_one_cover_in_memory_not_every_files(root, tmp_path):
    from ironbark.tag_window import TagWindow
    paths = []
    for i in range(3):
        paths += _album(tmp_path / f"A{i}", 1, picture("PNG", (i * 40, 0, 0), (30, 30)))
    tracks = [library.read_track(p) for p in paths]
    tw = TagWindow(root, tracks, {}, on_saved=lambda s: None, busy=lambda: set())
    _wait(root, lambda: str(tw.save_button.cget("state")) == "normal")
    assert not any(isinstance(c, (bytes, bytearray)) for c in tw.covers)
    assert tw.cover_text.cget("text").startswith("(various)")
    tw.close()
    root.update()


def test_undo_reads_the_files_off_the_tk_thread(root, tmp_path, monkeypatch):
    from ironbark.tag_window import TagHistoryWindow
    path = make(tmp_path / "a.flac", "flac")
    TagSave(tag_editor.edit_jobs([library.read_track(path)], Edit({"album": "New"})), "edit").run()
    threads, real = [], tag_editor.undo_jobs

    def recording(entry):
        threads.append(threading.current_thread())
        return real(entry)
    monkeypatch.setattr(tag_editor, "undo_jobs", recording)
    saves = []
    hw = TagHistoryWindow(root, {}, on_saved=saves.append, busy=lambda: set(), confirm=lambda m, d: True)
    _wait(root, lambda: hw.loading is None)   # (the history too is read off the Tk thread)
    hw.undo(hw.entries[0])
    _wait(root, lambda: saves)
    assert threads and threads[0] is not threading.main_thread()
    assert tag_editor.read_fields(path, "flac")["album"] == "Old album"
    hw.close()
    root.update()


def test_a_save_that_raises_still_reports_back(root, tmp_path, monkeypatch):
    from ironbark.tag_window import TagWindow
    path = make(tmp_path / "a.flac", "flac")
    monkeypatch.setattr(TagSave, "run", lambda self, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    saves = []
    tw = TagWindow(root, [library.read_track(path)], {}, on_saved=saves.append, busy=lambda: set(),
                   confirm=lambda m, d: True)
    _wait(root, lambda: str(tw.save_button.cget("state")) == "normal")
    tw.vars["album"].set("New")
    tw.save()
    _wait(root, lambda: saves)
    assert "boom" in tw.status.cget("text") and all(r.state == "failed" for r in saves[0].results)
    tw.close()
    root.update()


def test_a_running_save_can_be_stopped_and_waited_for(root, tmp_path):
    from ironbark.tag_window import _Saving
    paths = [make(tmp_path / f"{i}.flac", "flac") for i in range(3)]
    save = TagSave([_job(p, "flac", {"album": "New"}) for p in paths], "x")
    done = []
    saving = _Saving(root, save, set(), lambda *a: None, done.append)
    saving.stop()
    assert saving.wait(30) and saving.finished
    assert all(r.state in ("saved", "failed") for r in save.results)
    assert not any(tag_editor.temp_path(p).exists() for p in paths)

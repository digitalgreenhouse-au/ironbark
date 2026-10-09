"""Ironbark's own tag reader / writer (0.27.0, ironbark.tagio — replaces mutagen, which is GPL):
each format's container read and written from its published spec, byte-level details (ID3
unsynchronisation, v2.2, v1, compressed frames; Ogg CRC and re-paging; MP4 chunk offsets; FLAC
padding and the CUESHEET block), the sound never touched, and FFmpeg's ffprobe reading back what
was written (skipped when FFmpeg isn't installed). Plus: no mutagen anywhere."""
import base64
import io
import json
import shutil
import struct
import subprocess
import tokenize
import zlib
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mp4_maker import write_m4a
from PIL import Image
from tagging import text, write_id3

from ironbark import audiofile, library, settings, tag_editor, tagio
from ironbark.tagio import genres, id3, mp4, ogg
from ironbark.tagio.flac import CUESHEET, CueSheet, CueSheetIndex, CueSheetTrack, FLACFile
from ironbark.tagio.mpeg import MP3File
from ironbark.tagio.riff import AiffFile, WaveFile
from ironbark.tagio.vorbis import Picture, VComment

ROOT = Path(__file__).resolve().parent.parent
RNG = np.random.default_rng(27)
FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")
needs_ffprobe = pytest.mark.skipif(FFPROBE is None, reason="FFmpeg's ffprobe isn't on PATH")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="FFmpeg isn't on PATH")


@pytest.fixture(autouse=True)
def temp_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")


def noise(frames=20000, channels=2):
    return (RNG.standard_normal((frames, channels)) * 3000).astype(np.int16)


def jpeg(colour=(200, 40, 40), size=(16, 12)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG")
    return buffer.getvalue()


def decoded(path):
    with audiofile.open(path) as f:
        return f.read(dtype="float64", always_2d=True)


def make(path: Path, kind: str) -> Path:
    """A plain (untagged) file of `kind`, made by libsndfile (ALAC: tests/mp4_maker)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "flac":
        sf.write(path, noise(), 44100, format="FLAC", subtype="PCM_16")
    elif kind == "mp3":
        sf.write(path, noise() / 32768, 44100, format="MP3")
    elif kind == "wav":
        sf.write(path, noise(), 44100, format="WAV", subtype="PCM_16")
    elif kind == "aiff":
        sf.write(path, noise(), 44100, format="AIFF", subtype="PCM_16")
    elif kind == "mp4":
        write_m4a(path, noise(), bits=16)
    else:
        rate = 48000 if kind == "opus" else 44100
        sf.write(path, noise() / 32768, rate, format="OGG", subtype="OPUS" if kind == "opus" else "VORBIS")
    return path


SUFFIX = {"flac": ".flac", "mp3": ".mp3", "wav": ".wav", "aiff": ".aiff", "mp4": ".m4a", "ogg": ".ogg",
          "opus": ".opus"}
KINDS = list(SUFFIX)


# -- sniffing -------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_kind_is_worked_out_from_the_contents(tmp_path, kind):
    path = make(tmp_path / f"a{SUFFIX[kind]}", kind)
    misnamed = tmp_path / "really.bin"
    shutil.copy(path, misnamed)
    assert tagio.kind_of(path) == kind and tagio.kind_of(misnamed) == kind
    assert tagio.open_any(misnamed) is not None
    (tmp_path / "junk.mp3").write_bytes(b"\x00junk" * 100)
    assert tagio.kind_of(tmp_path / "junk.mp3") is None and tagio.open_any(tmp_path / "missing.flac") is None


# -- Vorbis comments and pictures ---------------------------------------------------------------
def test_vorbis_comments_round_trip_and_ignore_case():
    tags = VComment("vendor ✓", [("TITLE", "Song"), ("genre", "Rock"), ("Genre", "Pop")])
    data = tags.render()
    again, end = VComment.parse(data)
    assert end == len(data) and again.vendor == "vendor ✓" and again.items == tags.items
    assert again.get("genre") == ["Rock", "Pop"] and "TITLE" in again and again.get("missing") == []
    again.set("GENRE", ["Jazz"])
    assert again.get("genre") == ["Jazz"] and again.names() == ["TITLE", "GENRE"]
    broken = struct.pack("<I", 0) + struct.pack("<I", 2) + struct.pack("<I", 5) + b"novalue" + b"!"
    with pytest.raises(tagio.TagError):
        VComment.parse(broken)


def test_picture_block_layout_is_rfc_9639():
    picture = Picture(type=4, mime="image/png", desc="Back ✓", width=3, height=2, depth=24, colors=0, data=b"\x89PNG")
    block = picture.render()
    assert block[:8] == struct.pack(">II", 4, 9) and block[8:17] == b"image/png"
    assert Picture.parse(block) == picture
    with pytest.raises(tagio.TagError):
        Picture.parse(block[:-2])


# -- FLAC ------------------------------------------------------------------------------------------
def test_flac_streaminfo_figures(tmp_path):
    path = tmp_path / "a.flac"
    sf.write(path, noise(44100), 48000, format="FLAC", subtype="PCM_24")
    info = FLACFile(path).info
    assert (info.sample_rate, info.channels, info.bits_per_sample, info.total_samples) == (48000, 2, 24, 44100)
    assert info.length == pytest.approx(44100 / 48000) and info.md5_signature and info.bitrate > 0


def test_flac_tags_fit_in_the_padding_without_moving_the_audio(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    audio = FLACFile(path)
    audio["title"] = "x"
    audio.save()                       # a first save may add padding
    before = FLACFile(path)
    size, start = path.stat().st_size, before.audio_start
    sound = decoded(path)
    before["title"], before["album"] = "A longer title", "An album"
    before.save()
    after = FLACFile(path)
    assert (path.stat().st_size, after.audio_start) == (size, start)   # in place: the padding shrank
    assert after["title"] == ["A longer title"] and np.array_equal(decoded(path), sound)


def test_flac_outgrowing_the_padding_rebuilds_the_file_with_the_sound_intact(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    md5 = FLACFile(path).info.md5_signature
    sound = decoded(path)
    audio = FLACFile(path)
    audio.add_picture(Picture(type=3, mime="image/jpeg", data=bytes(RNG.integers(0, 256, 200_000, np.uint8))))
    audio.save()
    again = FLACFile(path)
    assert len(again.pictures[0].data) == 200_000 and again.info.md5_signature == md5
    assert np.array_equal(decoded(path), sound)
    assert tag_editor.same_audio(make(tmp_path / "b.flac", "flac"), path, "flac") is not None  # (different noise)


def test_flac_untouched_blocks_are_kept_byte_for_byte(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    data = bytearray(path.read_bytes())
    # Insert an APPLICATION block (type 2) straight after STREAMINFO.
    app = bytes([2]) + (8).to_bytes(3, "big") + b"TEST1234"
    data[4] &= 0x7F                                   # STREAMINFO is no longer the last block
    path.write_bytes(bytes(data[:42]) + app + bytes(data[42:]))
    audio = FLACFile(path)
    audio["title"] = "x"
    audio.save()
    assert (2, b"TEST1234") in FLACFile(path).blocks


def test_flac_with_an_id3_tag_in_front_keeps_it(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    prefix = id3.ID3Tag([text("TIT2", "id3 title")]).render(4, padding=10)
    path.write_bytes(prefix + path.read_bytes())
    assert tagio.kind_of(path) == "flac"
    audio = FLACFile(path)
    audio["title"] = "flac title"
    audio.save()
    assert path.read_bytes().startswith(prefix)
    assert library.read_track(path).title == "flac title"


def test_flac_cuesheet_block_round_trips(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    sheet = CueSheet("0077774600125", 88200, True, [
        CueSheetTrack(1, 0, "USABC8500001", 0, False, [CueSheetIndex(1, 0)]),
        CueSheetTrack(2, 588 * 10, "", 0, True, [CueSheetIndex(0, 0), CueSheetIndex(1, 588)]),
        CueSheetTrack(170, 20000)])
    audio = FLACFile(path)
    audio.cuesheet = sheet
    audio.save()
    assert FLACFile(path).cuesheet == sheet
    block = next(d for k, d in FLACFile(path).blocks if k == CUESHEET)
    assert len(block) == 396 + 2 * 36 + 3 * 12 + 36   # RFC 9639 §8.7: 396 + 36 per track + 12 per index


def test_damaged_flac_files_raise_tag_errors(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    (tmp_path / "cut.flac").write_bytes(path.read_bytes()[:30])
    (tmp_path / "empty.flac").write_bytes(b"")
    (tmp_path / "not.flac").write_bytes(b"RIFF" + bytes(100))
    for name in ("cut.flac", "empty.flac", "not.flac"):
        with pytest.raises(tagio.TagError):
            FLACFile(tmp_path / name)
        assert library.read_track(tmp_path / name).sample_rate == 0   # listed by name, nothing else


# -- ID3 -------------------------------------------------------------------------------------------
def frame23(frame_id: str, body: bytes, flags: bytes = b"\x00\x00") -> bytes:
    return frame_id.encode() + struct.pack(">I", len(body)) + flags + body


def tag(major: int, frames: bytes, flags: int = 0) -> bytes:
    return b"ID3" + bytes([major, 0, flags]) + id3.to_synchsafe(len(frames)) + frames


def test_synchsafe_numbers():
    assert id3.synchsafe(b"\x00\x00\x02\x01") == 257 and id3.to_synchsafe(257) == b"\x00\x00\x02\x01"
    assert id3.synchsafe(b"\x00\x00\x01\x7f") == 255     # the spec's own example: 255 → %00000001 01111111


def test_id3v23_dates_genres_and_unsynchronisation():
    frames = (frame23("TYER", b"\x002004") + frame23("TDAT", b"\x000305") + frame23("TIME", b"\x001020")
              + frame23("TCON", b"\x00(17)(4)Eurodisco") + frame23("TIT2", b"\x00caf\xe9 \xff\xe0"))
    unsynced = frames.replace(b"\xff", b"\xff\x00")       # the scheme in v2.3 §5
    parsed = id3.ID3Tag.parse(tag(3, unsynced, flags=0x80))
    assert parsed.text("TDRC") == ["2004-05-03T10:20"] and "TYER" not in parsed
    assert parsed.genres() == ["Rock", "Disco", "Eurodisco"]
    assert parsed.text("TIT2") == ["café \xff\xe0"]
    v23 = id3.ID3Tag.parse(parsed.render(3))               # written back as v2.3: TYER/TDAT/TIME again
    assert v23.text("TDRC") == ["2004-05-03T10:20"]
    rendered = parsed.render(3)
    assert b"TYER" in rendered and b"TDAT" in rendered and b"TDRC" not in rendered


def test_id3_genre_references():
    assert id3.genres(["(17)"]) == ["Rock"] and id3.genres(["17"]) == ["Rock"]
    assert id3.genres(["(RX)(CR)"]) == ["Remix", "Cover"] and id3.genres(["((Lost)"]) == ["(Lost)"]
    assert id3.genres(["Rock\x00Jazz", "Rock"]) == ["Rock", "Jazz"]
    assert genres.name(14) == "R&B" and genres.name(79) == "Hard Rock" and genres.name(500) == ""
    assert genres.number("rock") == 17


def test_id3v24_frame_flags_compression_and_data_length():
    body = b"\x03" + "Zipped ✓".encode()
    packed = zlib.compress(body)
    data = id3.to_synchsafe(len(body)) + packed           # data length indicator, then the zlib data
    frame = b"TIT2" + id3.to_synchsafe(len(data)) + b"\x00\x09" + data   # k (compressed) + p (length)
    unsync_body = b"\x00a\xff\x00\xe0"                      # an unsynchronised frame (n flag)
    frame2 = b"TALB" + id3.to_synchsafe(len(unsync_body)) + b"\x00\x02" + unsync_body
    parsed = id3.ID3Tag.parse(tag(4, frame + frame2))
    assert parsed.text("TIT2") == ["Zipped ✓"] and parsed.text("TALB") == ["a\xff\xe0"]


def test_id3v24_sizes_written_as_plain_numbers_are_still_read():
    body = b"\x00" + b"x" * 200                            # 201 bytes: 0x000000C9 has the high bit set
    frame = b"TIT2" + struct.pack(">I", len(body)) + b"\x00\x00" + body
    assert id3.ID3Tag.parse(tag(4, frame)).text("TIT2") == ["x" * 200]


def test_id3v22_is_read_and_upgraded():
    def frame22(frame_id, body):
        return frame_id.encode() + len(body).to_bytes(3, "big") + body
    frames = (frame22("TT2", b"\x00Old song") + frame22("TP1", b"\x00Singer") + frame22("TYE", b"\x001988")
              + frame22("COM", b"\x00engshort\x00Long comment") + frame22("PIC", b"\x00JPG\x03desc\x00" + b"\xff\xd8")
              + frame22("XYZ", b"unknown"))
    parsed = id3.ID3Tag.parse(tag(2, frames))
    assert parsed.text("TIT2") == ["Old song"] and parsed.text("TDRC") == ["1988"]
    picture = parsed.get("APIC")
    assert (picture.mime, picture.type, picture.desc, picture.data) == ("image/jpeg", 3, "desc", b"\xff\xd8")
    comment = parsed.get("COMM")
    assert (comment.lang, comment.desc, comment.text) == ("eng", "short", ["Long comment"])
    upgraded = id3.ID3Tag.parse(parsed.render(3))
    assert upgraded.text("TPE1") == ["Singer"] and upgraded.get("APIC").data == b"\xff\xd8"


def test_id3_text_encodings():
    utf16 = b"\x01" + "Ünïcode".encode("utf-16") + b"\x00\x00" + "Zwei".encode("utf-16")
    be = b"\x02" + "Big".encode("utf-16-be")
    frames = tag(4, b"TPE1" + id3.to_synchsafe(len(utf16)) + b"\x00\x00" + utf16
                 + b"TALB" + id3.to_synchsafe(len(be)) + b"\x00\x00" + be)
    parsed = id3.ID3Tag.parse(frames)
    assert parsed.text("TPE1") == ["Ünïcode", "Zwei"] and parsed.text("TALB") == ["Big"]
    v23 = id3.ID3Tag([text("TIT2", "Ünïcode ✓")]).render(3)   # v2.3: no UTF-8, so UTF-16 with a BOM
    assert b"\x01\xff\xfe" in v23
    assert id3.ID3Tag.parse(v23).text("TIT2") == ["Ünïcode ✓"]


def test_id3v1_read_and_kept_up_to_date(tmp_path):
    path = make(tmp_path / "a.mp3", "mp3")
    v1 = (b"TAG" + b"Old title".ljust(30, b"\0") + b"Old artist".ljust(30, b"\0") + b"Album".ljust(30, b"\0")
          + b"1999" + b"A comment".ljust(28, b"\0") + b"\x00\x07" + bytes([17]))
    path.write_bytes(path.read_bytes() + v1)
    audio = MP3File(path)
    assert audio.tags.version == (1, 1, 0)
    assert (audio.tags.text("TIT2"), audio.tags.text("TRCK"), audio.tags.genres()) == (["Old title"], ["7"], ["Rock"])
    track = library.read_track(path)
    assert (track.title, track.year, track.track_no, track.genre) == ("Old title", 1999, 7, "Rock")
    audio.tags.delall("TIT2")
    audio.tags.add(text("TIT2", "New title"))
    audio.save(3)
    data = path.read_bytes()
    assert data.startswith(b"ID3\x03") and data[-128:-125] == b"TAG" and data[-125:-95].startswith(b"New title")
    assert MP3File(path).tags.text("TIT2") == ["New title"]


# -- MP3 audio facts ---------------------------------------------------------------------------------
def test_mp3_lame_header_gives_the_exact_length(tmp_path):
    path = tmp_path / "a.mp3"
    sf.write(path, noise(44100 * 2) / 32768, 44100, format="MP3", bitrate_mode="VARIABLE", compression_level=0.5)
    info = MP3File(path).info
    assert info.length == pytest.approx(2.0, abs=1e-9)    # frames × 1152 − encoder delay − padding
    assert info.bitrate_mode == "VBR" and info.encoder_info.startswith("LAME") and info.encoder_settings == "-V 5"
    assert not info.sketchy and info.sample_rate == 44100 and info.channels == 2


def test_mp3_without_a_header_is_taken_as_constant(tmp_path):
    header = bytes([0xFF, 0xFB, 0x90, 0xC0])              # MPEG-1 Layer III, 128 kbps, 44.1 kHz, mono
    frame = header + bytes(417 - 4)                       # 144 × 128000 ÷ 44100 = 417 bytes
    path = tmp_path / "plain.mp3"
    path.write_bytes(frame * 20)
    info = MP3File(path).info
    assert (info.bitrate, info.bitrate_mode, info.channels) == (128000, "", 1)
    assert info.length == pytest.approx(417 * 20 * 8 / 128000)
    (tmp_path / "junk.mp3").write_bytes(b"\x00junk" * 300)
    with pytest.raises(tagio.TagError):
        MP3File(tmp_path / "junk.mp3")
    lonely = tmp_path / "lonely.mp3"                       # one valid header, then rubbish: flagged
    lonely.write_bytes(b"\x00" * 10 + header + b"\x01" * 2000)
    assert MP3File(lonely).info.sketchy


# -- Ogg ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["ogg", "opus"])
def test_ogg_crc_matches_the_encoder_on_every_page(tmp_path, kind):
    path = make(tmp_path / f"a{SUFFIX[kind]}", kind)
    data = path.read_bytes()
    at = pages = 0
    while at < len(data):
        count = data[at + 26]
        size = 27 + count + sum(data[at + 27: at + 27 + count])
        page = data[at: at + size]
        assert ogg.crc32(page[:22] + b"\0\0\0\0" + page[26:]) == struct.unpack_from("<I", page, 22)[0]
        at += size
        pages += 1
    assert pages > 2


def test_paginate_lacing_rules():
    pages = ogg.paginate([b"a" * 255, b"b" * 600], serial=7, first_sequence=1)
    lacing = [list(p.lacing) for p in pages]
    assert lacing == [[255, 0, 255, 255, 90]]              # 255 bytes end with a 0; 600 = 255 + 255 + 90
    big = ogg.paginate([b"c" * (255 * 300)], serial=7, first_sequence=1)
    assert [len(p.lacing) for p in big] == [255, 46] and big[1].flags == 0x01   # continued on page 2
    assert big[0].granule == -1 and big[1].granule == 0 and [p.sequence for p in big] == [1, 2]


@pytest.mark.parametrize("kind", ["ogg", "opus"])
def test_ogg_comment_growing_over_many_pages_renumbers_the_rest(tmp_path, kind):
    path = make(tmp_path / f"a{SUFFIX[kind]}", kind)
    sound = decoded(path)
    audio = tagio.open_file(path, kind)
    length = audio.length
    pages_before = len(audio.pages)
    big = Picture(type=3, mime="image/jpeg", data=bytes(RNG.integers(0, 256, 150_000, np.uint8)))
    audio["metadata_block_picture"] = [base64.b64encode(big.render()).decode("ascii")]
    audio["title"] = "Grown"
    audio.save()
    again = tagio.open_file(path, kind)
    assert len(again.pages) > pages_before and again["title"] == ["Grown"] and again.length == length
    assert tagio.pictures(again) == [(3, big.data)]
    assert np.array_equal(decoded(path), sound)
    data = path.read_bytes()                              # every page: CRC good, sequence numbers in a row
    at, sequence = 0, 0
    while at < len(data):
        count = data[at + 26]
        size = 27 + count + sum(data[at + 27: at + 27 + count])
        page = data[at: at + size]
        assert ogg.crc32(page[:22] + b"\0\0\0\0" + page[26:]) == struct.unpack_from("<I", page, 22)[0]
        assert struct.unpack_from("<I", page, 18)[0] == sequence
        sequence += 1
        at += size
    shrink = tagio.open_file(path, kind)                   # and back down again
    shrink.tags.delete("metadata_block_picture")
    shrink.save()
    assert len(tagio.open_file(path, kind).pages) == pages_before and np.array_equal(decoded(path), sound)


def test_opus_extra_comment_data_is_kept(tmp_path):
    path = make(tmp_path / "a.opus", "opus")
    audio = tagio.open_file(path, "opus")
    audio.extra = b"\x01binary"
    audio.save()
    audio = tagio.open_file(path, "opus")
    assert audio.extra == b"\x01binary"
    audio["title"] = "x"
    audio.save()
    assert tagio.open_file(path, "opus").extra == b"\x01binary"


# -- MP4 -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("layout", [{}, {"moov_first": True, "per_chunk": 3, "junk": 5}])
def test_mp4_tags_moving_the_audio_fix_the_chunk_offsets(tmp_path, layout):
    data = noise(30000)
    path = write_m4a(tmp_path / "a.m4a", data, bits=16, **layout)
    audio = mp4.MP4File(path)
    assert audio.tags is None                            # no udta at all: made from scratch
    audio["\xa9nam"], audio["trkn"], audio["disk"] = ["Song ✓"], [(3, 12)], [(1, 2)]
    audio["covr"] = [mp4.Cover(jpeg(), mp4.JPEG)]
    audio.save()
    with audiofile.open(path) as f:
        assert np.array_equal(f.read(dtype="int16"), data)
    again = mp4.MP4File(path)
    assert again["\xa9nam"] == ["Song ✓"] and again["trkn"] == [(3, 12)] and again["disk"] == [(1, 2)]
    payload = {i.key: i.data[0].payload for i in again.tags.items}
    assert len(payload["disk"]) == 6 and len(payload["trkn"]) == 8   # as iTunes writes them
    size = path.stat().st_size
    again["\xa9alb"] = ["Small"]                          # small change: the free box absorbs it
    again.save()
    assert path.stat().st_size == size
    with audiofile.open(path) as f:
        assert np.array_equal(f.read(dtype="int16"), data)


def test_mp4_numbered_genre_freeform_and_types(tmp_path):
    path = write_m4a(tmp_path / "a.m4a", noise(5000), bits=16)
    audio = mp4.MP4File(path)
    audio["gnre"] = [18]                                  # ID3v1 number + 1: 17 = Rock
    audio["----:com.apple.iTunes:MQAENCODER"] = [mp4.FreeForm(b"enc")]
    audio["cpil"], audio["tmpo"] = [True], [120]
    audio.save()
    again = mp4.MP4File(path)
    assert library.mp4_genres(again.tags) == ["Rock"] and library.read_track(path).genre == "Rock"
    assert again["cpil"] == [True] and again["tmpo"] == [120]
    assert tagio.text_values(again)["MQAENCODER"] == ["enc"]
    assert tag_editor.read_fields(path, "mp4")["genre"] == "Rock"


# -- WAV / AIFF -----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind,cls", [("wav", WaveFile), ("aiff", AiffFile)])
def test_riff_and_aiff_id3_chunk(tmp_path, kind, cls):
    path = make(tmp_path / f"a{SUFFIX[kind]}", kind)
    sound = decoded(path)
    write_id3(path, [text("TIT2", "Odd" * 3)], version=3)  # an odd-sized chunk gets a pad byte
    data = path.read_bytes()
    order = "<I" if kind == "wav" else ">I"
    assert struct.unpack(order, data[4:8])[0] == len(data) - 8 and len(data) % 2 == 0
    assert [c[0] for c in cls(path).chunks][-1] == (b"id3 " if kind == "wav" else b"ID3 ")
    assert cls(path).tags.text("TIT2") == ["OddOddOdd"] and np.array_equal(decoded(path), sound)
    audio = cls(path)
    audio.delete()
    assert cls(path).tags is None and np.array_equal(decoded(path), sound)
    assert struct.unpack(order, path.read_bytes()[4:8])[0] == path.stat().st_size - 8


# -- the tag editor, checked by other software -----------------------------------------------------
NEW = {"title": "New title ✓", "artist": "New artist", "album_artist": "Band", "album": "New album",
       "track": "3/12", "disc": "1/2", "year": "2004", "genre": "Jazz", "composer": "Mingus",
       "comment": "Remastered"}
FFPROBE_KEYS = {"title": "title", "artist": "artist", "album": "album", "album_artist": "album_artist",
                "composer": "composer", "genre": "genre"}


def ffprobe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    data = json.loads(out.stdout)
    tags = {k.lower(): v for k, v in (data.get("format", {}).get("tags") or {}).items()}
    for stream in data.get("streams", []):           # Ogg keeps the comments on the audio stream
        if stream.get("codec_type") == "audio":
            tags.update({k.lower(): v for k, v in (stream.get("tags") or {}).items()})
    covers = [s for s in data.get("streams", []) if s.get("disposition", {}).get("attached_pic")]
    return tags, covers


@needs_ffprobe
@pytest.mark.parametrize("kind", KINDS)
def test_written_tags_and_cover_are_read_by_ffprobe(tmp_path, kind):
    path = make(tmp_path / "Album" / f"a{SUFFIX[kind]}", kind)
    sound = decoded(path)
    cover = tag_editor.Cover(jpeg((10, 120, 230)), "image/jpeg", 16, 12)
    save = tag_editor.TagSave(tag_editor.edit_jobs([library.read_track(path)], tag_editor.Edit(dict(NEW), cover)), "x")
    save.run()
    assert save.saved == [path], save.results
    tags, covers = ffprobe(path)
    for field, key in FFPROBE_KEYS.items():
        assert tags.get(key) == NEW[field], (field, tags)
    assert tags.get("track", "").split("/")[0] == "3" and tags.get("date", "").startswith("2004")
    assert len(covers) == 1 and covers[0]["codec_name"] == "mjpeg"
    assert np.array_equal(decoded(path), sound)


@needs_ffmpeg
@pytest.mark.parametrize("kind,args", [("flac", []), ("mp3", ["-c:a", "libmp3lame"]),
                                       ("mp3", ["-c:a", "libmp3lame", "-id3v2_version", "3"]),
                                       ("aiff", ["-write_id3v2", "1"]), ("mp4", ["-c:a", "alac"]),
                                       ("ogg", ["-c:a", "libvorbis"]), ("opus", ["-c:a", "libopus"])])
def test_ffmpeg_made_tags_are_read(tmp_path, kind, args):
    source = make(tmp_path / "src.wav", "wav")
    out = tmp_path / f"b{SUFFIX[kind]}"
    meta = ["-metadata", "title=FF title", "-metadata", "artist=FF artist", "-metadata", "album=FF album",
            "-metadata", "track=4/9", "-metadata", "genre=Soul", "-metadata", "date=1971"]
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(source), *args, *meta, str(out)], check=True)
    track = library.read_track(out)
    assert (track.title, track.artist, track.album, track.track_no, track.genre, track.year) == (
        "FF title", "FF artist", "FF album", 4, "Soul", 1971)


# -- no GPL code -----------------------------------------------------------------------------------
def test_mutagen_is_not_used_anywhere():
    """mutagen (GPL-2.0-or-later) left in 0.27.0: no code in ironbark/ or tests/ names it
    (comments and strings may, to explain the history), and it's not a dependency."""
    used = []
    for folder in ("ironbark", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            with open(path, "rb") as f:
                for token in tokenize.tokenize(f.readline):
                    if token.type == tokenize.NAME and token.string == "mutagen":
                        used.append(f"{path.name}:{token.start[0]}")
    assert used == []
    assert "mutagen" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = ROOT / "uv.lock"
    if lock.exists():
        assert "mutagen" not in lock.read_text(encoding="utf-8")
    import importlib.util
    assert importlib.util.find_spec("mutagen") is None, "mutagen is still installed in this environment"


def test_a_damaged_id3_tag_is_never_rewritten(tmp_path):
    path = make(tmp_path / "a.mp3", "mp3")
    # An ID3v2.3 tag whose extended header claims 256 bytes inside a 20-byte tag.
    bad = b"ID3\x03\x00\x40" + id3.to_synchsafe(20) + b"\x00\x00\x01\x00" + bytes(16)
    path.write_bytes(bad + path.read_bytes())
    audio = MP3File(path)
    assert audio.tags is None and audio.damaged_tag and audio.info.sample_rate == 44100
    with pytest.raises(tagio.TagError):
        audio.save(3)
    assert path.read_bytes().startswith(bad)

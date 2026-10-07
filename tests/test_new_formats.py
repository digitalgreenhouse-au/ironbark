"""AIFF, Apple Lossless (.m4a), Ogg Vorbis and Ogg Opus (0.14.0): listed, tagged, labelled,
decoded through the one decode path (audiofile.open), seeked exactly. AAC and FLAC-in-Ogg are
recognised and refused clearly."""
import base64
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mp4_maker import write_aac_m4a, write_m4a
from tagging import text, write_id3, write_vorbis
from ironbark.tagio import id3
from ironbark.tagio.mp4 import PNG as PNG_TYPE
from ironbark.tagio.mp4 import Cover, MP4File
from ironbark.tagio.vorbis import Picture
from ironbark import analysis, artwork, audiofile, library, m4a
from ironbark.details import describe_analysis

RNG = np.random.default_rng(9)
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def ints(frames, bits=16, channels=2):
    """Integer samples (as int16 / int32) so every writer stores exactly the same values."""
    if bits == 16:
        return (RNG.standard_normal((frames, channels)) * 3000).astype(np.int16)
    return ((RNG.standard_normal((frames, channels)) * 3000).astype(np.int32) << 16)


def tone(frames, rate=44100):
    t = np.arange(frames) / rate
    return np.stack([0.4 * np.sin(2 * np.pi * 440 * t), 0.3 * np.sin(2 * np.pi * 660 * t)], axis=1)


# -- Apple Lossless ---------------------------------------------------------------------
@pytest.mark.parametrize("bits", [16, 24])
@pytest.mark.parametrize("layout", [{}, {"moov_first": True, "per_chunk": 3, "junk": 17}])
def test_alac_decodes_to_exactly_the_original_samples(tmp_path, bits, layout):
    data = ints(50003, 16 if bits == 16 else 32)
    path = write_m4a(tmp_path / "song.m4a", data, bits=bits, **layout)
    want = data.astype(np.int32) << 16 if bits == 16 else data
    with audiofile.open(path) as f:
        assert f.subtype == f"ALAC_{bits}" and f.frames == 50003 and f.samplerate == 44100
        assert np.array_equal(f.read(dtype="int32"), want)
        for spot in (0, 4095, 4096, 33333, 50002):  # seeks land on the exact sample
            f.seek(spot)
            assert np.array_equal(f.read(700, dtype="int32"), want[spot:spot + 700])


def test_alac_listed_tagged_and_labelled_lossless(tmp_path):
    data = ints(30000)
    path = write_m4a(tmp_path / "03 Song.m4a", data, bits=16)
    tags = MP4File(path)
    tags["\xa9nam"], tags["\xa9ART"], tags["\xa9alb"] = ["Lark"], ["Joni"], ["Court"]
    tags["trkn"], tags["disk"] = [(3, 10)], [(1, 2)]
    tags["covr"] = [Cover(PNG, PNG_TYPE)]
    tags.save()
    track = library.read_track(path)
    assert (track.codec, track.bits, track.sample_rate, track.channels) == ("ALAC", 16, 44100, 2)
    assert (track.title, track.artist, track.album, track.track_no, track.disc_no) == ("Lark", "Joni", "Court", 3, 1)
    assert not track.lossy and not track.unsupported and not track.misnamed
    assert track.format_line.startswith("ALAC · 16-bit / 44.1 kHz · stereo")
    assert track.duration == pytest.approx(30000 / 44100, abs=1e-3)
    assert artwork.embedded_art(path) == PNG
    with audiofile.open(path) as f:  # writing the tags moved the audio (moov grew): still exact
        assert np.array_equal(f.read(dtype="int16"), data)
    assert [t.codec for t in library.scan_album(tmp_path)] == ["ALAC"]


def test_alac_check_says_no_fingerprint(tmp_path):
    path = write_m4a(tmp_path / "a.m4a", ints(44100), bits=16)
    result = analysis.analyse(path, 16, 0)
    assert result.bits_used == 16 and not result.lossy
    assert describe_analysis(result, "ALAC")[0][1].startswith("Not applicable: Apple Lossless")


def test_aac_m4a_is_recognised_and_refused(tmp_path):
    path = write_aac_m4a(tmp_path / "aac.m4a")
    track = library.read_track(path)
    assert track.codec == "AAC" and track.lossy and "AAC" in track.unsupported
    assert "can't be played" in track.format_line
    with pytest.raises(audiofile.Unsupported):
        audiofile.open(path)
    with pytest.raises(m4a.NotAlac) as caught:
        m4a.read_index(path)
    assert caught.value.codec == "mp4a"


def test_damaged_mp4_index_is_a_clean_refusal(tmp_path):
    path = write_m4a(tmp_path / "a.m4a", ints(9000), bits=16)
    data = bytearray(path.read_bytes())
    at = data.find(b"stsz")
    data[at + 12: at + 16] = (10 ** 6).to_bytes(4, "big")  # claims a million packets
    path.write_bytes(bytes(data))
    with pytest.raises(audiofile.Unsupported):
        audiofile.open(path)
    assert library.read_track(path).unsupported


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg not installed (an extra, independent check)")
def test_alac_made_by_ffmpeg(tmp_path):
    source = tmp_path / "src.flac"
    data = ints(70001, 32)
    sf.write(source, data, 48000, subtype="PCM_24")
    out = tmp_path / "ff.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-c:a", "alac", str(out)], check=True)
    with audiofile.open(out) as f, sf.SoundFile(source) as ref:
        assert f.frames == ref.frames and f.samplerate == 48000
        assert np.array_equal(f.read(dtype="int32"), ref.read(dtype="int32"))


# -- AIFF ----------------------------------------------------------------------------------
@pytest.mark.parametrize("bits,suffix", [(16, ".aiff"), (24, ".aif")])
def test_aiff_listed_tagged_and_exact(tmp_path, bits, suffix):
    data = ints(20000, bits if bits == 16 else 32)
    path = tmp_path / f"01 Tune{suffix}"
    sf.write(path, data, 96000, format="AIFF", subtype=f"PCM_{bits}")
    write_id3(path, [text("TIT2", "Tune"), text("TPE1", "Band"), text("TALB", "Record"), text("TRCK", "1/9"),
                     id3.PictureFrame("image/png", 3, "", PNG)])
    track = library.read_track(path)
    assert (track.codec, track.bits, track.sample_rate) == ("AIFF", bits, 96000)
    assert (track.title, track.artist, track.album, track.track_no) == ("Tune", "Band", "Record", 1)
    assert track.bitrate == 96000 * bits * 2 and not track.lossy and not track.misnamed
    assert artwork.embedded_art(path) == PNG
    with audiofile.open(path) as f:
        want = data.astype(np.int32) << (16 if bits == 16 else 0)
        assert np.array_equal(f.read(dtype="int32"), want)
    assert describe_analysis(analysis.analyse(path, bits, 0), "AIFF")[0][1].startswith("Not applicable: AIFF")


def test_aiff_named_wav_is_flagged(tmp_path):
    path = tmp_path / "really.wav"
    sf.write(path, ints(1000), 44100, format="AIFF", subtype="PCM_16")
    track = library.read_track(path)
    assert track.codec == "AIFF" and track.misnamed == ".wav"


# -- Ogg Vorbis / Opus -------------------------------------------------------------------------
def write_ogg(path, subtype, frames=60000, rate=None, tags=None):
    rate = rate or (48000 if subtype == "OPUS" else 44100)
    sf.write(path, tone(frames, rate), rate, format="OGG", subtype=subtype)
    if tags:
        write_vorbis(path, **tags)
    return path


def test_vorbis_listed_tagged_and_labelled_lossy(tmp_path):
    picture = Picture()
    picture.type, picture.mime, picture.data = 3, "image/png", PNG
    path = write_ogg(tmp_path / "02 x.ogg", "VORBIS", tags={
        "title": "Song", "artist": "Singer", "album": "LP", "tracknumber": "2",
        "metadata_block_picture": base64.b64encode(picture.render()).decode("ascii")})
    track = library.read_track(path)
    assert (track.codec, track.title, track.artist, track.album, track.track_no) == ("Vorbis", "Song", "Singer",
                                                                                     "LP", 2)
    assert track.lossy and track.sample_rate == 44100 and track.duration == pytest.approx(60000 / 44100)
    assert track.format_line.endswith("lossy") and "Vorbis" in track.format_line
    assert artwork.embedded_art(path) == PNG
    with audiofile.open(path) as f:
        assert audiofile.is_lossy(f)


def test_vorbis_seeks_land_on_the_exact_sample(tmp_path):
    """libsndfile 1.2.2's own Vorbis seek often lands thousands of samples out; audiofile reads
    to the spot instead, so playback and the proof's second decode always agree."""
    path = write_ogg(tmp_path / "v.ogg", "VORBIS", frames=150000)
    with audiofile.open(path) as f:
        whole = f.read(dtype="float32")
    spots = np.random.default_rng(3).integers(0, 147000, 40)
    with audiofile.open(path) as f:
        for spot in spots:
            assert f.seek(int(spot)) == spot and f.tell() == spot
            assert np.array_equal(f.read(2500, dtype="float32"), whole[spot:spot + 2500])


def test_opus_plays_at_48k_whole_length_exact_seeks(tmp_path):
    path = write_ogg(tmp_path / "o.opus", "OPUS", frames=96000, tags={"title": "Op"})
    info = audiofile.info(path)
    assert info.samplerate == 48000 and info.frames == 96000  # the encoder's pre-skip is taken off
    track = library.read_track(path)
    assert track.codec == "Opus" and track.lossy and track.title == "Op" and not track.misnamed
    assert dict(track.details)["Plays at"].startswith("48 kHz")
    with audiofile.open(path) as f:
        whole = f.read(dtype="float32")
        for spot in (0, 12345, 70000):
            f.seek(spot)
            assert np.array_equal(f.read(2000, dtype="float32"), whole[spot:spot + 2000])
    assert analysis.analyse(path, 0, 0).lossy


def test_opus_original_rate_read_from_its_header(tmp_path):
    path = tmp_path / "head.opus"
    # An Ogg page with an OpusHead packet: version 1, 2 channels, pre-skip 312, input rate 44100.
    head = b"OpusHead" + bytes([1, 2]) + (312).to_bytes(2, "little") + (44100).to_bytes(4, "little") + b"\0\0\0"
    path.write_bytes(b"OggS" + bytes(23) + bytes([len(head)]) + head)
    assert library.opus_input_rate(path) == 44100
    assert library.opus_input_rate(tmp_path / "missing.opus") == 0


def test_ogg_misnaming(tmp_path):
    opus_as_ogg = write_ogg(tmp_path / "a.ogg", "OPUS")
    assert not library.read_track(opus_as_ogg).misnamed      # still an Ogg file: fine
    vorbis_as_flac = write_ogg(tmp_path / "b.flac", "VORBIS")
    track = library.read_track(vorbis_as_flac)
    assert track.codec == "Vorbis" and track.misnamed == ".flac"


def fake_ogg_flac(path):
    """The first Ogg page of a FLAC-in-Ogg stream ("\\x7fFLAC" packet): enough to recognise."""
    packet = b"\x7fFLAC\x01\x00\x00\x01fLaC" + bytes(38)
    path.write_bytes(b"OggS" + bytes(23) + bytes([len(packet)]) + packet)
    return path


def test_ogg_flac_recognised_and_refused(tmp_path):
    path = fake_ogg_flac(tmp_path / "x.oga")
    assert audiofile.sniff(path) == "oggflac"
    track = library.read_track(path)
    assert track.codec == "Ogg FLAC" and track.unsupported and "can't be played" in track.format_line
    with pytest.raises(audiofile.Unsupported):
        audiofile.open(path)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg not installed (an extra, independent check)")
def test_real_ogg_flac_from_ffmpeg_is_refused(tmp_path):
    source = tmp_path / "s.flac"
    sf.write(source, ints(5000), 44100, subtype="PCM_16")
    out = tmp_path / "s.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-c:a", "flac", "-f", "ogg", str(out)],
                   check=True)
    assert library.read_track(out).codec == "Ogg FLAC"
    with pytest.raises(audiofile.Unsupported):
        audiofile.open(out)


def test_new_formats_in_the_library(tmp_path):
    album = tmp_path / "Mixed"
    album.mkdir()
    sf.write(album / "1.aiff", ints(4410), 44100, format="AIFF", subtype="PCM_16")
    write_m4a(album / "2.m4a", ints(4410), bits=16)
    write_ogg(album / "3.ogg", "VORBIS", frames=4410)
    write_ogg(album / "4.opus", "OPUS", frames=4800)
    (album / "notes.txt").write_text("hi")
    tracks = library.scan_album(album)
    assert [t.codec for t in tracks] == ["AIFF", "ALAC", "Vorbis", "Opus"]
    found = library.find_albums(tmp_path)
    assert found[0].track_count == 4 and found[0].quality == "mixed"
    ogg_only = tmp_path / "Ogg"
    ogg_only.mkdir()
    write_ogg(ogg_only / "1.ogg", "VORBIS", frames=4410)
    assert next(a for a in library.find_albums(tmp_path) if a.name == "Ogg").quality == "Vorbis"


def test_engine_plays_every_new_format_through_the_same_decode(tmp_path, monkeypatch):
    """The engine's _Source opens files through audiofile.open — the proof's second decode does
    too, so the two always agree."""
    from ironbark import engine, proof

    opened = []
    real_open = audiofile.open
    # (the engine opens a LocalTrack: the file itself, unless it's a network song's cached copy)
    monkeypatch.setattr(audiofile, "open",
                        lambda p, **kw: opened.append(Path(str(getattr(p, "source", p)))) or real_open(p, **kw))
    path = write_m4a(tmp_path / "a.m4a", ints(9000), bits=16)
    source = engine._Source(path)
    assert source.bits == 16 and source.total_frames == 9000
    record = proof.SentRecord(path, 44100, "int32", 4096)
    block = source.file.read(4096, dtype="int32", always_2d=True)
    record.add(0, 0, block, 1.0)
    with real_open(path) as reference:
        record.check(reference)
    assert record.summary().complete
    source.close()
    assert opened == [path]

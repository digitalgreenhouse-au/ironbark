"""Tag editing (0.16.0, behind Experimental "Tag editing"): every format round-trips, the sound is
proved untouched (a difference stops the save, original kept), the old tags go to the history
first and undo restores them exactly, "(various)" fields are left alone, and a file that's in use
waits rather than being forced. All on files made in a temp folder."""
import base64
import io
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mp4_maker import write_m4a
from PIL import Image
from tagging import text, write_id3

from ironbark import artwork, audiofile, experiments, library, settings, tag_editor, tagio
from ironbark.tag_editor import VARIOUS, Edit, TagSave
from ironbark.tagio import id3
from ironbark.tagio import mp4 as mp4tags
from ironbark.tagio.flac import FLACFile
from ironbark.tagio.mpeg import MP3File
from ironbark.tagio.vorbis import Picture

RNG = np.random.default_rng(11)


def picture(kind="PNG", colour=(200, 30, 30), size=(8, 6)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, kind)
    return buffer.getvalue()


OLD_COVER = picture("PNG", (10, 10, 200))


@pytest.fixture(autouse=True)
def temp_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")


def noise(frames=30000, channels=2):
    return (RNG.standard_normal((frames, channels)) * 3000).astype(np.int16)


def make(path: Path, kind: str) -> Path:
    """A short file of `kind` with tags (incl. one the editor never touches) and a cover."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "flac":
        sf.write(path, noise(), 44100, format="FLAC", subtype="PCM_16")
        audio = FLACFile(path)
        audio["title"], audio["artist"], audio["album"] = "Old title", "Old artist", "Old album"
        audio["tracknumber"], audio["genre"], audio["replaygain_track_gain"] = "1", ["Rock", "Pop"], "-3.1 dB"
        audio.add_picture(Picture(type=3, mime="image/png", data=OLD_COVER))
        audio.add_picture(Picture(type=4, mime="image/png", data=picture("PNG", (0, 255, 0))))
        audio.save()
    elif kind in ("mp3", "wav", "aiff"):
        if kind == "mp3":
            sf.write(path, noise() / 32768, 44100, format="MP3", subtype="MPEG_LAYER_III")
        elif kind == "wav":
            sf.write(path, noise(), 44100, format="WAV", subtype="PCM_16")
        else:
            sf.write(path, noise(), 44100, format="AIFF", subtype="PCM_16")
        write_id3(path, [text("TIT2", "Old title"), text("TPE1", "Old artist"), text("TALB", "Old album"),
                         text("TRCK", "1"), text("TDRC", "1999"),
                         id3.UserTextFrame("REPLAYGAIN_TRACK_GAIN", ["-3.1 dB"]),
                         id3.CommentFrame("iTunNORM", ["0000"], "eng"),
                         id3.PictureFrame("image/png", 3, "", OLD_COVER)], version=3)
    elif kind == "mp4":
        write_m4a(path, noise(), bits=16)
        audio = tagio.MP4File(path)
        audio["\xa9nam"], audio["\xa9ART"], audio["\xa9alb"] = ["Old title"], ["Old artist"], ["Old album"]
        audio["trkn"], audio["cpil"], audio["tmpo"] = [(1, 9)], [False], [120]
        audio["----:com.apple.iTunes:replaygain_track_gain"] = [mp4tags.FreeForm(b"-3.1 dB")]
        audio["covr"] = [mp4tags.Cover(OLD_COVER, mp4tags.PNG)]
        audio.save()
    else:  # ogg / opus
        rate = 48000 if kind == "opus" else 44100
        sf.write(path, noise(20000) / 32768, rate, format="OGG", subtype="OPUS" if kind == "opus" else "VORBIS")
        audio = tagio.open_file(path, kind)
        old = Picture(type=3, mime="image/png", data=OLD_COVER)
        audio["title"], audio["artist"], audio["album"] = "Old title", "Old artist", "Old album"
        audio["replaygain_track_gain"] = "-3.1 dB"
        audio["metadata_block_picture"] = [base64.b64encode(old.render()).decode("ascii")]
        audio.save()
    return path


FORMATS = [("flac", "a.flac"), ("mp3", "a.mp3"), ("wav", "a.wav"), ("aiff", "a.aiff"), ("mp4", "a.m4a"),
           ("ogg", "a.ogg"), ("opus", "a.opus")]


def decoded(path):
    with audiofile.open(path) as f:
        return f.read(dtype="float64", always_2d=True)


def run(tracks, edit, **kwargs):
    save = TagSave(tag_editor.edit_jobs(tracks, edit), edit.describe())
    save.run(**kwargs)
    return save


NEW = {"title": "New title", "artist": "New artist", "album_artist": "Band", "album": "New album",
       "track": "3/12", "disc": "1/2", "year": "2004", "genre": "Jazz; Blues", "composer": "Mingus",
       "comment": "Remastered"}


@pytest.mark.parametrize("kind,name", FORMATS)
def test_every_format_round_trips_with_the_sound_untouched(tmp_path, kind, name):
    path = make(tmp_path / "Album" / name, kind)
    track = library.read_track(path)
    assert tag_editor.kind_of(track) == kind and tag_editor.why_read_only(track) == ""
    sound = decoded(path)
    cover = tag_editor.Cover(picture("JPEG", (250, 200, 10)), "image/jpeg", 8, 6)
    save = run([track], Edit(dict(NEW), cover))
    assert [r.state for r in save.results] == ["saved"], save.results
    assert tag_editor.read_fields(path, kind) == NEW
    assert np.array_equal(decoded(path), sound)            # sample for sample
    assert artwork.embedded_art(path) == cover.data        # what the player shows
    again = library.read_track(path)
    assert (again.title, again.artist, again.album, again.track_no, again.disc_no, again.year, again.genre,
            again.composer, again.album_artist) == ("New title", "New artist", "New album", 3, 1, 2004,
                                                     "Jazz; Blues", "Mingus", "Band")
    values = tagio.text_values(tag_editor._open(path, kind))  # a tag the editor never touches is still there
    assert values["REPLAYGAIN_TRACK_GAIN"] == ["-3.1 dB"]
    assert not tag_editor.temp_path(path).exists()


def test_saves_are_named_in_the_order_they_were_made_across_a_second(monkeypatch):
    # (the seconds and the microseconds come from one reading of the clock)
    second = 1_760_000_000 * 1_000_000_000
    readings = iter([second - 1_000, second + 1_000])   # 1 µs before a second turns over, 1 µs after
    monkeypatch.setattr(tag_editor.time, "time_ns", lambda: next(readings))
    first, then = TagSave([], "First"), TagSave([], "Then")
    assert first.id < then.id and first.id.endswith("_999999") and then.id.endswith("_000001")


@pytest.mark.parametrize("kind,name", FORMATS)
def test_undo_restores_exactly(tmp_path, kind, name):
    path = make(tmp_path / "Album" / name, kind)
    before = tag_editor.snapshot(path, kind)
    sound = decoded(path)
    save = run([library.read_track(path)], Edit(dict(NEW), tag_editor.REMOVE_COVER))
    assert save.saved == [path] and artwork.embedded_art(path) is None
    entries = tag_editor.history()
    assert len(entries) == 1 and entries[0]["files"][0]["before"] == before
    jobs, changed, gone = tag_editor.undo_jobs(entries[0])
    assert changed == [] and gone == []
    undo = TagSave(jobs, "Undo", undo_of=entries[0])
    undo.run()
    assert undo.saved == [path], undo.results
    assert tag_editor.snapshot(path, kind) == before
    assert np.array_equal(decoded(path), sound)
    assert tag_editor.history()[1]["undone_by"] == undo.id  # the edit is marked as undone


def test_flac_keeps_the_back_cover_and_mp3_keeps_its_id3_version(tmp_path):
    flac = make(tmp_path / "a.flac", "flac")
    mp3 = make(tmp_path / "a.mp3", "mp3")
    cover = tag_editor.Cover(picture("JPEG"), "image/jpeg", 8, 6)
    run([library.read_track(flac), library.read_track(mp3)], Edit({"album": "X"}, cover))
    assert sorted(p.type for p in FLACFile(flac).pictures) == [3, 4]
    assert MP3File(mp3).tags.version == (2, 3, 0)
    comments = {c.desc for c in MP3File(mp3).tags.getall("COMM")}
    assert comments == {"iTunNORM"}  # other comments are left alone
    run([library.read_track(flac)], Edit({}, tag_editor.REMOVE_COVER))
    assert FLACFile(flac).pictures == []  # removing the cover takes every picture (any left would show instead)


def test_clearing_a_wav_field_hides_its_riff_info_value(tmp_path):
    path = tmp_path / "a.wav"
    with sf.SoundFile(path, "w", 44100, 2, "PCM_16", format="WAV") as f:
        f.title = "Info title"
        f.write(noise(2000))
    assert library.read_track(path).title == "Info title"
    assert tag_editor.read_fields(path, "wav")["title"] == "Info title"
    run([library.read_track(path)], Edit({"title": ""}))
    assert tag_editor.read_fields(path, "wav")["title"] == ""
    assert library.read_track(path).title == "a"  # back to the file name, as for any untitled file


def test_various_fields_are_left_alone_unless_typed_over(tmp_path):
    a = make(tmp_path / "Album" / "01.flac", "flac")
    b = make(tmp_path / "Album" / "02.flac", "flac")
    audio = FLACFile(b)
    audio["title"], audio["tracknumber"] = "Second", "2"
    audio.save()
    values = [tag_editor.read_fields(p, "flac") for p in (a, b)]
    shown = tag_editor.common(values)
    assert shown["title"] == VARIOUS and shown["track"] == VARIOUS and shown["album"] == "Old album"
    typed = dict(shown, album="Better album")
    assert tag_editor.changes(shown, typed) == {"album": "Better album"}
    assert tag_editor.changes(shown, dict(shown, title="")) == {"title": ""}        # typed over: cleared
    assert tag_editor.changes(shown, dict(shown, album=" Old album ")) == {}        # nothing really changed
    run([library.read_track(a), library.read_track(b)], Edit(tag_editor.changes(shown, typed)))
    assert [tag_editor.read_fields(p, "flac")["title"] for p in (a, b)] == ["Old title", "Second"]
    assert [tag_editor.read_fields(p, "flac")["album"] for p in (a, b)] == ["Better album"] * 2


def test_validation_and_the_description():
    assert tag_editor.validate({"track": "3/12", "disc": "1", "year": "2004-05-03"}) == []
    assert len(tag_editor.validate({"track": "three", "year": "about 2004"})) == 2
    assert Edit({"album": "x", "year": "2004"}, tag_editor.REMOVE_COVER).describe() == \
        "Album, Year and removing the cover"
    assert not Edit()


def test_a_difference_in_the_sound_stops_the_save(tmp_path, monkeypatch):
    path = make(tmp_path / "a.flac", "flac")
    original = path.read_bytes()
    monkeypatch.setattr(tag_editor, "same_audio", lambda a, b, kind, cancel=None: "the sound came out different at 0:01")
    save = run([library.read_track(path)], Edit({"album": "X"}))
    assert save.results[0].state == "failed" and "different at 0:01" in save.results[0].reason
    assert path.read_bytes() == original and not tag_editor.temp_path(path).exists()
    assert tag_editor.history()[0]["files"][0]["saved"] is False


def test_same_audio_finds_one_changed_sample(tmp_path):
    data = noise(50000)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(a, data, 44100, subtype="PCM_16")
    data[44100 * 1 + 7, 1] += 1
    sf.write(b, data, 44100, subtype="PCM_16")
    assert tag_editor.same_audio(a, a, "wav") is None
    assert tag_editor.same_audio(a, b, "wav") == "the sound came out different at 0:01"


def test_the_sound_check_reads_the_files_themselves_even_with_the_network_cache_on(tmp_path, monkeypatch):
    data = noise(30000)
    original = tmp_path / "a.wav"
    sf.write(original, data, 44100, subtype="PCM_16")
    copy = tag_editor.temp_path(original)
    shutil.copyfile(original, copy)
    asked = []

    def resolver(path, cancel=None, wait=None):   # (the cache, as it answers for a song on a slow network)
        asked.append(path)
        raise audiofile.NotCached("it took too long to download", 0.1, "timeout")

    monkeypatch.setattr(audiofile, "_resolver", resolver)
    assert tag_editor.same_audio(original, copy, "wav") is None
    assert asked == []        # never a download of the song, or of the temporary copy, into the cache


def test_a_flac_whose_sound_doesnt_match_its_md5_is_left_alone(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    data = bytearray(path.read_bytes())
    at = data.index(b"fLaC") + 4 + 4 + 18  # STREAMINFO: the MD5 is its last 16 bytes
    data[at] ^= 0xFF
    path.write_bytes(bytes(data))
    original = path.read_bytes()
    save = run([library.read_track(path)], Edit({"album": "X"}))
    assert save.results[0].state == "failed" and "fingerprint (MD5)" in save.results[0].reason
    assert path.read_bytes() == original


def test_a_file_in_use_waits_and_is_never_forced(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    track = library.read_track(path)
    playing = {path}
    save = run([track], Edit({"album": "Later"}), busy=lambda p: p in playing)
    assert save.waiting == [path] and save.results[0].reason.startswith("playing")
    assert tag_editor.read_fields(path, "flac")["album"] == "Old album"
    assert tag_editor.temp_path(path).exists()       # checked already, ready to swap in
    playing.clear()
    if sys.platform == "win32":  # held open by another reader: Windows refuses the swap
        held = open(path, "rb")
        assert save.retry() == [] and save.waiting == [path]
        held.close()
    assert save.retry() == [path] and save.waiting == []
    assert tag_editor.read_fields(path, "flac")["album"] == "Later"
    assert tag_editor.history()[0]["files"][0]["saved"] is True


def test_a_waiting_file_that_changes_meanwhile_is_not_saved(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    save = run([library.read_track(path)], Edit({"album": "Later"}), busy=lambda p: True)
    audio = FLACFile(path)
    audio["album"] = "Someone else's edit"
    audio.save()
    assert save.retry() == [] and save.failed[0].reason.startswith("the file changed")
    assert tag_editor.read_fields(path, "flac")["album"] == "Someone else's edit"
    assert not tag_editor.temp_path(path).exists()


def test_abandon_drops_the_copies(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    save = run([library.read_track(path)], Edit({"album": "Later"}), busy=lambda p: True)
    save.abandon()
    assert save.waiting == [] and not tag_editor.temp_path(path).exists()


def test_undo_warns_when_the_file_was_edited_again(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    run([library.read_track(path)], Edit({"album": "First"}))
    run([library.read_track(path)], Edit({"album": "Second"}))
    older = tag_editor.history()[1]
    jobs, changed, gone = tag_editor.undo_jobs(older)
    assert changed == [path] and len(jobs) == 1


def test_read_only_songs_say_why(tmp_path):
    from ironbark.library import Track
    flac = make(tmp_path / "a.flac", "flac")
    cue = audiofile.CuePath(str(flac), cue=audiofile.CueRange(tmp_path / "a.cue", 2, 100, 200))
    assert "CUE sheet" in tag_editor.why_read_only(Track(cue, "x"))
    assert "can't be found" in tag_editor.why_read_only(Track(tmp_path / "gone.flac", "x"))
    aac = Track(flac, "x", codec="AAC", unsupported="AAC (.m4a) can't be played")
    assert "decode" in tag_editor.why_read_only(aac)
    assert tag_editor.edit_jobs([Track(cue, "x"), aac], Edit({"album": "X"})) == []


def test_read_only_files_are_refused(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    os.chmod(path, 0o444)
    try:
        save = run([library.read_track(path)], Edit({"album": "X"}))
        assert save.results[0].state == "failed" and "read-only" in save.results[0].reason
    finally:
        os.chmod(path, 0o666)


def test_cover_files_must_be_jpeg_or_png(tmp_path):
    good = tmp_path / "cover.jpg"
    good.write_bytes(picture("JPEG", size=(30, 20)))
    cover = tag_editor.load_cover(good)
    assert (cover.mime, cover.width, cover.height) == ("image/jpeg", 30, 20) and cover.data == good.read_bytes()
    bmp = tmp_path / "cover.bmp"
    bmp.write_bytes(picture("BMP"))
    with pytest.raises(tag_editor.SaveError, match="JPEG or PNG"):
        tag_editor.load_cover(bmp)


@pytest.mark.skipif(sys.platform != "win32", reason="creation times are a Windows thing")
def test_the_creation_time_is_kept(tmp_path):
    path = make(tmp_path / "a.flac", "flac")
    created = os.stat(path).st_birthtime_ns
    run([library.read_track(path)], Edit({"album": "X"}))
    assert abs(os.stat(path).st_birthtime_ns - created) < 1000


def test_tag_editing_is_no_longer_an_experiment():
    assert "tag_editor" not in experiments.known()            # rolled in at 0.37.0
    assert not experiments.enabled({"experiments": {"tag_editor": True}}, "tag_editor")   # an old switch: ignored


def test_the_old_tags_are_in_the_history_before_the_file_is_replaced(tmp_path, monkeypatch):
    path = make(tmp_path / "a.flac", "flac")
    before = tag_editor.snapshot(path, "flac")
    seen = []
    real_commit = tag_editor.commit

    def commit(prepared):
        seen.append([e["files"][0]["before"] for e in tag_editor.history()])
        real_commit(prepared)

    monkeypatch.setattr(tag_editor, "commit", commit)
    run([library.read_track(path)], Edit({"album": "X"}))
    assert seen == [[before]]
    assert tag_editor.history_folder() == tmp_path / "app" / "TagHistory"


def test_history_saved_before_0_27_0_still_undoes(tmp_path):
    """Until 0.26.0 mutagen wrote the snapshots: MP4 as typed values ({"atoms": …}) and ID3 as
    mutagen's own rendering. They're still restored, and compared by meaning, not bytes."""
    path = make(tmp_path / "a.m4a", "mp4")
    b64 = lambda data: base64.b64encode(data).decode("ascii")   # noqa: E731
    old = {"atoms": {"\xa9nam": [{"text": "Old title"}], "\xa9ART": [{"text": "Old artist"}],
                     "\xa9alb": [{"text": "Old album"}], "trkn": [{"pair": [1, 9]}], "cpil": {"bool": False},
                     "tmpo": [{"int": 120}], "covr": [{"cover": b64(OLD_COVER), "format": 14}],
                     "----:com.apple.iTunes:replaygain_track_gain": [{"freeform": b64(b"-3.1 dB"), "format": 1}]}}
    assert tag_editor.same_snapshot(tag_editor.snapshot(path, "mp4"), old, "mp4")
    run([library.read_track(path)], Edit({"title": "Changed"}))
    assert not tag_editor.same_snapshot(tag_editor.snapshot(path, "mp4"), old, "mp4")
    audio = tag_editor._open(path, "mp4")
    tag_editor.restore(audio, "mp4", old)
    tag_editor.save(audio, "mp4")
    assert tag_editor.read_fields(path, "mp4")["title"] == "Old title"
    assert tag_editor.same_snapshot(tag_editor.snapshot(path, "mp4"), old, "mp4")
    # ID3: the same frames in another order and encoding mean the same tags.
    mp3 = make(tmp_path / "a.mp3", "mp3")
    snap = tag_editor.snapshot(mp3, "mp3")
    frames = list(reversed(id3.ID3Tag.parse(base64.b64decode(snap["id3"])).frames))
    for frame in frames:
        frame.raw, frame.encoding = None, getattr(frame, "encoding", None) and id3.UTF16
    other = {"id3": b64(id3.ID3Tag(frames).render(3)), "version": 3}
    assert other != snap and tag_editor.same_snapshot(snap, other, "mp3")

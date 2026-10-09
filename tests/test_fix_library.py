"""The library area under awkward conditions: a library drive that's unplugged or a folder that
can't be listed for a moment, a file that's locked while it's indexed, macOS "._" files, odd
digits and file names, non-Western text files, playlist CUE tracks that can't be found just now,
damaged or hand-edited settings, and how many times a file is opened to read it."""
import io
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from tagging import text, write_id3

from ironbark import (
    APP_NAME,
    OLD_APP_NAME,
    audiofile,
    cuesheet,
    dragdrop,
    library,
    library_db,
    playlists,
    settings,
    watcher,
)
from ironbark.audiofile import CuePath
from ironbark.library import Track, title_from_filename
from ironbark.library_index import LibraryIndex

RNG = np.random.default_rng(23)
HOUR = 3600.0


def flac(path: Path, frames=4410, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, (RNG.standard_normal((frames, 2)) * 3000).astype(np.int16), 44100, subtype="PCM_16",
             format="FLAC")
    if tags:
        from tagging import write_vorbis
        write_vorbis(path, **tags)
    return path


def age(path: Path, seconds: float = HOUR):
    when = time.time_ns() - int(seconds * 1e9)
    os.utime(path, ns=(when, when))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    return tmp_path / "app"


@pytest.fixture
def music(tmp_path, app):
    root = tmp_path / "Music"
    for n in range(3):
        for t in range(3):
            age(flac(root / f"Artist {n}" / f"Album {n}" / f"{t + 1:02d} Song.flac", title=f"Song {t + 1}",
                     album=f"Album {n}", artist=f"Artist {n}"))
    return root


def dates(index: LibraryIndex) -> dict[str, tuple[float, float]]:
    return {name: (e.added, e.seen) for name, e in index.entries.items()}


# -- an unreachable library folder never empties the index ----------------------------------------
def test_an_unplugged_library_drive_leaves_the_index_alone(music, app):
    index = LibraryIndex(music)
    index.refresh()
    before, counts = dates(index), index.db.counts()
    since = time.time()
    unplugged = music.with_name("Music (unplugged)")
    music.rename(unplugged)

    diff = index.refresh()
    assert diff.error and diff.nothing and not diff.removed
    assert dates(index) == before and index.db.counts() == counts      # nothing marked missing, nothing gone
    assert index.changes().error and not index.changes().removed
    assert LibraryIndex(music).entries.keys() == before.keys()        # the database still holds it all

    unplugged.rename(music)                                           # the drive comes back
    diff = index.refresh()
    assert diff.nothing and not diff.error and dates(index) == before
    assert watcher.new_albums(index, since - 1, {}) == []             # nothing comes back as "new"


def test_an_empty_mount_point_is_not_taken_as_every_file_deleted(music, app):
    index = LibraryIndex(music)
    index.refresh()
    before = dates(index)
    away = music.with_name("away")
    music.rename(away)
    music.mkdir()                                                     # what's left when a drive unmounts
    diff = index.refresh()
    assert diff.error and not diff.removed and dates(index) == before
    music.rmdir()
    away.rename(music)
    assert index.refresh().nothing and dates(index) == before


def test_a_library_folder_that_cant_be_listed_is_left_alone(music, monkeypatch):
    index = LibraryIndex(music)
    index.refresh()
    before = dates(index)
    real = os.scandir

    def scandir(path="."):
        if os.fspath(path) == str(music):
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    diff = index.refresh()
    assert diff.error and not diff.removed and dates(index) == before


def test_a_folder_that_cant_be_listed_keeps_its_entries(music, monkeypatch):
    index = LibraryIndex(music)
    look = watcher.Watcher(lambda: index)
    look.check()
    before = dates(index)
    since = time.time() - 1
    real = os.scandir
    locked = str(music / "Artist 1")

    def scandir(path="."):
        if os.fspath(path) == locked:
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    report = look.check()
    assert report.diff.nothing and not report.news and dates(index) == before
    monkeypatch.setattr(os, "scandir", real)
    report = look.check()
    assert report.diff.nothing and not report.news and dates(index) == before
    assert watcher.new_albums(index, since, {}) == []


def test_a_file_that_cant_be_looked_at_keeps_its_entry(music, monkeypatch):
    index = LibraryIndex(music)
    index.refresh()
    before = dates(index)
    real = os.stat
    flaky = str(music / "Artist 2" / "Album 2" / "02 Song.flac")

    def stat(path, *args, **kwargs):
        if os.fspath(path) == flaky:
            raise OSError(64, "The specified network name is no longer available", flaky)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    assert index.refresh().nothing and dates(index) == before
    monkeypatch.setattr(os, "stat", real)
    assert index.refresh().nothing and dates(index) == before


# -- a file that can't be read just now is read again later ----------------------------------------
def test_a_locked_file_during_indexing_is_read_again_next_time(music, monkeypatch):
    locked = music / "Artist 0" / "Album 0" / "02 Song.flac"
    real = library.FLACFile
    fail = {"left": 1}

    def flaky(path, *args, **kwargs):
        if Path(path) == locked and fail["left"]:
            fail["left"] -= 1
            raise PermissionError(32, "The process cannot access the file because it is being used", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(library, "FLACFile", flaky)
    index = LibraryIndex(music)
    index.refresh()
    stored = next(t for t in index.tracks() if t.path == locked)
    assert stored.artist == ""                                 # (the read failed this time)
    added, seen = index.entries[str(locked)].added, index.entries[str(locked)].seen

    diff = index.refresh()
    assert diff.changed == [locked] and not diff.added         # read again: a quiet change, never "new"
    stored = next(t for t in index.tracks() if t.path == locked)
    assert (stored.title, stored.artist, stored.album, stored.sample_rate) == ("Song 2", "Artist 0", "Album 0", 44100)
    assert (index.entries[str(locked)].added, index.entries[str(locked)].seen) == (added, seen)
    assert index.refresh().nothing


def test_a_damaged_file_is_still_listed_with_what_is_known(tmp_path):
    bad = tmp_path / "Album" / "01 Broken.flac"
    bad.parent.mkdir()
    bad.write_bytes(b"fLaC" + b"\x00" * 50)
    track = library.read_track(bad)
    assert track.title == "Broken" and not track.unreadable    # damaged, not "try again later"


# -- macOS "._" files and system folders ---------------------------------------------------------
APPLE_DOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00" + b"Mac OS X        " + b"\x00" * 60


def test_macos_dot_underscore_files_are_not_music(tmp_path, app):
    album = tmp_path / "Music" / "Album"
    for n in (1, 2, 3):
        flac(album / f"0{n} Song.flac", title=f"Song {n}")
        (album / f"._0{n} Song.flac").write_bytes(APPLE_DOUBLE)
    (album / "._cover.jpg").write_bytes(APPLE_DOUBLE)
    assert [t.title for t in library.scan_album(album)] == ["Song 1", "Song 2", "Song 3"]
    assert [t.title for t in library.scan_folder(album.parent)] == ["Song 1", "Song 2", "Song 3"]
    found = library.find_albums(album.parent)
    assert [(a.name, a.track_count, a.quality) for a in found] == [("Album", 3, "16/44.1")]
    index = LibraryIndex(album.parent)
    assert len(index.refresh().added) == 3 and index.albums()[0].track_count == 3
    assert index.album_tracks(album) == library.scan_album(album)
    plan = dragdrop.plan_drop([album / "._01 Song.flac", album / "01 Song.flac"])
    assert plan.files == [album / "01 Song.flac"] and plan.not_music == [album / "._01 Song.flac"]


def cue_image(folder: Path, name="Image.flac", tracks=3) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    sf.write(folder / name, (RNG.standard_normal((44100 * 2 * tracks, 2)) * 3000).astype(np.int16), 44100,
             subtype="PCM_16")
    lines = [f'FILE "{name}" WAVE']
    for n in range(1, tracks + 1):
        lines += [f"  TRACK {n:02d} AUDIO", f'    TITLE "Part {n}"', f"    INDEX 01 00:{2 * (n - 1):02d}:00"]
    (folder / "Image.cue").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder / name


def test_a_dot_underscore_twin_doesnt_stop_a_cue_image_splitting(tmp_path):
    image = cue_image(tmp_path / "Disc")
    (image.parent / "._Image.flac").write_bytes(APPLE_DOUBLE)
    (image.parent / "._Image.cue").write_bytes(APPLE_DOUBLE)
    assert [t.title for t in library.scan_album(image.parent)] == ["Part 1", "Part 2", "Part 3"]
    # and a file that can't be read at all doesn't veto the split either
    tracks = [library.read_track(image), Track(image.parent / "junk.flac", "junk")]
    split = library.split_cue_images(tracks)
    assert [t.title for t in split] == ["Part 1", "Part 2", "Part 3", "junk"]


def test_the_recycle_bin_and_system_folders_are_not_searched(tmp_path, app):
    root = tmp_path / "Drive"
    flac(root / "Album" / "01 Kept.flac", title="Kept")
    flac(root / "$RECYCLE.BIN" / "S-1-5-21" / "$R1A2B3C.flac", title="Deleted")
    flac(root / "@eaDir" / "01 Kept.flac" / "thumb.flac", title="Thumbnail")
    assert [t.title for t in library.scan_folder(root)] == ["Kept"]
    assert [a.name for a in library.find_albums(root)] == ["Album"]
    index = LibraryIndex(root)
    index.refresh()
    assert [t.title for t in index.tracks()] == ["Kept"]
    assert index.album_tracks(root, whole_tree=True) == library.scan_folder(root)


# -- digits that aren't plain 0-9 ---------------------------------------------------------------------
def test_superscript_and_circled_digits_sort_and_name_without_errors(tmp_path):
    for text_ in ("4²", "Track 1²", "①", "Live 1²", "²³"):
        library.natural_key(text_)
        library.sort_name(text_)
    assert sorted(["Live 1²", "Live 10", "Live 2"], key=library.natural_key) == ["Live 1²", "Live 2", "Live 10"]
    assert title_from_filename(Path("①.flac")) == "①" and title_from_filename(Path("07.flac")) == "Track 7"
    flac(tmp_path / "Odd" / "①.flac")
    assert [t.title for t in library.scan_album(tmp_path / "Odd")] == ["①"]


# -- a file name that isn't valid Unicode ---------------------------------------------------------
def test_a_file_name_with_a_lone_surrogate_doesnt_stop_the_index_saving(tmp_path, app):
    root = tmp_path / "Music"
    flac(root / "Album" / "01 Fine.flac", title="Fine")
    made = flac(tmp_path / "staging.flac", title="Odd")
    odd = root / "Album" / "02 Odd\udc80.flac"
    try:
        os.replace(made, odd)
    except (OSError, UnicodeError):
        pytest.skip("this file system can't hold such a name")
    index = LibraryIndex(root)
    diff = index.refresh()
    assert len(diff.added) == 2
    assert (app / library_db.FILE_NAME).is_file()        # (0.31: the database, where JSON once was)
    again = LibraryIndex(root)
    assert str(odd) in again.entries and again.refresh().nothing
    playlist = playlists.path_for("Odd names")
    playlists.write(playlist, list(index.tracks()))
    assert sorted(map(str, playlists.read(playlist))) == sorted(str(t.path) for t in index.tracks())


# -- text files in other encodings ---------------------------------------------------------------------
JAPANESE = "宇多田ヒカル"


def test_shift_jis_cue_sheets_and_playlists_on_a_japanese_pc(tmp_path, monkeypatch):
    monkeypatch.setattr(cuesheet.locale, "getencoding", lambda: "cp932")
    sheet = f'PERFORMER "{JAPANESE}"\nFILE "{JAPANESE}.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
    (tmp_path / "a.cue").write_bytes(sheet.encode("cp932"))
    read = cuesheet.read(tmp_path / "a.cue")
    assert read.performer == JAPANESE and read.files == [f"{JAPANESE}.wav"]
    song = tmp_path / f"{JAPANESE}.flac"
    (tmp_path / "jp.m3u").write_bytes(f"#EXTM3U\r\n{song}\r\n".encode("cp932"))
    assert playlists.read(tmp_path / "jp.m3u") == [song]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_playlists_with_or_without_a_byte_order_mark(tmp_path, encoding):
    songs = [tmp_path / "Café" / "01.flac", tmp_path / JAPANESE / "02.flac"]
    path = tmp_path / "mix.m3u"
    path.write_bytes(("#EXTM3U\r\n" + "\r\n".join(map(str, songs)) + "\r\n").encode(encoding))
    assert playlists.read(path) == songs


def test_plain_old_encodings_still_read(tmp_path):
    text_ = 'TITLE "Café"\nFILE "a.wav" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n'
    assert cuesheet.decode(text_.encode("cp1252")) == text_
    assert cuesheet.decode(text_.encode("utf-8")) == text_
    assert cuesheet.decode(b"\xef\xbb\xbf" + text_.encode("utf-8")) == text_
    assert "\ufffd" in cuesheet.decode(b"caf\x81")         # a byte nothing defines: never an error


# -- playlist CUE tracks survive a save while they can't be found -------------------------------------
def read_like_the_player(path: Path) -> list[Track]:
    return [library.read_track(song) if song.is_file() else Track(song, title_from_filename(song), missing=True)
            for song in playlists.read(path)]


def test_playlist_cue_tracks_survive_a_save_while_the_drive_is_unplugged(tmp_path, app):
    image = cue_image(tmp_path / "Music" / "Disc")
    tracks = library.scan_album(image.parent)
    playlist = playlists.path_for("Mix")
    playlists.write(playlist, [tracks[1], tracks[2]])
    away = tmp_path / "Music (unplugged)"
    (tmp_path / "Music").rename(away)

    offline = read_like_the_player(playlist)
    assert all(t.missing for t in offline) and [audiofile.cue_of(t.path).number for t in offline] == [2, 3]
    playlists.write(playlist, offline + [Track(tmp_path / "other.flac", "Other")])    # an edit while unplugged
    assert playlist.read_text(encoding="utf-8").count(playlists.CUE_MARK) == 2

    away.rename(tmp_path / "Music")
    back = playlists.read(playlist)
    assert back[:2] == [tracks[1].path, tracks[2].path]
    assert [t.title for t in read_like_the_player(playlist)[:2]] == ["Part 2", "Part 3"]


def test_playlist_cue_tracks_survive_a_save_while_the_sheet_is_away(tmp_path, app):
    image = cue_image(tmp_path / "Disc")
    tracks = library.scan_album(image.parent)
    playlist = playlists.path_for("Mix")
    playlists.write(playlist, [tracks[1]])
    (image.parent / "Archive").mkdir()
    shutil.move(image.parent / "Image.cue", image.parent / "Archive" / "Image.cue")

    listed = read_like_the_player(playlist)
    assert listed[0].missing and audiofile.cue_of(listed[0].path).number == 2    # not the whole disc
    playlists.write(playlist, listed)
    shutil.move(image.parent / "Archive" / "Image.cue", image.parent / "Image.cue")
    assert playlists.read(playlist) == [tracks[1].path]


# -- renaming a playlist by case only ---------------------------------------------------------------
def test_renaming_a_playlist_by_case_only(app):
    rock = playlists.path_for("rock")
    playlists.write(rock, [])
    renamed = playlists.rename(rock, "Rock")
    assert renamed.name == "Rock.m3u8" and [p.name for p in playlists.saved()] == ["Rock.m3u8"]
    other = playlists.path_for("Jazz")
    playlists.write(other, [])
    with pytest.raises(FileExistsError):
        playlists.rename(renamed, "jazz")


# -- settings --------------------------------------------------------------------------------------
@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    path = tmp_path / "Ironbark" / "settings.json"
    monkeypatch.setattr(settings, "settings_path", lambda: path)
    monkeypatch.setattr(settings, "problem", "")
    return path


@pytest.mark.parametrize("content", ["null", "42", "[1]", "[[1, 2]]", '"text"', "{not json", "\x00\x00",
                                     "[" * 100_000],
                         ids=["null", "number", "list", "pairs", "text", "broken", "zeros", "deep"])
def test_settings_that_arent_an_object_dont_stop_the_start(prefs_file, content):
    prefs_file.parent.mkdir(parents=True)
    prefs_file.write_text(content, encoding="utf-8")
    assert settings.load() == settings.DEFAULTS
    damaged = list(prefs_file.parent.glob("settings.json.damaged-*"))
    assert len(damaged) == 1 and damaged[0].read_text(encoding="utf-8") == content    # kept, never overwritten
    assert settings.problem


def test_a_damaged_settings_file_falls_back_to_the_last_good_copy(prefs_file):
    settings.save({**settings.DEFAULTS, "library_folder": "D:/Music", "volume": 0.5})
    assert settings.load()["library_folder"] == "D:/Music"                     # a good file: kept as .bak
    assert prefs_file.with_name("settings.json.bak").is_file()
    prefs_file.write_text('{"library_folder": "D:/Mus', encoding="utf-8")       # cut short (a crash, a full disk)
    loaded = settings.load()
    assert (loaded["library_folder"], loaded["volume"]) == ("D:/Music", 0.5) and settings.problem
    assert settings.load()["library_folder"] == "D:/Music"                       # (and again: it was put back)
    assert len(list(prefs_file.parent.glob("settings.json.damaged-*"))) == 1


def test_wrong_typed_settings_fall_back_one_by_one(prefs_file):
    prefs_file.parent.mkdir(parents=True)
    saved = {"volume": None, "current_tab": "2", "shuffle": "yes", "library_folder": "D:/Music",
             "clip_headroom": 1, "abx_trials": True, "experiments": [1], "new_music": {"since": 5.0},
             "track_columns": ["artist", 3], "from_a_newer_version": {"x": 1},
             "tabs": ["bad", {"kind": "album"}, {"kind": "album", "path": "D:/A", "resume": {"at": "soon"}},
                      {"kind": "folder", "path": "D:/B", "query": "x", "resume": {"path": "D:/B/1.flac", "at": 5}}]}
    prefs_file.write_bytes(b"\xef\xbb\xbf" + json.dumps(saved).encode())       # (with a byte-order mark)
    loaded = settings.load()
    assert (loaded["volume"], loaded["current_tab"], loaded["shuffle"]) == (0.8, 0, False)
    assert (loaded["library_folder"], loaded["clip_headroom"], loaded["abx_trials"]) == ("D:/Music", 1, 16)
    assert loaded["experiments"] is None and loaded["new_music"] == {"since": 5.0}
    assert loaded["track_columns"] == ["artist"] and loaded["from_a_newer_version"] == {"x": 1}
    assert loaded["tabs"] == [{"kind": "album", "path": "D:/A"},
                              {"kind": "folder", "path": "D:/B", "query": "x",
                               "resume": {"path": "D:/B/1.flac", "at": 5}}]
    assert not list(prefs_file.parent.glob("settings.json.damaged-*")) and not settings.problem
    prefs_file.write_text('{"volume": 1' + "0" * 400 + ', "testsignals_level": NaN}', encoding="utf-8")
    loaded = settings.load()
    assert (loaded["volume"], loaded["testsignals_level"]) == (0.8, -20.0)     # no number a float can't hold


def test_settings_are_flushed_to_disk_under_a_unique_temporary_name(prefs_file, monkeypatch):
    synced, names = [], []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))
    monkeypatch.setattr(os, "replace", lambda a, b: names.append(Path(a).name) or real_replace(a, b))
    settings.save({"volume": 0.3})
    assert synced and names and names[0] != "settings.tmp" and names[0].endswith(".tmp")
    assert json.loads(prefs_file.read_text(encoding="utf-8")) == {"volume": 0.3}
    assert [p.name for p in prefs_file.parent.iterdir()] == ["settings.json"]   # no temporary file left


def test_a_failed_carry_over_still_starts_and_is_tried_again(tmp_path, monkeypatch):
    old = tmp_path / OLD_APP_NAME
    (old / "Playlists").mkdir(parents=True)
    (old / "settings.json").write_text(json.dumps({"tabs": [{"path": str(old / "Playlists")}]}), encoding="utf-8")
    (old / "Playlists" / "Rock.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    msvcrt = pytest.importorskip("msvcrt")
    with open(old / "Playlists" / "Rock.m3u8", "r+b") as held:              # another program has it locked
        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 8)
        try:
            assert settings.carry_over_old_folder(tmp_path) is False        # no crash
        finally:
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 8)
    assert not (tmp_path / APP_NAME).exists()                               # no half copy left behind
    assert settings.carry_over_old_folder(tmp_path) is True                 # the next start finishes it
    new = tmp_path / APP_NAME
    assert (new / "Playlists" / "Rock.m3u8").is_file()
    assert json.loads((new / "settings.json").read_text(encoding="utf-8"))["tabs"][0]["path"] == str(
        new / "Playlists")
    assert [p.name for p in tmp_path.iterdir()] == sorted([APP_NAME, OLD_APP_NAME])


# -- find_albums and drops ----------------------------------------------------------------------------
def test_find_albums_still_says_where_same_named_folders_live(tmp_path):
    flac(tmp_path / "Root" / "Main" / "Greatest Hits" / "01.flac")
    flac(tmp_path / "Root" / "Backup" / "Greatest Hits" / "01.flac")
    flac(tmp_path / "Root" / "Other" / "01.flac")
    names = [a.name for a in library.find_albums(tmp_path / "Root")]
    assert names == ["Greatest Hits  (Backup)", "Greatest Hits  (Main)", "Other"]


def test_a_dropped_cue_is_resolved_off_the_window_thread(tmp_path, monkeypatch):
    image = cue_image(tmp_path / "Disc")
    plan = dragdrop.plan_drop([image.parent / "Image.cue"])
    assert plan.cue_folders == [image.parent]
    monkeypatch.setattr(dragdrop, "cue_folder", lambda cue: pytest.fail("disk work on the window's thread"))
    assert dragdrop.actions_for(plan, on_tab=False) == [dragdrop.Action("open_folder", path=image.parent)]
    assert dragdrop.actions_for(plan, on_tab=True)[0].folders == (image.parent,)


# -- how often a file is opened to read it -------------------------------------------------------------
@pytest.fixture
def opens(monkeypatch):
    """Counts every open of a file: Python's own, and libsndfile's (each SoundFile / info)."""
    counts: dict[str, int] = {}
    real_open, real_soundfile = io.open, sf.SoundFile

    def counting_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            name = Path(file).name
            counts[name] = counts.get(name, 0) + 1
        return real_open(file, *args, **kwargs)

    class CountingSoundFile(real_soundfile):
        def __init__(self, file, *args, **kwargs):
            name = Path(file).name
            counts[name] = counts.get(name, 0) + 1
            super().__init__(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    monkeypatch.setattr(io, "open", counting_open)
    monkeypatch.setattr(sf, "SoundFile", CountingSoundFile)
    return counts


def test_each_format_is_opened_as_few_times_as_it_needs(tmp_path, opens):
    data = (RNG.standard_normal((4410, 2)) * 3000).astype(np.int16)
    made = {"a.flac": dict(format="FLAC"), "b.wav": dict(format="WAV"), "c.aiff": dict(format="AIFF"),
            "d.ogg": dict(format="OGG", subtype="VORBIS")}
    for name, kind in made.items():
        sf.write(tmp_path / name, data if kind["format"] != "OGG" else data / 32768.0, 44100, **kind)
    sf.write(tmp_path / "e.mp3", (data / 32768.0).astype("float32"), 44100, format="MP3")
    write_id3(tmp_path / "e.mp3", [text("TIT2", "Song")], version=4)
    opens.clear()
    tracks = {name: library.read_track(tmp_path / name) for name in ("a.flac", "b.wav", "c.aiff", "d.ogg", "e.mp3")}
    assert opens == {"a.flac": 1, "b.wav": 3, "c.aiff": 3, "d.ogg": 3, "e.mp3": 2}   # (a FLAC: one open)
    assert [t.codec for t in tracks.values()] == ["FLAC", "WAV", "AIFF", "Vorbis", "MP3"]
    assert tracks["e.mp3"].title == "Song" and all(t.sample_rate == 44100 and not t.misnamed for t in tracks.values())


def test_contents_not_names_still_decide_the_format(tmp_path):
    data = (RNG.standard_normal((4410, 2)) * 3000).astype(np.int16)
    sf.write(tmp_path / "really_wav.flac", data, 44100, format="WAV")
    sf.write(tmp_path / "really_mp3.flac", (data / 32768.0).astype("float32"), 44100, format="MP3")
    write_id3(tmp_path / "really_mp3.flac", [text("TIT2", "Tagged")], version=3)
    wav, mp3 = library.read_track(tmp_path / "really_wav.flac"), library.read_track(tmp_path / "really_mp3.flac")
    assert (wav.codec, wav.misnamed, wav.sample_rate) == ("WAV", ".flac", 44100)
    assert (mp3.codec, mp3.misnamed, mp3.title) == ("MP3", ".flac", "Tagged")
    (tmp_path / "empty.flac").write_bytes(b"")
    assert library.read_track(tmp_path / "empty.flac").title == "empty"


def test_a_cue_track_path_still_reads_as_that_track(tmp_path):
    image = cue_image(tmp_path / "Disc")
    tracks = library.scan_album(image.parent)
    assert isinstance(tracks[1].path, CuePath) and library.read_track(tracks[1].path).title == "Part 2"

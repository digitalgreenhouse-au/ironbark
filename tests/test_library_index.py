"""(0.15.0): genre / composer / date / album artist read from every format; the saved
library index (refresh with added / changed / removed files, CUE images, atomic save); the
browse views' grouping (artists incl. compilations, genres, years, composers) and sort order."""
import json
import os
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mp4_maker import write_m4a
from tagging import text, write_id3, write_vorbis
from ironbark.tagio import id3 as id3tags
from ironbark.tagio.flac import FLACFile
from ironbark.tagio.mp4 import MP4File
from ironbark import browse, library, library_index, settings
from ironbark.audiofile import CuePath
from ironbark.library import Album, Track, sort_name, year_of
from ironbark.library_index import LibraryIndex

RNG = np.random.default_rng(10)


def ints(frames=4410):
    return (RNG.standard_normal((frames, 2)) * 3000).astype(np.int16)


def flac(path: Path, frames=4410, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, ints(frames), 44100, subtype="PCM_16", format="FLAC")
    if tags:
        write_vorbis(path, **tags)
    return path


def id3_frames():
    return [text("TIT2", "Song"), text("TPE1", "Singer"), text("TPE2", "The Band"), text("TALB", "Record"),
            text("TCON", "Rock", "Pop"), text("TCOM", "Writer"), text("TDRC", "1999-05-01"), text("TCMP", "1")]


def check_extras(track, genre="Rock; Pop", composer="Writer", year=1999, album_artist="The Band", compilation=True):
    assert (track.album_artist, track.genre, track.composer, track.year, track.compilation) == (
        album_artist, genre, composer, year, compilation)


# -- tags: every format ----------------------------------------------------------------------
def test_flac_vorbis_comments(tmp_path):
    path = flac(tmp_path / "01.flac", title="Song", artist="Singer", albumartist="The Band", genre=["Rock", "Pop"],
                composer="Writer", date="1999-05-01", compilation="1")
    check_extras(library.read_track(path))


def test_flac_alternative_spellings_and_no_tags(tmp_path):
    path = flac(tmp_path / "a.flac", **{"album artist": "The Band", "year": "2004", "genre": "Jazz; Jazz"})
    track = library.read_track(path)
    assert (track.album_artist, track.date, track.year, track.genre, track.compilation) == (
        "The Band", "2004", 2004, "Jazz; Jazz", False)  # (one value "Jazz; Jazz": kept as written)
    bare = library.read_track(flac(tmp_path / "b.flac"))
    assert (bare.album_artist, bare.genre, bare.composer, bare.date, bare.year) == ("", "", "", "", None)


def test_mp3_id3(tmp_path):
    path = tmp_path / "x.mp3"
    sf.write(path, (ints(44100) / 32768).astype("float32"), 44100, format="MP3")
    write_id3(path, id3_frames(), version=4)
    check_extras(library.read_track(path))


def test_mp3_id3v23_year_and_numeric_genre(tmp_path):
    path = tmp_path / "old.mp3"
    sf.write(path, (ints(44100) / 32768).astype("float32"), 44100, format="MP3")
    write_id3(path, [text("TYER", "1987", encoding=id3tags.LATIN1),
                     text("TCON", "(17)", encoding=id3tags.LATIN1)], version=3)   # an ID3v1 genre number: 17 = Rock
    track = library.read_track(path)
    assert track.year == 1987 and track.genre == "Rock"


def test_wav_id3_and_riff_info(tmp_path):
    path = tmp_path / "a.wav"
    sf.write(path, ints(), 44100, subtype="PCM_16")
    write_id3(path, id3_frames())
    check_extras(library.read_track(path))
    info = tmp_path / "b.wav"   # RIFF INFO only (what many rippers write): IGNR genre, ICRD date
    with sf.SoundFile(info, "w", 44100, 2, "PCM_16") as f:
        f.genre, f.date = "Blues", "2001"
        f.write(ints())
    track = library.read_track(info)
    assert track.genre == "Blues" and track.year == 2001


def test_aiff_id3(tmp_path):
    path = tmp_path / "a.aiff"
    sf.write(path, ints(), 44100, format="AIFF", subtype="PCM_16")
    write_id3(path, id3_frames())
    check_extras(library.read_track(path))


def test_alac_m4a(tmp_path):
    path = write_m4a(tmp_path / "a.m4a", ints(), bits=16)
    tags = MP4File(path)
    tags["\xa9gen"], tags["\xa9wrt"], tags["\xa9day"] = ["Rock", "Pop"], ["Writer"], ["1999-05-01T00:00:00Z"]
    tags["aART"], tags["cpil"] = ["The Band"], True
    tags.save()
    check_extras(library.read_track(path))


@pytest.mark.parametrize("subtype", ["VORBIS", "OPUS"])
def test_ogg_vorbis_comments(tmp_path, subtype):
    path = tmp_path / "a.ogg"
    rate = 48000 if subtype == "OPUS" else 44100
    sf.write(path, (ints(rate) / 32768).astype("float32"), rate, format="OGG", subtype=subtype)
    write_vorbis(path, albumartist="The Band", genre=["Rock", "Pop"], composer="Writer", date="1999",
                 compilation="1")
    check_extras(library.read_track(path))


def cue_image(folder: Path, name="Album.flac", genre="Ambient", date="1994"):
    folder.mkdir(parents=True, exist_ok=True)
    sf.write(folder / name, ints(44100 * 6), 44100, subtype="PCM_16")
    (folder / "Album.cue").write_text(f'''REM GENRE {genre}
REM DATE {date}
PERFORMER "The Orb"
SONGWRITER "Paterson"
TITLE "Live 93"
FILE "{name}" WAVE
  TRACK 01 AUDIO
    TITLE "One"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Two"
    PERFORMER "Guest"
    SONGWRITER "Someone Else"
    INDEX 01 00:03:00
''', encoding="utf-8")
    return folder / name


def test_cue_tracks_take_genre_date_composer_and_album_artist_from_the_sheet(tmp_path):
    cue_image(tmp_path / "Orb")
    one, two = library.scan_album(tmp_path / "Orb")
    assert (one.album_artist, one.genre, one.year, one.composer) == ("The Orb", "Ambient", 1994, "Paterson")
    assert (two.artist, two.album_artist, two.composer) == ("Guest", "The Orb", "Someone Else")


def test_year_and_sort_name():
    assert [year_of(d) for d in ("2004", "2004-05-03", "03/05/2004", "", "May", "12345")] == [
        2004, 2004, 2004, None, None, None]
    names = ["The Who", "10cc", "2Pac", "ABBA", "the Beatles", "Theory of a Deadman", "The The"]
    assert sorted(names, key=sort_name) == ["2Pac", "10cc", "ABBA", "the Beatles", "The The",
                                            "Theory of a Deadman", "The Who"]


# -- the index ---------------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    return tmp_path / "app"


@pytest.fixture
def music(tmp_path):
    root = tmp_path / "Music"
    flac(root / "Adele - 21" / "01 Rolling.flac", title="Rolling", artist="Adele", album="21", tracknumber="1",
         date="2011", genre="Pop")
    flac(root / "Adele - 21" / "02 Rumour.flac", title="Rumour", artist="Adele", album="21", tracknumber="2",
         date="2011", genre="Pop")
    flac(root / "Hits" / "01 A.flac", title="A", artist="Alpha", album="Hits", genre="Pop; Dance", date="1999")
    flac(root / "Hits" / "02 B.flac", title="B", artist="Beta", album="Hits", genre="Dance", date="1999")
    flac(root / "Box" / "CD1" / "01 X.flac", title="X", artist="The Who", album="Box", discnumber="1",
         tracknumber="1")
    flac(root / "Box" / "CD2" / "01 Y.flac", title="Y", artist="The Who", album="Box", discnumber="2",
         tracknumber="1")
    return root


def test_first_build_reads_everything_and_saves_atomically(app, music):
    index = LibraryIndex(music)
    assert not index.built and index.entries == {}
    diff = index.refresh()
    assert len(diff.added) == 6 and not diff.changed and not diff.removed
    assert index.built and index.progress == (6, 6)
    saved = app / "library_index.json"
    assert saved.is_file() and not saved.with_suffix(".json.tmp").exists()
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["format"] == library_index.FORMAT and Path(data["root"]) == music and len(data["files"]) == 6
    # first build: "added" is when the file arrived (its creation time), never in the future
    assert all(0 < e.added <= time.time() for e in index.entries.values())


def test_second_start_reads_nothing(app, music, monkeypatch):
    LibraryIndex(music).refresh()
    reads = []
    real = library_index.read_track
    monkeypatch.setattr(library_index, "read_track", lambda p: reads.append(p) or real(p))
    again = LibraryIndex(music)          # a new start: loaded from the file
    assert len(again.entries) == 6 and again.built
    diff = again.refresh()
    assert diff.nothing and reads == []
    assert {t.title for t in again.tracks()} == {"Rolling", "Rumour", "A", "B", "X", "Y"}
    rolling = next(t for t in again.tracks() if t.title == "Rolling")
    assert (rolling.album, rolling.genre, rolling.year, rolling.track_no, rolling.codec) == ("21", "Pop", 2011, 1,
                                                                                             "FLAC")
    assert rolling == library.read_track(rolling.path)  # the stored track is the same as a fresh read


def test_refresh_with_added_changed_and_removed_files(app, music, monkeypatch):
    index = LibraryIndex(music)
    index.refresh()
    seen = {name: (e.added, e.seen) for name, e in index.entries.items()}
    rumour = music / "Adele - 21" / "02 Rumour.flac"
    audio = FLACFile(rumour)
    audio["title"] = "Rumour Has It"      # a tag edit: changed, never new
    audio.save()
    os.utime(rumour, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    (music / "Hits" / "02 B.flac").unlink()
    new = flac(music / "Fresh" / "01 New.flac", title="New", artist="Newcomer")
    reads = []
    real = library_index.read_track
    monkeypatch.setattr(library_index, "read_track", lambda p: reads.append(Path(p)) or real(p))
    assert index.changes().note() == "1 new · 1 changed · 1 gone"   # (nothing read for that)
    assert reads == []
    diff = index.refresh()
    assert diff.added == [new] and diff.changed == [rumour] and diff.removed == [music / "Hits" / "02 B.flac"]
    assert sorted(reads) == sorted([new, rumour])                     # only what changed was read again
    assert next(t for t in index.tracks() if t.path == rumour).title == "Rumour Has It"
    assert (index.entries[str(rumour)].added, index.entries[str(rumour)].seen) == seen[str(rumour)]
    assert index.entries[str(new)].added >= index.built               # arrived after the first build
    assert LibraryIndex(music).refresh().nothing                      # and it was all saved


def test_update_after_a_tag_save_rereads_those_files(app, music):
    index = LibraryIndex(music)
    index.refresh()
    path = music / "Hits" / "01 A.flac"
    stat = os.stat(path)
    audio = FLACFile(path)
    audio["genre"] = "Rock"
    audio.save()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # even with the same modified time
    diff = index.update([path])
    assert diff.changed == [path]
    assert next(t for t in index.tracks() if t.path == path).genre == "Rock"


def test_damaged_old_or_other_folders_index_is_rebuilt(app, music, tmp_path):
    LibraryIndex(music).refresh()
    saved = app / "library_index.json"
    saved.write_text("{not json", encoding="utf-8")
    assert not LibraryIndex(music).entries
    LibraryIndex(music).refresh()
    data = json.loads(saved.read_text(encoding="utf-8"))
    data["format"] = library_index.FORMAT - 1
    saved.write_text(json.dumps(data), encoding="utf-8")
    assert not LibraryIndex(music).entries                 # made by an older reader: read again
    LibraryIndex(music).refresh()
    assert not LibraryIndex(tmp_path / "Elsewhere").entries  # an index of a different folder


def test_cue_images_index_as_their_tracks(app, tmp_path):
    root = tmp_path / "Music"
    image = cue_image(root / "Orb")
    flac(root / "Other" / "01 a.flac", title="a")
    index = LibraryIndex(root)
    index.refresh()
    tracks = [t for t in index.tracks() if str(t.path) == str(image)]
    assert [t.title for t in tracks] == ["One", "Two"] and all(isinstance(t.path, CuePath) for t in tracks)
    again = LibraryIndex(root)   # CuePaths survive the file
    stored = [t for t in again.tracks() if str(t.path) == str(image)]
    assert [t.path for t in stored] == [t.path for t in tracks] and [t.path.cue for t in stored][1].start == 3 * 44100
    assert again.album_tracks(root / "Orb") == library.scan_album(root / "Orb")
    albums = {a.name: a for a in again.albums()}
    assert albums["Orb"].track_count == 2 and albums["Orb"].artist == "The Orb" and albums["Orb"].year == 1994
    # a changed sheet re-reads the folder, even though the music file itself didn't change
    (root / "Orb" / "Album.cue").write_text((root / "Orb" / "Album.cue").read_text().replace('"Two"', '"Deux"'))
    os.utime(root / "Orb" / "Album.cue", ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    assert again.album_tracks(root / "Orb") is None           # stale: the caller reads the files itself
    diff = again.refresh()
    assert diff.nothing                                          # no music file changed…
    assert [t.title for t in again.tracks() if str(t.path) == str(image)] == ["One", "Deux"]  # …but it's re-read


def test_album_tracks_from_the_index_match_a_real_scan(app, music):
    index = LibraryIndex(music)
    index.refresh()
    for folder in (music / "Adele - 21", music / "Box"):
        assert index.album_tracks(folder) == library.scan_album(folder)
    assert index.album_tracks(music, whole_tree=True) == library.scan_folder(music)
    assert index.album_tracks(music.parent / "Elsewhere") is None       # not in the library folder
    flac(music / "Adele - 21" / "03 Turning.flac", title="Turning")      # new since: not trusted
    assert index.album_tracks(music / "Adele - 21") is None


def test_shared_index_follows_the_settings_folder(app, music):
    one = library_index.shared(music)
    assert library_index.shared(music) is one and one.path == app / "library_index.json"


# -- browse views ----------------------------------------------------------------------------------
def t(folder, title, artist="", album_artist="", genre="", year="", composer="", compilation=False, n=1):
    return Track(Path("C:/Music") / folder / f"{n:02d} {title}.flac", title, artist=artist, album=folder,
                 album_artist=album_artist, genre=genre, date=year, composer=composer, compilation=compilation,
                 track_no=n, sample_rate=44100, bits=16)


def test_album_artist_rules():
    assert browse.album_artist([t("A", "x", "Guest", album_artist="Band"), t("A", "y", "Band")]) == "Band"
    assert browse.album_artist([t("A", "x", "Adele"), t("A", "y", "adele ")]) == "Adele"
    assert browse.album_artist([t("A", "x", "Alpha"), t("A", "y", "Beta")]) == browse.VARIOUS
    assert browse.album_artist([t("A", "x", "Alpha", compilation=True)]) == browse.VARIOUS
    assert browse.album_artist([t("A", "x")]) == ""


def library_albums():
    tracks = [t("21", "Rolling", "Adele", genre="Pop", year="2011"),
              t("Hits", "A", "Alpha", genre="Pop; Dance", year="1999"),
              t("Hits", "B", "Beta", genre="Dance", year="1999", n=2),
              t("Tommy", "Overture", "The Who", year="1969", composer="Pete Townshend"),
              t("Quadrophenia", "Real Me", "The Who", year="1973", composer="Pete Townshend; John Entwistle"),
              t("Untagged", "Noise")]
    return browse.albums_from(tracks, {}, Path("C:/Music"))


def names(group):
    return [a.name for a in group.albums]


def test_artists_view_puts_compilations_under_various_artists():
    groups = browse.group_albums(library_albums(), "artists")
    assert [g.name for g in groups] == ["Adele", "The Who", "Various Artists", "Unknown artist"]
    assert names(groups[1]) == ["Quadrophenia", "Tommy"] and names(groups[2]) == ["Hits"]


def test_genres_years_and_composers_views():
    albums = library_albums()
    genres = browse.group_albums(albums, "genres")
    assert [(g.name, names(g)) for g in genres] == [("Dance", ["Hits"]), ("Pop", ["21", "Hits"]),
                                                    ("No genre", ["Quadrophenia", "Tommy", "Untagged"])]
    years = browse.group_albums(albums, "years")
    assert [g.name for g in years] == ["2011", "1999", "1973", "1969", "Unknown year"]  # newest first
    composers = browse.group_albums(albums, "composers")
    assert [(g.name, names(g)) for g in composers][:2] == [("John Entwistle", ["Quadrophenia"]),
                                                           ("Pete Townshend", ["Quadrophenia", "Tommy"])]
    assert browse.group_albums(albums, "albums")[0].albums == albums


def test_sorting_by_each_column():
    albums = [Album(Path(n), n, c, sample_rate=r, bits=b, codecs=k, artist=a, year=y, added=d)
              for n, c, r, b, k, a, y, d in [
                  ("Album 10", 3, 96000, 24, ("FLAC",), "The Who", 1969, 30.0),
                  ("Album 2", 12, 44100, 16, ("FLAC",), "Adele", None, 10.0),
                  ("The Album 3", 7, 44100, 16, ("MP3",), "", 2011, 20.0),
                  ("Mixed", 5, 44100, 16, ("FLAC", "MP3"), "Beck", 1999, 40.0)]]

    def order(column, reverse=False):
        return [a.name for a in browse.sort_albums(albums, column, reverse)]

    assert order("album") == ["Album 2", "The Album 3", "Album 10", "Mixed"]   # natural numbers, "The" ignored
    assert order("artist") == ["Album 2", "Mixed", "Album 10", "The Album 3"]  # Adele, Beck, (The) Who, blank last
    assert order("year") == ["Album 10", "Mixed", "The Album 3", "Album 2"]    # unknown year last
    assert order("tracks", reverse=True) == ["Album 2", "The Album 3", "Mixed", "Album 10"]
    assert order("quality") == ["The Album 3", "Album 2", "Album 10", "Mixed"]  # lossy, 16/44.1, 24/96, mixed
    assert order("added", reverse=True) == ["Mixed", "Album 10", "The Album 3", "Album 2"]  # newest first


def test_search_matches_artist_genre_year_and_composer():
    albums = {a.name: a for a in library_albums()}
    assert browse.matches(albums["Hits"], ["dance"]) and browse.matches(albums["Hits"], ["various", "1999"])
    assert browse.matches(albums["Tommy"], ["townshend"]) and not browse.matches(albums["Tommy"], ["adele"])

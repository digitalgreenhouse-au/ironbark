from pathlib import Path

import numpy as np
import soundfile as sf
from ironbark.tagio.flac import FLACFile

from ironbark.library import natural_key, parse_number, scan_folder, title_from_filename


def make_flac(path: Path, seconds=0.1, rate=44100, tags=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros((int(seconds * rate), 2), dtype="float32"), rate, format="FLAC")
    if tags:
        audio = FLACFile(path)
        for key, value in tags.items():
            audio[key] = value
        audio.save()
    return path


def test_natural_key_orders_numbers_numerically():
    names = ["10 Ten.flac", "2 Two.flac", "1 One.flac"]
    assert sorted(names, key=natural_key) == ["1 One.flac", "2 Two.flac", "10 Ten.flac"]


def test_parse_number():
    assert parse_number("3") == 3
    assert parse_number("03/12") == 3
    assert parse_number("") is None
    assert parse_number("A1") is None


def test_title_from_filename_strips_track_prefixes():
    for name in ["01 Rolling in the Deep", "01 - Rolling in the Deep", "01. Rolling in the Deep",
                 "01.-Rolling in the Deep", "1) Rolling in the Deep"]:
        assert title_from_filename(Path(name + ".flac")) == "Rolling in the Deep"
    assert title_from_filename(Path("Intro.flac")) == "Intro"
    assert title_from_filename(Path("07.flac")) == "Track 7"


def test_flat_folder_of_two_albums_does_not_interleave(tmp_path):
    for album in ("Beta", "Alpha"):
        for n in (1, 2):
            make_flac(tmp_path / f"{album} - 0{n}.flac",
                      tags={"title": f"{album} {n}", "tracknumber": str(n), "album": album})
    assert [t.title for t in scan_folder(tmp_path)] == ["Alpha 1", "Alpha 2", "Beta 1", "Beta 2"]


def test_untagged_files_play_in_natural_filename_order(tmp_path):
    for name in ["10 Ten", "2 Two", "1 One"]:
        make_flac(tmp_path / f"{name}.flac")
    (tmp_path / "cover.jpg").write_bytes(b"not audio")
    assert [t.title for t in scan_folder(tmp_path)] == ["One", "Two", "Ten"]


def test_tags_override_filenames(tmp_path):
    make_flac(tmp_path / "a.flac", tags={"title": "Second", "tracknumber": "2", "artist": "X", "album": "Y"})
    make_flac(tmp_path / "b.flac", tags={"title": "First", "tracknumber": "1/2"})
    tracks = scan_folder(tmp_path)
    assert [t.title for t in tracks] == ["First", "Second"]
    assert tracks[1].display_artist_album == "X — Y"
    assert 0.09 < tracks[0].duration < 0.11


def test_disc_subfolders_play_in_order(tmp_path):
    make_flac(tmp_path / "Disc 2" / "01 C.flac")
    make_flac(tmp_path / "Disc 1" / "02 B.flac")
    make_flac(tmp_path / "Disc 1" / "01 A.flac")
    assert [t.title for t in scan_folder(tmp_path)] == ["A", "B", "C"]


def test_broken_file_still_listed(tmp_path):
    (tmp_path / "01 Broken.flac").write_bytes(b"not really flac")
    assert [t.title for t in scan_folder(tmp_path)] == ["Broken"]


def test_find_albums_merges_disc_folders_and_skips_empty(tmp_path):
    from ironbark.library import find_albums

    make_flac(tmp_path / "Artist - Double" / "CD1" / "01 A.flac")
    make_flac(tmp_path / "Artist - Double" / "CD2" / "01 B.flac", rate=96000)
    make_flac(tmp_path / "Artist" / "Single Album" / "01 C.flac")
    (tmp_path / "Empty").mkdir()
    albums = {a.name: a for a in find_albums(tmp_path)}
    assert set(albums) == {"Artist - Double", "Single Album"}
    assert albums["Artist - Double"].track_count == 2
    assert albums["Single Album"].quality == "16/44.1"


def test_find_albums_labels_duplicate_names(tmp_path):
    from ironbark.library import find_albums

    make_flac(tmp_path / "Backup" / "Same Name" / "01.flac")
    make_flac(tmp_path / "Same Name" / "01.flac")
    names = sorted(a.name for a in find_albums(tmp_path))
    assert names == ["Same Name  (Backup)", f"Same Name  ({tmp_path.name})"]


def test_scan_album_ignores_nested_albums_but_keeps_discs(tmp_path):
    from ironbark.library import scan_album

    make_flac(tmp_path / "01 Loose.flac")
    make_flac(tmp_path / "Disc 2" / "01 Second Disc.flac")
    make_flac(tmp_path / "Some Other Album" / "01 Not Mine.flac")
    assert [t.title for t in scan_album(tmp_path)] == ["Loose", "Second Disc"]


def test_group_by_album_treats_disc_folders_as_one_album(tmp_path):
    from ironbark.library import Track, group_by_album

    a = tmp_path / "Album A"
    b = tmp_path / "Album B"
    tracks = [Track(a / "CD1" / "1.flac", "1"), Track(a / "CD2" / "1.flac", "2"),
              Track(b / "1.flac", "3"), Track(b / "2.flac", "4")]
    assert group_by_album(tracks) == [(a, 0, 2), (b, 2, 4)]


def test_describe_encoder():
    from ironbark.library import describe_encoder

    assert describe_encoder("Lavf62.12.100") == "FFmpeg (Lavf62.12.100)"
    assert describe_encoder(" reference libFLAC 1.2.1 ") == "reference libFLAC 1.2.1"

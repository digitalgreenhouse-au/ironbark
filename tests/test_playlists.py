
import pytest

from ironbark import playlists, settings
from ironbark.library import Track


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "settings.json")


def test_round_trip_keeps_order_and_unicode(tmp_path):
    tracks = [Track(tmp_path / "Lïve" / "01 Rattlesnake.flac", "Rattlesnake", artist="Lïve"),
              Track(tmp_path / "A" / "02 B.wav", "B")]
    path = playlists.path_for("Road trip")
    playlists.write(path, tracks)
    assert playlists.read(path) == [t.path for t in tracks]
    assert path.read_text(encoding="utf-8").startswith("#EXTM3U")
    assert playlists.saved() == [path]


def test_a_line_break_in_a_tag_adds_no_song(tmp_path):
    tracks = [Track(tmp_path / "01 Suite.flac", "Part one\r\nPart two", artist="Band\u2028Name"),
              Track(tmp_path / "02 Coda.flac", "Coda\tend")]
    path = playlists.path_for("Breaks")
    playlists.write(path, tracks)
    assert playlists.read(path) == [t.path for t in tracks]
    assert "#EXTINF:0,Band Name - Part one Part two" in path.read_text(encoding="utf-8").splitlines()


def test_relative_paths_and_old_encodings(tmp_path):
    old = tmp_path / "old.m3u"
    old.write_bytes("#EXTM3U\r\nsub\\Caf\xe9.flac\r\n".encode("cp1252"))
    assert playlists.read(old) == [tmp_path / "sub\\Café.flac"]


def test_names_are_made_safe_and_unique():
    assert playlists.safe_name('Hits: "best" of / 2024?') == "Hits best of  2024"
    playlists.write(playlists.path_for("Playlist"), [])
    assert playlists.unused_name() == "Playlist 2"


def test_rename_refuses_to_overwrite():
    a, b = playlists.path_for("A"), playlists.path_for("B")
    playlists.write(a, [])
    playlists.write(b, [])
    with pytest.raises(FileExistsError):
        playlists.rename(a, "B")
    assert playlists.rename(a, "C").stem == "C"

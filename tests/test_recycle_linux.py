"""Linux's Trash (recycle.py): where a deleted playlist goes, what the file manager is told so it can
put it back, and no Trash for a network folder. The Trash folders are made under tmp_path."""

import os
import sys
from urllib.parse import unquote

import pytest

from ironbark import recycle
from ironbark.recycle import _to_trash as to_trash  # (the real one: conftest stands in for it in every test)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's Trash")


def info_of(note):
    lines = note.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "[Trash Info]"
    fields = dict(line.split("=", 1) for line in lines[1:])
    return unquote(fields["Path"]), fields["DeletionDate"]


def mounts(tmp_path, monkeypatch, *lines):
    table = tmp_path / "mounts"
    table.write_text("".join(f"{line} rw 0 0\n" for line in ("/dev/sda2 / ext4", *lines)), encoding="utf-8")
    monkeypatch.setattr(recycle, "_MOUNTS", str(table))


def test_a_playlist_in_the_home_file_system_goes_to_the_home_trash(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))   # (not made yet: the Trash makes it)
    mounts(tmp_path, monkeypatch)
    trash = tmp_path / "data" / "Trash"
    for text in ("one", "two"):
        mix = tmp_path / "Road trip – Café.m3u8"
        mix.write_text(text, encoding="utf-8")
        assert recycle.can_recycle(mix)
        to_trash(str(mix))
        assert not mix.exists()
    assert (trash / "files" / "Road trip – Café.m3u8").read_text(encoding="utf-8") == "one"
    assert (trash / "files" / "Road trip – Café.2.m3u8").read_text(encoding="utf-8") == "two"   # (not over the first)
    path, when = info_of(trash / "info" / "Road trip – Café.2.m3u8.trashinfo")
    assert path == str(tmp_path / "Road trip – Café.m3u8") and len(when) == 19 and when[10] == "T"
    assert "%20" in (trash / "info" / "Road trip – Café.m3u8.trashinfo").read_text(encoding="utf-8")
    assert oct(trash.stat().st_mode & 0o777) == "0o700"


def test_a_playlist_on_another_disk_goes_to_that_disks_trash(tmp_path, monkeypatch):
    stick = tmp_path / "USB STICK"
    (stick / "Playlists").mkdir(parents=True)
    mounts(tmp_path, monkeypatch, f"/dev/sdb1 {str(stick).replace(' ', chr(92) + '040')} vfat")
    monkeypatch.setattr(recycle, "_device", lambda path: -1)   # (the home folder is on another disk)
    mix = stick / "Playlists" / "Mix.m3u8"
    mix.write_text("#EXTM3U\n", encoding="utf-8")
    assert recycle.can_recycle(mix)
    to_trash(str(mix))
    own = stick / f".Trash-{os.getuid()}"
    assert (own / "files" / "Mix.m3u8").exists()
    assert info_of(own / "info" / "Mix.m3u8.trashinfo")[0] == "Playlists/Mix.m3u8"   # (from the disk's top)
    # A shared .Trash there (sticky, as an administrator makes it) is used first.
    (stick / ".Trash").mkdir()
    os.chmod(stick / ".Trash", 0o1777)
    mix.write_text("#EXTM3U\n", encoding="utf-8")
    to_trash(str(mix))
    assert (stick / ".Trash" / str(os.getuid()) / "files" / "Mix.m3u8").exists()


def test_a_playlist_reached_through_a_link_goes_to_the_trash_of_the_disk_it_is_on(tmp_path, monkeypatch):
    stick = tmp_path / "stick"
    (stick / "Playlists").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    link = tmp_path / "home" / "Music"
    link.symlink_to(stick / "Playlists", target_is_directory=True)   # ~/Music -> a folder on the stick
    mounts(tmp_path, monkeypatch, f"/dev/sdb1 {stick} ext4")
    monkeypatch.setattr(recycle, "_device", lambda path: -1)   # (the home folder is on another disk)
    mix = link / "Mix.m3u8"
    mix.write_text("#EXTM3U\n", encoding="utf-8")
    assert recycle.can_recycle(mix)
    to_trash(str(mix))
    own = stick / f".Trash-{os.getuid()}"
    assert (own / "files" / "Mix.m3u8").exists()
    assert info_of(own / "info" / "Mix.m3u8.trashinfo")[0] == "Playlists/Mix.m3u8"   # (not "../home/…")


def test_a_network_folder_has_no_trash_and_is_never_looked_at(tmp_path, monkeypatch):
    mounts(tmp_path, monkeypatch, "//nas/music /mnt/music cifs", "gvfsd-fuse /run/user/1000/gvfs fuse.gvfsd-fuse")
    monkeypatch.setattr(os, "stat", lambda *a, **k: pytest.fail("a network folder was looked at"))
    assert not recycle.can_recycle("/mnt/music/Mix.m3u8")
    assert not recycle.can_recycle("/run/user/1000/gvfs/smb-share:server=nas,share=music/Mix.m3u8")
    with pytest.raises(OSError, match="no Trash"):
        to_trash("/mnt/music/Mix.m3u8")


def test_only_a_file_goes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(IsADirectoryError):
        recycle.recycle("")   # (would be this folder)
    assert tmp_path.is_dir()


def test_the_trash_is_called_trash():
    assert recycle.BIN_NAME == "Trash"

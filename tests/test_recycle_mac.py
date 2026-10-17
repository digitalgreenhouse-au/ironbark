"""The Mac's Bin (recycle.py): only a file on the same disk as ~/.Trash goes there; one on another
disk or a network share is deleted for good after the player has said so. Runs on any system: the
Mac is stood in for (the home folder and the disks are made under tmp_path)."""

import os
import sys

import pytest

from ironbark import platforms, recycle


@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setattr(platforms, "IS_WINDOWS", False)
    monkeypatch.setattr(platforms, "IS_MAC", True)
    monkeypatch.setattr(platforms, "IS_LINUX", False)
    home = tmp_path / "home"
    (home / ".Trash").mkdir(parents=True)
    monkeypatch.setattr(recycle.Path, "home", classmethod(lambda cls: home))
    return home


def test_a_playlist_on_the_home_disk_can_go_to_the_bin(mac):
    (mac / "Music").mkdir()
    assert recycle.can_recycle(mac / "Music" / "Mix.m3u8")


def test_a_playlist_on_another_disk_or_share_cannot(mac, monkeypatch):
    assert not recycle.can_recycle("/Volumes/USB STICK/Mix.m3u8")
    assert not recycle.can_recycle("/Volumes/MUSIC/Playlists/Mix.m3u8")   # (never looked at: a server may be slow)
    real_stat = os.stat
    other = mac / "Elsewhere"
    other.mkdir()

    def stat(path, *args, **kwargs):   # a folder on another disk, mounted outside /Volumes
        found = real_stat(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(other):
            return os.stat_result((found.st_mode, found.st_ino, found.st_dev + 1, *found[3:]))
        return found
    monkeypatch.setattr(os, "stat", stat)
    assert not recycle.can_recycle(other / "Mix.m3u8")


def test_without_a_bin_nothing_can_go_there(mac):
    (mac / ".Trash").rmdir()
    assert not recycle.can_recycle(mac / "Mix.m3u8")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows names paths its own way")
def test_a_playlist_reached_through_a_link_is_judged_by_where_it_really_is(mac, tmp_path, monkeypatch):
    (tmp_path / "stick").mkdir()
    (mac / "Linked").symlink_to(tmp_path / "stick", target_is_directory=True)
    real_stat = os.stat

    def stat(path, *args, **kwargs):   # the stick is another disk
        found = real_stat(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(tmp_path / "stick"):
            return os.stat_result((found.st_mode, found.st_ino, found.st_dev + 1, *found[3:]))
        return found
    monkeypatch.setattr(os, "stat", stat)
    assert not recycle.can_recycle(mac / "Linked" / "Mix.m3u8")

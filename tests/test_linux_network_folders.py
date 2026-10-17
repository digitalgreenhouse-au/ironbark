"""Linux network folders (mounts.py, places.py): told from the mount table alone, never by asking
the server; named by their share, so two ways to one song are one copy; the cache's server check
used only where it can tell. The parsing tests run anywhere; the classifier ones stand in for Linux
with a mount table of their own."""

import pytest

from ironbark import mounts, places, platforms
from ironbark.places import Kind

TABLE = "\n".join([
    "/dev/sda2 / ext4 rw,relatime 0 0",
    "/dev/sdb1 /media/alex/USB\\040STICK vfat rw 0 0",
    "//192.0.2.10/TUNES /mnt/tunes cifs rw,vers=3.1.1 0 0",
    "//192.0.2.10/TUNES /home/alex/Music\\040on\\040NAS cifs rw 0 0",   # (the same share, again)
    "nas.local:/export/music /mnt/nfs-music nfs4 rw 0 0",
    "alex@nas:/srv/music /home/alex/sshmusic fuse.sshfs rw 0 0",
    "gvfsd-fuse /run/user/1000/gvfs fuse.gvfsd-fuse rw,nosuid 0 0",
    "/dev/sdc1 /mnt/tunes/inner ext4 rw 0 0",   # (a disk mounted inside a share's folder: deepest wins)
]) + "\n"

GVFS = "/run/user/1000/gvfs/smb-share:server=192.0.2.10,share=tunes,user=listener"


def test_the_table_is_read_with_its_escapes_and_the_deepest_mount_wins():
    found = mounts.parse(TABLE)
    assert found[1].point == "/media/alex/USB STICK"
    assert mounts.mount_of("/media/alex/USB STICK/a.flac", found).kind == "vfat"
    assert mounts.mount_of("/mnt/tunes/inner/a.flac", found).kind == "ext4"
    assert mounts.mount_of("/mnt/tunesX/a.flac", found).point == "/"   # (a name that only starts the same)


@pytest.mark.parametrize("path, root, server, smb, share", [
    ("/mnt/tunes/MUSIC/a.flac", "/mnt/tunes", "192.0.2.10", True, "tunes"),
    ("/mnt/nfs-music/a.flac", "/mnt/nfs-music", "nas.local", False, "export/music"),
    ("/home/alex/sshmusic/a.flac", "/home/alex/sshmusic", "nas", False, "srv/music"),
    (GVFS + "/MUSIC/a.flac", GVFS, "192.0.2.10", True, "tunes"),
    ("/run/user/1000/gvfs/sftp:host=nas,user=alex/music/a.flac",
     "/run/user/1000/gvfs/sftp:host=nas,user=alex", "nas", False, ""),
    ("/run/user/1000/gvfs/oddthing/a.flac", "/run/user/1000/gvfs/oddthing", "", False, ""),
])
def test_a_network_folder_is_known_with_its_server(path, root, server, smb, share):
    place = mounts.place_of(path, mounts.parse(TABLE))
    assert place == mounts.Place(root, server, smb, share)


@pytest.mark.parametrize("path", ["/home/alex/Music/a.flac", "/media/alex/USB STICK/a.flac",
                                  "/mnt/tunes/inner/a.flac", "/run/user/1000/gvfs"])
def test_this_computers_folders_are_not(path):
    assert mounts.place_of(path, mounts.parse(TABLE)) is None


@pytest.fixture
def linux(tmp_path, monkeypatch):
    table = tmp_path / "mounts"
    table.write_text(TABLE, encoding="utf-8")
    monkeypatch.setattr(platforms, "IS_LINUX", True)
    monkeypatch.setattr(platforms, "IS_WINDOWS", False)
    classifier = places.Classifier()
    classifier._mounts = mounts.Table(str(table))
    asked = []
    monkeypatch.setattr(places, "_stat", lambda path: asked.append(path) or pytest.fail("a share was looked at"))
    return classifier


def test_songs_in_network_folders_are_shares_and_the_rest_local(linux):
    assert linux.classify("/mnt/tunes/MUSIC/a.flac") is Kind.SHARE
    assert linux.classify(GVFS + "/MUSIC/a.flac") is Kind.SHARE
    assert linux.classify("/mnt/nfs-music/a.flac") is Kind.SHARE
    assert linux.classify("/home/alex/Music/a.flac") is Kind.LOCAL
    assert linux.classify("/mnt/tunes/inner/a.flac") is Kind.LOCAL
    assert linux.any_remote(["/home/alex/Music/a.flac", "/mnt/nfs-music/b.flac"])
    assert not linux.any_remote(["/home/alex/Music/a.flac", "/media/alex/USB STICK/b.flac"])


def test_local_network_access_keeps_them_closed_until_switched_on(linux, monkeypatch):
    monkeypatch.setattr(places, "_default", linux)
    assert not places.allowed("/mnt/tunes/MUSIC/a.flac", {})
    assert places.allowed("/mnt/tunes/MUSIC/a.flac", {"local_network": True})
    assert places.allowed("/home/alex/Music/a.flac", {})


def test_one_song_has_one_name_whichever_folder_reached_it(linux):
    # The same share mounted twice by fstab and opened in the file manager: one copy in the cache.
    assert (linux.source_key("/mnt/tunes/MUSIC/Café/01.flac")
            == linux.source_key("/home/alex/Music on NAS/MUSIC/Café/01.flac")
            == linux.source_key(GVFS + "/MUSIC/Café/01.flac")
            == "smb://192.0.2.10/tunes/MUSIC/Café/01.flac")
    assert linux.source_key("/mnt/tunes/MUSIC/a.flac") != linux.source_key("/mnt/tunes/MUSIC/A.flac")
    assert linux.host_of("/mnt/tunes/MUSIC/a.flac") == "192.0.2.10"
    assert linux.host_of("/run/user/1000/gvfs/oddthing/a.flac") == "share"
    assert linux.unc("/mnt/tunes/MUSIC/a.flac") is None   # (read from where it was found)


def test_only_smb_servers_are_asked_through_their_file_sharing_port(linux):
    assert linux.smb_server("/mnt/tunes/MUSIC/a.flac")
    assert linux.smb_server(GVFS + "/a.flac")
    assert not linux.smb_server("/mnt/nfs-music/a.flac")
    assert not linux.smb_server("/home/alex/sshmusic/a.flac")
    assert not linux.smb_server("/run/user/1000/gvfs/oddthing/a.flac")


def test_a_share_mounted_later_is_seen_within_moments(tmp_path, monkeypatch):
    now = [0.0]
    table = tmp_path / "mounts"
    table.write_text("/dev/sda2 / ext4 rw 0 0\n", encoding="utf-8")
    watched = mounts.Table(str(table), clock=lambda: now[0])
    assert watched.place_of("/mnt/tunes/a.flac") is None
    table.write_text(TABLE, encoding="utf-8")
    assert watched.place_of("/mnt/tunes/a.flac") is None   # (read a moment ago)
    now[0] += mounts.MOUNTS_CACHE_S
    assert watched.place_of("/mnt/tunes/a.flac") is not None


def test_windows_paths_never_reach_the_mount_table(tmp_path, monkeypatch):
    monkeypatch.setattr(platforms, "IS_LINUX", False)
    classifier = places.Classifier()
    assert classifier._mounts is None
    assert classifier.smb_server("\\\\server\\share\\a.flac")

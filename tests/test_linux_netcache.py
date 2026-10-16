"""The network playback cache on Linux, end to end: a song in a mounted network folder (a real
folder here, named a CIFS or NFS mount in a mount table of the test's own) goes through the real
cache — classified as a share, keyed by its server and share, read by the real fetcher from the
folder it was found in, put in place and handed back as a local copy. Runs on any system: Linux is
stood in for."""
from __future__ import annotations

import importlib

import numpy as np
import pytest
import soundfile as sf
from netfakes import FakePlayer, Usage, wait_until

from ironbark import audiofile, mounts, places, platforms
from ironbark.netcache import fetch
from ironbark.netcache.store import READY

manager_mod = importlib.import_module("ironbark.netcache.manager")


class LinuxClassifier(places.Classifier):
    """The real classifier as on Linux, reading the test's mount table."""

    def __init__(self, table):
        super().__init__()
        self._mounts = mounts.Table(str(table))


@pytest.fixture
def share_folder(tmp_path, monkeypatch):
    """(folder, mount table): `folder` stands for a mounted network folder."""
    monkeypatch.setattr(platforms, "IS_LINUX", True)
    monkeypatch.setattr(platforms, "IS_WINDOWS", False)
    folder = tmp_path / "mnt" / "tunes"
    folder.mkdir(parents=True)
    table = tmp_path / "mounts"
    return folder, table


def _manager(tmp_path, classifier, player, probes):
    made = manager_mod.Manager(tmp_path / "NetCache", {"net_verify": True}, player, classifier=classifier,
                               fetcher=fetch.ShareFetcher, probe_host=lambda name: probes.append(name) or True,
                               disk_usage=lambda path: Usage(10 ** 12, 0, 10 ** 12), watch_network=None)
    made.start()
    assert made.wait_loaded(10)
    return made


def _song(folder):
    rng = np.random.default_rng(7)
    audio = (rng.standard_normal((44100 * 2, 2)) * 0.1).astype(np.float32)
    path = folder / "Café" / "01 Song.flac"
    path.parent.mkdir()
    sf.write(path, audio, 44100, subtype="PCM_16")
    return path


@pytest.mark.parametrize("kind, source, smb", [("cifs", "//192.0.2.10/TUNES", True),
                                               ("nfs4", "nas:/export/music", False)])
def test_a_song_in_a_mounted_network_folder_is_cached_and_played_from_the_copy(tmp_path, share_folder, kind,
                                                                               source, smb):
    folder, table = share_folder
    table.write_text(f"/dev/sda2 / ext4 rw 0 0\n{source} {folder.as_posix()} {kind} rw 0 0\n", encoding="utf-8")
    if not str(folder).startswith("/"):
        pytest.skip("Linux paths: runs where tmp_path is a POSIX path")
    path = _song(folder)
    data = path.read_bytes()
    classifier = LinuxClassifier(table)
    probes: list[str] = []
    m = _manager(tmp_path, classifier, FakePlayer([path]), probes)
    try:
        assert classifier.classify(path) is places.Kind.SHARE
        seen = []
        assert wait_until(lambda: seen.append(m.prepare(path)) or isinstance(seen[-1], audiofile.LocalTrack), 20)
        key = classifier.source_key(path)
        item = m.items[key]
        assert item.state == READY and item.source == str(path)   # (read from where it was found)
        assert item.ready_path(item.gen, path).read_bytes() == data
        host = m.hosts[item.host]
        assert host.smb is smb   # (an NFS server isn't asked through the SMB port)
        assert key.startswith("smb://") and ("Café/01 Song.flac" in key)
    finally:
        m.shutdown()


def test_a_song_gone_from_the_network_folder_says_where_it_was(tmp_path, share_folder, monkeypatch):
    folder, table = share_folder
    if not str(folder).startswith("/"):
        pytest.skip("Linux paths: runs where tmp_path is a POSIX path")
    table.write_text(f"/dev/sda2 / ext4 rw 0 0\n//nas/Music {folder.as_posix()} cifs rw 0 0\n", encoding="utf-8")
    monkeypatch.setattr(places, "_default", LinuxClassifier(table))
    gone = folder / "gone.flac"
    with pytest.raises(fetch.Permanent, match="no longer on //nas/music"):
        fetch.ShareFetcher(gone).identity()

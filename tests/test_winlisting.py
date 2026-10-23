"""Folders on network file servers that send the end of a listing unsigned ("Invalid Signature",
error -2146893818, from os.scandir and PowerShell on a mapped NAS share that `dir` and File
Explorer list): winlisting.py lists them the other way and takes that reply as the end. The
Windows tests list a real folder that way; the others fake the failing listing."""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import pytest

from ironbark import winlisting

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="FindFirstFileExW is Windows'")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """No drive or share is known to need the other way yet."""
    monkeypatch.setattr(winlisting, "_other_way", set())


def _signature_error(path) -> OSError:
    error = OSError(22, "Invalid Signature", str(path))
    error.winerror = winlisting.BAD_SIGNATURE
    return error


def test_only_a_server_that_wont_answer_that_way_is_asked_the_other_way():
    def failing(code):
        error = OSError(13, "x")
        error.winerror = code
        return error

    assert winlisting.needs_other_way(_signature_error("Y:\\Music"))
    assert winlisting.needs_other_way(failing(0x80090006))
    assert not winlisting.needs_other_way(failing(5))      # (access denied: asking another way won't help)
    assert not winlisting.needs_other_way(FileNotFoundError(2, "gone"))


def test_a_listing_that_fails_with_a_bad_signature_is_listed_the_other_way(tmp_path, monkeypatch):
    (tmp_path / "a.flac").write_bytes(b"12345")
    (tmp_path / "Disc 1").mkdir()
    seen = []

    def refusing(path="."):
        raise _signature_error(path)

    def basic(folder):
        seen.append(folder)
        stat = winlisting.Stat(0x20, 5, 0, 2_000_000_000, 1_000_000_000, 0)
        return [winlisting.Entry(folder, "a.flac", stat),
                winlisting.Entry(folder, "Disc 1", winlisting.Stat(0x10, 0, 0, 0, 0, 0))]

    monkeypatch.setattr(winlisting, "_scandir", refusing)
    monkeypatch.setattr(winlisting, "_listdir", refusing)
    monkeypatch.setattr(winlisting, "list_basic", basic)
    with winlisting.scandir(tmp_path) as entries:
        found = {entry.name: entry for entry in entries}
    assert seen == [str(tmp_path)] and set(found) == {"a.flac", "Disc 1"}
    song = found["a.flac"]
    assert song.is_file() and not song.is_dir() and not song.is_symlink() and song.path == str(tmp_path / "a.flac")
    assert (song.stat().st_size, song.stat().st_mtime_ns, song.stat().st_birthtime) == (5, 2_000_000_000, 1.0)
    assert found["Disc 1"].is_dir() and not found["Disc 1"].is_junction()
    assert sorted(winlisting.listdir(tmp_path)) == ["Disc 1", "a.flac"]


def test_a_listing_that_fails_at_its_end_is_listed_the_other_way_and_then_so_is_its_share(tmp_path, monkeypatch):
    """The NAS's case: os.scandir starts, gives the names, then its last reply is refused. The
    folder is listed the other way, and the next folder on the same drive straight away."""
    (tmp_path / "a.flac").write_bytes(b"")
    real = os.scandir
    tries = []

    class FailsAtTheEnd:
        def __init__(self, path):
            tries.append(os.fspath(path))
            self._entries = iter(list(real(path)))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            try:
                return next(self._entries)
            except StopIteration:
                raise _signature_error(tmp_path) from None

    basic = []
    monkeypatch.setattr(winlisting, "_scandir", FailsAtTheEnd)
    monkeypatch.setattr(winlisting, "list_basic", lambda folder: basic.append(folder) or [
        winlisting.Entry(folder, "a.flac", winlisting.Stat(0x20, 0, 0, 0, 0, 0))])
    assert [e.name for e in winlisting.scandir(tmp_path)] == ["a.flac"]
    assert tries == [str(tmp_path)] and basic == [str(tmp_path)]
    other = tmp_path / "Other"
    other.mkdir()
    assert [e.name for e in winlisting.scandir(other)] == ["a.flac"]   # (the fake's answer)
    assert tries == [str(tmp_path)] and basic == [str(tmp_path), str(other)]   # (no first try now)


def test_an_unsigned_end_of_listing_is_taken_as_the_end(monkeypatch):
    """Listing the other way: ERROR_NO_MORE_FILES ends it, and so does the bad signature the NAS
    gives its last reply; any other error is an error."""
    def data(name):
        time = SimpleNamespace(dwHighDateTime=0, dwLowDateTime=0)
        return SimpleNamespace(cFileName=name, dwFileAttributes=0x20, nFileSizeHigh=0, nFileSizeLow=3,
                               ftCreationTime=time, ftLastAccessTime=time, ftLastWriteTime=time, dwReserved0=0)

    def listing(last_error):
        names = iter(["..", "b.flac", "c.flac"])
        found = data(".")

        def next_one():
            name = next(names, None)
            if name is None:
                return False
            found.cFileName = name
            return True

        return winlisting._collect("Y:\\MUSIC", found, next_one, lambda: last_error)

    assert [e.name for e in listing(18)] == ["b.flac", "c.flac"]
    assert [e.name for e in listing(0x80090006)] == ["b.flac", "c.flac"]
    monkeypatch.setattr(winlisting, "_error", lambda code, folder: OSError(code, "failed"))
    with pytest.raises(OSError):
        listing(59)


def test_when_the_other_way_fails_too_the_first_error_stands(tmp_path, monkeypatch):
    first = _signature_error(tmp_path)

    def refusing(path="."):
        raise first

    def basic(folder):
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(winlisting, "_scandir", refusing)
    monkeypatch.setattr(winlisting, "list_basic", basic)
    with pytest.raises(OSError) as raised:
        winlisting.scandir(tmp_path)
    assert raised.value is first


def test_other_errors_are_left_as_they_are(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(winlisting, "list_basic", lambda folder: asked.append(folder) or [])
    with pytest.raises(FileNotFoundError):
        winlisting.scandir(tmp_path / "missing")
    assert asked == []


@windows_only
def test_the_other_way_lists_a_folder_as_os_scandir_does(tmp_path):
    (tmp_path / "01 Song.flac").write_bytes(b"x" * 1234)
    (tmp_path / "cover.jpg").write_bytes(b"")
    (tmp_path / "CD2").mkdir()
    time.sleep(0.01)
    with os.scandir(tmp_path) as entries:
        python = {e.name: (e.is_dir(), e.is_file(), e.is_symlink(), e.stat().st_size, e.stat().st_mtime_ns,
                           e.stat().st_birthtime) for e in entries}
    other = {e.name: (e.is_dir(), e.is_file(), e.is_symlink(), e.stat().st_size, e.stat().st_mtime_ns,
                      e.stat().st_birthtime) for e in winlisting.list_basic(str(tmp_path))}
    assert other.keys() == python.keys() == {"01 Song.flac", "cover.jpg", "CD2"}
    for name in python:
        assert other[name][:5] == python[name][:5], name
        assert abs(other[name][5] - python[name][5]) < 1e-3, name


@windows_only
def test_the_other_way_says_why_a_folder_cant_be_listed(tmp_path):
    with pytest.raises(OSError) as raised:
        winlisting.list_basic(str(tmp_path / "missing"))
    assert raised.value.winerror in (2, 3) and raised.value.filename == str(tmp_path / "missing")


@windows_only
def test_on_windows_the_package_puts_it_in_place():
    import pathlib

    assert os.scandir is winlisting.scandir and os.listdir is winlisting.listdir
    assert pathlib.Path(".").exists() and list(pathlib.Path(".").iterdir()) is not None


@windows_only
def test_on_windows_os_walk_and_os_scandir_still_list_a_real_folder(tmp_path):
    (tmp_path / "Album" / "CD1").mkdir(parents=True)
    (tmp_path / "Album" / "CD1" / "01.flac").write_bytes(b"x")
    walked = [(os.path.relpath(here, tmp_path), sorted(dirs), sorted(names)) for here, dirs, names in os.walk(tmp_path)]
    assert walked == [(".", ["Album"], []), ("Album", ["CD1"], []), (os.path.join("Album", "CD1"), [], ["01.flac"])]
    with os.scandir(tmp_path / "Album" / "CD1") as entries:
        (song,) = list(entries)
    assert song.name == "01.flac" and song.is_file() and song.stat().st_size == 1

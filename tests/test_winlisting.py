"""Folders some network file servers list only the way File Explorer asks ("Invalid Signature",
error -2146893818, from os.scandir on a mapped drive Explorer opens): winlisting.py lists them the
other way. The Windows tests list a real folder that way; the others fake the failing os.scandir."""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import pytest

from ironbark import winlisting

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="FindFirstFileExW is Windows'")


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


class _PartListing:
    """os.scandir's listing of a big folder on such a server: the first name comes, then the next
    batch is refused — after os.scandir has returned, while the listing is read."""

    def __init__(self, path, first):
        self.path, self.first, self.given = path, first, 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        self.given += 1
        if self.given == 1:
            return self.first
        raise _signature_error(self.path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


@windows_only   # (the other way is the real FindFirstFileExW)
def test_a_listing_refused_partway_through_is_listed_the_other_way(tmp_path, monkeypatch):
    (tmp_path / "Album 1").mkdir()
    (tmp_path / "Album 2").mkdir()
    real = os.scandir
    first = next(iter(real(tmp_path)))
    made = []

    def part(path="."):
        made.append(_PartListing(path, first))
        return made[-1]

    monkeypatch.setattr(winlisting, "_scandir", part)
    found = [entry.name for entry in winlisting.scandir(str(tmp_path))]
    assert sorted(found) == ["Album 1", "Album 2"]          # all of it, not the first name, then the error
    assert made[0].closed                                     # (the refused listing was let go)
    # os.walk, which the library's look relies on, sees the whole folder too.
    monkeypatch.setattr(os, "scandir", winlisting.scandir)
    assert sorted(next(os.walk(tmp_path))[1]) == ["Album 1", "Album 2"]


def test_a_listing_read_whole_still_works_as_os_scandirs_does(tmp_path):
    (tmp_path / "a.flac").write_bytes(b"123")
    with winlisting.scandir(tmp_path) as entries:
        listed = {entry.name: entry.stat().st_size for entry in entries}
    assert listed == {"a.flac": 3}


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


def test_an_unsigned_end_of_listing_is_taken_as_the_end(monkeypatch):
    """Listing the other way: ERROR_NO_MORE_FILES ends it, and so does the bad signature a NAS can
    give the reply that ends a listing (`dir` lists such a folder; PowerShell and Python didn't);
    any other error is an error."""
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

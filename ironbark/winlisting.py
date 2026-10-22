"""Folder listings on Windows from network file servers that answer only some ways of asking.

Python lists a folder (os.scandir, os.listdir, and so os.walk and Path.iterdir) with
FindFirstFileW, which asks the server for the names a batch at a time. Some NAS boxes and file
servers answer the second batch of a big folder with a reply that isn't signed properly, and
Windows, which requires signed replies, refuses it: "Invalid Signature" (NTE_BAD_SIGNATURE, error
-2146893818). File Explorer asks for large batches (FIND_FIRST_EX_LARGE_FETCH) and lists the same
folder. Seen on a mapped network drive: the first name came, then the error; asked Explorer's way,
all 1,846 names came in a tenth of a second. Its albums (small folders) listed either way.

`install()` (run on Windows when the package is imported, in the player and in its worker
processes) wraps os.scandir and os.listdir: they work as before, and only when one fails with
such an error is the folder listed again the way Explorer asks (FindFirstFileExW, the basic
information, large batches); if that fails too, the first error stands. The error can come at the
start of a listing or partway through it, after os.scandir has returned, so os.scandir's listing
is read whole before it's handed on. The entries it gives
carry what os.scandir's do on Windows (name, path, is_dir / is_file / is_symlink / is_junction,
and stat()'s size and times, from the listing itself), so the code that lists folders needn't know.
"""

from __future__ import annotations

import os
import stat as stat_module
import sys
from collections.abc import Iterator

BAD_SIGNATURE = -2146893818        # 0x80090006, NTE_BAD_SIGNATURE: the server's reply wasn't signed right
# The errors that send a listing to the other way of asking: the bad signature (as Windows reports
# it, signed or not), and a server that doesn't take the request at all.
RETRY_ERRORS = {BAD_SIGNATURE, BAD_SIGNATURE & 0xFFFFFFFF,
                1,    # ERROR_INVALID_FUNCTION
                50,   # ERROR_NOT_SUPPORTED
                59,   # ERROR_UNEXP_NET_ERR
                87}   # ERROR_INVALID_PARAMETER

_DIRECTORY = 0x10                  # FILE_ATTRIBUTE_DIRECTORY
_REPARSE_POINT = 0x400             # FILE_ATTRIBUTE_REPARSE_POINT
_READONLY = 0x1
_TAG_SYMLINK = 0xA000000C          # IO_REPARSE_TAG_SYMLINK
_TAG_JUNCTION = 0xA0000003         # IO_REPARSE_TAG_MOUNT_POINT
_EPOCH = 116444736000000000        # 1970-01-01 in FILETIME's 100 ns steps from 1601
# FindFirstFileExW: the names without short names (FindExInfoBasic), in large batches; then without
# the large batches, for a server that minds those instead.
_WAYS = ((1, 2), (1, 0))           # (info level, flags): FindExInfoBasic, FIND_FIRST_EX_LARGE_FETCH

_scandir = os.scandir
_listdir = os.listdir


def needs_other_way(error: OSError) -> bool:
    """A listing that failed in a way the other way of asking can get past."""
    return getattr(error, "winerror", None) in RETRY_ERRORS


class Stat:
    """What os.DirEntry.stat() gives on Windows, from the listing (no call to the server)."""

    __slots__ = ("st_mode", "st_size", "st_atime_ns", "st_mtime_ns", "st_birthtime_ns", "st_file_attributes",
                 "st_reparse_tag")

    def __init__(self, attributes: int, size: int, atime_ns: int, mtime_ns: int, birth_ns: int, tag: int):
        if attributes & _DIRECTORY:
            mode = stat_module.S_IFDIR | 0o555
        else:
            mode = stat_module.S_IFREG | 0o444
        if not attributes & _READONLY:
            mode |= 0o222
        if attributes & _REPARSE_POINT and tag == _TAG_SYMLINK:
            mode = stat_module.S_IFLNK | (mode & 0o777)
        self.st_mode = mode
        self.st_size = size
        self.st_atime_ns = atime_ns
        self.st_mtime_ns = mtime_ns
        self.st_birthtime_ns = birth_ns
        self.st_file_attributes = attributes
        self.st_reparse_tag = tag

    @property
    def st_atime(self) -> float:
        return self.st_atime_ns / 1e9

    @property
    def st_mtime(self) -> float:
        return self.st_mtime_ns / 1e9

    @property
    def st_birthtime(self) -> float:
        return self.st_birthtime_ns / 1e9

    st_ctime = st_birthtime   # (on Windows, st_ctime is the creation time)


class Entry:
    """One name in a folder listed the other way: what code reads from an os.DirEntry."""

    __slots__ = ("name", "path", "_stat")

    def __init__(self, folder: str, name: str, stat: Stat):
        self.name = name
        self.path = os.path.join(folder, name)
        self._stat = stat

    def __fspath__(self) -> str:
        return self.path

    def __repr__(self) -> str:
        return f"<winlisting.Entry {self.name!r}>"

    def is_symlink(self) -> bool:
        return bool(self._stat.st_file_attributes & _REPARSE_POINT) and self._stat.st_reparse_tag == _TAG_SYMLINK

    def is_junction(self) -> bool:
        return bool(self._stat.st_file_attributes & _REPARSE_POINT) and self._stat.st_reparse_tag == _TAG_JUNCTION

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        if not follow_symlinks and self.is_symlink():
            return False
        return bool(self._stat.st_file_attributes & _DIRECTORY)

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        if not follow_symlinks and self.is_symlink():
            return False
        return not self._stat.st_file_attributes & _DIRECTORY

    def stat(self, *, follow_symlinks: bool = True):
        if follow_symlinks and self.is_symlink():
            return os.stat(self.path)
        return self._stat

    def inode(self) -> int:
        return 0


class Listing:
    """os.scandir's iterator over a listing already read — its os.DirEntry's, or Entry's for a
    folder listed the other way (a context manager, as os.scandir's is)."""

    def __init__(self, entries: list):
        self._entries = iter(entries)

    def __iter__(self) -> Iterator[Entry]:
        return self

    def __next__(self) -> Entry:
        return next(self._entries)

    def __enter__(self) -> Listing:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._entries = iter(())


def list_basic(folder: str) -> list[Entry]:
    """`folder`'s entries asked for as File Explorer asks (each way in _WAYS until one works);
    OSError when none does (the last way's)."""
    error: OSError | None = None
    for level, flags in _WAYS:
        try:
            return _find(folder, level, flags)
        except OSError as failed:
            error = failed
    assert error is not None
    raise error


def _find(folder: str, level: int, flags: int) -> list[Entry]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstFileExW
    find_first.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.WIN32_FIND_DATAW),
                           ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextFileW
    find_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.WIN32_FIND_DATAW)]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL

    data = wintypes.WIN32_FIND_DATAW()
    pattern = os.path.join(folder, "*")
    handle = find_first(pattern, level, ctypes.byref(data), 0, None, flags)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        code = ctypes.get_last_error()
        raise _error(code, folder)
    entries: list[Entry] = []
    try:
        while True:
            name = data.cFileName
            if name not in (".", ".."):
                entries.append(Entry(folder, name, _stat_of(data)))
            if not find_next(handle, ctypes.byref(data)):
                code = ctypes.get_last_error()
                if code == 18:   # ERROR_NO_MORE_FILES
                    return entries
                raise _error(code, folder)
    finally:
        find_close(handle)


def _error(code: int, folder: str) -> OSError:
    import ctypes

    signed = code - (1 << 32) if code >= 1 << 31 else code   # (as Python reports Windows' HRESULT-style codes)
    error = ctypes.WinError(signed)
    error.filename = folder
    return error


def _filetime_ns(time) -> int:
    ticks = (time.dwHighDateTime << 32) | time.dwLowDateTime
    return (ticks - _EPOCH) * 100


def _stat_of(data) -> Stat:
    attributes = int(data.dwFileAttributes)
    tag = int(data.dwReserved0) if attributes & _REPARSE_POINT else 0
    size = (int(data.nFileSizeHigh) << 32) | int(data.nFileSizeLow)
    return Stat(attributes, size, _filetime_ns(data.ftLastAccessTime), _filetime_ns(data.ftLastWriteTime),
                _filetime_ns(data.ftCreationTime), tag)


def scandir(path=None):
    """os.scandir, and a folder it can't list for a reason in RETRY_ERRORS listed the other way —
    whether the server refuses at the start or partway through (the listing is read whole here,
    so an error while it's read is caught too)."""
    if path is None:
        path = "."
    try:
        with _scandir(path) as listing:
            return Listing(list(listing))
    except OSError as error:
        if isinstance(path, (bytes, int)) or not needs_other_way(error):
            raise
        try:
            return Listing(list_basic(os.fspath(path)))
        except OSError:
            raise error from None


def listdir(path=None):
    """os.listdir, and a folder it can't list for a reason in RETRY_ERRORS listed the other way."""
    if path is None:
        path = "."
    try:
        return _listdir(path)
    except OSError as error:
        if isinstance(path, (bytes, int)) or not needs_other_way(error):
            raise
        try:
            return [entry.name for entry in list_basic(os.fspath(path))]
        except OSError:
            raise error from None


def install() -> None:
    """os.scandir and os.listdir (and so os.walk and Path.iterdir) fall back to the other way of
    asking. Windows only; once is enough."""
    if sys.platform != "win32" or os.scandir is scandir:
        return
    os.scandir = scandir   # type: ignore[assignment]
    os.listdir = listdir   # type: ignore[assignment]

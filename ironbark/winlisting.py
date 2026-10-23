"""Folder listings on Windows from network file servers that answer only some ways of asking.

Some NAS boxes and file servers send their "no more files" reply, the one that ends every folder
listing, without signing it properly, and Windows reports "Invalid Signature" (NTE_BAD_SIGNATURE,
error -2146893818) instead of the end of the folder. File Explorer and cmd's `dir` show what came
before it and take it as the end; Python (os.scandir, os.listdir, and so os.walk and Path.iterdir)
and PowerShell's Get-ChildItem throw the whole listing away. (Seen on a NAS share mapped as Y:,
whose folders `dir` listed and Get-ChildItem refused.) Microsoft's note on such servers: they
"don't return a correctly signed error response".

`install()` (run on Windows when the package is imported, in the player and in its worker
processes) wraps os.scandir and os.listdir. A listing is read in full before it's handed on (an
error can come at its end, after the names); when one fails with such an error the folder is
listed again here (FindFirstFileExW: the basic information, in large batches), taking that
unsigned last reply as the end of the folder. From then on folders on the same drive or share are
listed here straight away. If this way fails too, the first error stands. The entries carry what
os.scandir's do on Windows (name, path, is_dir / is_file / is_symlink / is_junction, and stat()'s
size and times, from the listing itself), so the code that lists folders needn't know.
"""

from __future__ import annotations

import logging
import os
import stat as stat_module
import sys
import threading
from collections.abc import Callable, Iterator

log = logging.getLogger(__name__)

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
_other_way: set[str] = set()       # drives and shares (their splitdrive) whose folders are listed here
_other_way_lock = threading.Lock()


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
    """os.scandir's iterator over a listing read in full (a context manager, as its is)."""

    def __init__(self, entries: list):
        self._entries = iter(entries)

    def __iter__(self) -> Iterator:
        return self

    def __next__(self):
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
    handle = find_first(os.path.join(folder, "*"), level, ctypes.byref(data), 0, None, flags)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise _error(ctypes.get_last_error(), folder)
    try:
        return _collect(folder, data, lambda: bool(find_next(handle, ctypes.byref(data))), ctypes.get_last_error)
    finally:
        find_close(handle)


def _collect(folder: str, data, next_one: Callable[[], bool], last_error: Callable[[], int]) -> list[Entry]:
    """The entries of a listing whose first name is in `data` (each next_one() puts the next there,
    False at the end, last_error() then saying why). ERROR_NO_MORE_FILES ends it, and so does a bad
    signature: the server's unsigned "no more files" reply."""
    entries: list[Entry] = []
    while True:
        name = data.cFileName
        if name not in (".", ".."):
            entries.append(Entry(folder, name, _stat_of(data)))
        if next_one():
            continue
        code = last_error()
        if code == 18:   # ERROR_NO_MORE_FILES
            return entries
        if _signed(code) == BAD_SIGNATURE:
            log.debug("listing %s: its end came back unsigned (Invalid Signature): taken as the end", folder)
            return entries
        raise _error(code, folder)


def _signed(code: int) -> int:
    """A Windows error code as Python reports it (an HRESULT-style one comes out negative)."""
    return code - (1 << 32) if code >= 1 << 31 else code


def _error(code: int, folder: str) -> OSError:
    import ctypes

    error = ctypes.WinError(_signed(code))
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


def _anchor(folder: str) -> str:
    """The drive or share a folder is on, as _other_way keeps it."""
    return os.path.splitdrive(os.path.abspath(folder))[0].lower()


def _listed(folder: str, first_try: Callable[[], list]) -> list:
    """`first_try()` (Python's own listing, read in full), or for a drive or share whose server
    needs it, or when that fails in a way RETRY_ERRORS names, the listing asked for here."""
    anchor = _anchor(folder)
    if anchor in _other_way:
        return list_basic(folder)
    try:
        return first_try()
    except OSError as error:
        if not needs_other_way(error):
            raise
        try:
            found = list_basic(folder)
        except OSError as other:
            log.warning("couldn't list %s: %r, and asked the other way: %r", folder, error, other)
            raise error from None
        with _other_way_lock:
            if anchor not in _other_way:
                _other_way.add(anchor)
                log.warning("listing %s failed (%r): folders on %s are listed the other way from now on",
                            folder, error, anchor)
        return found


def scandir(path=None):
    """os.scandir, its listing read in full (an error can come at its end), and a folder it can't
    list for a reason in RETRY_ERRORS listed the other way."""
    if path is None:
        path = "."
    if isinstance(path, (bytes, int)):
        return _scandir(path)

    def first_try() -> list:
        with _scandir(path) as entries:
            return list(entries)

    return Listing(_listed(os.fspath(path), first_try))


def listdir(path=None):
    """os.listdir, and a folder it can't list for a reason in RETRY_ERRORS listed the other way."""
    if path is None:
        path = "."
    if isinstance(path, (bytes, int)):
        return _listdir(path)
    found = _listed(os.fspath(path), lambda: _listdir(path))
    return [entry if isinstance(entry, str) else entry.name for entry in found]


def install() -> None:
    """os.scandir and os.listdir (and so os.walk and Path.iterdir) fall back to the other way of
    asking. Windows only; once is enough."""
    if sys.platform != "win32" or os.scandir is scandir:
        return
    os.scandir = scandir   # type: ignore[assignment]
    os.listdir = listdir   # type: ignore[assignment]

"""Reading a copy that is still downloading: a file object that waits for its bytes.

The decoder (libsndfile) reads a cached copy through `GrowingFile` while it downloads. It says the
file is its final size from the start; a read of bytes already on disk returns them all; a read
of bytes still to come waits for them — up to the gate's deadline (set before each decoder call),
or until something newer wants the handle (`gate.cancel()`), or the copy fails. Giving up returns
0 bytes and notes why on the gate; `audiofile._Guarded` then throws that decoder call's output
away and raises Waiting. It never raises (libsndfile's read callback swallows exceptions and would
just see 0 bytes) and never returns fewer bytes than asked before the true end of the file.

Measured: a cancel reaches a read waiting inside libsndfile in 1.5 ms, and the wait lets go of
Python's global lock. The copy is opened so that it can be renamed (to its finished name) and
deleted while it's being read: on Windows with CreateFileW and FILE_SHARE_DELETE.
"""

from __future__ import annotations

import io
import os
import sys
import time

from ..audiofile import Gate

__all__ = ["Gate", "GrowingFile", "open_shared"]

WAIT_SLICE_S = 0.02   # a waiting read looks at cancel at least this often


def open_shared(path) -> io.RawIOBase:
    """Open a file for reading so that others may still write, rename and delete it."""
    if sys.platform != "win32":
        return io.open(os.fspath(path), "rb", buffering=0)
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.restype = wintypes.HANDLE
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    generic_read, share_all, open_existing = 0x80000000, 0x1 | 0x2 | 0x4, 3
    handle = create(os.fspath(path), generic_read, share_all, None, open_existing, 0x80, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        error = ctypes.get_last_error()
        raise OSError(0, ctypes.FormatError(error), os.fspath(path), error)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    return io.open(fd, "rb", buffering=0)


class GrowingFile(io.RawIOBase):
    """One reader of one version (`gen`) of one cached copy (`item`). `role` says who reads:
    "play", "proof", "probe" or "verify". Holds a lease on the version while open. `furthest` is
    the furthest byte any read has needed (the probe records it as where an open reads to)."""

    def __init__(self, item, gen: int, gate: Gate, role: str = "play"):
        super().__init__()
        self.item, self.gen, self.gate, self.role = item, gen, gate, role
        self.size = int(item.size)
        self.furthest = 0
        self._pos = 0
        self._file = None
        item.acquire(gen)
        gate.wakers.append(item.notify)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.size}[whence]
        self._pos = max(0, int(base + offset))
        return self._pos

    def _give_up(self, reason: str) -> int:
        if not self.gate.gave_up:
            self.gate.gave_up = reason
        return 0

    def _wait_for(self, end: int) -> bool:
        """Wait until bytes [.., end) of this version are on disk. False (and the gate notes why)
        when it gives up first."""
        item, gate = self.item, self.gate
        item.demand(self.gen, self._pos, end, self.role)
        gate.waiting = True
        try:
            while True:
                if gate.cancel is not None and gate.cancel():
                    return not self._give_up("cancel")
                with item.cond:
                    if item.has(self.gen, end):
                        return True
                    if item.gen_failed(self.gen):
                        return not self._give_up("failed")
                    left = gate.deadline - time.monotonic()
                    if left <= 0:
                        return not self._give_up("patience")
                    item.cond.wait(min(left, WAIT_SLICE_S))
        finally:
            gate.waiting = False

    def _open(self):
        if self._file is None:
            self._file = self.item.open_bytes(self.gen)
        return self._file

    def readinto(self, buffer) -> int:
        try:
            view = memoryview(buffer).cast("B")
            n = min(len(view), max(0, self.size - self._pos))
            if n <= 0 or self.gate.gave_up:
                return 0
            end = self._pos + n
            self.furthest = max(self.furthest, end)
            self.gate.furthest = max(self.gate.furthest, end)
            if not self.item.has_now(self.gen, end) and not self._wait_for(end):
                return 0
            f = self._open()
            f.seek(self._pos)
            done = 0
            while done < n:
                got = f.readinto(view[done:n])
                if not got:
                    return self._give_up("failed")   # the copy on disk is shorter than it said
                done += got
            self._pos = end
            return n
        except Exception:   # noqa: BLE001 — never raise into the decoder: a read that can't happen gives up
            return self._give_up("failed")

    def close(self):
        if self.closed:
            return
        try:
            if self._file is not None:
                self._file.close()
        finally:
            try:
                self.gate.wakers.remove(self.item.notify)
            except ValueError:
                pass
            self.item.release(self.gen)
            super().close()

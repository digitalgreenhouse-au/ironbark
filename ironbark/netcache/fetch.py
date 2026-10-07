"""Reading a song's bytes from where it really lives. Called only on the cache's I/O threads:
every call here may wait on the network (SMB may hold a read for 60 s).

Shares (and OneDrive files) are read through Windows' own file system, as any program reads
them — so they need no "Allow network access". The file's identity is its size and modification
time, the rule the library already uses: if either changes, it's a different file.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    size: int
    mtime_ns: int | None
    etag: str | None = None
    last_modified: str | None = None
    ranges: bool = True

    def same_file(self, other: "Identity | None") -> bool:
        return other is not None and self.size == other.size and self.mtime_ns == other.mtime_ns


class Transient(Exception):
    """Couldn't read just now (the server isn't answering, the connection dropped): try again later."""


class Permanent(Exception):
    """The source is gone or refused (deleted, moved, not allowed): no point trying again until asked."""


class Changed(Exception):
    """The source is a different file from the one being downloaded (edited, replaced)."""


class NetworkOff(Exception):
    """Ironbark isn't allowed to connect to the internet itself (web addresses only)."""


_GONE = {2, 3}   # Windows: file not found, path not found (the server answered: it isn't there)


def _classify(error: OSError) -> Exception:
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        return Permanent(str(error)) if winerror in _GONE else Transient(str(error))
    if isinstance(error, (FileNotFoundError, NotADirectoryError, PermissionError)):
        return Permanent(str(error))
    return Transient(str(error))


class ShareFetcher:
    """One song on a share (or OneDrive). The file is kept open between chunks (unbuffered), so a
    resumed or continued read is just a seek."""

    def __init__(self, path):
        self.path = os.fspath(path)
        self._file = None

    def identity(self) -> Identity:
        try:
            st = os.stat(self.path)
        except OSError as error:
            raise _classify(error) from error
        return Identity(int(st.st_size), int(st.st_mtime_ns))

    def read(self, offset: int, n: int) -> bytes:
        """Up to `n` bytes from `offset` (fewer only at the end of the file)."""
        try:
            if self._file is None:
                self._file = io.open(self.path, "rb", buffering=0)
            self._file.seek(offset)
            parts, got = [], 0
            while got < n:
                piece = self._file.read(n - got)
                if not piece:
                    break
                parts.append(piece)
                got += len(piece)
            return b"".join(parts)
        except OSError as error:
            self.close()
            raise _classify(error) from error

    def close(self) -> None:
        f, self._file = self._file, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass

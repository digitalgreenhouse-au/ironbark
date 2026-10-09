"""Reading a song's bytes from where it really lives. Called only on the cache's I/O threads:
every call here may wait on the network (SMB may hold a read for 60 s).

Shares (and OneDrive files) are read through Windows' own file system, as any program reads
them — so they need no "Allow network access". The file's identity is its size and modification
time, the rule the library already uses: if either changes, it's a different file. A file is held
open between chunks in a way that still lets others rename or delete it (Explorer, another
computer), and let go as soon as its download stops (so a tag save can replace it); a file that
changed meanwhile is caught by its identity at the end.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .growing import open_shared


@dataclass(frozen=True)
class Identity:
    size: int
    mtime_ns: int | None
    etag: str | None = None
    last_modified: str | None = None
    ranges: bool = True

    def same_file(self, other: Identity | None) -> bool:
        return other is not None and self.size == other.size and self.mtime_ns == other.mtime_ns


class Transient(Exception):
    """Couldn't read just now (the server isn't answering, the connection dropped): try again later."""
    detail = ""   # Windows' own words, for the log


class Permanent(Exception):
    """The source is gone or refused (deleted, moved, not allowed): no point trying again until asked.
    Its text is said to the listener ("Skipped “x.flac”: …"); `detail` keeps Windows' own words for the log."""
    detail = ""


class Changed(Exception):
    """The source is a different file from the one being downloaded (edited, replaced)."""


class NetworkOff(Exception):
    """Ironbark isn't allowed to connect to the internet itself (web addresses only)."""


_GONE = {2, 3}   # Windows: file not found, path not found (the server answered: it isn't there)
_REFUSED = {5, 32, 33}   # access denied, open in another program, part of it locked (the server answered: no)


def _where(path) -> str:
    """Where a song was, in a few words: `\\\\server\\share`, a drive letter, or "the drive"."""
    text = os.fspath(path).replace("/", "\\") if path else ""
    if text.startswith("\\\\"):
        parts = text[2:].split("\\")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return "\\\\" + parts[0] + "\\" + parts[1]
    elif len(text) >= 2 and text[1] == ":":
        return text[:2].upper()
    return "the drive"


def _classify(error: OSError, path=None) -> Exception:
    """Gone or refused (Permanent, said in plain words), or anything else (Transient: the network)."""
    winerror = getattr(error, "winerror", None)
    if winerror in _GONE or (winerror is None and isinstance(error, (FileNotFoundError, NotADirectoryError))):
        found = Permanent(f"it's no longer on {_where(path or error.filename)}")
    elif winerror in _REFUSED or (winerror is None and isinstance(error, PermissionError)):
        found = Permanent("the server won't let Ironbark read it (it may be open in another program)")
    else:
        found = Transient(str(error))
    found.detail = str(error)
    return found


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
            raise _classify(error, self.path) from error
        return Identity(int(st.st_size), int(st.st_mtime_ns))

    def read(self, offset: int, n: int) -> bytes:
        """Up to `n` bytes from `offset` (fewer only at the end of the file)."""
        try:
            if self._file is None:
                self._file = open_shared(self.path)
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
            raise _classify(error, self.path) from error

    def close(self) -> None:
        f, self._file = self._file, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass

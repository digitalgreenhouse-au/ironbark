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
import threading
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
    """Where a song was, in a few words: `\\\\server\\share`, a drive letter, or "the drive" (Linux:
    the share's folder, `//server/share`, or the folder it was in)."""
    raw = os.fspath(path) if path else ""
    if raw.startswith("/") and not raw.startswith("//"):
        from .. import places

        try:
            key = places.source_key(raw)
        except Exception:   # noqa: BLE001 — only wording: never let it hide the real error
            key = ""
        if key.startswith("smb://"):
            parts = key[len("smb://"):].split("/")
            return "//" + "/".join(parts[:2]) if len(parts) >= 2 else "the network folder"
        return "the network folder" if key.startswith("smb:") else "the drive"
    text = raw.replace("/", "\\")
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
    found: Permanent | Transient
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
    resumed or continued read is just a seek.

    `close()` never waits. Windows' C runtime holds a file's lock for the whole of a read, and a
    close (or seek) from another thread waits for it — for a read stuck on a server that has
    stopped answering, a minute or more, and the cache calls `close()` with its own lock held (a tag
    save on the window's thread, quitting). So a close that comes while a read is under way only
    marks the file, and the thread reading closes it as soon as its read returns."""

    def __init__(self, path):
        self.path = os.fspath(path)
        self._file = None
        self._guard = threading.Lock()   # (held only for a moment: never across a read)
        self._reading = 0                # reads under way
        self._close_due = False          # close() came while a read was under way

    def identity(self) -> Identity:
        try:
            st = os.stat(self.path)
        except OSError as error:
            raise _classify(error, self.path) from error
        return Identity(int(st.st_size), int(st.st_mtime_ns))

    def read(self, offset: int, n: int) -> bytes:
        """Up to `n` bytes from `offset` (fewer only at the end of the file)."""
        with self._guard:
            self._reading += 1
            f = self._file   # (None: opened here — after a close, a read opens the file afresh)
        try:
            if f is None:
                f = open_shared(self.path)
                with self._guard:
                    self._file = f
            f.seek(offset)
            parts, got = [], 0
            while got < n:
                piece = f.read(n - got)
                if not piece:
                    break
                parts.append(piece)
                got += len(piece)
            return b"".join(parts)
        except OSError as error:
            with self._guard:
                self._close_due = True
            raise _classify(error, self.path) from error
        finally:
            with self._guard:
                self._reading -= 1
                due = self._close_due and not self._reading
                if due:
                    f, self._file, self._close_due = self._file, None, False
            if due:
                _quietly_close(f)

    def close(self) -> None:
        """Let go of the file on the server: at once, or — a read under way — as that read returns."""
        with self._guard:
            if self._reading:
                self._close_due = True
                return
            f, self._file = self._file, None
        _quietly_close(f)


def _quietly_close(f) -> None:
    if f is not None:
        try:
            f.close()
        except OSError:
            pass

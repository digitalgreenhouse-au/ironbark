"""Small file helpers shared by the tag readers and writers. No UI."""

from __future__ import annotations

import os
from pathlib import Path

COPY_CHUNK = 1 << 20


class TagError(ValueError):
    """A file's tags can't be read or written: it's damaged, or not the format it was opened as."""


def read_at(f, offset: int, size: int) -> bytes:
    """Exactly `size` bytes at `offset`; TagError if the file ends first (damaged / cut short)."""
    f.seek(offset)
    data = f.read(size)
    if len(data) != size:
        raise TagError("the file ends sooner than its own headers say (damaged or cut short)")
    return data


def file_size(f) -> int:
    f.seek(0, os.SEEK_END)
    return f.tell()


def replace_region(path, start: int, end: int, data: bytes) -> None:
    """Replace bytes [start, end) of the file with `data`, in place: the rest of the file is moved
    along (in 1 MB pieces) and the file grows or is cut to fit. The same file is rewritten — not
    replaced by a new one — so it keeps its identity and creation time. Not crash-safe on its own:
    the tag editor only ever calls this on its own temporary copy (tag_editor.prepare), which is
    checked before it's swapped in."""
    delta = len(data) - (end - start)
    with open(Path(path), "r+b") as f:
        size = file_size(f)
        if delta > 0:       # grow: move the tail back, last piece first
            f.truncate(size + delta)
            at = size
            while at > end:
                n = min(COPY_CHUNK, at - end)
                at -= n
                f.seek(at)
                piece = f.read(n)
                f.seek(at + delta)
                f.write(piece)
        elif delta < 0:     # shrink: move the tail forward, first piece first
            at = end
            while at < size:
                n = min(COPY_CHUNK, size - at)
                f.seek(at)
                piece = f.read(n)
                f.seek(at + delta)
                f.write(piece)
                at += n
            f.truncate(size + delta)
        f.seek(start)
        f.write(data)


def overwrite(path, source) -> None:
    """Copy the file `source` over `path`, in place (same file, new contents)."""
    with open(source, "rb") as src, open(Path(path), "r+b") as out:
        while True:
            piece = src.read(COPY_CHUNK)
            if not piece:
                break
            out.write(piece)
        out.truncate()

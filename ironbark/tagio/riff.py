"""The ID3 tag inside WAV and AIFF / AIFF-C files: read and write. No UI.

Written from the published layouts (clean-room — no other tag library's code was read):
- WAV is a RIFF file (Microsoft/IBM "Multimedia Programming Interface and Data Specifications
  1.0", 1991): "RIFF", a 32-bit little-endian size, "WAVE", then chunks — a 4-character ID, a
  32-bit little-endian size, the data, and a pad byte when the size is odd.
- AIFF / AIFF-C (Apple "Audio Interchange File Format" 1.3 and the AIFF-C draft): the same idea
  in big-endian — "FORM", size, "AIFF" or "AIFC", then chunks (ID, 32-bit big-endian size,
  data, pad byte when odd).
- The tag itself is an ordinary ID3v2 tag (id3.py) stored as the data of a chunk called "id3 "
  or "ID3 " — the convention FFmpeg, foobar2000 and other taggers use; neither RIFF nor AIFF
  defines it. WAV's own RIFF INFO list and AIFF's NAME / AUTH chunks are read by libsndfile
  (library.py) and left untouched here.

Writing replaces the ID3 chunk's data (or adds a new chunk at the end of the file) and updates
the chunk's and the file's size fields; the audio chunk isn't moved unless the tag sits before
it and grows.
"""

from __future__ import annotations

import struct
from pathlib import Path

from . import id3
from ._files import TagError, file_size, read_at, reading, replace_region

NEW_PADDING = 1024
_ID3_IDS = (b"id3 ", b"ID3 ")


class ChunkFile:
    """A WAV (little-endian RIFF) or AIFF (big-endian FORM) file's chunks and its ID3 tag."""

    big_endian = False
    magic = b"RIFF"
    forms: tuple[bytes, ...] = (b"WAVE",)
    new_id = b"id3 "

    @reading
    def __init__(self, path):
        self.path = Path(path)
        order = ">I" if self.big_endian else "<I"
        self.chunks: list[tuple[bytes, int, int]] = []   # (ID, where its header starts, data size)
        self.tags: id3.ID3Tag | None = None
        self.damaged_tag = False          # an ID3 chunk that won't parse or be followed to its end: never rewritten
        with open(self.path, "rb") as f:
            size = file_size(f)
            head = read_at(f, 0, 12) if size >= 12 else b""
            if head[:4] != self.magic or head[8:12] not in self.forms:
                raise TagError(f"not a {self.forms[0].decode()} file")
            at = 12
            while at + 8 <= size:
                header = read_at(f, at, 8)
                chunk_id, length = header[:4], struct.unpack(order, header[4:])[0]
                self.chunks.append((chunk_id, at, length))
                if chunk_id in _ID3_IDS and self.tags is None:
                    data = read_at(f, at + 8, min(length, size - at - 8))
                    try:
                        self.tags = id3.ID3Tag.parse(data)
                        self.damaged_tag = bool(self.tags.problem)
                    except TagError:
                        self.damaged_tag = True
                at += 8 + length + (length & 1)
            self.size = size

    def _id3_chunk(self):
        return next((c for c in self.chunks if c[0] in _ID3_IDS), None)

    def add_tags(self) -> id3.ID3Tag:
        if self.tags is None:
            self.tags = id3.ID3Tag([], (2, 3, 0))
        return self.tags

    def _pack(self, value: int) -> bytes:
        return struct.pack(">I" if self.big_endian else "<I", value)

    def save(self, major: int) -> None:
        if self.damaged_tag:
            raise TagError(id3.damaged_reason(self.tags))
        tag = self.add_tags()
        body = tag.render(major)
        chunk = self._id3_chunk()
        if chunk is not None:
            chunk_id, at, length = chunk
            padding = length - len(body) if len(body) <= length else NEW_PADDING
            data = tag.render(major, padding)
            old_end = at + 8 + length + (length & 1)
            new = chunk_id + self._pack(len(data)) + data + (b"\x00" if len(data) & 1 else b"")
            replace_region(self.path, at, min(old_end, self.size), new)
        else:
            data = tag.render(major, 0)
            new = self.new_id + self._pack(len(data)) + data + (b"\x00" if len(data) & 1 else b"")
            end = self.size + (self.size & 1)   # chunks start on an even byte
            with open(self.path, "r+b") as f:
                f.seek(self.size)
                f.write(b"\x00" * (end - self.size) + new)
        self._fix_form_size()
        type(self).__init__(self, self.path)   # re-read in place

    def delete(self) -> None:
        if self.damaged_tag:
            raise TagError(id3.damaged_reason(self.tags))
        chunk = self._id3_chunk()
        if chunk is not None:
            _chunk_id, at, length = chunk
            replace_region(self.path, at, min(at + 8 + length + (length & 1), self.size), b"")
            self._fix_form_size()
        type(self).__init__(self, self.path)   # re-read in place

    def _fix_form_size(self) -> None:
        with open(self.path, "r+b") as f:
            f.seek(0, 2)
            total = f.tell()
            f.seek(4)
            f.write(self._pack(total - 8))


class WaveFile(ChunkFile):
    big_endian = False
    magic = b"RIFF"
    forms = (b"WAVE",)
    new_id = b"id3 "


class AiffFile(ChunkFile):
    big_endian = True
    magic = b"FORM"
    forms = (b"AIFF", b"AIFC")
    new_id = b"ID3 "

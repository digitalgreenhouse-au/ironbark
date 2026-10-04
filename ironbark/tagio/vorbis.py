"""Vorbis comments and FLAC picture blocks — shared by FLAC, Ogg Vorbis and Ogg Opus. No UI.

Written from the published specifications (clean-room; no other tag library's code was read):

- Vorbis comments: Xiph.Org "Ogg Vorbis I format specification: comment field and header
  specification" (xiph.org/vorbis/doc/v-comment.html) and RFC 9639 §8.6 (FLAC's VORBIS_COMMENT
  block). Layout, all lengths 32-bit little-endian: vendor length, vendor string (UTF-8), number
  of comments, then each comment as length + "NAME=value" (UTF-8). Field names are ASCII
  0x20–0x7D except "=", compared without regard to case; a name may appear more than once (two
  GENRE comments = two genres). In an Ogg Vorbis header packet the list is followed by a single
  "framing bit" byte (1); FLAC and Opus (RFC 7845 §5.2) have none.
- Pictures: RFC 9639 §8.8 (the PICTURE block), all numbers 32-bit big-endian: picture type
  (3 = front cover), media type length + ASCII media type, description length + UTF-8
  description, width, height, colour depth, number of colours (indexed pictures), data length +
  the picture's bytes. Ogg files carry the same block, base64-encoded, in a
  METADATA_BLOCK_PICTURE comment (Xiph wiki, "VorbisComment", read 04/10/2026).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ._files import TagError


def _take(data: bytes, at: int, size: int) -> tuple[bytes, int]:
    if size < 0 or at + size > len(data):
        raise TagError("a Vorbis comment runs past the end of its block (damaged)")
    return data[at: at + size], at + size


class VComment:
    """A Vorbis comment block: the vendor string and the (name, value) list, in file order
    (names kept as written; looked up without regard to case)."""

    def __init__(self, vendor: str = "", items: list[tuple[str, str]] | None = None):
        self.vendor = vendor
        self.items: list[tuple[str, str]] = list(items or [])

    @classmethod
    def parse(cls, data: bytes, at: int = 0) -> tuple["VComment", int]:
        """(the comments, where they end in `data`). Comments without "=" are skipped."""
        (length,) = struct.unpack_from("<I", data, at) if at + 4 <= len(data) else (-1,)
        vendor, at = _take(data, at + 4, length)
        if at + 4 > len(data):
            raise TagError("a Vorbis comment block is cut short")
        (count,) = struct.unpack_from("<I", data, at)
        at += 4
        items = []
        for _ in range(count):
            if at + 4 > len(data):
                raise TagError("a Vorbis comment block is cut short")
            (size,) = struct.unpack_from("<I", data, at)
            raw, at = _take(data, at + 4, size)
            text = raw.decode("utf-8", "replace")
            name, eq, value = text.partition("=")
            if eq:
                items.append((name, value))
        return cls(vendor.decode("utf-8", "replace"), items), at

    def render(self) -> bytes:
        vendor = self.vendor.encode("utf-8")
        out = [struct.pack("<I", len(vendor)), vendor, struct.pack("<I", len(self.items))]
        for name, value in self.items:
            entry = f"{name}={value}".encode("utf-8")
            out += [struct.pack("<I", len(entry)), entry]
        return b"".join(out)

    # -- dictionary-style access (names without regard to case) --
    def get(self, name: str, default=None) -> list[str]:
        wanted = name.lower()
        found = [v for k, v in self.items if k.lower() == wanted]
        return found if found else ([] if default is None else default)

    def __contains__(self, name: str) -> bool:
        wanted = name.lower()
        return any(k.lower() == wanted for k, _ in self.items)

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def delete(self, name: str) -> None:
        wanted = name.lower()
        self.items = [(k, v) for k, v in self.items if k.lower() != wanted]

    def set(self, name: str, values: list[str]) -> None:
        """Replace every `name` comment with `values` (added at the end, in order)."""
        self.delete(name)
        self.items += [(name, value) for value in values]

    def names(self) -> list[str]:
        """Each name once, upper case, in first-seen order."""
        out: list[str] = []
        for k, _ in self.items:
            if k.upper() not in out:
                out.append(k.upper())
        return out


def valid_name(name: str) -> bool:
    """A field name the Vorbis comment spec allows: ASCII 0x20–0x7D, no "="."""
    return bool(name) and all(0x20 <= ord(c) <= 0x7D and c != "=" for c in name)


@dataclass
class Picture:
    """A FLAC PICTURE block (also used, base64-encoded, by Ogg's METADATA_BLOCK_PICTURE)."""
    type: int = 3
    mime: str = ""
    desc: str = ""
    width: int = 0
    height: int = 0
    depth: int = 0
    colors: int = 0
    data: bytes = b""

    @classmethod
    def parse(cls, block: bytes) -> "Picture":
        try:
            kind, length = struct.unpack_from(">II", block, 0)
            mime, at = _take(block, 8, length)
            (length,) = struct.unpack_from(">I", block, at)
            desc, at = _take(block, at + 4, length)
            width, height, depth, colors, length = struct.unpack_from(">IIIII", block, at)
            data, _ = _take(block, at + 20, length)
        except struct.error as error:
            raise TagError("a picture block is cut short") from error
        return cls(kind, mime.decode("ascii", "replace"), desc.decode("utf-8", "replace"),
                   width, height, depth, colors, data)

    def render(self) -> bytes:
        mime = self.mime.encode("ascii", "replace")
        desc = self.desc.encode("utf-8")
        return (struct.pack(">II", self.type, len(mime)) + mime + struct.pack(">I", len(desc)) + desc
                + struct.pack(">IIIII", self.width, self.height, self.depth, self.colors, len(self.data))
                + bytes(self.data))

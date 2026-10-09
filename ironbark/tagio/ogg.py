"""Ogg Vorbis and Ogg Opus: the audio facts and the Vorbis comments — read and write. No UI.

Written from the published specifications (clean-room — no other tag library's code was read):
- Ogg pages, RFC 3533 §6: "OggS", version 0, header type (0x01 continues a packet from the
  previous page, 0x02 first page of the stream, 0x04 last page), granule position (64-bit
  little-endian; −1 = no packet finishes on this page), stream serial number, page sequence
  number, CRC-32 (polynomial 0x04C11DB7, computed over the whole page with the CRC field set to
  zero), the number of segments, the segment ("lacing") table, then the data. A packet is a
  run of segments: 255 means the packet continues, less than 255 ends it (a packet whose length
  is a multiple of 255 ends with a 0). At most 255 segments per page.
- Vorbis, Xiph.Org "Vorbis I specification" §4.2 and §5: three header packets — identification
  ("\\x01vorbis": version, channels, sample rate, maximum / nominal / minimum bitrate),
  comment ("\\x03vorbis" + the comments + a framing bit), setup ("\\x05vorbis") — and the
  first audio packet starts a fresh page. Length = the last page's granule position (samples)
  ÷ the sample rate.
- Opus, RFC 7845 §5: "OpusHead" (version, channels, pre-skip at byte 10, original input sample
  rate at byte 12), then "OpusTags" (the comments, optionally followed by binary data that's
  kept when its first byte's lowest bit is 1 — kept here in every case); the comment header
  "MUST finish the page on which it completes" (§3). Length = (granule − pre-skip) ÷ 48000.

Writing rebuilds the pages that hold the header packets after the first (comment, and for
Vorbis the setup packet) with granule 0 (−1 on a page where no packet ends), sequence numbers
following the first page, and a fresh CRC. When the new comment needs a different number of
pages, every later page of the stream is renumbered and its CRC recomputed (the audio data in
them is copied unchanged); bytes after the last page that aren't an Ogg page (an ID3v1 or APE
tag some programs append, or a cut-off last page) are copied as they are. A file whose last
header page also carries sound isn't written: rebuilding that page would drop it.
"""

from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

from ._files import COPY_CHUNK, TagError, file_size, overwrite, read_at, reading, replace_region
from .vorbis import VComment

LAST_PAGE_TRIES = 256   # candidate last pages checked (CRC) before the length is given up as unknown

# Ogg's CRC is the same polynomial as zlib's CRC-32, but not reflected and with no inversion at
# the start or end. Fed bytes with their bits reversed, zlib's (reflected) CRC from a zero
# register gives the bit-reversed Ogg CRC — so the work runs at zlib's C speed.
_REVERSED_BITS = bytes(int(f"{n:08b}"[::-1], 2) for n in range(256))


def crc32(data: bytes) -> int:
    """Ogg's CRC-32 (RFC 3533 §6: polynomial 0x04C11DB7, initial value 0, not reflected)."""
    register = zlib.crc32(bytes(data).translate(_REVERSED_BITS), 0xFFFFFFFF) ^ 0xFFFFFFFF
    return int(f"{register:032b}"[::-1], 2)


class Page:
    __slots__ = ("offset", "flags", "granule", "serial", "sequence", "lacing", "data", "size")

    @classmethod
    def read(cls, f, offset: int, size: int) -> Page:
        head = read_at(f, offset, 27)
        if head[:4] != b"OggS" or head[4] != 0:
            raise TagError("an Ogg page is damaged (no \"OggS\" where one should be)")
        page = cls()
        page.offset = offset
        page.flags = head[5]
        page.granule, page.serial, page.sequence = struct.unpack_from("<qII", head, 6)
        count = head[26]
        page.lacing = read_at(f, offset + 27, count)
        page.data = read_at(f, offset + 27 + count, sum(page.lacing))
        page.size = 27 + count + len(page.data)
        return page

    def render(self) -> bytes:
        head = (b"OggS\x00" + bytes([self.flags]) + struct.pack("<qII", self.granule, self.serial, self.sequence)
                + b"\x00\x00\x00\x00" + bytes([len(self.lacing)]) + bytes(self.lacing))
        page = head + self.data
        return page[:22] + struct.pack("<I", crc32(page)) + page[26:]


def _split(pages: list[Page]) -> tuple[list[bytes], bool]:
    """(the complete packets in these pages, whether a packet is still open at the end). One
    pass: a packet's segments on one page are a single slice, joined across pages once."""
    packets: list[bytes] = []
    pieces: list[bytes] = []
    for page in pages:
        start = at = 0
        for value in page.lacing:
            at += value
            if value < 255:
                pieces.append(page.data[start:at])
                packets.append(b"".join(pieces))
                pieces, start = [], at
        if start < at:
            pieces.append(page.data[start:at])
    return packets, bool(pieces)


def _ends(page: Page) -> int:
    """How many packets end on this page (each segment shorter than 255 bytes ends one)."""
    return sum(1 for value in page.lacing if value < 255)


def paginate(packets: list[bytes], serial: int, first_sequence: int) -> list[Page]:
    """Header packets laid out in pages (granule 0, or −1 on a page where no packet ends); the
    last packet finishes its page."""
    pages: list[Page] = []
    lacing: list[int] = []
    data: list[bytes] = []
    continued = ends = False   # the page being filled: starts mid-packet / has a packet ending on it

    def flush():
        page = Page()
        page.flags = 0x01 if continued else 0
        page.granule = 0 if ends else -1
        page.serial, page.sequence = serial, first_sequence + len(pages)
        page.lacing, page.data = bytes(lacing), b"".join(data)
        pages.append(page)

    for packet in packets:
        sizes = [255] * (len(packet) // 255) + [len(packet) % 255]
        at = 0
        for i, value in enumerate(sizes):
            if len(lacing) == 255:
                flush()
                lacing, data, continued, ends = [], [], i > 0, False
            lacing.append(value)
            data.append(packet[at: at + value])
            at += value
        ends = True
    if lacing:
        flush()
    return pages


class OggFile:
    """The first logical stream of an Ogg file: its header packets, comments and length."""

    header_packets = 2
    codec = ""

    @reading
    def __init__(self, path):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            self.size = file_size(f)
            first = Page.read(f, 0, self.size)
            self.serial = first.serial
            self.pages = [first]          # the pages holding the header packets
            at = first.size
            ended = _ends(first)
            while ended < self.header_packets:
                if at >= self.size:
                    raise TagError("the Ogg file ends inside its headers")
                page = Page.read(f, at, self.size)
                at += page.size
                if page.serial == self.serial:
                    self.pages.append(page)
                    ended += _ends(page)
            self.headers_end = at
            packets, still_open = _split(self.pages)
            self.packets = packets[: self.header_packets]
            # Sound sharing the last header page (not allowed by the specs, but seen): that page
            # is rebuilt from the header packets alone on a save, so such a file isn't written.
            self.sound_on_header_page = len(packets) > self.header_packets or still_open
            self.last_granule = self._last_granule(f)
        self._parse_headers()

    def _last_granule(self, f) -> int:
        """The granule position of the stream's last page, searched for from the end (a page
        only counts when its CRC checks out, so "OggS" inside audio data can't fool it). At most
        LAST_PAGE_TRIES candidates are checked, so a file full of fake page headers can't stall it."""
        pos = self.size
        tries = 0
        while pos > 0:
            start = max(0, pos - (1 << 16))
            chunk = read_at(f, start, min(self.size, pos + 26) - start)
            at = chunk.rfind(b"OggS")
            while at >= 0:
                if at + 27 <= len(chunk) and chunk[at + 4] == 0:
                    granule, serial = struct.unpack_from("<qI", chunk, at + 6)
                    if serial == self.serial and granule >= 0:
                        tries += 1
                        if tries > LAST_PAGE_TRIES:
                            return 0
                        try:
                            page = Page.read(f, start + at, self.size)
                            stored = struct.unpack_from("<I", chunk, at + 22)[0]
                            if page.render()[22:26] == struct.pack("<I", stored):
                                return granule
                        except TagError:
                            pass
                at = chunk.rfind(b"OggS", 0, at)
            pos = start
        return 0

    def _parse_headers(self):
        raise NotImplementedError

    def _comment_packet(self) -> bytes:
        raise NotImplementedError

    def add_tags(self) -> VComment:
        return self.tags

    @property
    def info(self):
        return self   # (the audio facts are attributes of the file itself)

    def __getitem__(self, name: str) -> list[str]:
        return self.tags.get(name)

    def __setitem__(self, name: str, value) -> None:
        """ogg["genre"] = ["Rock", "Pop"] (or one string): replaces every `name` comment."""
        self.tags.set(name, list(value) if isinstance(value, (list, tuple)) else [value])

    def save(self) -> None:
        packets = [self._comment_packet()] + self.packets[2:]
        old = self.pages[1:]            # the pages after the first (the identification packet's own page)
        if self.pages[0].lacing and _split([self.pages[0]]) != (self.packets[:1], False):
            raise TagError("this Ogg file's first page holds more than its first header — not written")
        if self.sound_on_header_page:
            raise TagError("this Ogg file has sound on the page that ends its headers — not written "
                           "(rewriting that page would lose it)")
        new = paginate(packets, self.serial, self.pages[0].sequence + 1)
        delta = len(new) - len(old)
        header = b"".join(page.render() for page in new)
        start = self.pages[1].offset if len(self.pages) > 1 else self.pages[0].size
        # Pages of other streams sitting among the header pages would be lost: refuse instead.
        if self.headers_end - start != sum(p.size for p in old):
            raise TagError("this Ogg file mixes several streams in its headers — not written")
        if delta == 0:   # same number of pages: only the header pages change
            replace_region(self.path, start, self.headers_end, header)
        else:            # every later page is renumbered (and gets a fresh CRC)
            temp = self.path.with_name(self.path.name + ".tagio")
            try:
                with open(self.path, "rb") as src, open(temp, "wb") as out:
                    out.write(read_at(src, 0, start))
                    out.write(header)
                    at = self.headers_end
                    while at < self.size:
                        try:
                            page = Page.read(src, at, self.size)
                        except TagError:   # not an Ogg page: whatever follows is copied as it is
                            src.seek(at)
                            shutil.copyfileobj(src, out, COPY_CHUNK)
                            break
                        if page.serial == self.serial:
                            page.sequence = (page.sequence + delta) & 0xFFFFFFFF
                            out.write(page.render())
                        else:
                            out.write(read_at(src, at, page.size))
                        at += page.size
                overwrite(self.path, temp)
            finally:
                if temp.exists():
                    temp.unlink()
        self.__init__(self.path)


class OggVorbisFile(OggFile):
    header_packets = 3
    codec = "vorbis"

    def _parse_headers(self):
        ident, comment = self.packets[0], self.packets[1]
        if ident[:7] != b"\x01vorbis" or len(ident) < 29 or comment[:7] != b"\x03vorbis":
            raise TagError("not an Ogg Vorbis file")
        self.channels = ident[11]
        self.sample_rate, maximum, nominal, minimum = struct.unpack_from("<Iiii", ident, 12)
        self.tags, _ = VComment.parse(comment, 7)
        self.length = self.last_granule / self.sample_rate if self.sample_rate else 0.0
        if nominal > 0:
            self.bitrate = nominal
        elif maximum > 0 and minimum > 0:
            self.bitrate = (maximum + minimum) // 2
        else:
            self.bitrate = 0

    def _comment_packet(self) -> bytes:
        return b"\x03vorbis" + self.tags.render() + b"\x01"


class OggOpusFile(OggFile):
    header_packets = 2
    codec = "opus"

    def _parse_headers(self):
        head, comment = self.packets[0], self.packets[1]
        if head[:8] != b"OpusHead" or len(head) < 19 or comment[:8] != b"OpusTags":
            raise TagError("not an Ogg Opus file")
        self.channels = head[9]
        self.pre_skip, self.input_rate = struct.unpack_from("<HI", head, 10)
        self.sample_rate = 48000
        self.tags, end = VComment.parse(comment, 8)
        self.extra = comment[end:]     # binary data after the comments (RFC 7845 §5.2): kept
        self.length = max(0, self.last_granule - self.pre_skip) / 48000
        audio_bytes = self.size - self.headers_end
        self.bitrate = int(audio_bytes * 8 / self.length + 0.5) if self.length else 0

    def _comment_packet(self) -> bytes:
        return b"OpusTags" + self.tags.render() + self.extra


def sniff(head: bytes) -> str:
    """"vorbis", "opus", "flac" (FLAC in Ogg) or "" from the first page's first packet."""
    if head[:4] != b"OggS" or len(head) < 28:
        return ""
    body = head[27 + head[26]:]
    if body.startswith(b"\x01vorbis"):
        return "vorbis"
    if body.startswith(b"OpusHead"):
        return "opus"
    if body.startswith(b"\x7fFLAC"):
        return "flac"
    return ""

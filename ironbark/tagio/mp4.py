"""MP4 / M4A files (Apple Lossless and AAC): the audio facts and the iTunes-style tags — read and
write. No UI.

Written from the published descriptions (clean-room — no other tag library's code was read):
- Boxes ("atoms"), ISO/IEC 14496-12: a 32-bit big-endian size (1 = a 64-bit size follows, 0 =
  to the end of the file) and a 4-character type. Used here: moov → trak → mdia → mdhd (time
  scale, duration: version 0 32-bit, version 1 64-bit), hdlr ("soun" = the sound track), minf →
  stbl → stsd (the sample entry: "alac" with its ALACSpecificConfig, or "mp4a" with an "esds"
  descriptor), stco / co64 (where each chunk of audio sits in the file).
- Tags, Apple's QuickTime File Format "Metadata" chapter and the iTunes conventions documented
  by AtomicParsley (atomicparsley.sourceforge.net/mpeg-4files.html, read 05/10/2026): moov →
  udta → meta (a full box: 4 bytes version/flags; its hdlr says "mdir" / "appl") → ilst. Each
  item in ilst is a box named for the field ("©nam" title, "©ART" artist, "aART" album artist,
  "©alb" album, "©day" date, "©gen" genre text, "gnre" a numbered genre (ID3v1 number + 1),
  "©wrt" composer, "©cmt" comment, "©too" encoder, "trkn" / "disk" (16-bit number and total
  after 2 zero bytes), "cpil" compilation, "covr" pictures, "tmpo" tempo…) holding one or more
  "data" boxes: a type indicator (Apple's "well-known types" table: 0 binary, 1 UTF-8, 2
  UTF-16BE, 13 JPEG, 14 PNG, 21 big-endian signed integer, 22 unsigned, 27 BMP), a 4-byte locale,
  then the value. "----" items are free-form: a "mean" box (the owner, e.g.
  "com.apple.iTunes"), a "name" box, then "data".

Writing rebuilds only the ilst box (items not changed are copied byte for byte), resizing meta,
udta and moov to match. When moov sits before the audio ("fast start" files), the audio moves
by the same amount, so every chunk offset in every track's stco / co64 table is shifted to
match; a "free" box straight after ilst is used up first so small edits move nothing.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from ._files import TagError, file_size, read_at, reading, replace_region

FREE_PADDING = 1024   # spare room left after ilst (in a "free" box) when moov has to grow
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts"}
TEXT_UTF8, TEXT_UTF16, JPEG, PNG, BMP, SIGNED, UNSIGNED = 1, 2, 13, 14, 27, 21, 22
_BOOL_KEYS = {"cpil", "pgap", "pcst", "hdvd"}
_PAIR_KEYS = {"trkn", "disk"}


def boxes(data: bytes, start: int = 0, end: int | None = None):
    """(type, header start, body start, end) for each box in data[start:end]."""
    end = len(data) if end is None else end
    at = start
    while at + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, at)
        header = 8
        if size == 1:
            if at + 16 > end:
                return
            size, header = struct.unpack_from(">Q", data, at + 8)[0], 16
        elif size == 0:
            size = end - at
        if size < header or at + size > end:
            return   # damaged: stop rather than guess
        yield kind, at, at + header, at + size
        at += size


def _find(data: bytes, start: int, end: int, kind: bytes):
    return next(((h, s, e) for k, h, s, e in boxes(data, start, end) if k == kind), None)


def box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(body), kind) + body


# -- values ------------------------------------------------------------------------------------
class Cover(bytes):
    """A picture from "covr" (the image file's bytes); `imageformat` = 13 JPEG / 14 PNG / 27 BMP."""

    imageformat: int

    def __new__(cls, data: bytes, imageformat: int = JPEG):
        value = super().__new__(cls, data)
        value.imageformat = imageformat
        return value


class FreeForm(bytes):
    """A free-form ("----") value's bytes; `dataformat` = its data type (1 = UTF-8 text)."""

    dataformat: int

    def __new__(cls, data: bytes, dataformat: int = TEXT_UTF8):
        value = super().__new__(cls, data)
        value.dataformat = dataformat
        return value


@dataclass
class Data:
    type: int
    locale: int
    payload: bytes


@dataclass
class Item:
    key: str                 # "©nam", "trkn", or "----:com.apple.iTunes:NAME"
    data: list[Data]
    raw: bytes | None = None   # the item's box as read (kept when unchanged)

    def values(self) -> list:
        return [_value(self.key, d) for d in self.data]

    def render(self) -> bytes:
        if self.raw is not None:
            return self.raw
        body = b""
        if self.key.startswith("----:"):
            _, mean, name = self.key.split(":", 2)
            body += box(b"mean", b"\x00\x00\x00\x00" + mean.encode("utf-8"))
            body += box(b"name", b"\x00\x00\x00\x00" + name.encode("utf-8"))
            name4 = b"----"
        else:
            name4 = self.key.encode("latin-1")
        for d in self.data:
            body += box(b"data", struct.pack(">II", d.type & 0xFFFFFF, d.locale) + d.payload)
        return box(name4, body)


def _value(key: str, d: Data):
    if key.startswith("----:"):
        return FreeForm(d.payload, d.type)
    if key in _PAIR_KEYS:
        number = struct.unpack_from(">H", d.payload, 2)[0] if len(d.payload) >= 4 else 0
        total = struct.unpack_from(">H", d.payload, 4)[0] if len(d.payload) >= 6 else 0
        return (number, total)
    if key == "covr" or d.type in (JPEG, PNG, BMP):
        return Cover(d.payload, d.type)
    if d.type == TEXT_UTF8:
        return d.payload.decode("utf-8", "replace")
    if d.type == TEXT_UTF16:
        return d.payload.decode("utf-16-be", "replace")
    if key == "gnre" and len(d.payload) >= 2:
        return struct.unpack_from(">H", d.payload)[0]
    if d.type in (SIGNED, UNSIGNED, 0) and len(d.payload) in (1, 2, 3, 4, 8) and (key in _BOOL_KEYS or d.type):
        number = int.from_bytes(d.payload, "big", signed=d.type == SIGNED)
        return bool(number) if key in _BOOL_KEYS else number
    return bytes(d.payload)


def data_for(key: str, value) -> Data:
    """A data box for a Python value (str, (number, total), bool, int, Cover, FreeForm, bytes)."""
    if isinstance(value, FreeForm):
        return Data(value.dataformat, 0, bytes(value))
    if isinstance(value, Cover):
        return Data(value.imageformat, 0, bytes(value))
    if key in _PAIR_KEYS:
        number, total = value
        tail = b"" if key == "disk" else b"\x00\x00"   # iTunes writes disk as 6 bytes, trkn as 8
        return Data(0, 0, struct.pack(">HHH", 0, number, total) + tail)
    if isinstance(value, bool):
        return Data(SIGNED, 0, bytes([1 if value else 0]))
    if key == "gnre":
        return Data(0, 0, struct.pack(">H", value))
    if isinstance(value, int):
        size = 2 if key == "tmpo" else 1 if -128 <= value <= 127 and key in ("rtng", "stik") else 4
        return Data(SIGNED, 0, value.to_bytes(size, "big", signed=True))
    if isinstance(value, bytes):
        return Data(0, 0, bytes(value))
    return Data(TEXT_UTF8, 0, str(value).encode("utf-8"))


def parse_items(data: bytes, start: int = 0, end: int | None = None) -> list[Item]:
    items = []
    for kind, head, body, stop in boxes(data, start, end):
        name = kind.decode("latin-1")
        found: list[Data] = []
        mean = item_name = ""
        for sub, _h, sbody, sstop in boxes(data, body, stop):
            if sub == b"data" and sstop - sbody >= 8:
                type_, locale = struct.unpack_from(">II", data, sbody)
                found.append(Data(type_ & 0xFFFFFF, locale, data[sbody + 8: sstop]))
            elif sub == b"mean":
                mean = data[sbody + 4: sstop].decode("utf-8", "replace")
            elif sub == b"name":
                item_name = data[sbody + 4: sstop].decode("utf-8", "replace")
        if kind == b"----":
            name = f"----:{mean}:{item_name}"
        items.append(Item(name, found, data[head: stop]))
    return items


class MP4Tags:
    """The ilst items, in file order. Mapping-style access by key; values come back as Python
    values (see _value)."""

    def __init__(self, items: list[Item] | None = None):
        self.items: list[Item] = list(items or [])

    def keys(self) -> list[str]:
        return [i.key for i in self.items]

    def get(self, key: str, default=None):
        for item in self.items:
            if item.key == key:
                return item.values()
        return default

    def __getitem__(self, key: str):
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: str) -> bool:
        return any(i.key == key for i in self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __setitem__(self, key: str, values) -> None:
        if not isinstance(values, list):
            values = [values]
        item = Item(key, [data_for(key, v) for v in values])
        for i, old in enumerate(self.items):
            if old.key == key:
                self.items[i] = item
                self.items = [x for j, x in enumerate(self.items) if j <= i or x.key != key]
                return
        self.items.append(item)

    def pop(self, key: str, default=None):
        value = self.get(key, default)
        self.items = [i for i in self.items if i.key != key]
        return value

    def clear(self) -> None:
        self.items = []

    def render(self) -> bytes:
        return b"".join(i.render() for i in self.items)


# -- the file ------------------------------------------------------------------------------------
@dataclass
class MP4Info:
    length: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    bits_per_sample: int = 0
    bitrate: int = 0
    codec: str = ""                # the sample entry: "alac", "mp4a"…
    codec_description: str = ""    # e.g. "ALAC", "AAC LC"


_AAC_TYPES = {1: "AAC Main", 2: "AAC LC", 3: "AAC SSR", 4: "AAC LTP", 5: "HE-AAC", 29: "HE-AACv2"}


def _descriptor(data: bytes, at: int) -> tuple[int, int, int]:
    """(tag, body start, body end) of an MPEG-4 descriptor (ISO/IEC 14496-1: a tag byte, then a
    size of 1–4 bytes, 7 bits each, high bit = more follow)."""
    tag = data[at]
    size, at = 0, at + 1
    for _ in range(4):
        byte = data[at]
        at += 1
        size = (size << 7) | (byte & 0x7F)
        if not byte & 0x80:
            break
    return tag, at, at + size


def _esds(info: MP4Info, data: bytes, start: int, end: int) -> None:
    """The average bitrate and AAC profile from an "esds" box (best effort)."""
    try:
        tag, body, stop = _descriptor(data, start + 4)   # ES_Descriptor after version/flags
        if tag != 3:
            return
        flags = data[body + 2]
        at = body + 3 + (2 if flags & 0x80 else 0)
        if flags & 0x40:
            at += 1 + data[at]
        if flags & 0x20:
            at += 2
        tag, body, stop = _descriptor(data, at)          # DecoderConfigDescriptor
        if tag != 4:
            return
        info.bitrate = struct.unpack_from(">I", data, body + 9)[0]
        if stop > body + 13:
            tag, sbody, _ = _descriptor(data, body + 13)   # DecoderSpecificInfo: AudioSpecificConfig
            if tag == 5:
                object_type = data[sbody] >> 3
                info.codec_description = _AAC_TYPES.get(object_type, "AAC")
    except (IndexError, struct.error):
        pass


class MP4File:
    """An MP4 / M4A file's audio facts (`info`) and tags (`tags`: MP4Tags or None)."""

    @reading
    def __init__(self, path):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            self.size = file_size(f)
            head = read_at(f, 0, 8) if self.size >= 8 else b""
            if head[4:8] != b"ftyp":
                raise TagError("not an MP4 file")
            self.top: list[tuple[bytes, int, int]] = []   # (type, start, end) of each top-level box
            at = 0
            while at + 8 <= self.size:
                size, kind = struct.unpack(">I4s", read_at(f, at, 8))
                header = 8
                if size == 1:
                    size, header = struct.unpack(">Q", read_at(f, at + 8, 8))[0], 16
                elif size == 0:
                    size = self.size - at
                if size < header or at + size > self.size:
                    break
                self.top.append((kind, at, at + size))
                at += size
            moov = next((b for b in self.top if b[0] == b"moov"), None)
            if moov is None:
                raise TagError("no moov box in this MP4 file")
            self.moov_start, self.moov_end = moov[1], moov[2]
            self.moov = read_at(f, moov[1], moov[2] - moov[1])
        self.info = self._read_info()
        self.tags = self._read_tags()

    # -- reading --
    def _meta(self):
        """(meta header start, its children's start, end) — meta is a full box (4 bytes of
        version/flags) in iTunes files; QuickTime-style files omit them, so look first."""
        udta = _find(self.moov, 8, len(self.moov), b"udta")
        if udta is None:
            return None
        meta = _find(self.moov, udta[1], udta[2], b"meta")
        if meta is None:
            return None
        head, body, end = meta
        children = body + 4
        if body + 8 <= end and self.moov[body + 4: body + 8] in (b"hdlr", b"ilst", b"keys", b"free"):
            children = body      # no version/flags
        return head, children, end

    def _read_tags(self) -> MP4Tags | None:
        meta = self._meta()
        if meta is None:
            return None
        ilst = _find(self.moov, meta[1], meta[2], b"ilst")
        if ilst is None:
            return None
        return MP4Tags(parse_items(self.moov, ilst[1], ilst[2]))

    def _read_info(self) -> MP4Info:
        info = MP4Info()
        moov = self.moov
        for kind, _h, start, end in boxes(moov, 8):
            if kind != b"trak":
                continue
            mdia = _find(moov, start, end, b"mdia")
            if mdia is None:
                continue
            hdlr = _find(moov, mdia[1], mdia[2], b"hdlr")
            if hdlr is None or moov[hdlr[1] + 8: hdlr[1] + 12] != b"soun":
                continue
            mdhd = _find(moov, mdia[1], mdia[2], b"mdhd")
            if mdhd is not None:
                body = mdhd[1]
                if moov[body] == 1:
                    scale, duration = struct.unpack_from(">IQ", moov, body + 20)
                else:
                    scale, duration = struct.unpack_from(">II", moov, body + 12)
                if scale:
                    info.length = duration / scale
            minf = _find(moov, mdia[1], mdia[2], b"minf")
            stbl = _find(moov, minf[1], minf[2], b"stbl") if minf else None
            stsd = _find(moov, stbl[1], stbl[2], b"stsd") if stbl else None
            if stsd is None:
                break
            entry = next(boxes(moov, stsd[1] + 8, stsd[2]), None)
            if entry is None:
                break
            codec, _eh, ebody, eend = entry
            info.codec = codec.decode("latin-1").strip()
            # AudioSampleEntry: 6 reserved + 2 index, version (2) + 6 reserved, channels, sample
            # size, 2 + 2, rate as 16.16; QuickTime version 1 / 2 entries add 16 / 36 bytes.
            version = struct.unpack_from(">H", moov, ebody + 8)[0]
            info.channels, info.bits_per_sample = struct.unpack_from(">HH", moov, ebody + 16)
            info.sample_rate = struct.unpack_from(">I", moov, ebody + 24)[0] >> 16
            children = ebody + 28 + {1: 16, 2: 36}.get(version, 0)
            if codec == b"alac":
                cookie = _find(moov, children, eend, b"alac")
                if cookie is not None and cookie[2] - cookie[1] >= 28:
                    (_frame, _compat, bits, _pb, _mb, _kb, channels, _run, _max, avg, rate) = struct.unpack_from(
                        ">IBBBBBBHIII", moov, cookie[1] + 4)
                    info.bits_per_sample, info.channels, info.sample_rate = bits, channels, rate
                    info.bitrate = avg
                info.codec_description = "ALAC"
                if not info.bitrate:
                    info.bitrate = info.sample_rate * info.bits_per_sample * info.channels
            elif codec == b"mp4a":
                esds = _find(moov, children, eend, b"esds")
                if esds is not None:
                    _esds(info, moov, esds[1], esds[2])
            break
        return info

    # -- writing --
    def add_tags(self) -> MP4Tags:
        if self.tags is None:
            self.tags = MP4Tags()
        return self.tags

    def __getitem__(self, key: str):
        return self.add_tags()[key]

    def __setitem__(self, key: str, values) -> None:
        self.add_tags()[key] = values

    def save(self) -> None:
        tags = self.add_tags()
        moov = bytearray(self.moov)
        new_ilst_body = tags.render()
        meta = self._meta()
        if meta is None:
            # hdlr (a full box): pre-defined 0, handler "mdir", reserved (iTunes puts "appl"
            # first), an empty name.
            hdlr = box(b"hdlr", bytes(8) + b"mdir" + b"appl" + bytes(9))
            meta_box = box(b"meta", bytes(4) + hdlr + box(b"ilst", new_ilst_body)
                           + box(b"free", bytes(FREE_PADDING - 8)))
            udta = _find(bytes(moov), 8, len(moov), b"udta")
            if udta is None:
                moov += box(b"udta", meta_box)
                path_sizes = [0]
                delta = len(meta_box) + 8
            else:
                moov[udta[2]: udta[2]] = meta_box
                path_sizes = [0, udta[0]]
                delta = len(meta_box)
        else:
            head, children, end = meta
            ilst = _find(bytes(moov), children, end, b"ilst")
            udta = _find(bytes(moov), 8, len(moov), b"udta")
            if ilst is None:
                insert = box(b"ilst", new_ilst_body)
                moov[end:end] = insert
                delta = len(insert)
            else:
                old_start, old_end = ilst[0], ilst[2]
                free = _find(bytes(moov), old_end, end, b"free")
                room_end = old_end
                if free is not None and free[0] == old_end:
                    room_end = free[2]      # the free box straight after ilst is spare room
                new = box(b"ilst", new_ilst_body)
                room = room_end - old_start
                if len(new) == room or len(new) + 8 <= room:
                    spare = room - len(new)
                    if spare:
                        new += box(b"free", bytes(spare - 8))
                    delta = 0
                else:
                    new += box(b"free", bytes(FREE_PADDING - 8))
                    delta = len(new) - room
                moov[old_start:room_end] = new
            path_sizes = [0, udta[0], head]
        for at in path_sizes:   # moov, udta, meta grow by delta
            size = struct.unpack_from(">I", moov, at)[0]
            if size == 1:
                big = struct.unpack_from(">Q", moov, at + 8)[0]
                struct.pack_into(">Q", moov, at + 8, big + delta)
            else:
                struct.pack_into(">I", moov, at, size + delta)
        if delta and any(kind == b"mdat" and start > self.moov_start for kind, start, _ in self.top):
            self._shift_chunks(moov, delta)
        replace_region(self.path, self.moov_start, self.moov_end, bytes(moov))
        type(self).__init__(self, self.path)   # re-read in place

    def _shift_chunks(self, moov: bytearray, delta: int) -> None:
        """Every chunk offset that points past moov moves by delta (stco: 32-bit, co64: 64-bit)."""
        data = bytes(moov)
        old_end = self.moov_end

        def walk(start: int, end: int):
            for kind, _h, body, stop in boxes(data, start, end):
                if kind in _CONTAINERS:
                    walk(body, stop)
                elif kind in (b"stco", b"co64"):
                    count = struct.unpack_from(">I", data, body + 4)[0]
                    wide = kind == b"co64"
                    step, fmt = (8, ">Q") if wide else (4, ">I")
                    for k in range(count):
                        at = body + 8 + k * step
                        offset = struct.unpack_from(fmt, data, at)[0]
                        if offset >= old_end:
                            offset += delta
                            if not wide and not 0 <= offset < 1 << 32:
                                raise TagError("the MP4 is too big to move its audio — not written")
                            struct.pack_into(fmt, moov, at, offset)

        walk(8, len(data))

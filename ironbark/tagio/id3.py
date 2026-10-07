"""ID3 tags (MP3, and the ID3 chunk inside WAV and AIFF files): read v1, v2.2, v2.3 and v2.4;
write v2.3 and v2.4. No UI.

Written from the id3.org documents (clean-room — no other tag library's code was read):
"ID3 tag version 2.4.0 - Main Structure" and "- Native Frames", "ID3 tag version 2.3.0",
"ID3 tag version 2" (2.2, id3v2-00) and the ID3v1 layout; all read 05/10/2026.

The tag (v2.4 §3.1): "ID3", major and revision version bytes, flags (%abcd0000: a =
unsynchronisation, b = extended header, c = experimental, d = footer), then the size of
everything after the 10-byte header as a 28-bit "synchsafe" integer (7 bits per byte, §6.2).
Frames follow (§4): a 4-character ID, the size (v2.4: synchsafe; v2.3: plain 32-bit), and two
flag bytes — v2.4 %0abc0000 %0h00kmnp (h grouping byte follows, k zlib-compressed, m encrypted
(method byte follows), n unsynchronised, p a 4-byte data length follows); v2.3 %abc00000
%ijk00000 (i compressed — a 4-byte decompressed size follows, j encrypted, k grouping). v2.2
frames have a 3-character ID and a 3-byte size, no flags. Padding (zero bytes) may follow the
frames.

Unsynchronisation (v2.3 §5, v2.4 §6.1): a writer may insert a 0x00 after every 0xFF so that no
false MPEG sync appears in the tag; a reader removes the 0x00 from every "FF 00". In v2.2/v2.3 it
applies to the whole tag; in v2.4 to each frame flagged n (the tag flag means all frames are).
This writer never applies it (it's optional, and every modern reader copes without).

Text (v2.4 §4 / "Native Frames" §4.2): the first byte is the encoding — 0 ISO-8859-1, 1 UTF-16
with a byte-order mark, 2 UTF-16BE (v2.4 only), 3 UTF-8 (v2.4 only). v2.4 text frames may hold
several values separated by a null; v2.3 separates some lists with "/". Frames understood here:
text frames (T***), TXXX (description + value), COMM / USLT (encoding, 3-letter language,
description, text), APIC (encoding, MIME type, picture type, description, data; v2.2 PIC has a
3-letter image format instead of a MIME type). Every other frame is kept as it was read; one that
can't be read at all (encrypted, or compressed data that won't inflate within MAX_INFLATED or to
the size it states) is kept exactly as stored, flags included. When the frames can't be followed
to the padding (an unreadable frame ID, a frame running past the end, a v2.2 frame with no v2.3
name), the tag's `problem` says why, and the file classes refuse to rewrite it: what came after
would be lost.

Dates: v2.4 has TDRC ("2004-05-03", ISO 8601); v2.3 has TYER (yyyy), TDAT (DDMM) and TIME
(HHMM). A v2.3 tag is read as TDRC (TYER + TDAT + TIME joined: "2004-05-03T10:20") and written
back as TYER/TDAT/TIME; likewise TORY ⇄ TDOR. Genres (TCON): v2.3 writes ID3v1 numbers as
"(17)", "(4)Eurodisco", "(RX)" = Remix, "(CR)" = Cover, "((" = a literal "("; v2.4 as plain
values "17", "RX", "CR" — all are turned into names (`genres`).

ID3v1 (the last 128 bytes of an MP3): "TAG", title 30, artist 30, album 30, year 4, comment 30
(v1.1: comment 28, a zero byte, track number), genre number. Read when a file has no ID3v2 tag;
kept up to date (if present) when the v2 tag is written.
"""

from __future__ import annotations

import struct
import zlib

from . import genres as genre_list
from ._files import TagError

LATIN1, UTF16, UTF16BE, UTF8 = 0, 1, 2, 3
HEADER = 10
MAX_INFLATED = 32 << 20   # compressed frames: at most this many bytes inflated per tag, all frames together
MAX_VALUES = 4096         # values in one text frame (more: the frame is kept as it was, unread)
_V22_IDS = {  # id3v2-00 (2.2) frame IDs -> their v2.3/v2.4 equivalents (a tag with others isn't rewritten)
    "TT1": "TIT1", "TT2": "TIT2", "TT3": "TIT3", "TP1": "TPE1", "TP2": "TPE2", "TP3": "TPE3",
    "TP4": "TPE4", "TAL": "TALB", "TRK": "TRCK", "TPA": "TPOS", "TYE": "TYER", "TDA": "TDAT",
    "TIM": "TIME", "TCO": "TCON", "TCM": "TCOM", "TEN": "TENC", "TSS": "TSSE", "TCR": "TCOP",
    "TBP": "TBPM", "TLA": "TLAN", "TOR": "TORY", "TPB": "TPUB", "TRC": "TSRC", "TXT": "TEXT",
    "TCP": "TCMP", "TXX": "TXXX", "COM": "COMM", "ULT": "USLT", "PIC": "APIC",
}
_PIC_FORMATS = {"JPG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "BMP": "image/bmp"}


# -- low-level helpers ----------------------------------------------------------------------
def synchsafe(data: bytes) -> int:
    value = 0
    for byte in data:
        value = (value << 7) | (byte & 0x7F)
    return value


def to_synchsafe(value: int, size: int = 4) -> bytes:
    if value >= 1 << (7 * size):
        raise TagError("the ID3 tag is too big")
    return bytes((value >> (7 * (size - 1 - i))) & 0x7F for i in range(size))


def deunsync(data: bytes) -> bytes:
    return data.replace(b"\xff\x00", b"\xff")


def _terminator(encoding: int) -> bytes:
    return b"\x00\x00" if encoding in (UTF16, UTF16BE) else b"\x00"


def _utf16_end(data: bytes, start: int = 0) -> int:
    """Where the first UTF-16 terminator at or after `start` begins (two zero bytes on a 2-byte
    boundary counted from `start`), or −1."""
    at = data.find(b"\x00\x00", start)
    while at >= 0 and (at - start) % 2:
        at = data.find(b"\x00\x00", at + 1)
    return at


def _split_term(encoding: int, data: bytes) -> tuple[bytes, bytes]:
    """(first string's bytes, the rest after its terminator); no terminator → (all, b"")."""
    if encoding in (UTF16, UTF16BE):
        at = _utf16_end(data)
        return (data, b"") if at < 0 else (data[:at], data[at + 2:])
    at = data.find(b"\x00")
    return (data, b"") if at < 0 else (data[:at], data[at + 1:])


def _decode(encoding: int, data: bytes) -> str:
    if encoding == LATIN1:
        return data.decode("latin-1")
    if encoding == UTF16:
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return data.decode("utf-16", "replace")
        return data.decode("utf-16-le", "replace")   # no byte-order mark: little-endian (as Windows writes)
    if encoding == UTF16BE:
        return data.decode("utf-16-be", "replace")
    return data.decode("utf-8", "replace")


def _decode_list(encoding: int, data: bytes) -> list[str]:
    """Every null-separated string (trailing terminators dropped), split in one pass. More than
    MAX_VALUES values → TagError (the frame is then kept as it was, unread)."""
    if encoding in (UTF16, UTF16BE):
        if len(data) % 2 == 0:                       # trailing terminators: whole zero pairs
            zeros = len(data) - len(data.rstrip(b"\x00"))
            data = data[: len(data) - zeros // 2 * 2]
        pieces, start = [], 0
        while start < len(data):
            end = _utf16_end(data, start)
            if end < 0:
                end = len(data)
            pieces.append(data[start:end])
            if len(pieces) > MAX_VALUES:
                raise TagError("a text frame holds too many values")
            start = end + 2
    else:
        data = data.rstrip(b"\x00")
        pieces = data.split(b"\x00", MAX_VALUES) if data else []
        if len(pieces) > MAX_VALUES:
            raise TagError("a text frame holds too many values")
    out = [_decode(encoding, piece) for piece in pieces]
    while out and out[-1] == "":
        out.pop()
    return out


def _encode(encoding: int, text: str) -> bytes:
    if encoding == LATIN1:
        return text.encode("latin-1")
    if encoding == UTF16:
        return b"\xff\xfe" + text.encode("utf-16-le") if text else b"\xff\xfe"
    if encoding == UTF16BE:
        return text.encode("utf-16-be")
    return text.encode("utf-8")


def _best_encoding(texts, major: int, wanted: int | None) -> int:
    """The frame's own encoding when the version allows it (2 and 3 are v2.4 only); new text:
    ISO-8859-1 when it fits, else UTF-8 (v2.4) / UTF-16 (v2.3)."""
    if wanted is not None and (major >= 4 or wanted in (LATIN1, UTF16)):
        if wanted != LATIN1 or _latin1(texts):
            return wanted
    if _latin1(texts):
        return LATIN1
    return UTF8 if major >= 4 else UTF16


def _latin1(texts) -> bool:
    try:
        for text in texts:
            text.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


# -- frames ------------------------------------------------------------------------------------
class Frame:
    """Any frame. `raw` is its body as read (unsynchronisation and compression undone) and
    `source` the major version it came from: an untouched frame is written back byte for byte."""

    def __init__(self, id: str, raw: bytes | None = None, source: int = 0):
        self.id = id
        self.raw = raw
        self.source = source

    def render_body(self, major: int) -> bytes:
        if self.raw is not None:
            return self.raw
        raise TagError(f"can't write the {self.id} frame")

    @property
    def key(self) -> str:
        return self.id

    def __repr__(self):
        return f"<{type(self).__name__} {self.id}>"


class StoredFrame(Frame):
    """A frame that can't be read here — encrypted, or compressed data that won't inflate within
    its limits. `raw` is its body exactly as stored and `flags` its two flag bytes: it's written
    back unchanged into a tag of the same version (other versions lay the flags out differently)."""

    def __init__(self, id: str, raw: bytes, flags: bytes, source: int):
        super().__init__(id, raw, source)
        self.flags = bytes(flags)


class TextFrame(Frame):
    def __init__(self, id: str, text: list[str], encoding: int | None = None, **kw):
        super().__init__(id, **kw)
        self.text = list(text)
        self.encoding = encoding

    @classmethod
    def parse(cls, id: str, body: bytes, source: int) -> "TextFrame":
        if not body:
            return cls(id, [], None, raw=body, source=source)
        encoding = body[0] if body[0] <= 3 else LATIN1
        return cls(id, _decode_list(encoding, body[1:]), encoding, raw=body, source=source)

    def render_body(self, major: int) -> bytes:
        if self.raw is not None and self.source == major:
            return self.raw
        values = self.text if major >= 4 else ["/".join(self.text)] if self.text else []
        encoding = _best_encoding(values, major, self.encoding)
        return bytes([encoding]) + _terminator(encoding).join(_encode(encoding, v) for v in values)


class UserTextFrame(TextFrame):
    """TXXX: a named value (e.g. MQAENCODER, or "PRE_EMPHASIS")."""

    def __init__(self, desc: str, text: list[str], encoding: int | None = None, **kw):
        super().__init__("TXXX", text, encoding, **kw)
        self.desc = desc

    @classmethod
    def parse(cls, id: str, body: bytes, source: int) -> "UserTextFrame":
        encoding = body[0] if body and body[0] <= 3 else LATIN1
        desc, rest = _split_term(encoding, body[1:])
        return cls(_decode(encoding, desc), _decode_list(encoding, rest), encoding, raw=body, source=source)

    @property
    def key(self) -> str:
        return f"TXXX:{self.desc}"

    def render_body(self, major: int) -> bytes:
        if self.raw is not None and self.source == major:
            return self.raw
        values = self.text if major >= 4 else ["/".join(self.text)] if self.text else []
        encoding = _best_encoding([self.desc, *values], major, self.encoding)
        term = _terminator(encoding)
        return bytes([encoding]) + _encode(encoding, self.desc) + term + term.join(_encode(encoding, v) for v in values)


class CommentFrame(Frame):
    """COMM (and USLT, which is laid out the same): language, description, text."""

    def __init__(self, desc: str, text: list[str], lang: str = "eng", encoding: int | None = None,
                 id: str = "COMM", **kw):
        super().__init__(id, **kw)
        self.desc, self.text, self.lang, self.encoding = desc, list(text), lang, encoding

    @classmethod
    def parse(cls, id: str, body: bytes, source: int) -> "CommentFrame":
        if len(body) < 4:
            raise TagError("a comment frame is too short")
        encoding = body[0] if body[0] <= 3 else LATIN1
        lang = body[1:4].decode("latin-1")
        desc, rest = _split_term(encoding, body[4:])
        return cls(_decode(encoding, desc), _decode_list(encoding, rest), lang, encoding, id=id, raw=body,
                   source=source)

    @property
    def key(self) -> str:
        return f"{self.id}:{self.desc}:{self.lang}"

    def render_body(self, major: int) -> bytes:
        if self.raw is not None and self.source == major:
            return self.raw
        encoding = _best_encoding([self.desc, *self.text], major, self.encoding)
        term = _terminator(encoding)
        lang = (self.lang.encode("latin-1", "replace") + b"XXX")[:3]
        return (bytes([encoding]) + lang + _encode(encoding, self.desc) + term
                + term.join(_encode(encoding, t) for t in self.text))


class PictureFrame(Frame):
    """APIC: an embedded picture (type 3 = front cover)."""

    def __init__(self, mime: str, type: int, desc: str, data: bytes, encoding: int | None = None, **kw):
        super().__init__("APIC", **kw)
        self.mime, self.type, self.desc, self.data, self.encoding = mime, type, desc, bytes(data), encoding

    @classmethod
    def parse(cls, id: str, body: bytes, source: int) -> "PictureFrame":
        if not body:
            raise TagError("an empty picture frame")
        encoding = body[0] if body[0] <= 3 else LATIN1
        if source == 2:   # PIC: a 3-letter image format instead of a MIME type
            fmt = body[1:4].decode("latin-1").upper()
            mime, rest = _PIC_FORMATS.get(fmt, "image/" + fmt.lower()), body[4:]
        else:
            mime_bytes, rest = _split_term(LATIN1, body[1:])
            mime = mime_bytes.decode("latin-1")
        if not rest:
            raise TagError("a picture frame is cut short")
        kind = rest[0]
        desc, data = _split_term(encoding, rest[1:])
        return cls(mime, kind, _decode(encoding, desc), data, encoding, raw=body if source != 2 else None,
                   source=source)

    @property
    def key(self) -> str:
        return f"APIC:{self.desc}"

    def render_body(self, major: int) -> bytes:
        if self.raw is not None and self.source == major:
            return self.raw
        encoding = _best_encoding([self.desc], major, self.encoding)
        return (bytes([encoding]) + self.mime.encode("latin-1", "replace") + b"\x00" + bytes([self.type & 0xFF])
                + _encode(encoding, self.desc) + _terminator(encoding) + self.data)


def _is_text_id(id: str) -> bool:
    return id.startswith("T") and id != "TXXX"


_PARSERS = {"TXXX": UserTextFrame, "COMM": CommentFrame, "USLT": CommentFrame, "APIC": PictureFrame}


def parse_frame(id: str, body: bytes, source: int) -> Frame:
    """A frame from its (already decoded) body; anything not understood stays a plain Frame."""
    parser = _PARSERS.get(id) or (TextFrame if _is_text_id(id) else None)
    if parser is None:
        return Frame(id, body, source)
    try:
        return parser.parse(id, body, source)
    except (TagError, IndexError, UnicodeDecodeError):
        return Frame(id, body, source)


# -- the tag ------------------------------------------------------------------------------------
class ID3Tag:
    """An ID3 tag's frames, in file order. `version` is (2, major, revision) as read — (1, 1, 0)
    for one made from an ID3v1 tag. `problem` is "" when every byte of the tag was accounted for;
    otherwise why a rewrite would lose something (the frames read so far are still there to show,
    but the file classes refuse to write such a tag)."""

    def __init__(self, frames: list[Frame] | None = None, version: tuple[int, int, int] = (2, 4, 0)):
        self.frames: list[Frame] = list(frames or [])
        self.version = version
        self.problem = ""

    # -- reading --
    @classmethod
    def parse(cls, data: bytes) -> "ID3Tag":
        """A tag from bytes starting with "ID3" (the whole tag; anything after it is ignored)."""
        if len(data) < HEADER or data[:3] != b"ID3":
            raise TagError("no ID3v2 tag here")
        major, revision, flags = data[3], data[4], data[5]
        if major not in (2, 3, 4):
            raise TagError(f"ID3v2.{major} isn't a version this reader knows")
        size = synchsafe(data[6:10])
        body = data[HEADER: HEADER + size]
        if len(body) < size:
            raise TagError("the ID3 tag runs past the end of the file")
        if major < 4 and flags & 0x80:
            body = deunsync(body)
        at = 0
        if flags & 0x40:
            if major == 2:   # v2.2: this bit meant "compressed", with no scheme ever defined
                raise TagError("a compressed ID3v2.2 tag can't be read")
            if len(body) < 4:
                raise TagError("the ID3 extended header is cut short")
            if major == 3:
                at = 4 + struct.unpack(">I", body[:4])[0]
            else:
                at = synchsafe(body[:4])
            if at > len(body):
                raise TagError("the ID3 extended header runs past the end of the tag")
        tag = cls([], (2, major, revision))
        tag.frames, tag.problem = _frames(body, at, major, bool(flags & 0x80))
        if major < 4:
            tag._upgrade_dates()
        if major == 2:
            for frame in tag.frames:
                frame.source = 0   # v2.2 bodies are never written back as they were
        return tag

    def _upgrade_dates(self):
        """v2.3's TYER / TDAT / TIME → one TDRC; TORY → TDOR (v2.4 "Native Frames" §4.2.5). A TDAT
        or TIME that can't be merged (not four digits, or TIME without a day) is kept as it is."""
        year = self.text("TYER")
        if year and not self.getall("TDRC"):
            date = year[0].strip()
            ddmm = (self.text("TDAT") or [""])[0].strip()
            hhmm = (self.text("TIME") or [""])[0].strip()
            if _digits(ddmm, 4):
                date += f"-{ddmm[2:]}-{ddmm[:2]}"
                if _digits(hhmm, 4):
                    date += f"T{hhmm[:2]}:{hhmm[2:]}"
            at = self.frames.index(self.getall("TYER")[0])
            self.frames.insert(at, TextFrame("TDRC", [date], getattr(self.get("TYER"), "encoding", None)))
        if self.getall("TDRC"):   # the v2.3 frames this date is written back as go; any others stay
            merged = _v23_date_frames((self.text("TDRC") or [""])[0])
            self.frames = [f for f in self.frames if f.id not in merged or not isinstance(f, TextFrame)]
        old = self.get("TORY")
        if isinstance(old, TextFrame) and not self.getall("TDOR"):
            self.frames[self.frames.index(old)] = TextFrame("TDOR", old.text, old.encoding)

    @classmethod
    def from_v1(cls, block: bytes) -> "ID3Tag | None":
        """An ID3v1 / v1.1 tag (the file's last 128 bytes) as frames; None if it isn't one."""
        if len(block) != 128 or block[:3] != b"TAG":
            return None

        def text(raw: bytes) -> str:
            return raw.split(b"\x00")[0].decode("latin-1").strip()

        frames: list[Frame] = []
        for frame_id, value in (("TIT2", text(block[3:33])), ("TPE1", text(block[33:63])),
                                ("TALB", text(block[63:93])), ("TDRC", text(block[93:97]))):
            if value:
                frames.append(TextFrame(frame_id, [value], LATIN1))
        comment = block[97:127]
        if comment[28] == 0 and comment[29] != 0:   # v1.1: a track number in the comment's last byte
            frames.append(TextFrame("TRCK", [str(comment[29])], LATIN1))
            comment = comment[:28]
        if text(comment):
            frames.append(CommentFrame("ID3v1 Comment", [text(comment)], "eng", LATIN1))
        genre = genre_list.name(block[127])
        if genre:
            frames.append(TextFrame("TCON", [genre], LATIN1))
        return cls(frames, (1, 1, 0))

    # -- access --
    def getall(self, frame_id: str) -> list[Frame]:
        return [f for f in self.frames if f.id == frame_id]

    def get(self, frame_id: str):
        found = self.getall(frame_id)
        return found[0] if found else None

    def text(self, frame_id: str) -> list[str]:
        frame = self.get(frame_id)
        return list(getattr(frame, "text", []) or []) if frame is not None else []

    def __contains__(self, frame_id: str) -> bool:
        return any(f.id == frame_id for f in self.frames)

    def __len__(self) -> int:
        return len(self.frames)

    def delall(self, frame_id: str) -> None:
        self.frames = [f for f in self.frames if f.id != frame_id]

    def remove(self, frame: Frame) -> None:
        self.frames = [f for f in self.frames if f is not frame]

    def add(self, frame: Frame) -> None:
        self.frames.append(frame)

    def genres(self) -> list[str]:
        return genres(self.text("TCON"))

    # -- writing --
    def render(self, major: int, padding: int = 0) -> bytes:
        """The whole tag as ID3v2.`major` (3 or 4), no unsynchronisation, `padding` zero bytes."""
        if major not in (3, 4):
            raise TagError("only ID3v2.3 and v2.4 are written")
        frames = list(self.frames)
        if major == 3:
            frames = _downgrade_dates(frames)
        out = []
        for frame in frames:
            flags = b"\x00\x00"
            if isinstance(frame, StoredFrame):
                if frame.source != major:
                    raise TagError(f"the {frame.id} frame can only be written back as ID3v2.{frame.source}")
                flags = frame.flags
            elif type(frame) is Frame and (frame.raw is None or frame.source not in (0, major)):
                raise TagError(f"the {frame.id} frame can't be written as ID3v2.{major}")
            if len(frame.id) != 4:
                raise TagError(f"the {frame.id} frame has no ID3v2.{major} name")
            body = frame.render_body(major)
            if major == 4:
                size = to_synchsafe(len(body))
            else:
                size = struct.pack(">I", len(body))
            out.append(frame.id.encode("latin-1") + size + flags + body)
        frames_data = b"".join(out)
        return b"ID3" + bytes([major, 0, 0]) + to_synchsafe(len(frames_data) + padding) + frames_data + bytes(padding)


def _digits(text: str, count: int) -> bool:
    return len(text) == count and text.isascii() and text.isdigit()


def _v23_date_frames(date: str) -> set[str]:
    """The v2.3 frames a TDRC date is written as: TYER, + TDAT with a day, + TIME with a time."""
    date = date.strip()
    found = {"TYER"} if date else set()
    if len(date) >= 10 and date[4] == "-" and date[7] == "-":
        found.add("TDAT")
        if len(date) >= 16 and date[10] == "T":
            found.add("TIME")
    return found


def _downgrade_dates(frames: list[Frame]) -> list[Frame]:
    """TDRC → TYER (+ TDAT, TIME); TDOR → TORY, for a v2.3 tag. A v2.3 date frame the TDRC
    doesn't rewrite (TRDA, or a TDAT / TIME it has no day / time for) is kept."""
    regenerated: set[str] = set()
    for frame in frames:
        if frame.id == "TDRC" and isinstance(frame, TextFrame):
            regenerated |= _v23_date_frames((frame.text or [""])[0])
    out: list[Frame] = []
    for frame in frames:
        if frame.id == "TDRC" and isinstance(frame, TextFrame):
            date = (frame.text or [""])[0].strip()
            if not date:
                continue
            out.append(TextFrame("TYER", [date[:4]], frame.encoding))
            if "TDAT" in _v23_date_frames(date):
                out.append(TextFrame("TDAT", [date[8:10] + date[5:7]], frame.encoding))
                if "TIME" in _v23_date_frames(date):
                    out.append(TextFrame("TIME", [date[11:13] + date[14:16]], frame.encoding))
        elif frame.id == "TDOR" and isinstance(frame, TextFrame):
            out.append(TextFrame("TORY", [(frame.text or [""])[0][:4]], frame.encoding))
        elif frame.id in regenerated and isinstance(frame, TextFrame):
            continue
        else:
            out.append(frame)
    return out


def _valid_id(raw: bytes) -> bool:
    return bool(raw) and all(0x41 <= b <= 0x5A or 0x30 <= b <= 0x39 for b in raw)


def _frame_size(body: bytes, at: int, major: int, header: int, content_end: int) -> int:
    """A frame's size. v2.4 sizes are synchsafe, but some taggers wrote plain 32-bit numbers:
    a size byte with the high bit set can only be plain; otherwise, when the two readings
    differ, the one that lands on what must come next (a frame header, the padding or the end
    of the tag) wins — the synchsafe one when both do."""
    size_bytes = body[at + header - (6 if major > 2 else 3): at + header - (2 if major > 2 else 0)]
    plain = int.from_bytes(size_bytes, "big")
    if major != 4 or any(b & 0x80 for b in size_bytes):
        return plain
    safe = synchsafe(size_bytes)
    if safe == plain:
        return safe

    def landing(size: int) -> int:   # 2 = on a frame header or exactly at the end, 1 = inside the padding
        end = at + header + size
        if end == content_end or end == len(body):
            return 2
        if end > len(body):
            return 0
        if end > content_end:
            return 1
        if end + header > len(body) or not _valid_id(body[end: end + 4]):
            return 0
        follows = body[end + 4: end + 8]
        return 2 if min(synchsafe(follows), int.from_bytes(follows, "big")) <= len(body) - end - header else 0

    return plain if landing(plain) > landing(safe) else safe


def _frames(body: bytes, at: int, major: int, tag_unsync: bool) -> tuple[list[Frame], str]:
    """(the frames, a problem — "" when the walk accounted for every byte up to the padding)."""
    id_size = 3 if major == 2 else 4
    header = 6 if major == 2 else 10
    content_end = len(body.rstrip(b"\x00"))   # the zero padding starts here
    frames: list[Frame] = []
    problem = ""
    inflated = 0
    while at < content_end:
        if at + header > len(body):
            return frames, "a few stray bytes after the last frame"
        raw_id = body[at: at + id_size]
        if raw_id[0] == 0:
            return frames, "bytes after the start of the padding"
        if not _valid_id(raw_id):
            return frames, "a frame it can't follow (an unreadable frame ID)"
        frame_id = raw_id.decode("ascii")
        size = _frame_size(body, at, major, header, content_end)
        flags = body[at + header - 2: at + header] if major > 2 else b"\x00\x00"
        data = body[at + header: at + header + size]
        at += header + size
        if len(data) < size:
            return frames, f"the {frame_id} frame runs past the end of the tag"
        frame, used = _decode_frame(frame_id, data, flags, major, tag_unsync, MAX_INFLATED - inflated)
        inflated += used
        if major == 2:
            new_id = _V22_IDS.get(frame_id)
            if new_id is None:   # read on past it, but a rewrite (as v2.3) would drop it
                problem = problem or f"an ID3v2.2 {frame_id} frame, which has no v2.3 equivalent"
                continue
            frame = parse_frame(new_id, frame.raw, 2)
        frames.append(frame)
    return frames, problem


def _inflate(data: bytes, declared: int | None, allowance: int) -> bytes | None:
    """zlib data inflated, or None when it's damaged, inflates past `allowance` bytes, or doesn't
    come to the size the frame declares."""
    limit = min(declared, allowance) if declared is not None else allowance
    if limit < 0 or (declared is not None and declared > allowance):
        return None
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(data, limit + 1)
    except zlib.error:
        return None
    if len(out) > limit or not inflater.eof or (declared is not None and len(out) != declared):
        return None
    return out


def _decode_frame(frame_id: str, data: bytes, flags: bytes, major: int, tag_unsync: bool,
                  allowance: int) -> tuple[Frame, int]:
    """(the frame, bytes inflated). A frame that can't be read (encrypted, or compressed data
    that won't inflate within `allowance` or to its declared size) is kept as a StoredFrame."""
    stored = data
    if major == 4:
        status, fmt = flags
        kept = StoredFrame(frame_id, stored, bytes([status, fmt | (0x02 if tag_unsync else 0)]), 4)
        if fmt & 0x40:   # grouping identity byte
            data = data[1:]
        if fmt & 0x04:   # encrypted: can't be read
            return kept, 0
        declared = None
        if fmt & 0x01:   # data length indicator
            if len(data) < 4:
                return kept, 0
            declared, data = synchsafe(data[:4]), data[4:]
        if fmt & 0x02 or tag_unsync:
            data = deunsync(data)
        if fmt & 0x08:
            data = _inflate(data, declared, allowance)
            if data is None:
                return kept, 0
            return parse_frame(frame_id, data, major), len(data)
    elif major == 3:
        status, fmt = flags
        kept = StoredFrame(frame_id, stored, flags, 3)
        if fmt & 0x40:   # encrypted
            return kept, 0
        declared = None
        if fmt & 0x80:   # compressed: the inflated size comes first
            if len(data) < 4:
                return kept, 0
            declared, data = int.from_bytes(data[:4], "big"), data[4:]
        if fmt & 0x20:   # grouping identity byte
            data = data[1:]
        if fmt & 0x80:
            data = _inflate(data, declared, allowance)
            if data is None:
                return kept, 0
            return parse_frame(frame_id, data, major), len(data)
    return parse_frame(frame_id, data, major), 0   # (written back with no flags: the body is stored plainly)


def _genre_number(text: str) -> int | None:
    """A genre reference's number ("17"), or None: plain ASCII digits only ("²" counts as a digit
    to str.isdigit, but isn't one int() reads)."""
    return int(text) if 0 < len(text) <= 9 and text.isascii() and text.isdigit() else None


def genres(values: list[str]) -> list[str]:
    """TCON values as genre names: "(17)" / "17" → "Rock", "(4)Eurodisco" → "Disco", "Eurodisco",
    "(RX)" / "RX" → "Remix", "(CR)" / "CR" → "Cover", "((name" → "(name"; repeats dropped."""
    out: list[str] = []

    def add(name: str):
        name = name.strip()
        if name and name not in out:
            out.append(name)

    for value in values:
        for part in value.split("\x00"):
            text = part
            while text.startswith("(") and not text.startswith("(("):
                end = text.find(")")
                if end < 0:
                    break
                code, text = text[1:end], text[end + 1:]
                if _genre_number(code) is not None:
                    add(genre_list.name(_genre_number(code)) or code)
                elif code == "RX":
                    add("Remix")
                elif code == "CR":
                    add("Cover")
                else:
                    add(code)
            if text.startswith("(("):
                text = text[1:]
            if _genre_number(text) is not None:
                add(genre_list.name(_genre_number(text)) or text)
            elif text in ("RX", "CR"):
                add("Remix" if text == "RX" else "Cover")
            else:
                add(text)
    return out


def render_v1(tag: ID3Tag, old: bytes) -> bytes:
    """A fresh ID3v1.1 block from the v2 tag's values (for a file that already has one)."""
    def field(frame_id: str, size: int) -> bytes:
        value = (tag.text(frame_id) or [""])[0]
        return value.encode("latin-1", "replace")[:size].ljust(size, b"\x00")

    comments = [f for f in tag.getall("COMM") if isinstance(f, CommentFrame) and f.desc in ("", "ID3v1 Comment")]
    comment = (comments[0].text or [""])[0] if comments else ""
    track = (tag.text("TRCK") or [""])[0].split("/")[0].strip()
    names = tag.genres()
    genre = next((n for n in (genre_list.number(g) for g in names) if n is not None), 255)
    year = (tag.text("TDRC") or [""])[0][:4]
    body = (b"TAG" + field("TIT2", 30) + field("TPE1", 30) + field("TALB", 30)
            + year.encode("latin-1", "replace")[:4].ljust(4, b"\x00"))
    if track.isascii() and track.isdigit() and 0 < int(track) < 256:
        body += comment.encode("latin-1", "replace")[:28].ljust(28, b"\x00") + b"\x00" + bytes([int(track)])
    else:
        body += comment.encode("latin-1", "replace")[:30].ljust(30, b"\x00")
    return body + bytes([genre])


def damaged_reason(tag: "ID3Tag | None") -> str:
    """Why a file's ID3 tag isn't rewritten, in plain words."""
    if tag is not None and tag.problem:
        return f"its ID3 tag couldn't be read to the end ({tag.problem}), so it isn't rewritten (nothing in it is lost)"
    return "its ID3 tag is damaged, so it isn't rewritten (nothing in it is lost)"


def tag_size(head: bytes) -> int:
    """The whole size of an ID3v2 tag at the start of `head` (header, frames, padding, footer), else 0."""
    if len(head) >= HEADER and head[:3] == b"ID3" and head[3] in (2, 3, 4) and all(b < 0x80 for b in head[6:10]):
        return HEADER + synchsafe(head[6:10]) + (HEADER if head[5] & 0x10 and head[3] == 4 else 0)
    return 0

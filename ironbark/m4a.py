"""Apple Lossless (ALAC) in .m4a files, decoded by the libsndfile the player already uses. No UI.

libsndfile (the decoder behind every other format here) has Apple's own ALAC decoder built in,
but only reads it from Apple's CAF container (Core Audio Format), not from an .m4a (the MP4
container iTunes and Apple Music use). The audio inside is the same either way: a list of ALAC
"packets" (4096 samples each) plus a 24-byte setup block (the "magic cookie"). So this module
reads the MP4's index of where each packet sits, and presents the file to libsndfile as a CAF
file made on the fly — a small CAF header followed by the .m4a's own packet bytes, read straight
from the file as they're needed. Nothing is converted or written to disk, no new decoder is
added, and the bit-perfect proof checks against the same libsndfile decode as every other file.

Checked 04/10/2026 by experiment: FFmpeg-made ALAC files (16- and 24-bit) decode sample-for-
sample identical to the FLAC they were made from, and seeking lands on the exact sample.

What's read from the MP4 (ISO/IEC 14496-12 boxes, as written by iTunes, Apple Music and FFmpeg):
moov → trak (the one whose handler is "soun") → mdia → mdhd (time scale), minf → stbl →
stsd (the "alac" sample entry and its "alac" cookie box), stts (packet durations), stsz (packet
sizes), stsc (packets per chunk), stco / co64 (where each chunk starts in the file).

What's written for libsndfile (Apple's CAF specification): the "caff" header; "desc" (rate,
format "alac", format flags 1/2/3/4 = 16/20/24/32-bit source — libsndfile refuses flags 0);
"kuki" (the cookie, wrapped in "frma" + "alac" boxes as FFmpeg writes it); "pakt" (packet count,
valid frames, priming 0, remainder, then each packet's size as a variable-length number); "data"
(an edit count, then the packets). Only the 16- and 24-bit flags were tested (FFmpeg's ALAC
encoder makes those); 20 and 32 follow the same numbering, unverified.

AAC (.m4a's lossy codec) isn't decoded: libsndfile has no AAC decoder, and adding one would mean
a new dependency (see CHANGELOG 0.14.0). Edit lists (`elst`) aren't applied: ALAC files carry no
encoder delay to trim.
"""

from __future__ import annotations

import io
import struct
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}
_ALAC_FLAGS = {16: 1, 20: 2, 24: 3, 32: 4}  # CAF desc format flags for ALAC, by source bit depth


class NotAlac(RuntimeError):
    """The MP4 holds something other than ALAC (AAC, usually), or can't be read as audio."""

    def __init__(self, message: str, codec: str = ""):
        super().__init__(message)
        self.codec = codec


@dataclass
class Mp4Audio:
    codec: str = ""              # the sample entry's name: "alac", "mp4a" (AAC)…
    rate: int = 0
    channels: int = 0
    bits: int = 0
    timescale: int = 0
    frames: int = 0              # valid frames (from the packet durations)
    frame_length: int = 4096     # samples per packet (from the cookie)
    cookie: bytes = b""          # the 24-byte ALACSpecificConfig
    sizes: list[int] = field(default_factory=list)     # each packet's size in bytes
    offsets: list[int] = field(default_factory=list)   # each packet's position in the file


def is_mp4(head: bytes) -> bool:
    """An MP4 / M4A file starts with an "ftyp" box."""
    return len(head) >= 8 and head[4:8] == b"ftyp"


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    """(type, body start, body end) for each box in data[start:end]."""
    end = len(data) if end is None else end
    at = start
    while at + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, at)
        header = 8
        if size == 1:
            if at + 16 > end:
                return
            size = struct.unpack_from(">Q", data, at + 8)[0]
            header = 16
        elif size == 0:
            size = end - at
        if size < header or at + size > end:
            return  # damaged: stop rather than guess
        yield kind, at + header, at + size
        at += size


def _find(data: bytes, start: int, end: int, kind: bytes):
    return next(((s, e) for k, s, e in _boxes(data, start, end) if k == kind), None)


def _top_level(f) -> bytes:
    """The "moov" box's bytes (the index; small even for long files), read without loading the
    audio data around it."""
    f.seek(0, io.SEEK_END)
    length = f.tell()
    at = 0
    while at + 8 <= length:
        f.seek(at)
        head = f.read(16)
        size, kind = struct.unpack_from(">I4s", head)
        header = 8
        if size == 1:
            size, header = struct.unpack_from(">Q", head, 8)[0], 16
        elif size == 0:
            size = length - at
        if size < header:
            break
        if kind == b"moov":
            f.seek(at)
            return f.read(size)
        at += size
    raise NotAlac("no audio index (moov box) in this MP4 file")


def read_index(path: str | Path) -> Mp4Audio:
    """The ALAC track's layout. Raises NotAlac for anything else (AAC, video-only, damaged)."""
    try:
        return _read_index(path)
    except (struct.error, IndexError, ValueError) as error:
        raise NotAlac(f"this MP4 file's index is damaged ({error})") from error


def _read_index(path: str | Path) -> Mp4Audio:
    with open(path, "rb") as f:
        if not is_mp4(f.read(8)):
            raise NotAlac("not an MP4 file")
        moov = _top_level(f)
    for kind, start, end in _boxes(moov, 8):
        if kind != b"trak":
            continue
        mdia = _find(moov, start, end, b"mdia")
        if mdia is None:
            continue
        hdlr = _find(moov, *mdia, b"hdlr")
        if hdlr is None or moov[hdlr[0] + 8: hdlr[0] + 12] != b"soun":
            continue
        return _read_track(moov, mdia)
    raise NotAlac("no sound track in this MP4 file")


def _read_track(moov: bytes, mdia: tuple[int, int]) -> Mp4Audio:
    audio = Mp4Audio()
    mdhd = _find(moov, *mdia, b"mdhd")
    if mdhd is not None:
        version = moov[mdhd[0]]
        audio.timescale = struct.unpack_from(">I", moov, mdhd[0] + (20 if version == 1 else 12))[0]
    minf = _find(moov, *mdia, b"minf")
    stbl = _find(moov, *minf, b"stbl") if minf else None
    if stbl is None:
        raise NotAlac("no sample table in this MP4 file")
    stsd = _find(moov, *stbl, b"stsd")
    if stsd is None:
        raise NotAlac("no sample description in this MP4 file")
    entry = next(_boxes(moov, stsd[0] + 8, stsd[1]), None)  # after version/flags + entry count
    if entry is None:
        raise NotAlac("empty sample description")
    kind, start, end = entry
    audio.codec = kind.decode("latin-1").strip()
    if kind != b"alac":
        name = "AAC (lossy)" if kind == b"mp4a" else audio.codec.upper()
        raise NotAlac(f"this .m4a holds {name}, which the player can't decode — only Apple Lossless "
                      ".m4a files play", audio.codec)
    # Sample entry: 6 reserved + 2 data reference index, then the sound description (version 0:
    # 20 bytes; QuickTime version 1 adds 16, version 2 adds 36), then child boxes.
    version = struct.unpack_from(">H", moov, start + 8)[0]
    children = start + 28 + {1: 16, 2: 36}.get(version, 0)
    cookie_box = _find(moov, children, end, b"alac")
    if cookie_box is None or cookie_box[1] - cookie_box[0] < 4 + 24:
        raise NotAlac("Apple Lossless setup block missing")
    audio.cookie = moov[cookie_box[0] + 4: cookie_box[0] + 28]  # after the box's version/flags
    (audio.frame_length, _compatible, audio.bits, _pb, _mb, _kb, audio.channels, _max_run,
     _max_frame, _avg_rate, audio.rate) = struct.unpack(">IBBBBBBHIII", audio.cookie)

    stsz = _find(moov, *stbl, b"stsz")
    stsc = _find(moov, *stbl, b"stsc")
    stco = _find(moov, *stbl, b"stco") or _find(moov, *stbl, b"co64")
    stts = _find(moov, *stbl, b"stts")
    if None in (stsz, stsc, stco, stts):
        raise NotAlac("incomplete sample table in this MP4 file")
    fixed, count = struct.unpack_from(">II", moov, stsz[0] + 4)
    audio.sizes = ([fixed] * count if fixed else
                   list(struct.unpack_from(f">{count}I", moov, stsz[0] + 12)))
    wide = moov[stco[0] - 4: stco[0]] == b"co64"
    chunks = struct.unpack_from(">I", moov, stco[0] + 4)[0]
    starts = list(struct.unpack_from(f">{chunks}{'Q' if wide else 'I'}", moov, stco[0] + 8))
    runs = struct.unpack_from(">I", moov, stsc[0] + 4)[0]
    table = [struct.unpack_from(">III", moov, stsc[0] + 8 + 12 * k) for k in range(runs)]
    packet = 0
    for k, (first, per_chunk, _desc) in enumerate(table):
        last = table[k + 1][0] - 1 if k + 1 < runs else chunks
        for chunk in range(first, last + 1):
            at = starts[chunk - 1]
            for _ in range(per_chunk):
                if packet >= count:
                    break
                audio.offsets.append(at)
                at += audio.sizes[packet]
                packet += 1
    if packet != count:
        raise NotAlac("the MP4's sample table doesn't add up")
    entries = struct.unpack_from(">I", moov, stts[0] + 4)[0]
    duration = sum(n * d for n, d in (struct.unpack_from(">II", moov, stts[0] + 8 + 8 * k)
                                      for k in range(entries)))
    scale = audio.timescale or audio.rate
    audio.frames = duration * audio.rate // scale if scale else count * audio.frame_length
    audio.frames = min(audio.frames, count * audio.frame_length)
    return audio


def _varint(n: int) -> bytes:
    """CAF's packet-table numbers: 7 bits per byte, most significant first, top bit = more follow."""
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def caf_header(audio: Mp4Audio) -> bytes:
    """Everything a CAF file has before its packet bytes, for this ALAC track."""
    if audio.bits not in _ALAC_FLAGS:
        raise NotAlac(f"unusual ALAC bit depth ({audio.bits})")
    packets = len(audio.sizes)
    desc = struct.pack(">d4sIIIII", float(audio.rate), b"alac", _ALAC_FLAGS[audio.bits], 0,
                       audio.frame_length, audio.channels, 0)
    cookie = struct.pack(">I4s4s", 12, b"frma", b"alac") + struct.pack(">I4sI", 36, b"alac", 0) + audio.cookie
    remainder = packets * audio.frame_length - audio.frames
    pakt = struct.pack(">qqii", packets, audio.frames, 0, remainder) + b"".join(_varint(s) for s in audio.sizes)
    data_size = 4 + sum(audio.sizes)
    return (b"caff" + struct.pack(">HH", 1, 0)
            + b"desc" + struct.pack(">q", len(desc)) + desc
            + b"kuki" + struct.pack(">q", len(cookie)) + cookie
            + b"pakt" + struct.pack(">q", len(pakt)) + pakt
            + b"data" + struct.pack(">q", data_size) + struct.pack(">I", 0))


class CafStream(io.RawIOBase):
    """A read-only, seekable file object that looks like a CAF file of the .m4a's ALAC audio:
    the made-up header, then the packets read from the .m4a as they're asked for. Runs of
    packets that sit next to each other in the file are read as one."""

    def __init__(self, path: str | Path, audio: Mp4Audio | None = None):
        super().__init__()
        self.audio = audio or read_index(path)
        self._header = caf_header(self.audio)
        # Pieces of the made-up file: (where it starts in the CAF, length, where it is in the .m4a)
        self._starts: list[int] = []
        self._pieces: list[tuple[int, int]] = []
        at = len(self._header)
        for offset, size in zip(self.audio.offsets, self.audio.sizes):
            if self._pieces and self._pieces[-1][0] + self._pieces[-1][1] == offset:
                self._pieces[-1] = (self._pieces[-1][0], self._pieces[-1][1] + size)
            else:
                self._starts.append(at)
                self._pieces.append((offset, size))
            at += size
        self._length = at
        self._pos = 0
        self._file = open(path, "rb")

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._length}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, buffer) -> int:
        view = memoryview(buffer).cast("B")
        wanted = min(len(view), max(0, self._length - self._pos))
        done = 0
        while done < wanted:
            at = self._pos + done
            if at < len(self._header):
                take = min(wanted - done, len(self._header) - at)
                view[done: done + take] = self._header[at: at + take]
            else:
                k = bisect_right(self._starts, at) - 1
                offset, size = self._pieces[k]
                inside = at - self._starts[k]
                take = min(wanted - done, size - inside)
                self._file.seek(offset + inside)
                got = self._file.readinto(view[done: done + take])
                if not got:
                    break  # the file is shorter than its index says (damaged)
                take = got
            done += take
        self._pos += done
        return done

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = max(0, self._length - self._pos)
        buffer = bytearray(size)
        n = self.readinto(buffer)
        return bytes(buffer[:n])

    def close(self):
        if not self.closed:
            self._file.close()
        super().close()

"""FLAC metadata: read and write. No UI.

Written from RFC 9639 ("Free Lossless Audio Codec", December 2024; clean-room — no other tag
library's code was read). A FLAC file is "fLaC", then metadata blocks, then the audio frames
(§6, §8). Each block has a 4-byte header (§8.1): 1 bit "last block", 7 bits type, 24 bits
length (big-endian). Types: 0 STREAMINFO (always first), 1 PADDING, 2 APPLICATION, 3 SEEKTABLE,
4 VORBIS_COMMENT, 5 CUESHEET, 6 PICTURE; 127 is forbidden.

- STREAMINFO (§8.2): min/max block size (16+16 bits), min/max frame size (24+24), sample rate
  (20), channels − 1 (3), bits per sample − 1 (5), total samples (36), MD5 of the decoded audio
  (128).
- VORBIS_COMMENT (§8.6): see vorbis.py. PICTURE (§8.8): see vorbis.Picture.
- CUESHEET (§8.7): media catalog number (128 bytes ASCII, null-padded), lead-in samples (64), a
  "CD-DA" bit + 7 + 258×8 reserved bits, number of tracks (8); each track: offset in samples
  (64), number (8), ISRC (12 bytes), a "non-audio" bit, a pre-emphasis bit, 6 + 13×8 reserved
  bits, number of index points (8); each index point: offset in samples from the track's
  offset (64), number (8), 3 reserved bytes.

Writing keeps every block it doesn't change (STREAMINFO, SEEKTABLE, APPLICATION, CUESHEET,
unknown types) byte for byte and in order; only VORBIS_COMMENT and PICTURE blocks are rebuilt
(and CUESHEET, if `cuesheet` was changed — the tag editor never does).
If the new metadata fits in the space the old metadata and padding took, it's written in place
and the padding shrinks or grows to fill it exactly — the audio isn't moved; otherwise the file
is rebuilt with fresh padding. A file that starts with an ID3v2 tag (some taggers add one) keeps
it untouched.
"""

from __future__ import annotations

import copy
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ._files import TagError, file_size, read_at, replace_region
from .vorbis import Picture, VComment

STREAMINFO, PADDING, APPLICATION, SEEKTABLE, VORBIS_COMMENT, CUESHEET, PICTURE = range(7)
NEW_PADDING = 4096           # padding left after the tags when the file has to be rebuilt
_MAX_BLOCK = (1 << 24) - 1   # a block's length field is 24 bits


@dataclass
class StreamInfo:
    min_block: int = 0
    max_block: int = 0
    min_frame: int = 0
    max_frame: int = 0
    sample_rate: int = 0
    channels: int = 0
    bits_per_sample: int = 0
    total_samples: int = 0
    md5_signature: int = 0     # 0 = the encoder stored none
    length: float = 0.0        # seconds
    bitrate: int = 0           # average over the audio frames, bits per second

    @classmethod
    def parse(cls, data: bytes) -> "StreamInfo":
        if len(data) < 34:
            raise TagError("the FLAC STREAMINFO block is too short")
        min_block, max_block = struct.unpack_from(">HH", data, 0)
        min_frame = int.from_bytes(data[4:7], "big")
        max_frame = int.from_bytes(data[7:10], "big")
        packed = int.from_bytes(data[10:18], "big")   # rate 20 | channels-1 3 | bits-1 5 | samples 36
        info = cls(min_block, max_block, min_frame, max_frame,
                   sample_rate=packed >> 44, channels=((packed >> 41) & 0x7) + 1,
                   bits_per_sample=((packed >> 36) & 0x1F) + 1, total_samples=packed & ((1 << 36) - 1),
                   md5_signature=int.from_bytes(data[18:34], "big"))
        if info.sample_rate:
            info.length = info.total_samples / info.sample_rate
        return info


@dataclass
class CueSheetIndex:
    index_number: int
    index_offset: int          # samples from the track's offset


@dataclass
class CueSheetTrack:
    track_number: int
    start_offset: int          # samples from the start of the audio
    isrc: str = ""
    type: int = 0              # 0 = audio, 1 = non-audio
    pre_emphasis: bool = False
    indexes: list[CueSheetIndex] = field(default_factory=list)


@dataclass
class CueSheet:
    media_catalog_number: str = ""
    lead_in_samples: int = 0
    compact_disc: bool = False
    tracks: list[CueSheetTrack] = field(default_factory=list)

    @classmethod
    def parse(cls, data: bytes) -> "CueSheet":
        try:
            catalog = data[:128].rstrip(b"\0").decode("ascii", "replace")
            lead_in = struct.unpack_from(">Q", data, 128)[0]
            sheet = cls(catalog, lead_in, bool(data[136] & 0x80))
            count, at = data[136 + 259], 136 + 260
            for _ in range(count):
                offset, number = struct.unpack_from(">QB", data, at)
                isrc = data[at + 9: at + 21].rstrip(b"\0").decode("ascii", "replace")
                flags = data[at + 21]
                points = data[at + 35]
                track = CueSheetTrack(number, offset, isrc, (flags >> 7) & 1, bool(flags & 0x40))
                at += 36
                for _ in range(points):
                    index_offset, index_number = struct.unpack_from(">QB", data, at)
                    track.indexes.append(CueSheetIndex(index_number, index_offset))
                    at += 12
                sheet.tracks.append(track)
        except (struct.error, IndexError) as error:
            raise TagError("the FLAC CUESHEET block is cut short") from error
        return sheet

    def render(self) -> bytes:
        out = [self.media_catalog_number.encode("ascii", "replace")[:128].ljust(128, b"\0"),
               struct.pack(">Q", self.lead_in_samples), bytes([0x80 if self.compact_disc else 0]),
               bytes(258), bytes([len(self.tracks)])]
        for track in self.tracks:
            out += [struct.pack(">QB", track.start_offset, track.track_number),
                    track.isrc.encode("ascii", "replace")[:12].ljust(12, b"\0"),
                    bytes([(track.type & 1) << 7 | (0x40 if track.pre_emphasis else 0)]), bytes(13),
                    bytes([len(track.indexes)])]
            out += [struct.pack(">QB", i.index_offset, i.index_number) + bytes(3) for i in track.indexes]
        return b"".join(out)


def id3_prefix_size(head: bytes) -> int:
    """The size of an ID3v2 tag at the very start (ID3v2.4 §3.1: "ID3", version, flags, 4-byte
    synchsafe size; +10 when a footer is present), else 0."""
    if len(head) >= 10 and head[:3] == b"ID3" and all(b < 0x80 for b in head[6:10]):
        size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        return 10 + size + (10 if head[5] & 0x10 else 0)
    return 0


class FLACFile:
    """A FLAC file's metadata. `tags` (VComment or None), `pictures`, `cuesheet` and `info`
    (StreamInfo) as read; change `tags` / `pictures`, then `save()`."""

    def __init__(self, path):
        self.path = Path(path)
        self.blocks: list[tuple[int, bytes]] = []   # every block except PADDING, as stored
        with open(self.path, "rb") as f:
            size = file_size(f)
            start = id3_prefix_size(read_at(f, 0, 10)) if size >= 10 else 0
            if size < start + 4 or read_at(f, start, 4) != b"fLaC":
                raise TagError("not a FLAC file")
            self.prefix = start              # bytes before "fLaC" (an ID3v2 tag), kept as they are
            at, last = start + 4, False
            padding = 0
            while not last:
                header = read_at(f, at, 4)
                last, kind = bool(header[0] & 0x80), header[0] & 0x7F
                length = int.from_bytes(header[1:4], "big")
                if kind == 127:
                    raise TagError("a FLAC metadata block has the forbidden type 127 (damaged)")
                if kind == PADDING:
                    if at + 4 + length > size:
                        raise TagError("FLAC padding runs past the end of the file")
                    padding += length
                else:
                    self.blocks.append((kind, read_at(f, at + 4, length)))
                at += 4 + length
            self.audio_start = at          # where the first audio frame begins
            self.size = size
        if not self.blocks or self.blocks[0][0] != STREAMINFO:
            raise TagError("the FLAC file has no STREAMINFO block first (damaged)")
        self.info = StreamInfo.parse(self.blocks[0][1])
        if self.info.length:
            self.info.bitrate = int((size - self.audio_start) * 8 / self.info.length)
        self.padding = padding
        self.tags: VComment | None = None
        self.pictures: list[Picture] = []
        self.cuesheet: CueSheet | None = None
        for kind, data in self.blocks:
            if kind == VORBIS_COMMENT and self.tags is None:
                self.tags = VComment.parse(data)[0]
            elif kind == PICTURE:
                try:
                    self.pictures.append(Picture.parse(data))
                except TagError:
                    pass   # an unreadable picture block: kept as it is, not shown
            elif kind == CUESHEET and self.cuesheet is None:
                self.cuesheet = CueSheet.parse(data)
        self._cuesheet_read = copy.deepcopy(self.cuesheet)

    # -- writing --
    def add_tags(self) -> VComment:
        if self.tags is None:
            self.tags = VComment("Ironbark")
        return self.tags

    def __getitem__(self, name: str) -> list[str]:
        return self.tags.get(name) if self.tags is not None else []

    def __setitem__(self, name: str, value) -> None:
        """audio["genre"] = ["Rock", "Pop"] (or one string): replaces every `name` comment."""
        self.add_tags().set(name, list(value) if isinstance(value, (list, tuple)) else [value])

    def add_picture(self, picture: Picture) -> None:
        self.pictures.append(picture)

    def clear_pictures(self) -> None:
        self.pictures = []

    def _new_blocks(self) -> list[tuple[int, bytes]]:
        """The blocks to write: STREAMINFO and every untouched block where it was; the Vorbis
        comment where the first one was (else after STREAMINFO); the pictures where the first
        picture was (else at the end)."""
        out: list[tuple[int, bytes]] = []
        comment_done = picture_done = cue_done = False
        cue_changed = self.cuesheet != self._cuesheet_read
        unreadable = [d for k, d in self.blocks if k == PICTURE and not _readable_picture(d)]
        for kind, data in self.blocks:
            if kind == CUESHEET and cue_changed:
                if not cue_done and self.cuesheet is not None:
                    out.append((CUESHEET, self.cuesheet.render()))
                cue_done = True
            elif kind == VORBIS_COMMENT:
                if not comment_done and self.tags is not None:
                    out.append((VORBIS_COMMENT, self.tags.render()))
                comment_done = True
            elif kind == PICTURE:
                if not picture_done:
                    out += [(PICTURE, p.render()) for p in self.pictures]
                    out += [(PICTURE, d) for d in unreadable]
                picture_done = True
            else:
                out.append((kind, data))
        if not comment_done and self.tags is not None:
            out.insert(1, (VORBIS_COMMENT, self.tags.render()))
        if cue_changed and not cue_done and self.cuesheet is not None:
            out.insert(1, (CUESHEET, self.cuesheet.render()))
        if not picture_done:
            out += [(PICTURE, p.render()) for p in self.pictures]
        for kind, data in out:
            if len(data) > _MAX_BLOCK:
                raise TagError("a FLAC metadata block can't be over 16 MB (the picture is too big)")
        return out

    def save(self) -> None:
        blocks = self._new_blocks()
        body = b"".join(bytes([kind]) + len(data).to_bytes(3, "big") + data for kind, data in blocks)
        room = self.audio_start - self.prefix - 4      # the old metadata, padding included
        if len(body) + 4 <= room and room - len(body) - 4 <= _MAX_BLOCK:
            padding = room - len(body) - 4               # in place: the padding fills the rest
        else:
            padding = NEW_PADDING
        metadata = body + bytes([0x80 | PADDING]) + padding.to_bytes(3, "big") + bytes(padding)
        replace_region(self.path, self.prefix + 4, self.audio_start, metadata)
        self.__init__(self.path)


def _readable_picture(data: bytes) -> bool:
    try:
        Picture.parse(data)
        return True
    except TagError:
        return False

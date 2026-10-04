"""Test helper: real Apple Lossless .m4a files without any outside tool.

libsndfile can *write* ALAC, but only inside a CAF file. This reads such a CAF file's packets and
setup block back out and wraps them in a small MP4 (.m4a) the way iTunes / FFmpeg lay one out —
optionally with the index (moov) first, and the packets spread over several chunks with other
bytes between them, to exercise the player's MP4 reader. Also makes an AAC-labelled MP4 (the
header only: enough to be recognised and refused)."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import soundfile as sf


def box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(body), kind) + body


def full(kind: bytes, body: bytes, version: int = 0, flags: int = 0) -> bytes:
    return box(kind, struct.pack(">I", (version << 24) | flags) + body)


def _caf_chunks(data: bytes) -> dict[bytes, bytes]:
    assert data[:4] == b"caff"
    chunks, at = {}, 8
    while at + 12 <= len(data):
        kind, size = data[at:at + 4], struct.unpack(">q", data[at + 4:at + 12])[0]
        body = data[at + 12: at + 12 + size] if size >= 0 else data[at + 12:]
        chunks[kind] = body
        at += 12 + len(body)
    return chunks


def _varints(data: bytes, count: int) -> list[int]:
    out, n = [], 0
    for byte in data:
        n = (n << 7) | (byte & 0x7F)
        if not byte & 0x80:
            out.append(n)
            n = 0
            if len(out) == count:
                break
    return out


def alac_packets(samples: np.ndarray, rate: int, bits: int, tmp: Path):
    """(24-byte ALAC config, packet byte strings, valid frames), encoded by libsndfile."""
    caf = tmp / "_encode.caf"
    sf.write(caf, samples, rate, format="CAF", subtype=f"ALAC_{bits}")
    chunks = _caf_chunks(caf.read_bytes())
    kuki = chunks[b"kuki"]
    at = kuki.find(b"alac")
    config = kuki[at + 8: at + 32] if b"frma" in kuki else kuki[:24]
    packets, valid = struct.unpack(">qq", chunks[b"pakt"][:16])
    sizes = _varints(chunks[b"pakt"][24:], packets)
    data, out, at = chunks[b"data"][4:], [], 0
    for size in sizes:
        out.append(data[at: at + size])
        at += size
    caf.unlink()
    return config, out, valid


def _moov(entry: bytes, rate: int, sizes: list[int], frames: int, frame_length: int,
          chunk_offsets: list[int], per_chunk: int) -> bytes:
    duration = frames
    mvhd = full(b"mvhd", struct.pack(">IIII", 0, 0, rate, duration) + struct.pack(">IH10x", 0x10000, 0x100)
                + bytes(36) + bytes(24) + struct.pack(">I", 2))
    tkhd = full(b"tkhd", struct.pack(">IIIII", 0, 0, 1, 0, duration) + bytes(8)
                + struct.pack(">HHHH", 0, 0, 0x100, 0) + bytes(36) + struct.pack(">II", 0, 0), flags=7)
    mdhd = full(b"mdhd", struct.pack(">IIIIHH", 0, 0, rate, duration, 0x55C4, 0))
    hdlr = full(b"hdlr", struct.pack(">I4s12s", 0, b"soun", bytes(12)) + b"\0")
    stsd = full(b"stsd", struct.pack(">I", 1) + entry)
    deltas = [frame_length] * (len(sizes) - 1) + [frames - frame_length * (len(sizes) - 1)]
    runs = []
    for d in deltas:
        if runs and runs[-1][1] == d:
            runs[-1][0] += 1
        else:
            runs.append([1, d])
    stts = full(b"stts", struct.pack(">I", len(runs)) + b"".join(struct.pack(">II", n, d) for n, d in runs))
    chunks = len(chunk_offsets)
    last = len(sizes) - per_chunk * (chunks - 1)
    stsc_runs = [(1, per_chunk, 1)] + ([(chunks, last, 1)] if last != per_chunk else [])
    stsc = full(b"stsc", struct.pack(">I", len(stsc_runs)) + b"".join(struct.pack(">III", *r) for r in stsc_runs))
    stsz = full(b"stsz", struct.pack(">II", 0, len(sizes)) + b"".join(struct.pack(">I", s) for s in sizes))
    stco = full(b"stco", struct.pack(">I", chunks) + b"".join(struct.pack(">I", o) for o in chunk_offsets))
    stbl = box(b"stbl", stsd + stts + stsc + stsz + stco)
    dinf = box(b"dinf", full(b"dref", struct.pack(">I", 1) + full(b"url ", b"", flags=1)))
    minf = box(b"minf", full(b"smhd", struct.pack(">HH", 0, 0)) + dinf + stbl)
    trak = box(b"trak", tkhd + box(b"mdia", mdhd + hdlr + minf))
    return box(b"moov", mvhd + trak)


def write_m4a(path: Path, samples: np.ndarray, rate: int = 44100, bits: int = 16, moov_first: bool = False,
              per_chunk: int = 0, junk: int = 0) -> Path:
    """An ALAC .m4a of `samples` (frames x channels, float or int). per_chunk: packets per chunk
    (0 = all in one); junk: bytes of other data between chunks."""
    config, packets, frames = alac_packets(samples, rate, bits, path.parent)
    frame_length, channels = struct.unpack(">I", config[:4])[0], config[9]
    entry = box(b"alac", bytes(6) + struct.pack(">H", 1) + struct.pack(">HHI", 0, 0, 0)
                + struct.pack(">HHHHI", channels, bits, 0, 0, rate << 16)
                + full(b"alac", config))
    per_chunk = per_chunk or len(packets)
    groups = [packets[k: k + per_chunk] for k in range(0, len(packets), per_chunk)]
    ftyp = box(b"ftyp", b"M4A " + struct.pack(">I", 0) + b"M4A mp42isom")
    sizes = [len(p) for p in packets]

    def layout(mdat_start: int):
        offsets, body, at = [], b"", mdat_start + 8
        for group in groups:
            offsets.append(at)
            chunk = b"".join(group) + b"\xAA" * junk
            body += chunk
            at += len(chunk)
        return offsets, box(b"mdat", body)

    if moov_first:
        probe = _moov(entry, rate, sizes, frames, frame_length, [0] * len(groups), per_chunk)
        offsets, mdat = layout(len(ftyp) + len(probe))
        data = ftyp + _moov(entry, rate, sizes, frames, frame_length, offsets, per_chunk) + mdat
    else:
        offsets, mdat = layout(len(ftyp))
        data = ftyp + mdat + _moov(entry, rate, sizes, frames, frame_length, offsets, per_chunk)
    path.write_bytes(data)
    return path


def write_aac_m4a(path: Path, rate: int = 44100) -> Path:
    """An MP4 whose sound track says AAC ("mp4a"): recognised, never decoded."""
    entry = box(b"mp4a", bytes(6) + struct.pack(">H", 1) + struct.pack(">HHI", 0, 0, 0)
                + struct.pack(">HHHHI", 2, 16, 0, 0, rate << 16))
    ftyp = box(b"ftyp", b"M4A " + struct.pack(">I", 0) + b"M4A mp42isom")
    mdat = box(b"mdat", b"\0" * 64)
    moov = _moov(entry, rate, [64], 1024, 1024, [len(ftyp) + 8], 1)
    path.write_bytes(ftyp + mdat + moov)
    return path

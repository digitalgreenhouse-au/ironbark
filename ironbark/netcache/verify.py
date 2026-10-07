"""Checking a finished download before it becomes the copy everything plays (the cache's
"verify" thread; local disk only).

Always (the transfer):
1. Size: every byte from the start to the source's size is here, and the file on disk is exactly
   that long. (The I/O thread has already found the source ending exactly there.)
2. Transfer complete: every read returned what was asked up to the end, and the source's identity
   (size and modification time) was the same at the start, at every resume and at the end.

When "Check finished downloads" is on (the default):
3. Readable: it opens through the normal decode path, and the format, rate, channels and length
   match what the probe found while it downloaded.
4. Decodes: decoded to the end with no error, in 5-second pieces, pausing while the player's
   buffer is low (decoding holds Python's global lock). For FLAC, the MD5 fingerprint of the
   decoded samples must match the one the file carries (when it carries one) — proof the audio is
   exactly what was encoded. For an MP3, the frames are counted, so a header-less one that the
   decoder would stop short in is known about (its "shortfall").

A copy whose sound fails (3 or 4) after the transfer checks passed carries its SHA-256 (`digest`):
the same bytes failing the same way again mean the file on the drive is like that itself.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .. import audiofile
from ..analysis import pcm_bytes

VERIFY_CHUNK_S = 5
BUSY_SLEEP_S = 0.05
PCM_BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32}


@dataclass
class Result:
    ok: bool
    verified_by: str         # the checks it passed (on a failure: those before the one it failed)
    problem: str = ""
    info: dict = field(default_factory=dict)
    cancelled: bool = False
    digest: str = ""         # a copy whose sound failed its check: the SHA-256 of its bytes


def _digest(part: Path) -> str:
    sha = hashlib.sha256()
    with open(part, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _sound_failed(part: Path, steps: list, problem: str) -> Result:
    """The transfer checks passed but the sound didn't: what failed, and the bytes' fingerprint."""
    try:
        digest = _digest(part)
    except OSError:
        digest = ""
    return Result(False, "+".join(steps), problem, digest=digest)


def flac_md5(head: bytes) -> int | None:
    """The MD5 a FLAC file's STREAMINFO carries (0 = none recorded), from its first bytes."""
    at = head.find(b"fLaC")
    if at < 0 or len(head) < at + 42:
        return None
    if head[at + 4] & 0x7F != 0:   # STREAMINFO must come first
        return None
    return int.from_bytes(head[at + 26: at + 42], "big")


def check(item, gen: int, part: Path, *, decode: bool = True, should_yield=lambda: False,
          cancel=lambda: False) -> Result:
    """Check version `gen` of `item`, whose bytes are all in `part`."""
    steps = ["size", "identity"]
    try:
        on_disk = part.stat().st_size
    except OSError as error:
        return Result(False, "", f"the downloaded copy has gone ({error})")
    if item.size is None or on_disk != item.size or item.visible < item.size:
        return Result(False, "", f"the downloaded copy is {on_disk} bytes, not {item.size}")
    if not item.transfer_ok:
        return Result(False, "", "the download didn't finish cleanly")
    if not decode:
        return Result(True, "+".join(steps))
    probe = dict(item.info)
    found: dict = {}
    try:
        with open(part, "rb") as f:
            head = f.read(1 << 16)
        if audiofile.sniff_head(head[:64]) == "mp4":
            reader = audiofile.open_finished(part)
        else:
            reader = audiofile.open(audiofile.LocalTrack(Path(item.source), item, gen), patience=1.0,
                                    role="verify")
        with reader as f:
            found.update(format=f.format, subtype=f.subtype, rate=f.samplerate, channels=f.channels,
                         frames=f.frames)
            for name in ("format", "subtype", "rate", "channels", "frames"):
                if name in probe and probe[name] != found[name]:
                    return _sound_failed(part, steps, f"it doesn't open the same as while downloading ({name}: "
                                                      f"{probe[name]} then, {found[name]} now)")
            steps.append("readable")
            stored = flac_md5(head) if f.format == "FLAC" else None
            bits = PCM_BITS.get(f.subtype)
            md5 = hashlib.md5() if stored and bits else None
            decoded = 0
            for block in audiofile.blocks(f, max(1, f.samplerate * VERIFY_CHUNK_S), "int32", always_2d=True):
                if cancel():
                    return Result(False, "", "cancelled", cancelled=True)
                if md5 is not None:
                    md5.update(pcm_bytes(np.ascontiguousarray(block), bits))
                decoded += len(block)
                while should_yield() and not cancel():
                    time.sleep(BUSY_SLEEP_S)
            steps.append("decode")
            if md5 is not None:
                if int(md5.hexdigest(), 16) != stored:
                    return _sound_failed(part, steps, "its sound doesn't match the FLAC's own MD5 fingerprint")
                steps.append("flac-md5")
            if audiofile.is_mp3(f):
                shortfall = audiofile.mp3_shortfall(f, part)
                found["shortfall"] = list(shortfall) if shortfall else None
            found["decoded"] = decoded
    except audiofile.Waiting as error:
        return Result(False, "", f"part of the copy couldn't be read ({error.reason})")
    except (RuntimeError, ValueError, OSError) as error:
        return _sound_failed(part, steps, f"it doesn't decode ({error})")
    return Result(True, "+".join(steps), info=found)

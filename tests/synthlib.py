"""Test helper: synthetic music libraries — tracks with real-looking tags, paths and dates, and
nothing on disk — for checking the library database against the code it replaces (browse.py's
album rules, the old JSON index (legacy_index.py) and its new-music rules) and for timing it.

`make_library(n, root, seed)` gives a list of `SynthFile`s (one per music file: its path, size,
modified time, `added` / `seen` dates and tracks). It has what real libraries have: "The" bands,
accented names (Björk, Łódź, Ørsted…), albums tagged with an album artist and without,
compilations, "CD1" / "CD2" folders, one-file CUE images split into tracks, album folders with the
same name in two places ("Greatest Hits"), untagged rips, a few songs straight in the library
folder, MP3 and Ogg Vorbis among the FLACs, and the odd album of mixed formats.
"""
from __future__ import annotations

import os
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ironbark import library_db
from ironbark.audiofile import CuePath, CueRange
from ironbark.library import Track
from ironbark.library_index import Entry
from legacy_index import JsonIndex

WORDS = ("love night day heart fire rain blue black white dream light dark gold river road home song "
         "time world life soul moon star sun sea wind storm summer winter city street train ghost angel "
         "devil king queen girl boy man woman child baby heaven hell paradise sky ocean mountain desert "
         "Björk Beyoncé Motörhead Sigur Rós café naïve Ångström façade jalapeño Dvořák Fauré Łódź Ørsted "
         "Straße Œuvre Guðmundsdóttir AC/DC Guns N' Roses").split()
GENRES = ["Rock", "Pop", "Jazz", "Classical", "Electronic", "Hip-Hop", "Folk", "Blues", "Soul", "Metal",
          "Ambient", "Country", "Reggae", "Punk", "Indie", "R&B", "Funk", "Soundtrack", "Latin", "World",
          "Dance", "House", "Techno", "Opera", "Baroque", "Gospel", "Disco", "Grunge", "Shoegaze", "Trip-Hop"]
COMPOSERS = ["Johann Sebastian Bach", "Ludwig van Beethoven", "Wolfgang Amadeus Mozart", "Antonín Dvořák",
             "Gabriel Fauré", "Claude Debussy", "Frédéric Chopin", "Igor Stravinsky", "Philip Glass",
             "Arvo Pärt", "Lennon–McCartney", "Jagger/Richards"]
SHARED_NAMES = ["Greatest Hits", "Live", "Unplugged", "Demos"]
BASE_TIME = 1.6e9


@dataclass
class SynthFile:
    path: str                 # the music file
    size: int
    mtime: int                # modified time, ns
    added: float
    seen: float
    tracks: list[Track]


def _phrase(rng: random.Random, lo: int = 1, hi: int = 4) -> str:
    return " ".join(rng.choice(WORDS).capitalize() for _ in range(rng.randint(lo, hi)))


def _safe(name: str) -> str:
    """A tag as a file or folder name ("AC/DC" can't be one as it is)."""
    return name.replace("/", "-").replace("\\", "-")


def _details(codec: str) -> tuple:
    if codec == "MP3":
        return (("Codec", "MPEG-1 Layer III (MP3): lossy, detail was discarded when it was encoded"),
                ("Bitrate mode", "constant (CBR)"), ("Encoder", "LAME 3.100"))
    if codec == "Vorbis":
        return (("Codec", "Vorbis (in an Ogg file): lossy, detail was discarded when it was encoded"),
                ("Encoder", "Xiph.Org libVorbis I 20200704"))
    return ()


def make_library(n_tracks: int, root: str | Path, seed: int = 1) -> list[SynthFile]:
    """About `n_tracks` tracks under `root` (see the module notes); the same for the same seed."""
    rng = random.Random(seed)
    root = str(Path(root))
    n_albums = max(1, n_tracks // 11)
    artists = [("The " if rng.random() < 0.15 else "") + _phrase(rng, 1, 3) + f" {i}"
               for i in range(max(1, n_albums // 4))]
    files: list[SynthFile] = []
    taken: set[str] = set()   # every path once (two "Greatest Hits" by one artist share a folder)
    made = number = 0

    def unique(folder: str, name: str) -> str:
        path = os.path.join(folder, name)
        if path in taken:
            path = os.path.join(folder, f"{number} {name}")
        taken.add(path)
        return path
    while made < n_tracks:
        artist = rng.choice(artists)
        year = rng.randint(1950, 2025)
        shared = rng.random() < 0.04 or number in (3, 4)   # (always at least one repeated folder name)
        if number in (3, 4):
            artist = artists[0] if number == 3 else artists[-1]
        album = (SHARED_NAMES[0] if number in (3, 4) else rng.choice(SHARED_NAMES)) if shared else \
            _phrase(rng, 1, 4) + f" {number}"
        folder = os.path.join(root, _safe(artist), _safe(album if shared else f"{year} - {album}"))
        if number % 97 == 5:
            folder = root   # a few songs straight in the library folder
        untagged = rng.random() < 0.02
        compilation = rng.random() < 0.05
        album_artist = artist if rng.random() < 0.5 else ""
        genre = "; ".join(rng.sample(GENRES, rng.choice((1, 1, 1, 2))))
        composer = rng.choice(COMPOSERS) if rng.random() < 0.2 else ""
        roll = rng.random()
        codec = "FLAC" if roll < 0.9 else ("MP3" if roll < 0.98 else "Vorbis")
        mixed = rng.random() < 0.01
        rate, bits = rng.choice(((44100, 16), (44100, 16), (96000, 24), (48000, 24), (192000, 24)))
        count = min(rng.randint(6, 16), n_tracks - made)
        discs = 2 if count >= 12 and rng.random() < 0.15 and folder != root else 1
        cue = codec == "FLAC" and not mixed and discs == 1 and folder != root and rng.random() < 0.03
        added = BASE_TIME + rng.random() * 1e8
        seen = added + rng.random() * 1e6

        def tags(i: int, this_codec: str) -> dict:
            lossy = this_codec != "FLAC"
            return dict(title=_phrase(rng, 1, 5),
                        artist="" if untagged else (_phrase(rng, 1, 2) if compilation else artist),
                        album="" if untagged else album, album_artist="" if untagged else album_artist,
                        genre="" if untagged else genre, composer=composer, date="" if untagged else str(year),
                        compilation=compilation and not untagged, codec=this_codec,
                        sample_rate=44100 if lossy else rate, bits=0 if lossy else bits, channels=2,
                        bitrate=320000 if lossy else int(rate * bits * 2 * 0.6), details=_details(this_codec),
                        encoder="LAME 3.100" if this_codec == "MP3" else "reference libFLAC 1.4.3 20230623",
                        md5=0 if lossy else rng.getrandbits(128),
                        bitrate_mode="CBR" if this_codec == "MP3" else "",
                        channel_mode="joint stereo" if this_codec == "MP3" else "")

        if cue:
            image = unique(folder, f"{_safe(album)}.flac")
            total = int(sum(rng.uniform(90, 600) for _ in range(count)) * rate)
            starts = sorted(rng.sample(range(1, total), count - 1))
            bounds = [0] + starts + [None]
            size = int(total * bits * 2 / 8 * 0.6)
            tracks = []
            for i in range(count):
                start, end = bounds[i], bounds[i + 1]
                info = tags(i, "FLAC")
                info.update(md5=0, details=(("CUE sheet", f"track {i + 1} of {album}.cue"),))
                tracks.append(Track(path=CuePath(image, cue=CueRange(os.path.join(folder, f"{_safe(album)}.cue"),
                                                                     i + 1, start, end)),
                                    track_no=i + 1, disc_no=None,
                                    duration=((end or total) - start) / rate, size=size, **info))
            files.append(SynthFile(image, size, int(added * 1e9), added, seen, tracks))
            made += count
        else:
            for i in range(count):
                this_codec = rng.choice(("FLAC", "MP3")) if mixed else codec
                disc = 1 + (i * discs) // count
                here = os.path.join(folder, f"CD{disc}") if discs > 1 else folder
                info = tags(i, this_codec)
                ext = {"FLAC": ".flac", "MP3": ".mp3", "Vorbis": ".ogg"}[this_codec]
                path = unique(here, f"{i + 1:02d}{ext}" if untagged else f"{i + 1:02d} - {_safe(info['title'])}{ext}")
                if untagged:
                    info["title"] = f"Track {i + 1}"
                duration = rng.uniform(90, 600)
                size = int(duration * (rate * bits * 2 / 8 * 0.6 if this_codec == "FLAC" else 40000))
                files.append(SynthFile(path, size, int((added + i) * 1e9), added + i, seen + i,
                                       [Track(path=Path(path), track_no=None if untagged else i + 1,
                                              disc_no=disc if discs > 1 else None, duration=duration, size=size,
                                              **info)]))
                made += 1
        number += 1
    return files


def tracks_of(files: list[SynthFile]) -> list[Track]:
    return [t for f in files for t in f.tracks]


def added_of(files: list[SynthFile]) -> dict[str, float]:
    return {f.path: f.added for f in files}


def cue_sheets(files: list[SynthFile]) -> dict[str, list[list]]:
    """The .cue files the CUE images' folders would hold: {folder: [[name, size, modified ns]]}."""
    sheets = {}
    for f in files:
        if f.tracks and f.tracks[0].cue is not None:
            sheet = f.tracks[0].cue.sheet
            sheets[os.path.dirname(f.path)] = [[os.path.basename(sheet), 2000, f.mtime]]
    return sheets


def index_of(root: str | Path, files: list[SynthFile], built: float, path: Path) -> JsonIndex:
    """The old JSON index (legacy_index.py) holding exactly these files (saved to `path` by .save())."""
    index = JsonIndex(root, path)
    index.entries = {f.path: Entry(f.size, f.mtime, f.added, f.seen, list(f.tracks)) for f in files}
    index.cues = cue_sheets(files)
    index.built = built
    return index


def folder_results(root: str | Path, files: list[SynthFile]) -> list[library_db.FolderResult]:
    """What reading every folder would give (for LibraryDB.apply): each file's `added` as the time
    it was made, so a first look stores it as its `added`."""
    by_folder: dict[str, list[SynthFile]] = defaultdict(list)
    for f in files:
        by_folder[os.path.dirname(f.path)].append(f)
    sheets = cue_sheets(files)
    results = []
    for folder, group in by_folder.items():
        listing = {os.path.basename(f.path): (f.size, f.mtime, f.added) for f in group}
        tracks = {os.path.basename(f.path): f.tracks for f in group}
        sig = library_db.cue_signature_of(tuple(s) for s in sheets.get(folder, []))
        results.append(library_db.folder_result(root, folder, listing, tracks, cue_sig=sig))
    return results


def load(db: library_db.LibraryDB, root: str | Path, files: list[SynthFile]) -> None:
    """Store the files in the database as a library folder's first look would (then mark it built)."""
    now = max(f.added for f in files) + 1
    for result in folder_results(root, files):
        db.apply(result, now=now)
    db.mark_built(root, now)

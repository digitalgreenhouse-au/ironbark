"""The library database: every track's tags and details, kept in SQLite (`library.db` in the cache
folder: settings.cache_folder), with a search index over every song, play counts and favourites.
No UI.

It does what library_index.py's JSON file did, at any size: the Library opens without reading the
whole library into memory, one album's change is saved on its own (not the whole file again), and
a search for a song looks through the whole library in a few milliseconds.

**Several library folders** (`roots`): each music folder is stored relative to the library folder
it's in, so a library folder that moves (a drive back under another letter) keeps everything
(`move_root`).

**What's stored**, per music file: its size and modified time (to tell when it has changed), when
it arrived (`added`, for Recently added) and when the database first saw it (`seen`, for spotting
new music), which version of the tag reader read it (`read_format`), and its tracks — one, or one
per track for a CUE image. Per folder: its `.cue` files' names, sizes and times (a sheet changes how
the folder's files split into tracks). Per album folder: an album row made by browse.make_album, so
the Library's album list is one quick query and the album rules stay in browse.py.

**Writing** is one transaction per folder (`apply`), so a crash or a cancel loses at most the
folder being written, and the next look carries on from there. `read_folder` makes what `apply`
takes, with no database: it can run in a worker process.

**Not being able to see something is never taken as it being deleted.** A folder that couldn't be
listed keeps its rows as they were. A file that's gone from a folder's listing is only marked
missing (`missing_since`): it leaves the album list and the search at once, but keeps its dates,
so if it comes back (a drive plugged in again) it isn't "new". Missing rows are removed for good
by `purge_missing` — after a look at the whole library that went cleanly, or after
`MISSING_DAYS`.

**Search** (`search_songs`): SQLite's full-text index (FTS5) over every song's title, artist,
album, album artist, composer, genre, year and file name, folded by searchfold.py (case and
accents ignored). Every word typed must start a word somewhere; the best matches come first. When
the index finds nothing for 3 or more letters, a slower look for the letters anywhere ("ight"
inside "Night") is tried; and where this Python's SQLite has no FTS5 at all, every search is
done that slower way.

**Plays and favourites** are keyed by the file's key (audiofile.key: a CUE track is "file#3"),
not by row, so they survive the track rows being rebuilt. So are the **quality checks**
(library_check.py: one per song, with the size and modified time of the file it read, so a file
that changes is checked again).

**Threads**: any thread may read; each gets its own connection (SQLite's write-ahead log, WAL,
lets readers carry on while a writer writes). Only the indexer should write. A damaged file is set
aside as `library.db.damaged-dd-mm-yyyy` and an empty one started (`recover`), keeping the plays
and favourites when they can still be read. Damage found while the player runs is noted
(`note_damage`) and seen to at the next start, before anything reads the file. A file that's only
busy (another copy of the player writing) or can't be written (a full disk) is never set aside.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, audiofile, searchfold, settings
from .audiofile import CuePath, CueRange
from .browse import make_album
from .library import (_DISC_FOLDER, Album, Track, is_music_name, natural_key, play_order, read_track, skip_folder,
                      sort_name, split_cue_images)

FILE_NAME = "library.db"
SCHEMA_VERSION = 2          # PRAGMA user_version: bump with a step in _UPGRADES (else the library tables are remade)
# The tag reader's version: bump it when read_track() learns something new, and every file read by
# an older one is read again in the background (the library stays browsable meanwhile).
READ_FORMAT = 1
MISSING_DAYS = 7            # a missing file's row is kept this long without a clean look confirming it's gone
SEARCH_LIMIT = 200          # songs a search hands back (the best ones)
MIN_SEARCH = 2              # letters a search word needs before the index is asked (one letter finds most songs)
COUNT_CAP = 1000            # the slower searches stop counting here ("1,000+")
PLAY_FRACTION = 0.5         # heard this much of a song (or PLAY_SECONDS) and it counts as played
PLAY_SECONDS = 240.0
BUSY_S = 5.0                # how long a connection waits for another writer (another copy of the player) to finish
_BATCH = 500                # ids per IN (...) list
# SQLite's own result codes for a damaged file (the low byte of an extended code): SQLITE_CORRUPT and
# SQLITE_NOTADB. Anything else (busy, locked, a full disk, a folder that can't be written) isn't damage.
_DAMAGE_CODES = {11, 26}

# The search index's columns, and how much a match in each counts (bm25 weights).
FTS_COLUMNS = ("title", "artist", "album", "album_artist", "composer", "genre", "year", "path")
WEIGHTS = (10.0, 6.0, 5.0, 4.0, 3.0, 1.0, 1.0, 0.5)
FIELD_NAMES = ("title", "artist", "album", "album artist", "composer", "genre", "year", "file name")
_BM25 = "bm25(song_fts, " + ", ".join(f"{w:g}" for w in WEIGHTS) + ")"

_SEP = "\x1f"   # between the values of an album's codecs / genres / composers


def db_path() -> Path:
    return settings.cache_folder() / FILE_NAME


class Interrupted(Exception):
    """A search was stopped part-way (`LibraryDB.interrupt`): a newer one replaced it."""


class _NeedsFTS(sqlite3.DatabaseError):
    """The file has a search index this Python's SQLite can't open (no FTS5): it can't be used here."""


def is_damage(error: BaseException) -> bool:
    """Does this error say the database file is damaged (rather than busy, locked by another copy
    of the player, or on a full disk)?"""
    if isinstance(error, _NeedsFTS):
        return True
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(error, sqlite3.DatabaseError) and isinstance(code, int) and code & 0xFF in _DAMAGE_CODES


def is_interrupt(error: BaseException) -> bool:
    """Was this a read stopped by LibraryDB.interrupt?"""
    return isinstance(error, Interrupted) or (isinstance(error, sqlite3.OperationalError)
                                              and "interrupt" in str(error).lower())


def outermost(folders: Iterable[str | Path]) -> list[Path]:
    """The library folders as they should be kept: each once, and none inside another (a folder
    inside a library folder is already in the library; keeping both would list its albums
    twice). In the order given; a folder that takes in others keeps its own place."""
    paths = [Path(folder) for folder in folders]
    keys = [os.path.join(os.path.normcase(str(path)), "") for path in paths]
    kept = []
    for i, (path, key) in enumerate(zip(paths, keys)):
        if any(other != key and key.startswith(other) for other in keys) or key in keys[:i]:
            continue   # (inside another one, or listed already)
        kept.append(path)
    return kept


def fts_problem() -> str:
    """Why this Python's SQLite can't keep the song search index ("" when it can): it needs FTS5
    with the unicode61 word splitter and contentless_delete."""
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute(_FTS_TABLE)
        finally:
            con.close()
    except sqlite3.Error as error:
        return f"SQLite {sqlite3.sqlite_version} can't make the search index: {error}"
    return ""


# -- the tables ----------------------------------------------------------------------------------------
_TABLES = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS roots(
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
    built REAL NOT NULL DEFAULT 0);                     -- when its first look finished (0 = not yet)
CREATE TABLE IF NOT EXISTS folders(
    id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    rel TEXT NOT NULL,                                  -- below the root ("" = the root itself)
    album_folder_id INTEGER,                            -- itself, or the album a "CD1" folder belongs to
    cue_sig TEXT NOT NULL DEFAULT '',                   -- its .cue files (cue_signature)
    listed_ok INTEGER NOT NULL DEFAULT 1,               -- 0: the last look couldn't list it
    UNIQUE(root_id, rel));
CREATE INDEX IF NOT EXISTS folders_album ON folders(album_folder_id);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY, folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    name TEXT NOT NULL, size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,                          -- -1: read it again next time
    added REAL NOT NULL, seen REAL NOT NULL, read_format INTEGER NOT NULL DEFAULT 0,
    missing_since REAL,                                 -- gone from its folder's listing since then
    UNIQUE(folder_id, name));
CREATE INDEX IF NOT EXISTS files_seen ON files(seen);
CREATE INDEX IF NOT EXISTS files_missing ON files(missing_since) WHERE missing_since IS NOT NULL;
CREATE TABLE IF NOT EXISTS tracks(
    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    album_id INTEGER NOT NULL,                          -- the album folder's folders.id (= albums.id)
    cue_sheet TEXT, cue_number INTEGER, cue_start INTEGER, cue_end INTEGER,
    title TEXT NOT NULL, artist TEXT NOT NULL, album TEXT NOT NULL, album_artist TEXT NOT NULL,
    composer TEXT NOT NULL, genre TEXT NOT NULL, date TEXT NOT NULL, year INTEGER,
    track_no INTEGER, disc_no INTEGER, duration REAL NOT NULL, sample_rate INTEGER NOT NULL,
    bits INTEGER NOT NULL, channels INTEGER NOT NULL, bitrate INTEGER NOT NULL, size INTEGER NOT NULL,
    codec TEXT NOT NULL,
    md5 TEXT,                                           -- hex: 128 bits don't fit SQLite's 64-bit integers
    encoder TEXT NOT NULL, float_samples INTEGER NOT NULL, bitrate_mode TEXT NOT NULL,
    channel_mode TEXT NOT NULL, misnamed TEXT NOT NULL, unsupported TEXT NOT NULL,
    compilation INTEGER NOT NULL, details TEXT,         -- details: JSON [[name, value], …]
    haystack TEXT NOT NULL);                            -- folded search text (the slower searches)
CREATE INDEX IF NOT EXISTS tracks_file ON tracks(file_id);
CREATE INDEX IF NOT EXISTS tracks_album ON tracks(album_id);
CREATE TABLE IF NOT EXISTS albums(
    id INTEGER PRIMARY KEY REFERENCES folders(id) ON DELETE CASCADE,   -- the album folder's id
    name TEXT NOT NULL, display_name TEXT NOT NULL,     -- folder name; with "  (parent)" when repeated
    title TEXT NOT NULL, artist TEXT NOT NULL, year INTEGER, codecs TEXT NOT NULL,
    sample_rate INTEGER NOT NULL, bits INTEGER NOT NULL, track_count INTEGER NOT NULL, added REAL NOT NULL,
    genres TEXT NOT NULL, composers TEXT NOT NULL,
    sort_name TEXT NOT NULL, sort_artist TEXT NOT NULL);   -- for ordering in SQL (_sort_text)
CREATE INDEX IF NOT EXISTS albums_name ON albums(name);
CREATE TABLE IF NOT EXISTS plays(
    id INTEGER PRIMARY KEY, key TEXT NOT NULL,          -- audiofile.key: the file, "#n" for a CUE track
    played_at REAL NOT NULL,                            -- seconds since 1970 (UTC)
    seconds_heard REAL NOT NULL, completed INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS plays_key ON plays(key, played_at);
CREATE INDEX IF NOT EXISTS plays_when ON plays(played_at);
CREATE TABLE IF NOT EXISTS favourites(key TEXT PRIMARY KEY, added REAL NOT NULL) WITHOUT ROWID;
"""
# (Version 2) The whole-library quality check's results (library_check.py), one per song.
_CHECK_TABLES = """
CREATE TABLE IF NOT EXISTS checks(
    key TEXT PRIMARY KEY,                               -- audiofile.key: the file, "#n" for a CUE track
    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,   -- the file as it was read (changed since: check again)
    format INTEGER NOT NULL,                            -- which version of the check made it
    checked REAL NOT NULL,                              -- when (seconds since 1970)
    findings TEXT NOT NULL,                             -- what needs a look, worst first ("" = nothing)
    true_peak REAL,                                     -- linear, 1.0 = full scale (the clipping guard's figure)
    result TEXT NOT NULL) WITHOUT ROWID;                -- the whole result as JSON ("" = none to keep)
CREATE INDEX IF NOT EXISTS checks_found ON checks(findings) WHERE findings != '';
"""
_TABLES += _CHECK_TABLES
# The song search index: no text of its own (the rows are tracks.id), already-folded text in,
# "unicode61" splitting it into words. No prefix index: three times the size for little speed.
_FTS_TABLE = ("CREATE VIRTUAL TABLE IF NOT EXISTS song_fts USING fts5(" + ", ".join(FTS_COLUMNS)
              + ", content='', contentless_delete=1, tokenize='unicode61 remove_diacritics 2')")
_FTS_TRIGGER = ("CREATE TRIGGER IF NOT EXISTS tracks_unindexed AFTER DELETE ON tracks "
                "BEGIN DELETE FROM song_fts WHERE rowid = old.id; END")
_LIBRARY_TABLES = ("song_fts", "tracks", "albums", "files", "folders", "roots", "meta")
_KEPT_TABLES = {"plays": {"id", "key", "played_at", "seconds_heard", "completed"},
                "favourites": {"key", "added"},
                "checks": {"key", "size", "mtime_ns", "format", "checked", "findings", "true_peak", "result"}}
_CHECK_COLUMNS = "key, size, mtime_ns, format, checked, findings, true_peak, result"

# Upgrades from one schema version to the next: {old version: fn(connection)}. A version with no
# way up has its library tables made again (they're only a record of the music files).
def _to_2(con: sqlite3.Connection) -> None:
    """1 → 2: the quality check's table."""
    for statement in _statements(_CHECK_TABLES):
        con.execute(statement)


_UPGRADES: dict[int, Callable[[sqlite3.Connection], None]] = {1: _to_2}

_TRACK_FIELDS = ("cue_sheet", "cue_number", "cue_start", "cue_end", "title", "artist", "album", "album_artist",
                 "composer", "genre", "date", "year", "track_no", "disc_no", "duration", "sample_rate", "bits",
                 "channels", "bitrate", "size", "codec", "md5", "encoder", "float_samples", "bitrate_mode",
                 "channel_mode", "misnamed", "unsupported", "compilation", "details", "haystack")
_INSERT_TRACK = (f"INSERT INTO tracks(file_id, album_id, {', '.join(_TRACK_FIELDS)}) "
                 f"VALUES ({', '.join('?' * (len(_TRACK_FIELDS) + 2))})")
_INSERT_FTS = (f"INSERT INTO song_fts(rowid, {', '.join(FTS_COLUMNS)}) "
               f"VALUES ({', '.join('?' * (len(FTS_COLUMNS) + 1))})")
# A track and where its file is: (root path, folder below the root, file name, track id, fields…)
_TRACK_SELECT = ("SELECT r.path, d.rel, f.name, t.id, " + ", ".join(f"t.{n}" for n in _TRACK_FIELDS[:-1])
                 + " FROM tracks t JOIN files f ON f.id = t.file_id JOIN folders d ON d.id = f.folder_id"
                 " JOIN roots r ON r.id = d.root_id")
_ALBUM_TRACKS = "SELECT t.album_id, " + _TRACK_SELECT[len("SELECT "):]   # (the album's id, then as _TRACK_SELECT)
# As _TRACK_SELECT, then the file's size, modified time and missing_since (the quality check's list)
_CHECK_SELECT = _TRACK_SELECT.replace(" FROM tracks t", ", f.size, f.mtime_ns, f.missing_since FROM tracks t", 1)
# Just what the quality check reads of each song (LibraryDB.check_list), folder by folder.
_CHECK_LIST = ("SELECT r.path, d.rel, f.name, t.cue_sheet, t.cue_number, t.cue_start, t.cue_end, t.bits, t.md5,"
               " t.duration, t.sample_rate, t.misnamed, t.unsupported, f.size, f.mtime_ns, f.missing_since"
               " FROM tracks t JOIN files f ON f.id = t.file_id JOIN folders d ON d.id = f.folder_id"
               " JOIN roots r ON r.id = d.root_id ORDER BY r.id, d.rel, f.name, t.cue_number")


# -- what a look at one folder found ---------------------------------------------------------------------
@dataclass
class FolderResult:
    """One folder's music as it is now, for LibraryDB.apply. Plain values only (it may come from a
    worker process): made by `read_folder`, or `folder_result` from tracks already read."""
    root: str                         # the library folder it's in
    folder: str                       # the folder itself (full path)
    # Every music file in it now: name -> (size, modified ns, created s). Files that are being
    # copied are left out (a new one), or listed as they were (a changed one).
    listing: dict[str, tuple[int, int, float]]
    # The files read this time: name -> their tracks, as track_row values
    tracks: dict[str, list[tuple]] = field(default_factory=dict)
    retry: list[str] = field(default_factory=list)   # couldn't be read just now: read again next time
    cue_sig: str = ""                 # its .cue files (cue_signature)
    listed_ok: bool = True            # False: it couldn't be listed, so everything stored for it stays
    read_format: int = READ_FORMAT


@dataclass(frozen=True)
class FolderState:
    id: int
    cue_sig: str
    listed_ok: bool


@dataclass(frozen=True)
class FileState:
    size: int
    mtime_ns: int        # -1: due to be read again
    missing: bool        # gone from the folder's last listing (kept for a while: see the module notes)
    read_format: int


@dataclass(frozen=True)
class SongHit:
    id: int              # tracks.id (LibraryDB.tracks)
    track: Track
    album: Path          # its album folder
    where: str = ""      # the field the search words were found in when it isn't the title, artist or
                         # album ("composer", "genre", "file name"…), for "composer: Bach"


@dataclass(frozen=True)
class AlbumOrder:
    """An album's songs as a tab of many albums lists them, without a Track made for each: their ids
    in play order, how long they last, and the artists, album names and qualities among them (for
    the columns that show only where a value varies)."""
    ids: list[int]
    seconds: float
    artists: frozenset[str]
    names: frozenset[str]
    qualities: frozenset[tuple]


@dataclass(frozen=True)
class SongHits:
    rows: list[SongHit]  # the best matches first, at most the limit asked for
    total: int           # how many songs match
    capped: bool = False  # counting stopped at COUNT_CAP: there are at least `total` ("1,000+")


def cue_signature(folder: str | Path) -> str:
    """A folder's .cue files as text — their names, sizes and modified times — to tell when one has
    changed. "" when there are none. OSError when the folder can't be listed."""
    folder = os.fspath(folder)
    found = []
    for name in sorted(n for n in os.listdir(folder) if n.lower().endswith(".cue") and not n.startswith("._")):
        try:
            st = os.stat(os.path.join(folder, name))
        except OSError:
            continue
        found.append((name, st.st_size, st.st_mtime_ns))
    return cue_signature_of(found)


def cue_signature_of(sheets: Iterable[tuple[str, int, int]]) -> str:
    """cue_signature from (name, size, modified ns) already known (e.g. from a folder walk)."""
    found = sorted([str(n), int(s), int(m)] for n, s, m in sheets)
    return json.dumps(found, separators=(",", ":")) if found else ""


# -- tracks to and from rows -----------------------------------------------------------------------------
def _search_texts(title: str, artist: str, album: str, album_artist: str, composer: str, genre: str, year: str,
                  stem: str) -> tuple[tuple[str, ...], str]:
    """The folded texts the search index holds for a song, and the same joined (the haystack)."""
    folded = (searchfold.fold(title), searchfold.fold(artist), searchfold.fold(album), searchfold.fold(album_artist),
              searchfold.fold(composer), searchfold.fold(genre), year, searchfold.fold(stem))
    return folded, " ".join(folded)


def track_row(track: Track) -> tuple[tuple, tuple[str, ...]]:
    """A Track as plain values: (its tracks-table fields, its search-index texts). Folding is done
    here, so it can be done in a worker process."""
    cue = audiofile.cue_of(track.path)
    year = track.year
    stem = os.path.splitext(os.path.basename(os.fspath(track.path)))[0]
    fts, haystack = _search_texts(track.title, track.artist, track.album, track.album_artist, track.composer,
                                  track.genre, str(year) if year else "", stem)
    details = json.dumps([list(pair) for pair in track.details]) if track.details else None
    values = (_to_db(cue.sheet) if cue else None, cue.number if cue else None, cue.start if cue else None,
              cue.end if cue else None, track.title, track.artist, track.album, track.album_artist,
              track.composer, track.genre, track.date, year, track.track_no, track.disc_no, float(track.duration),
              track.sample_rate, track.bits, track.channels, track.bitrate, track.size, track.codec,
              format(track.md5, "032x") if track.md5 else None, track.encoder, int(track.float_samples),
              track.bitrate_mode, track.channel_mode, track.misnamed, track.unsupported, int(track.compilation),
              details, haystack)
    return values, fts


def _to_db(text: str) -> str | bytes:
    """A file or folder name as stored: as it is, or — a name Windows allows that isn't valid
    Unicode (a lone surrogate), which SQLite's text can't hold — its bytes, read back exactly by
    _from_db."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass")
    return text


def _from_db(value) -> str:
    return value.decode("utf-8", "surrogatepass") if isinstance(value, bytes) else value


def _text(value: str) -> str:
    """Text that's only shown or searched (a title made from such a file name), with any character
    SQLite can't hold replaced."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


def _run(con: sqlite3.Connection, sql: str, values: tuple) -> sqlite3.Cursor:
    """con.execute, and when a text can't be held (see _text), again with those texts made safe."""
    try:
        return con.execute(sql, values)
    except UnicodeEncodeError:
        return con.execute(sql, tuple(_text(v) if isinstance(v, str) else v for v in values))


def _join(root, rel, name="") -> str:
    root, rel, name = _from_db(root), _from_db(rel), _from_db(name)
    folder = os.path.join(root, rel) if rel else root
    return os.path.join(folder, name) if name else folder


def _play_ordered(folder: str, rows: list[tuple]) -> list[int]:
    """album_orders' rows (track id, root, folder below it, file name, album tag, disc, track, …) of
    one album: the ids in play order — library.play_order's sort (subfolder, album tag, disc, track
    number, file name, natural order) worked out from the rows. The names are only compared when
    the numbers leave a tie, so mostly they aren't made into sort keys at all."""
    top = folder if folder.endswith(os.sep) else folder + os.sep
    subs: dict[tuple, str] = {}

    def sub_of(root, rel) -> str:
        found = subs.get((root, rel))
        if found is None:
            here = _join(root, rel)
            if here == top or here + os.sep == top:
                found = "."
            elif here.startswith(top):
                found = here[len(top):].replace(os.sep, "/")
            else:
                found = Path(here).relative_to(folder).as_posix()
            subs[(root, rel)] = found
        return found

    numbers = [(sub_of(row[1], row[2]), row[4], row[5] or 0, row[6] if row[6] is not None else 10_000)
               for row in rows]
    if len({(sub, album) for sub, album, _disc, _track in numbers}) == 1:   # (one folder, one album tag)
        keys = [(disc, track) for _sub, _album, disc, track in numbers]
    else:
        keys = [(tuple(natural_key(sub)), tuple(natural_key(album)), disc, track)
                for sub, album, disc, track in numbers]

    if len(set(keys)) != len(keys):   # (a tie: the file names decide)
        keys = [key + (tuple(natural_key(_from_db(row[3]))),) for key, row in zip(keys, rows)]
    order = sorted(range(len(rows)), key=keys.__getitem__)
    return [rows[i][0] for i in order]


def _track_path(root, rel, name, cue_sheet, cue_number, cue_start, cue_end) -> Path:

    """A song's path from its stored parts: a CuePath (its stretch of the file) for a CUE track."""
    text = _join(root, rel, name)
    return (Path(text) if cue_number is None
            else CuePath(text, cue=CueRange(_from_db(cue_sheet), cue_number, cue_start, cue_end)))


def _track(row) -> Track:
    """A Track from a _TRACK_SELECT row."""
    (root, rel, name, _id, cue_sheet, cue_number, cue_start, cue_end, title, artist, album, album_artist, composer,
     genre, date, _year, track_no, disc_no, duration, sample_rate, bits, channels, bitrate, size, codec, md5,
     encoder, float_samples, bitrate_mode, channel_mode, misnamed, unsupported, compilation, details) = row
    path = _track_path(root, rel, name, cue_sheet, cue_number, cue_start, cue_end)
    return Track(path=path, title=title, artist=artist, album=album, track_no=track_no, disc_no=disc_no,
                 duration=duration, sample_rate=sample_rate, bits=bits, channels=channels, bitrate=bitrate,
                 encoder=encoder, md5=int(md5, 16) if md5 else 0, size=size, codec=codec,
                 float_samples=bool(float_samples), bitrate_mode=bitrate_mode, channel_mode=channel_mode,
                 details=tuple(tuple(pair) for pair in json.loads(details)) if details else (),
                 misnamed=misnamed, unsupported=unsupported, album_artist=album_artist, genre=genre,
                 composer=composer, date=date, compilation=bool(compilation))


def _sort_text(text: str) -> str:
    """A text that sorts in SQL roughly as library.sort_name does: folded, "The" left off, and
    numbers padded so "2" comes before "10"."""
    text = re.sub(r"^\s*the\s+", "", text or "", count=1, flags=re.IGNORECASE).strip() or (text or "")
    return re.sub(r"\d+", lambda m: m.group(0).rjust(12, "0"), searchfold.fold(text))


def _album_rel(rel: str) -> str:
    """The album folder (below the root) for a folder: its parent when it's a "CD1" / "Disc 2"
    folder inside an album, else itself. (A disc folder right at the top of a library folder is
    an album of its own: the library folder's parent isn't part of the library.)"""
    parent, name = os.path.split(rel)
    return parent if parent and _DISC_FOLDER.match(name) else rel


def _rel(root: str, folder: str) -> str:
    """`folder` below `root` ("" = the root). ValueError when it isn't in it."""
    if folder == root:
        return ""
    top = root if root.endswith(os.sep) else root + os.sep
    if folder.startswith(top):
        return folder[len(top):]
    rel = os.path.relpath(folder, root)   # (another spelling of the same folder, e.g. its case)
    if rel == ".":
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise ValueError(f"{folder} isn't in the library folder {root}")
    return rel


def _chunks(items: list, size: int = _BATCH):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def counts_as_play(seconds_heard: float, duration: float) -> bool:
    """A song counts as played once half of it (or 4 minutes of a long one) has been heard, as the
    usual scrobbling rule has it. (Short songs count too: these are the listener's own counts.)"""
    if seconds_heard <= 0:
        return False
    if duration > 0 and seconds_heard >= duration * PLAY_FRACTION:
        return True
    return seconds_heard >= PLAY_SECONDS


def _key(path_or_key) -> str:
    return path_or_key if isinstance(path_or_key, str) else audiofile.key(path_or_key)


def _keys_in(con: sqlite3.Connection, sql: str, keys: list[str]) -> list[tuple]:
    """Rows of `sql` (which has "IN ({marks})" for the keys) for these play / favourite keys, a
    batch at a time, with each row's key as text."""
    rows = []
    for chunk in _chunks(keys):
        rows += [(_from_db(key), *rest) for key, *rest in con.execute(
            sql.format(marks=", ".join("?" * len(chunk))), [_to_db(k) for k in chunk])]
    return rows


# -- the database ----------------------------------------------------------------------------------------
class LibraryDB:
    """The library database (see the module notes). Open with `LibraryDB.open(path, roots)`."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.fts = True                 # the search index is in use (else every search is the slow kind)
        self.problem = ""               # what went wrong opening it ("" = nothing): e.g. a damaged file set aside
        self._local = threading.local()
        self._lock = threading.Lock()
        # thread id -> (the thread, its connection); a thread that has ended has its closed for it
        self._connections: dict[int, tuple[threading.Thread, sqlite3.Connection]] = {}
        self._epoch = 0                 # bumped when every connection is closed (close, recover)
        self._closed = False
        self._ready = threading.Event()   # the file has been made or upgraded (_get_ready)
        self._ready_lock = threading.RLock()
        self._preparing = False

    @classmethod
    def open(cls, path: str | Path | None = None, roots: Iterable[str | Path] | None = None,
             lazy: bool = False) -> LibraryDB:
        """Open (or start) the database at `path` (default db_path()); `roots`: the library folders,
        when known (set_roots). A file that isn't a usable database is set aside and an empty one
        started (`problem` says so). `lazy`: nothing is done until it's first used — by then on a
        worker thread, as making or checking the file can take a moment."""
        db = cls(path or db_path())
        if not lazy:
            db._get_ready()
        if roots is not None:
            db.set_roots(roots)
        return db

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # -- connections --
    def _connect(self) -> sqlite3.Connection:
        # isolation_level None: no hidden transactions; reads and writes say where theirs begin.
        con = sqlite3.connect(self.path, timeout=BUSY_S, isolation_level=None, check_same_thread=False)
        try:
            con.execute(f"PRAGMA busy_timeout = {int(BUSY_S * 1000)}")
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("PRAGMA synchronous = NORMAL")   # with WAL: safe against crashes, quick to commit
            con.execute("PRAGMA cache_size = -32000")     # 32 MB
        except BaseException:
            con.close()   # (not a database: let go of the file, so it can be set aside)
            raise
        return con

    def _con(self) -> sqlite3.Connection:
        """This thread's connection (made on first use)."""
        local = self._local
        con = getattr(local, "con", None)
        if con is not None and local.epoch == self._epoch:
            return con
        if self._closed:
            raise sqlite3.ProgrammingError("the library database is closed")
        if not self._ready.is_set():
            self._get_ready()
        con = self._connect()
        with self._lock:
            ended = [ident for ident, (thread, _c) in self._connections.items() if not thread.is_alive()]
            left = [self._connections.pop(ident)[1] for ident in ended]
            self._connections[threading.get_ident()] = (threading.current_thread(), con)
            local.con, local.epoch = con, self._epoch
        for old in left:   # (workers that never said release: nobody else can be using theirs)
            old.close()
        return con

    def release(self) -> None:
        """Close this thread's connection (a thread that's finished with the database). A thread
        that ends without it has its connection closed when another thread next connects."""
        with self._lock:
            found = self._connections.pop(threading.get_ident(), None)
            self._local.con = None
        if found is not None:
            found[1].close()

    def _close_all(self) -> None:
        with self._lock:
            connections = [con for _thread, con in self._connections.values()]
            self._connections.clear()
            self._epoch += 1
            self._local.con = None
        for con in connections:
            try:
                con.close()
            except sqlite3.Error:
                pass

    def close(self) -> None:
        """Close every thread's connection. Only when no other thread is still using it."""
        self._closed = True
        self._close_all()

    def interrupt(self, thread: threading.Thread | int) -> None:
        """Stop what `thread` is asking the database for now (a search a newer one replaces): it
        gets Interrupted. Nothing happens if it isn't asking anything."""
        ident = thread if isinstance(thread, int) else thread.ident
        with self._lock:
            found = self._connections.get(ident)
        if found is not None:
            found[1].interrupt()

    @contextmanager
    def _writing(self, bump: bool = True):
        """One write transaction (taken at once, so two writers queue rather than fail part-way).
        `bump`: it changes the library, so the generation goes up with it."""
        con = self._con()
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
            if bump:
                con.execute("UPDATE meta SET value = value + 1 WHERE key = 'generation'")
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise

    @contextmanager
    def _reading(self):
        """Several reads that must see the same moment (a writer may commit in between)."""
        con = self._con()
        con.execute("BEGIN")
        try:
            yield con
        finally:
            if con.in_transaction:
                con.execute("COMMIT")

    # -- the file: making, upgrading, checking --
    def _get_ready(self) -> None:
        """Make or upgrade the file, once; other threads asking meanwhile wait for it. (Preparing
        opens this thread's connection through _con, hence the re-entrant lock.)"""
        with self._ready_lock:
            if self._ready.is_set() or self._preparing:
                return
            self._preparing = True
            try:
                self._prepare()
                self._ready.set()
            except BaseException:
                self.release()   # (so this thread's next use prepares the file again, not just reconnects)
                raise
            finally:
                self._preparing = False

    def _prepare(self) -> None:
        """Make or upgrade the file. A file that's damaged (or not a database at all) is set aside
        and an empty one started, keeping the library folders, plays and favourites that can
        still be read; so is one found damaged while the player last ran (note_damage), once
        SQLite's own check agrees. Anything else that stops it opening — another copy of the
        player writing, a full disk, a folder that can't be written — is raised as it is, and the
        next use tries again: the file is never set aside for that."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._prepare_file()
        except sqlite3.DatabaseError as error:
            if not is_damage(error):
                raise
            self._start_again(f"The library database couldn't be read ({error}): it was set aside")
            return
        mark = self._damage_mark()
        if mark.exists():
            found = self._check_file()   # (busy or locked: raised, and the mark stays for the next start)
            if found != "ok":
                self._start_again(f"The library database was damaged ({found}): it was set aside")
            try:
                mark.unlink()
            except OSError:
                pass

    def _damage_mark(self) -> Path:
        return self.path.with_name(self.path.name + ".check")

    def note_damage(self, error: BaseException) -> bool:
        """`error` came from this database: when it says the file is damaged, that's noted (a small
        file beside it), so the next start checks the file and, if SQLite agrees, sets it aside
        and starts again (keeping plays and favourites) — before anything is reading it, which
        can't be done safely while the player runs. Returns whether it was damage."""
        if not is_damage(error):
            return False
        try:
            self._damage_mark().touch()
        except OSError:
            pass
        return True

    def _start_again(self, why: str) -> Path | None:
        """Set the file aside (library.db.damaged-dd-mm-yyyy) and start an empty one, keeping the
        library folders, plays and favourites that can still be read."""
        roots, plays, favourites, checks = self._salvage()
        aside = self._set_aside()
        self._prepare_file()
        with self._writing() as con:
            con.executemany("INSERT OR IGNORE INTO roots(path) VALUES (?)", [(r,) for r in roots])
            con.executemany("INSERT INTO plays(key, played_at, seconds_heard, completed) VALUES (?, ?, ?, ?)", plays)
            con.executemany("INSERT OR IGNORE INTO favourites(key, added) VALUES (?, ?)", favourites)
            con.executemany(f"INSERT OR IGNORE INTO checks({_CHECK_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", checks)
        self.problem = (why + (f" as {aside.name}" if aside else "")
                        + ", and the library is being looked through again.")
        return aside

    def _prepare_file(self) -> None:
        con = self._con()
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA journal_size_limit = 67108864")   # the log is cut back to 64 MB after use
        version = con.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        fts_ok = not fts_problem()
        if "song_fts" in tables and not fts_ok:
            # Made where SQLite had FTS5; without it here even deleting a track would fail.
            raise _NeedsFTS("its search index needs FTS5, which this SQLite doesn't have")
        con.execute("BEGIN IMMEDIATE")
        try:
            if version != SCHEMA_VERSION and (version or tables):
                _upgrade(con, version, tables)
            _create(con, fts_ok)
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        self.fts = fts_ok

    def _set_aside(self) -> Path | None:
        """Move the database (and its log) out of the way, as library.db.damaged-dd-mm-yyyy. OSError
        when it can't be moved (another program has it open)."""
        self._close_all()
        stamp = time.strftime("%d-%m-%Y")
        aside, n = self.path.with_name(f"{self.path.name}.damaged-{stamp}"), 2
        while aside.exists():
            aside, n = self.path.with_name(f"{self.path.name}.damaged-{stamp}-{n}"), n + 1
        try:
            os.replace(self.path, aside)
        except FileNotFoundError:
            aside = None
        for suffix in ("-wal", "-shm"):   # (a log left behind would be read into the new file)
            try:
                if aside is not None:
                    os.replace(f"{self.path}{suffix}", f"{aside}{suffix}")
                else:
                    os.unlink(f"{self.path}{suffix}")
            except FileNotFoundError:
                pass
        return aside

    def quick_check(self) -> str:
        """SQLite's own check of the file ("ok" when it's sound). Takes a while on a big library:
        run it on a worker thread."""
        try:
            return self._check_file()
        except sqlite3.DatabaseError as error:
            return str(error) or "damaged"

    def _check_file(self) -> str:
        """quick_check, except that an error that isn't damage (busy, locked) is raised."""
        try:
            rows = self._con().execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as error:
            if not is_damage(error):
                raise
            return str(error) or "damaged"
        found = "; ".join(str(row[0]) for row in rows)
        return "ok" if found == "ok" else found

    def recover(self) -> Path | None:
        """Set a damaged database aside (library.db.damaged-dd-mm-yyyy) and start an empty one in its
        place, keeping the library folders, plays and favourites that can still be read. Every
        thread's connection is closed: no other thread may be using it. Returns where the damaged
        file went (None if there was no file)."""
        return self._start_again("The library database was damaged: it was set aside")

    def _salvage(self) -> tuple[list, list, list, list]:
        found = []
        for sql in ("SELECT path FROM roots", "SELECT key, played_at, seconds_heard, completed FROM plays",
                    "SELECT key, added FROM favourites", f"SELECT {_CHECK_COLUMNS} FROM checks"):
            try:
                rows = self._con().execute(sql).fetchall()
            except sqlite3.Error:
                rows = []
            found.append([row[0] for row in rows] if sql.startswith("SELECT path") else rows)
        return found[0], found[1], found[2], found[3]

    @property
    def generation(self) -> int:
        """Goes up with every change to the library's tracks and albums: re-read when it differs."""
        return int(self._con().execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()[0])

    @property
    def needs_refold(self) -> bool:
        """The search texts were folded by another version of searchfold (or there's no index for
        them yet): `refold` brings them up to date."""
        row = self._con().execute("SELECT value FROM meta WHERE key = 'fold_version'").fetchone()
        return row is None or row[0] != searchfold.FOLD_VERSION

    def refold(self, batch: int = 2000, cancel: Callable[[], bool] | None = None) -> bool:
        """Fold every song's search text again, `batch` songs per transaction (searches keep working
        meanwhile). False when `cancel()` stopped it (the next call starts again)."""
        con, last = self._con(), 0
        while True:
            rows = con.execute("SELECT t.id, t.title, t.artist, t.album, t.album_artist, t.composer, t.genre, t.year,"
                               " f.name, f.missing_since FROM tracks t JOIN files f ON f.id = t.file_id"
                               " WHERE t.id > ? ORDER BY t.id LIMIT ?", (last, batch)).fetchall()
            if not rows:
                break
            with self._writing() as w:
                for track_id, title, artist, album, album_artist, composer, genre, year, name, missing in rows:
                    fts, haystack = _search_texts(title, artist, album, album_artist, composer, genre,
                                                  str(year) if year else "", os.path.splitext(_from_db(name))[0])
                    _run(w, "UPDATE tracks SET haystack = ? WHERE id = ?", (haystack, track_id))
                    if self.fts:
                        w.execute("DELETE FROM song_fts WHERE rowid = ?", (track_id,))
                        if missing is None:
                            _run(w, _INSERT_FTS, (track_id, *fts))
            last = rows[-1][0]
            if cancel is not None and cancel():
                return False
        with self._writing() as w:
            w.execute("UPDATE meta SET value = ? WHERE key = 'fold_version'", (searchfold.FOLD_VERSION,))
        return True

    def counts(self) -> dict[str, int]:
        """How many folders, files, tracks and albums are stored, and files missing."""
        con = self._con()
        return {"folders": con.execute("SELECT count(*) FROM folders").fetchone()[0],
                "files": con.execute("SELECT count(*) FROM files").fetchone()[0],
                "missing": con.execute("SELECT count(*) FROM files WHERE missing_since IS NOT NULL").fetchone()[0],
                "tracks": con.execute("SELECT count(*) FROM tracks").fetchone()[0],
                "albums": con.execute("SELECT count(*) FROM albums").fetchone()[0]}

    # -- library folders --
    def roots(self) -> list[str]:
        return [_from_db(row[0]) for row in self._con().execute("SELECT path FROM roots ORDER BY id")]

    def set_roots(self, paths: Iterable[str | Path]) -> None:
        """The library folders are now these: new ones are added (empty until looked at), ones no
        longer listed are removed with everything stored for them (plays and favourites stay).
        A folder inside another one is left out (outermost)."""
        wanted = [str(path) for path in outermost(paths)]
        with self._writing() as con:
            have = [(root_id, _from_db(path)) for root_id, path in con.execute("SELECT id, path FROM roots")]
            for text in wanted:
                if not any(Path(path) == Path(text) for _id, path in have):
                    con.execute("INSERT INTO roots(path) VALUES (?)", (_to_db(text),))
            gone = [root_id for root_id, path in have if not any(Path(path) == Path(w) for w in wanted)]
            for root_id in gone:
                con.execute("DELETE FROM roots WHERE id = ?", (root_id,))
            if gone:
                _rename(con, [row[0] for row in con.execute("SELECT DISTINCT name FROM albums")])

    def move_root(self, old: str | Path, new: str | Path) -> bool:
        """A library folder has moved (a drive back under another letter, a share mounted somewhere
        else): everything stored for `old` now belongs to `new`, so nothing has to be read again.
        False when `old` isn't a library folder or `new` already is one."""
        with self._writing() as con:
            found = _find_root(con, str(Path(old)))
            if found is None or _find_root(con, str(Path(new))) is not None:
                return False
            con.execute("UPDATE roots SET path = ? WHERE id = ?", (_to_db(str(Path(new))), found[0]))
        return True

    def built(self, root: str | Path) -> float:
        """When the library folder's first look finished (0 = it hasn't yet)."""
        found = _find_root(self._con(), str(Path(root)))
        return found[1] if found else 0.0

    def mark_built(self, root: str | Path, when: float | None = None) -> None:
        """The first look at this library folder has finished (kept from then on: before it, files
        arriving are dated by when they were made, and nothing counts as new music)."""
        with self._writing() as con:
            root_id, built = _root(con, str(Path(root)))
            if not built:
                con.execute("UPDATE roots SET built = ? WHERE id = ?", (time.time() if when is None else when, root_id))

    # -- what the indexer reads and writes --
    def folders(self, root: str | Path) -> dict[str, FolderState]:
        """Every folder stored under a library folder: {full path: FolderState}."""
        con = self._con()
        found = _find_root(con, str(Path(root)))
        if found is None:
            return {}
        root_text = _from_db(con.execute("SELECT path FROM roots WHERE id = ?", (found[0],)).fetchone()[0])
        return {_join(root_text, rel): FolderState(folder_id, cue_sig, bool(listed_ok))
                for folder_id, rel, cue_sig, listed_ok in con.execute(
                    "SELECT id, rel, cue_sig, listed_ok FROM folders WHERE root_id = ?", (found[0],))}

    def file_states(self, folder_ids: Iterable[int]) -> dict[int, dict[str, FileState]]:
        """The files stored for these folders: {folder id: {file name: FileState}}."""
        ids = list(folder_ids)
        states: dict[int, dict[str, FileState]] = {folder_id: {} for folder_id in ids}
        with self._reading() as con:
            for chunk in _chunks(ids):
                for folder_id, name, size, mtime, missing, read_format in con.execute(
                        "SELECT folder_id, name, size, mtime_ns, missing_since, read_format FROM files"
                        f" WHERE folder_id IN ({', '.join('?' * len(chunk))})", chunk):
                    states[folder_id][_from_db(name)] = FileState(size, mtime, missing is not None, read_format)
        return states

    def cue_track_folders(self, folder_ids: Iterable[int]) -> set[int]:
        """The folders among these that hold CUE tracks (they're always read whole: how a folder's
        files split into tracks depends on all of them)."""
        found: set[int] = set()
        with self._reading() as con:
            for chunk in _chunks(list(folder_ids)):
                found.update(row[0] for row in con.execute(
                    "SELECT DISTINCT f.folder_id FROM files f JOIN tracks t ON t.file_id = f.id"
                    f" WHERE f.folder_id IN ({', '.join('?' * len(chunk))}) AND t.cue_number IS NOT NULL", chunk))
        return found

    def apply(self, result: FolderResult, now: float | None = None) -> set[int]:
        """Store what a look at one folder found, in one transaction: files read are stored with
        their tracks (a new one dated now, or on a library folder's first look by when it was made;
        a changed one keeps its dates), files gone from the listing are marked missing, files back
        in it are shown again. A folder that couldn't be listed keeps everything. Returns the ids of
        the albums that changed (their rows are made again)."""
        now = time.time() if now is None else now
        root = str(Path(result.root))
        retry = set(result.retry)
        with self._writing() as con:
            root_id, built = _root(con, root)
            folder_id, album_id = _folder(con, root_id, _rel(root, os.fspath(result.folder)))
            if not result.listed_ok:
                con.execute("UPDATE folders SET listed_ok = 0 WHERE id = ?", (folder_id,))
                return set()
            old = {_from_db(name): (file_id, missing) for file_id, name, missing in con.execute(
                "SELECT id, name, missing_since FROM files WHERE folder_id = ?", (folder_id,))}
            changed = False
            for name, rows in result.tracks.items():
                stat = result.listing.get(name)
                if stat is None or not rows:
                    continue
                size, mtime, created = stat
                if name in retry:
                    mtime = -1   # only partly read just now: read it again next time
                known = old.get(name)
                if known is not None:
                    file_id = known[0]
                    con.execute("DELETE FROM tracks WHERE file_id = ?", (file_id,))
                    con.execute("UPDATE files SET size = ?, mtime_ns = ?, read_format = ?, missing_since = NULL"
                                " WHERE id = ?", (size, mtime, result.read_format, file_id))
                else:
                    added = now if built else min(created, now)
                    file_id = con.execute(
                        "INSERT INTO files(folder_id, name, size, mtime_ns, added, seen, read_format)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (folder_id, _to_db(name), size, mtime, added, now, result.read_format)).lastrowid
                self._insert_tracks(con, file_id, album_id, rows)
                changed = True
            for name, (file_id, missing) in old.items():
                if result.tracks.get(name) and name in result.listing:
                    continue   # (stored above)
                if name not in result.listing:
                    if missing is None:
                        self._hide(con, [file_id], now)
                        changed = True
                    continue
                if name in retry:   # couldn't be read just now: what's known stays, read again next time
                    con.execute("UPDATE files SET mtime_ns = -1 WHERE id = ?", (file_id,))
                if missing is not None:   # back again: shown with the dates it had
                    self._restore(con, [file_id])
                    changed = True
            con.execute("UPDATE folders SET cue_sig = ?, listed_ok = 1 WHERE id = ?", (result.cue_sig, folder_id))
            if changed:
                self._rebuild_albums(con, {album_id})
        return {album_id} if changed else set()

    def mark_missing(self, folder_ids: Iterable[int], now: float | None = None) -> set[int]:
        """Every file in these folders is gone (the folders weren't there on a look): marked
        missing, kept until purge_missing. Returns the ids of the albums that changed."""
        now = time.time() if now is None else now
        ids = list(folder_ids)
        albums: set[int] = set()
        with self._writing() as con:
            for chunk in _chunks(ids):
                marks = ", ".join("?" * len(chunk))
                files = [row[0] for row in con.execute(
                    f"SELECT id FROM files WHERE folder_id IN ({marks}) AND missing_since IS NULL", chunk)]
                if files:
                    self._hide(con, files, now)
                    albums.update(row[0] for row in con.execute(
                        f"SELECT DISTINCT album_folder_id FROM folders WHERE id IN ({marks})", chunk))
            self._rebuild_albums(con, albums)
        return albums

    def purge_missing(self, root: str | Path | None = None, before: float | None = None) -> int:
        """Remove for good the files marked missing at or before `before` (default: MISSING_DAYS
        ago) — under `root`, or every library folder — and folders left with nothing in them.
        After a look at the whole library folder that went cleanly, pass `before=now`. Returns
        how many files were removed."""
        before = time.time() - MISSING_DAYS * 86400 if before is None else before
        with self._writing(bump=False) as con:
            if root is None:
                ids = [row[0] for row in con.execute(
                    "SELECT id FROM files WHERE missing_since IS NOT NULL AND missing_since <= ?", (before,))]
            else:
                found = _find_root(con, str(Path(root)))
                ids = [] if found is None else [row[0] for row in con.execute(
                    "SELECT f.id FROM files f JOIN folders d ON d.id = f.folder_id WHERE f.missing_since IS NOT NULL"
                    " AND f.missing_since <= ? AND d.root_id = ?", (before, found[0]))]
            if not ids:
                return 0   # (nothing changed: the generation stays as it is)
            for chunk in _chunks(ids):
                con.execute(f"DELETE FROM files WHERE id IN ({', '.join('?' * len(chunk))})", chunk)
            while con.execute(   # empty folders (a disc folder first, then the album folder it was in)
                    "DELETE FROM folders WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.folder_id = folders.id)"
                    " AND NOT EXISTS (SELECT 1 FROM folders c WHERE c.album_folder_id = folders.id"
                    " AND c.id != folders.id)").rowcount:
                pass
            con.execute("UPDATE meta SET value = value + 1 WHERE key = 'generation'")
        return len(ids)

    def _insert_tracks(self, con: sqlite3.Connection, file_id: int, album_id: int, rows: list[tuple]) -> None:
        for values, fts in rows:
            track_id = _run(con, _INSERT_TRACK, (file_id, album_id, *values)).lastrowid
            if self.fts:
                _run(con, _INSERT_FTS, (track_id, *fts))

    def _hide(self, con: sqlite3.Connection, file_ids: list[int], now: float) -> None:
        """Mark files missing: out of the albums and the search, dates kept."""
        for chunk in _chunks(file_ids):
            marks = ", ".join("?" * len(chunk))
            con.execute(f"UPDATE files SET missing_since = ? WHERE id IN ({marks}) AND missing_since IS NULL",
                        (now, *chunk))
            if self.fts:
                con.execute(f"DELETE FROM song_fts WHERE rowid IN (SELECT id FROM tracks WHERE file_id IN ({marks}))",
                            chunk)

    def _restore(self, con: sqlite3.Connection, file_ids: list[int]) -> None:
        """Missing files that are back: in the albums and the search again."""
        for chunk in _chunks(file_ids):
            marks = ", ".join("?" * len(chunk))
            con.execute(f"UPDATE files SET missing_since = NULL WHERE id IN ({marks})", chunk)
            if not self.fts:
                continue
            for track_id, title, artist, album, album_artist, composer, genre, year, name in con.execute(
                    "SELECT t.id, t.title, t.artist, t.album, t.album_artist, t.composer, t.genre, t.year, f.name"
                    f" FROM tracks t JOIN files f ON f.id = t.file_id WHERE t.file_id IN ({marks})", chunk).fetchall():
                fts, _haystack = _search_texts(title, artist, album, album_artist, composer, genre,
                                               str(year) if year else "", os.path.splitext(_from_db(name))[0])
                con.execute("DELETE FROM song_fts WHERE rowid = ?", (track_id,))
                _run(con, _INSERT_FTS, (track_id, *fts))

    def _rebuild_albums(self, con: sqlite3.Connection, album_ids: Iterable[int]) -> None:
        """Make these albums' rows again from their tracks (browse.make_album: the Library's own rules)."""
        names: set[str] = set()
        for album_id in album_ids:
            old = con.execute("SELECT name FROM albums WHERE id = ?", (album_id,)).fetchone()
            if old:
                names.add(old[0])
            rows = con.execute(_TRACK_SELECT + " WHERE t.album_id = ? AND f.missing_since IS NULL",
                               (album_id,)).fetchall()
            if not rows:
                con.execute("DELETE FROM albums WHERE id = ?", (album_id,))
                continue
            root, rel = con.execute("SELECT r.path, d.rel FROM folders d JOIN roots r ON r.id = d.root_id"
                                    " WHERE d.id = ?", (album_id,)).fetchone()
            added = con.execute("SELECT max(f.added) FROM files f WHERE f.id IN (SELECT file_id FROM tracks"
                                " WHERE album_id = ?) AND f.missing_since IS NULL", (album_id,)).fetchone()[0]
            folder = Path(_join(root, rel))
            album = make_album(folder, play_order(folder, [_track(row) for row in rows]), added)
            name = _text(album.name)
            _run(con, "INSERT OR REPLACE INTO albums(id, name, display_name, title, artist, year, codecs,"
                      " sample_rate, bits, track_count, added, genres, composers, sort_name, sort_artist)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (album_id, name, name, album.title, album.artist, album.year,
                  _SEP.join(album.codecs), album.sample_rate, album.bits, album.track_count, album.added,
                  _SEP.join(album.genres), _SEP.join(album.composers), _sort_text(name),
                  _sort_text(album.artist)))
            names.add(name)
        _rename(con, names)

    # -- what the Library reads --
    def albums(self, ids: Iterable[int] | None = None) -> list[Album]:
        """Every album (or the ones with these ids), with the details the browse views need, sorted
        by name as browse.albums_from sorts them."""
        sql = ("SELECT r.path, d.rel, a.display_name, a.track_count, a.sample_rate, a.bits, a.codecs, a.artist,"
               " a.year, a.genres, a.composers, a.added, a.title FROM albums a JOIN folders d ON d.id = a.id"
               " JOIN roots r ON r.id = d.root_id")
        with self._reading() as con:
            if ids is None:
                rows = con.execute(sql).fetchall()
            else:
                rows = []
                for chunk in _chunks(list(ids)):
                    rows += con.execute(sql + f" WHERE a.id IN ({', '.join('?' * len(chunk))})", chunk).fetchall()
        albums = [Album(path=Path(_join(root, rel)), name=name, track_count=count, sample_rate=rate, bits=bits,
                        codecs=_split(codecs), artist=artist, year=year, genres=_split(genres),
                        composers=_split(composers), added=added, title=title)
                  for root, rel, name, count, rate, bits, codecs, artist, year, genres, composers, added, title in rows]
        return sorted(albums, key=lambda a: natural_key(a.name))

    def album_id(self, folder: str | Path) -> int | None:
        """The id of the album in this folder (None: not an album in the library)."""
        con = self._con()
        where = _locate(con, os.fspath(folder))
        if where is None:
            return None
        row = con.execute("SELECT a.id FROM albums a JOIN folders d ON d.id = a.id WHERE d.root_id = ? AND d.rel = ?",
                          (where[0], _to_db(where[2]))).fetchone()
        return row[0] if row else None

    def album_of(self, folder: str | Path) -> Album | None:
        """One album's browse details (e.g. for a tab); None if it isn't an album in the library."""
        album_id = self.album_id(folder)
        found = self.albums([album_id]) if album_id is not None else []
        return found[0] if found else None

    def album_folders(self) -> set[str]:
        """Every album folder in the library (for tidying the new-music "opened" list)."""
        return {_join(root, rel) for root, rel in self._con().execute(
            "SELECT r.path, d.rel FROM albums a JOIN folders d ON d.id = a.id JOIN roots r ON r.id = d.root_id")}

    def album_tracks(self, folder: str | Path, whole_tree: bool = False, check: bool = True) -> list[Track] | None:
        """The tracks library.scan_album (or scan_folder, `whole_tree`) would give for `folder`, in
        play order, from the database. With `check`, every file there is looked at (names, sizes,
        times and .cue files): None when anything has changed since it was stored, or the folder
        isn't in the library — the caller then reads the files itself."""
        folder = Path(folder)
        text = os.fspath(folder)
        with self._reading() as con:
            where = _locate(con, text)
            if where is None:
                return None
            root_id, root, rel = where
            inside = _folders_under(con, root_id, rel, whole_tree)
            ids = [folder_id for folder_id, _rel_, _sig in inside]
            rows, states = [], {}
            for chunk in _chunks(ids):
                marks = ", ".join("?" * len(chunk))
                rows += con.execute(_TRACK_SELECT + f" WHERE f.folder_id IN ({marks}) AND f.missing_since IS NULL",
                                    chunk).fetchall()
                for sub, name, size, mtime in con.execute(
                        "SELECT d.rel, f.name, f.size, f.mtime_ns FROM files f JOIN folders d ON d.id = f.folder_id"
                        f" WHERE f.folder_id IN ({marks}) AND f.missing_since IS NULL", chunk):
                    states[_join(root, sub, name)] = (size, mtime)
        by_file: dict[str, list[Track]] = {}
        for row in rows:
            by_file.setdefault(_join(row[0], row[1], row[2]), []).append(_track(row))
        if not check:
            return play_order(folder, [t for found in by_file.values() for t in found])
        paths = _music_on_disk(folder, whole_tree)
        if paths is None:
            return None
        tracks: list[Track] = []
        for path in paths:
            try:
                st = os.stat(path)
            except OSError:
                return None
            if states.get(path) != (st.st_size, st.st_mtime_ns) or path not in by_file:
                return None
            tracks += by_file[path]
        sigs = {_join(root, sub): sig for _id, sub, sig in inside}
        for here in {os.path.dirname(p) for p in paths}:
            try:
                if cue_signature(here) != sigs.get(here):
                    return None
            except OSError:
                return None
        return play_order(folder, tracks)

    def tracks(self, ids: Iterable[int]) -> list[Track]:
        """These tracks (by tracks.id, e.g. from a search), in the order asked; unknown ids left out."""
        ids = list(ids)
        found: dict[int, Track] = {}
        with self._reading() as con:
            for chunk in _chunks(ids):
                for row in con.execute(_TRACK_SELECT + f" WHERE t.id IN ({', '.join('?' * len(chunk))})", chunk):
                    found[row[3]] = _track(row)
        return [found[i] for i in ids if i in found]

    def tracks_by_id(self, ids: Iterable[int]) -> dict[int, Track]:
        """These tracks (by tracks.id), keyed by id; unknown ids left out."""
        ids = list(ids)
        found: dict[int, Track] = {}
        with self._reading() as con:
            for chunk in _chunks(ids):
                for row in con.execute(_TRACK_SELECT + f" WHERE t.id IN ({', '.join('?' * len(chunk))})", chunk):
                    found[row[3]] = _track(row)
        return found

    def new_albums(self, since: float, opened: dict[str, float]) -> list[Path]:

        """The album folders with music first seen after `since` (and after their library folder's
        first look) that haven't been opened since it arrived; newest first. `opened`: {album
        folder: when opened}. (watcher.new_albums's rule, from the database.)"""
        return [folder for folder, seen in self.new_album_times(since) if seen > opened.get(str(folder), 0.0)]

    def new_album_times(self, since: float) -> list[tuple[Path, float]]:
        """(album folder, when its newest music was first seen) for the albums with music first
        seen after `since` and after their library folder's first look; newest first. Whether
        each was opened since is left to the caller (new_albums). A library folder whose first look
        hasn't finished has no new music: everything in it is arriving for the first time."""
        newest: dict[int, float] = {}
        with self._reading() as con:
            for root_id, built in con.execute("SELECT id, built FROM roots WHERE built > 0").fetchall():
                for album_id, seen in con.execute(
                        "SELECT d.album_folder_id, max(f.seen) FROM files f JOIN folders d ON d.id = f.folder_id"
                        " WHERE f.seen > ? AND f.missing_since IS NULL AND d.root_id = ? GROUP BY d.album_folder_id",
                        (max(since, built), root_id)):
                    newest[album_id] = seen
            paths = _folder_paths(con, list(newest))
        found = sorted(((seen, paths[album_id]) for album_id, seen in newest.items()),
                       key=lambda pair: (-pair[0], pair[1].lower()))
        return [(Path(folder), seen) for seen, folder in found]

    def albums_tracks(self, folders: Iterable[str | Path]) -> dict[Path, list[Track]]:
        """Each album's tracks in play order, from the database alone (nothing on disk is looked
        at, so a whole library loads quickly): {album folder: tracks}. A folder that isn't an album
        in the library is left out."""
        wanted = {os.fspath(folder) for folder in folders}
        by_album: dict[int, list[Track]] = {}
        with self._reading() as con:
            ids = {album_id: folder for album_id, folder in _folder_paths(con, [row[0] for row in con.execute(
                "SELECT id FROM albums")]).items() if folder in wanted}
            for chunk in _chunks(list(ids)):
                for row in con.execute(_ALBUM_TRACKS + f" WHERE t.album_id IN ({', '.join('?' * len(chunk))})"
                                       " AND f.missing_since IS NULL", chunk):
                    by_album.setdefault(row[0], []).append(_track(row[1:]))
        return {Path(ids[album_id]): play_order(Path(ids[album_id]), tracks) for album_id, tracks in by_album.items()}

    def album_orders(self, folders: Iterable[str | Path]) -> dict[str, AlbumOrder]:
        """Each album's song ids in play order (as albums_tracks lists them, by play_order's rule)
        and a little about them, from a light read of the database: no Track is made, so a tab of a
        whole library can be listed without holding every song. {album folder (as os.fspath gives
        it): AlbumOrder}; a folder that isn't an album in the library is left out."""
        wanted = {os.fspath(folder) for folder in folders}
        rows_of: dict[int, list[tuple]] = {}
        with self._reading() as con:
            ids = {album_id: folder for album_id, folder in _folder_paths(con, [row[0] for row in con.execute(
                "SELECT id FROM albums")]).items() if folder in wanted}
            for chunk in _chunks(list(ids)):
                for row in con.execute(
                        "SELECT t.album_id, t.id, r.path, d.rel, f.name, t.album, t.disc_no, t.track_no, t.duration,"
                        " t.artist, t.codec, t.bits, t.sample_rate FROM tracks t JOIN files f ON f.id = t.file_id"
                        " JOIN folders d ON d.id = f.folder_id JOIN roots r ON r.id = d.root_id"
                        f" WHERE t.album_id IN ({', '.join('?' * len(chunk))}) AND f.missing_since IS NULL"
                        " ORDER BY t.id", chunk):
                    rows_of.setdefault(row[0], []).append(row[1:])
        found: dict[str, AlbumOrder] = {}
        for album_id, rows in rows_of.items():
            folder = ids[album_id]
            found[folder] = AlbumOrder(_play_ordered(folder, rows), sum(row[7] for row in rows),
                                       frozenset(row[8] for row in rows),
                                       frozenset(row[4] or os.path.basename(folder.rstrip(os.sep)) for row in rows),
                                       frozenset((row[9], row[10], row[11]) for row in rows))
        return found

    def ids_matching(self, words: list[str]) -> set[int]:
        """The ids of the songs whose folded search text holds every one of these words (folded,
        searchfold.words) anywhere in it — the in-tab filter's rule, run by the database."""
        if not words:
            return set()
        where = " AND ".join("instr(haystack, ?) > 0" for _word in words)
        return {row[0] for row in self._con().execute(f"SELECT id FROM tracks WHERE {where}", list(words))}

    def file_times(self) -> dict[str, tuple[float, float]]:

        """{music file: (when it arrived, when the database first saw it)} for every file in the
        library (not those missing)."""
        return {_join(root, rel, name): (added, seen) for root, rel, name, added, seen in self._con().execute(
            "SELECT r.path, d.rel, f.name, f.added, f.seen FROM files f JOIN folders d ON d.id = f.folder_id"
            " JOIN roots r ON r.id = d.root_id WHERE f.missing_since IS NULL")}

    def files(self) -> dict[str, tuple[int, int, float, float, list[Track]]]:
        """{music file: (size, modified ns, added, seen, its tracks)} for every file in the library
        (not those missing). The whole library in memory: for checks, not for the player."""
        found: dict[str, tuple[int, int, float, float, list[Track]]] = {}
        with self._reading() as con:
            for root, rel, name, size, mtime, added, seen in con.execute(
                    "SELECT r.path, d.rel, f.name, f.size, f.mtime_ns, f.added, f.seen FROM files f"
                    " JOIN folders d ON d.id = f.folder_id JOIN roots r ON r.id = d.root_id"
                    " WHERE f.missing_since IS NULL"):
                found[_join(root, rel, name)] = (size, mtime, added, seen, [])
            for row in con.execute(_TRACK_SELECT + " WHERE f.missing_since IS NULL ORDER BY t.id"):
                found[_join(row[0], row[1], row[2])][4].append(_track(row))
        return found

    # -- song search --
    def search_songs(self, query: str, limit: int = SEARCH_LIMIT) -> SongHits:
        """Songs anywhere in the library matching what was typed (see the module notes): the best
        `limit` of them, best first, and how many there are. Nothing for fewer than MIN_SEARCH
        letters. Interrupted when `interrupt` stopped it."""
        found = searchfold.tokens(query)
        if not found or max(len(word) for word in found) < MIN_SEARCH:
            return SongHits([], 0)
        con = self._con()
        try:
            if self.fts:
                match = searchfold.fts_match(query)
                rows = con.execute(_TOP_HITS, (match, limit)).fetchall()
                if rows:
                    total = con.execute("SELECT count(*) FROM song_fts WHERE song_fts MATCH ?", (match,)).fetchone()[0]
                    return SongHits(_ranked(rows, found, whole_words=True), total)
                words = searchfold.words(query)
                if len("".join(words)) < 3:
                    return SongHits([], 0)
                return self._anywhere(con, words, limit, in_sql=True)
            return self._anywhere(con, searchfold.words(query), limit, in_sql=False)
        except sqlite3.OperationalError as error:
            if "interrupt" in str(error).lower():
                raise Interrupted(query) from error
            raise

    def _anywhere(self, con: sqlite3.Connection, words: list[str], limit: int, in_sql: bool) -> SongHits:
        """The slower search: each word anywhere in a song's folded text ("ight" finds "Night"), by
        SQLite's instr(), or in Python where there's no search index at all."""
        if in_sql:
            where = " AND ".join("instr(t.haystack, ?) > 0" for _w in words)
            rows = con.execute(_TRACK_SELECT + f" WHERE f.missing_since IS NULL AND {where} ORDER BY t.id LIMIT ?",
                               (*words, limit)).fetchall()
            count = con.execute("SELECT count(*) FROM (SELECT 1 FROM tracks t JOIN files f ON f.id = t.file_id"
                                f" WHERE f.missing_since IS NULL AND {where} LIMIT ?)",
                                (*words, COUNT_CAP + 1)).fetchone()[0]
        else:
            ids, count = [], 0
            for track_id, haystack in con.execute("SELECT t.id, t.haystack FROM tracks t JOIN files f"
                                                  " ON f.id = t.file_id WHERE f.missing_since IS NULL ORDER BY t.id"):
                if all(word in haystack for word in words):
                    count += 1
                    if len(ids) < limit:
                        ids.append(track_id)
                    if count > COUNT_CAP:
                        break
            rows = []
            for chunk in _chunks(ids):
                rows += con.execute(_TRACK_SELECT + f" WHERE t.id IN ({', '.join('?' * len(chunk))})", chunk).fetchall()
        return SongHits(_ranked(rows, words, whole_words=False), min(count, COUNT_CAP), count > COUNT_CAP)

    # -- songs for the Library's song views --
    def song_ids(self, order: str = "title", reverse: bool = False) -> list[int]:
        """Every song's id (not those missing), sorted by a column of the Library's Songs view —
        title, artist, album, year, quality or time (`reverse`: that column the other way round;
        unknown years stay last) — then by artist, album, disc and track. The column's names sort as
        the albums do (folded, "The" left off, 2 before 10); the ones after it plainly, ignoring case
        (a Python sort key for every name of every song took four times as long)."""
        terms = _SONG_ORDER.get(order, _SONG_ORDER["title"])
        first = ", ".join(f"{expr}{' DESC' if desc != (reverse and flips) else ''}" for expr, flips, desc in terms)
        con = self._con()
        con.create_function("ib_sort", 1, _sort_text, deterministic=True)
        return [row[0] for row in con.execute(
            "SELECT t.id FROM tracks t JOIN files f ON f.id = t.file_id WHERE f.missing_since IS NULL"
            f" ORDER BY {first}, t.artist COLLATE NOCASE, t.album COLLATE NOCASE, t.disc_no, t.track_no,"
            " t.title COLLATE NOCASE, t.id")]

    def songs(self, ids: Iterable[int]) -> list[SongHit]:
        """These songs (by tracks.id) with their album folders, in the order asked; unknown ids
        left out."""
        ids = list(ids)
        found: dict[int, SongHit] = {}
        with self._reading() as con:
            for chunk in _chunks(ids):
                for row in con.execute(_TRACK_SELECT + f" WHERE t.id IN ({', '.join('?' * len(chunk))})", chunk):
                    found[row[3]] = _hit(row)
        return [found[i] for i in ids if i in found]

    def songs_by_keys(self, keys: Iterable) -> list[SongHit]:
        """The songs with these play / favourite keys (audiofile.key: a CUE track is "file#3"), in
        the order asked, each song once; one that isn't in the library (or is missing) is left out."""
        seen: set[int] = set()
        return [hit for _asked, hit in self.songs_for_keys(keys) if not (hit.id in seen or seen.add(hit.id))]

    def songs_for_keys(self, keys: Iterable) -> list[tuple[str, SongHit]]:
        """(key, song) for each of these keys that finds a song in the library, in the order asked.
        Two keys can find one song — its file written another way ("c:\\music" and "C:\\Music":
        Windows doesn't mind the case) — so a song can come more than once, and the key that found
        it needn't be the song's own (audiofile.key of its path)."""
        keys = list(dict.fromkeys(_key(k) for k in keys))
        by_folder: dict[str, list[tuple[str, str, int | None]]] = {}   # folder -> (key, file name, CUE track)
        for key in keys:
            path, mark, number = key.rpartition("#")
            cue = int(number) if mark and number.isdecimal() else None
            folder, name = os.path.split(path if cue is not None else key)
            by_folder.setdefault(folder, []).append((key, name, cue))
        ids: dict[str, int] = {}
        with self._reading() as con:
            for folder, asked in by_folder.items():   # (a folder's songs in one read: thousands of keys stay quick)
                place = _locate(con, folder)
                if place is None:
                    continue
                found = {(_from_db(name), cue): track_id for name, cue, track_id in con.execute(
                    "SELECT f.name, t.cue_number, t.id FROM tracks t JOIN files f ON f.id = t.file_id"
                    " JOIN folders d ON d.id = f.folder_id"
                    " WHERE d.root_id = ? AND d.rel = ? AND f.missing_since IS NULL", (place[0], _to_db(place[2])))}
                for key, name, cue in asked:
                    if (name, cue) in found:
                        ids[key] = found[(name, cue)]
        hits = {hit.id: hit for hit in self.songs(dict.fromkeys(ids.values()))}
        return [(key, hits[ids[key]]) for key in keys if key in ids and ids[key] in hits]

    # -- plays and favourites --
    def record_play(self, key, seconds_heard: float, duration: float, when: float | None = None) -> bool:
        """A song was listened to for `seconds_heard` of its `duration`: stored as a play when it
        counts as one (counts_as_play). `key`: audiofile.key of its path (or the path). Returns
        whether it counted."""
        if not counts_as_play(seconds_heard, duration):
            return False
        completed = duration > 0 and seconds_heard >= duration * 0.95
        with self._writing(bump=False) as con:
            con.execute("INSERT INTO plays(key, played_at, seconds_heard, completed) VALUES (?, ?, ?, ?)",
                        (_to_db(_key(key)), time.time() if when is None else when, float(seconds_heard),
                         int(completed)))
        return True

    def play_counts(self, keys: Iterable) -> dict[str, int]:
        """{key: times played} for these songs (0 for one never played)."""
        counts = dict.fromkeys((_key(k) for k in keys), 0)
        with self._reading() as con:
            counts.update(_keys_in(con, "SELECT key, count(*) FROM plays WHERE key IN ({marks}) GROUP BY key",
                                   list(counts)))
        return counts

    def last_played(self, keys: Iterable) -> dict[str, float]:
        """{key: when last played} for the songs among these that have been played."""
        keys = list(dict.fromkeys(_key(k) for k in keys))
        with self._reading() as con:
            return dict(_keys_in(con, "SELECT key, max(played_at) FROM plays WHERE key IN ({marks}) GROUP BY key",
                                 keys))

    def most_played(self, n: int = 50) -> list[tuple[str, int]]:
        """The `n` songs played most: (key, times), most first (the latest played first on a tie)."""
        return [(_from_db(key), count) for key, count, _last in self._con().execute(
            "SELECT key, count(*) AS times, max(played_at) AS last FROM plays GROUP BY key"
            " ORDER BY times DESC, last DESC LIMIT ?", (n,))]

    def recently_played(self, n: int = 50) -> list[tuple[str, float]]:
        """The `n` songs played last: (key, when), latest first."""
        return [(_from_db(key), last) for key, last in self._con().execute(
            "SELECT key, max(played_at) AS last FROM plays GROUP BY key ORDER BY last DESC LIMIT ?", (n,))]

    def is_favourite(self, key) -> bool:
        return self._con().execute("SELECT 1 FROM favourites WHERE key = ?",
                                   (_to_db(_key(key)),)).fetchone() is not None

    def set_favourite(self, key, on: bool = True, when: float | None = None) -> None:
        with self._writing(bump=False) as con:
            if on:
                con.execute("INSERT OR IGNORE INTO favourites(key, added) VALUES (?, ?)",
                            (_to_db(_key(key)), time.time() if when is None else when))
            else:
                con.execute("DELETE FROM favourites WHERE key = ?", (_to_db(_key(key)),))

    def favourites(self) -> list[str]:
        """Every favourite's key, newest first."""
        return [_from_db(row[0]) for row in self._con().execute("SELECT key FROM favourites ORDER BY added DESC, key")]

    # -- the whole-library quality check (library_check.py) --
    def check_songs(self) -> list[tuple[Track, int, int, bool]]:
        """Every song, with its file's size and modified time as last seen and whether the file is
        missing just now: what the quality check works through, folder by folder."""
        with self._reading() as con:
            return [(_track(row[:-3]), row[-3], row[-2], row[-1] is not None) for row in con.execute(
                _CHECK_SELECT + " ORDER BY r.id, d.rel, f.name, t.cue_number")]

    def check_list(self) -> Iterator[tuple]:
        """What the quality check needs of every song, one at a time, folder by folder (a big
        library's whole Tracks would cost hundreds of megabytes, and seconds, at every run's start):
        (path, bits, MD5, duration, sample rate, misnamed, unsupported, size, modified ns, missing)
        — the path as text (a CuePath for a CUE track: its stretch of the file), the file's size and
        time as last seen, and whether it's missing just now. Read to the end (it holds a read open
        meanwhile)."""
        with self._reading() as con:
            for (root, rel, name, cue_sheet, cue_number, cue_start, cue_end, bits, md5, duration, rate, misnamed,
                 unsupported, size, mtime, missing) in con.execute(_CHECK_LIST):
                path = (_join(root, rel, name) if cue_number is None
                        else _track_path(root, rel, name, cue_sheet, cue_number, cue_start, cue_end))
                yield (path, bits, int(md5, 16) if md5 else 0, duration, rate, misnamed, unsupported, size, mtime,
                       missing is not None)

    def check_states(self) -> dict[str, tuple[int, int, int]]:
        """{song key: (size, modified ns, format)} for every stored check."""
        return {_from_db(key): (size, mtime, fmt) for key, size, mtime, fmt in self._con().execute(
            "SELECT key, size, mtime_ns, format FROM checks")}

    def save_check(self, key, size: int, mtime_ns: int, format: int, findings: str, true_peak: float | None,
                   result: str, when: float | None = None) -> None:
        """Keep a song's check (in place of any before). `key`: audiofile.key of its path (or the path)."""
        with self._writing(bump=False) as con:
            con.execute(f"INSERT OR REPLACE INTO checks({_CHECK_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (_to_db(_key(key)), int(size), int(mtime_ns), int(format),
                         time.time() if when is None else when, findings,
                         None if true_peak is None else float(true_peak), result))

    def check_of(self, key) -> tuple[int, int, int, float, str, float | None, str] | None:
        """A song's stored check: (size, modified ns, format, when, findings, true peak, result JSON)."""
        row = self._con().execute("SELECT size, mtime_ns, format, checked, findings, true_peak, result FROM checks"
                                  " WHERE key = ?", (_to_db(_key(key)),)).fetchone()
        return tuple(row) if row is not None else None

    def flagged_checks(self) -> list[tuple[str, str, float, str]]:
        """(key, findings, when, result JSON) of every stored check that found something."""
        return [(_from_db(key), findings, checked, result) for key, findings, checked, result in self._con().execute(
            "SELECT key, findings, checked, result FROM checks WHERE findings != ''")]

    def forget_checks(self, keys: Iterable) -> int:
        """Drop these songs' checks (songs gone from the library); how many there were."""
        keys = [_to_db(_key(k)) for k in keys]
        gone = 0
        with self._writing(bump=False) as con:
            for chunk in _chunks(keys):
                gone += con.execute(f"DELETE FROM checks WHERE key IN ({', '.join('?' * len(chunk))})",
                                    chunk).rowcount
        return gone

    # -- moving over from library_index.json --
    def import_json_index(self, path: str | Path | None = None,
                          progress: Callable[[int, int], None] | None = None) -> bool:
        """Bring a saved library_index.json (library_index.py) across: every file with exactly its
        size, modified time, `added` and `seen`, and the library folder's first-look time, so
        Recently added and new music carry on as they were. Only an index of the current format,
        for one of the library folders, into a library folder with nothing stored yet. Checked by
        counting the rows; then the JSON is renamed library_index.json.migrated (a backup).
        Returns whether it was brought across — if not (none, damaged, another folder, an older
        format), the library is simply looked through from scratch. `progress(done, total)`."""
        from . import library_index   # (it uses this module: imported here, not at the top)

        path = Path(path) if path is not None else library_index.index_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("format") != READ_FORMAT:
                return False
            built = float(data.get("built", 0.0))
            entries = []
            for name, item in data["files"].items():
                entries.append((name, int(item["size"]), int(item["mtime"]), float(item["added"]), float(item["seen"]),
                                [library_index.track_from_dict(Path(name), t) for t in item["tracks"]]))
            cues = {str(folder): list(sheet) for folder, sheet in data.get("cues", {}).items()}
            json_root = Path(data["root"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False
        total, expected = len(entries), sum(len(tracks) for *_rest, tracks in entries)
        try:
            with self._writing() as con:
                found = _find_root(con, str(json_root))
                if found is None:
                    return False
                root_id = found[0]
                root = _from_db(con.execute("SELECT path FROM roots WHERE id = ?", (root_id,)).fetchone()[0])
                if con.execute("SELECT 1 FROM files f JOIN folders d ON d.id = f.folder_id WHERE d.root_id = ? LIMIT 1",
                               (root_id,)).fetchone():
                    return False   # already has its own rows: those are newer
                folders: dict[str, tuple[int, int]] = {}
                albums: set[int] = set()
                for done, (name, size, mtime, added, seen, tracks) in enumerate(entries, 1):
                    here, base = os.path.split(name)
                    if here not in folders:
                        folders[here] = _folder(con, root_id, _rel(root, here))
                    folder_id, album_id = folders[here]
                    file_id = con.execute(
                        "INSERT INTO files(folder_id, name, size, mtime_ns, added, seen, read_format)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (folder_id, _to_db(base), size, mtime, added, seen, READ_FORMAT)).lastrowid
                    self._insert_tracks(con, file_id, album_id, [track_row(t) for t in tracks])
                    albums.add(album_id)
                    if progress is not None and (done % 1000 == 0 or done == total):
                        progress(done, total)
                for here, sheet in cues.items():
                    try:
                        folder_id = (folders.get(here) or _folder(con, root_id, _rel(root, here)))[0]
                    except ValueError:
                        continue
                    con.execute("UPDATE folders SET cue_sig = ? WHERE id = ?",
                                (cue_signature_of(tuple(s) for s in sheet), folder_id))
                self._rebuild_albums(con, albums)
                con.execute("UPDATE roots SET built = ? WHERE id = ?", (built, root_id))
                stored = con.execute("SELECT count(*), (SELECT count(*) FROM tracks t JOIN files f ON f.id = t.file_id"
                                     " JOIN folders d ON d.id = f.folder_id WHERE d.root_id = ?) FROM files f"
                                     " JOIN folders d ON d.id = f.folder_id WHERE d.root_id = ?",
                                     (root_id, root_id)).fetchone()
                if tuple(stored) != (total, expected):
                    raise _NotMoved(f"{stored} rows stored for {total} files, {expected} tracks")
        except (_NotMoved, ValueError, TypeError, sqlite3.IntegrityError):
            return False   # (nothing was kept: the transaction was undone)
        try:
            os.replace(path, path.with_name(path.name + ".migrated"))
        except OSError:
            pass   # (left where it is: the library folder has rows now, so it isn't brought across twice)
        return True


class _NotMoved(Exception):
    """The JSON index couldn't be brought across exactly."""


# -- helpers on a connection -----------------------------------------------------------------------------
def _statements(script: str):
    """The SQL statements in a script, one at a time (executescript would end the transaction)."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending.strip()
            pending = ""


def _create(con: sqlite3.Connection, fts_ok: bool) -> None:
    for statement in _statements(_TABLES):
        con.execute(statement)
    had_index = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'song_fts'").fetchone() is not None
    if fts_ok:
        con.execute(_FTS_TABLE)
        con.execute(_FTS_TRIGGER)
    for key, value in (("generation", 0), ("fold_version", searchfold.FOLD_VERSION), ("created", time.time())):
        con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (key, value))
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('app', ?)", (__version__,))
    if fts_ok and not had_index and con.execute("SELECT 1 FROM tracks LIMIT 1").fetchone():
        con.execute("UPDATE meta SET value = 0 WHERE key = 'fold_version'")   # songs with no index yet: refold
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _upgrade(con: sqlite3.Connection, version: int, tables: set[str]) -> None:
    """Bring an older file up to SCHEMA_VERSION step by step (_UPGRADES); without a way up (or from a
    newer version of the player), make the library tables again — they're only a record of the
    music files — keeping plays and favourites."""
    while version < SCHEMA_VERSION and version in _UPGRADES:
        _UPGRADES[version](con)
        version += 1
    if version == SCHEMA_VERSION:
        return
    con.execute("DROP TRIGGER IF EXISTS tracks_unindexed")
    for name in _LIBRARY_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {name}")
    for name, columns in _KEPT_TABLES.items():
        if name in tables and {row[1] for row in con.execute(f"PRAGMA table_info({name})")} != columns:
            con.execute(f"DROP TABLE {name}")   # not kept the way this version keeps it


def _find_root(con: sqlite3.Connection, root: str) -> tuple[int, float] | None:
    """(id, first-look time) of a library folder, or None."""
    rows = [(root_id, _from_db(path), built) for root_id, path, built in con.execute("SELECT id, path, built FROM roots")]
    for root_id, path, built in rows:
        if path == root:
            return root_id, built
    for root_id, path, built in rows:   # (the same folder written another way, e.g. its case)
        if Path(path) == Path(root):
            return root_id, built
    return None


def _root(con: sqlite3.Connection, root: str) -> tuple[int, float]:
    """_find_root, adding the library folder when it's new."""
    found = _find_root(con, root)
    if found is not None:
        return found
    return con.execute("INSERT INTO roots(path) VALUES (?)", (_to_db(root),)).lastrowid, 0.0


def _folder(con: sqlite3.Connection, root_id: int, rel: str) -> tuple[int, int]:
    """(folder id, its album folder's id), adding the folder (and its album folder) when new."""
    row = con.execute("SELECT id, album_folder_id FROM folders WHERE root_id = ? AND rel = ?",
                      (root_id, _to_db(rel))).fetchone()
    if row is not None:
        return row[0], row[1]
    album_rel = _album_rel(rel)
    album_id = _folder(con, root_id, album_rel)[0] if album_rel != rel else None
    folder_id = con.execute("INSERT INTO folders(root_id, rel, album_folder_id) VALUES (?, ?, ?)",
                            (root_id, _to_db(rel), album_id)).lastrowid
    if album_id is None:
        con.execute("UPDATE folders SET album_folder_id = id WHERE id = ?", (folder_id,))
        album_id = folder_id
    return folder_id, album_id


def _locate(con: sqlite3.Connection, folder: str) -> tuple[int, str, str] | None:
    """(library folder id, its path, `folder` below it) for a folder in the library, else None."""
    for root_id, root in con.execute("SELECT id, path FROM roots ORDER BY length(path) DESC").fetchall():
        root = _from_db(root)
        try:
            return root_id, root, _rel(root, folder)
        except ValueError:
            continue
    return None


def _folders_under(con: sqlite3.Connection, root_id: int, rel: str, whole_tree: bool) -> list[tuple[int, str, str]]:
    """(id, rel, cue_sig) of the stored folders an album at `rel` takes in: itself and its "CD1" /
    "Disc 2" folders — or, `whole_tree`, every folder below it."""
    prefix = rel + os.sep
    if rel and isinstance(_to_db(rel), str):
        rows = con.execute("SELECT id, rel, cue_sig FROM folders WHERE root_id = ?"
                           " AND (rel = ? OR (rel >= ? AND rel < ?))",   # (itself, or a folder inside it)
                           (root_id, rel, prefix, prefix + "\U0010ffff")).fetchall()
    else:   # (the whole library folder, or a folder whose name isn't valid Unicode: sorted out here)
        rows = con.execute("SELECT id, rel, cue_sig FROM folders WHERE root_id = ?", (root_id,)).fetchall()
    rows = [(folder_id, _from_db(sub), sig) for folder_id, sub, sig in rows]
    if rel:
        rows = [row for row in rows if row[1] == rel or row[1].startswith(prefix)]
    if whole_tree:
        return rows
    return [row for row in rows if row[1] == rel
            or (os.path.dirname(row[1]) == rel and _DISC_FOLDER.match(os.path.basename(row[1])))]


def _folder_paths(con: sqlite3.Connection, folder_ids: list[int]) -> dict[int, str]:
    paths: dict[int, str] = {}
    for chunk in _chunks(folder_ids):
        for folder_id, root, rel in con.execute(
                "SELECT d.id, r.path, d.rel FROM folders d JOIN roots r ON r.id = d.root_id"
                f" WHERE d.id IN ({', '.join('?' * len(chunk))})", chunk):
            paths[folder_id] = _join(root, rel)
    return paths


def _rename(con: sqlite3.Connection, names: Iterable[str]) -> None:
    """The names albums are listed under: the folder's name, or — when two album folders share a
    name — with the folder it's in, "Greatest Hits  (Queen)" (browse.albums_from's rule; an album
    that is a whole library folder keeps its plain name)."""
    for name in names:
        rows = con.execute("SELECT a.id, d.rel, r.path FROM albums a JOIN folders d ON d.id = a.id"
                           " JOIN roots r ON r.id = d.root_id WHERE a.name = ?", (name,)).fetchall()
        for album_id, rel, root in rows:
            shown = _text(f"{name}  ({Path(_join(root, rel)).parent.name})" if len(rows) > 1 and rel else name)
            con.execute("UPDATE albums SET display_name = ?, sort_name = ? WHERE id = ?",
                        (shown, _sort_text(shown), album_id))


def _split(text: str) -> tuple[str, ...]:
    return tuple(text.split(_SEP)) if text else ()


def _music_on_disk(folder: Path, whole_tree: bool) -> list[str] | None:
    """The music files scan_album (or scan_folder) would read for `folder`; None if it can't be listed."""
    paths: list[str] = []
    try:
        if whole_tree:
            for here, dirs, names in os.walk(folder):
                dirs[:] = [d for d in dirs if not skip_folder(d)]
                paths += [os.path.join(here, n) for n in names if is_music_name(n)]
        else:
            for entry in folder.iterdir():
                if entry.is_file() and is_music_name(entry.name):
                    paths.append(str(entry))
                elif entry.is_dir() and _DISC_FOLDER.match(entry.name):
                    paths += [str(p) for p in entry.iterdir() if p.is_file() and is_music_name(p.name)]
    except OSError:
        return None
    return paths


# Search: the best `limit` songs by the index's weighted score, then their rows.
_TOP_HITS = ("SELECT r.path, d.rel, f.name, t.id, " + ", ".join(f"t.{n}" for n in _TRACK_FIELDS[:-1])
             + f" FROM (SELECT rowid AS id, {_BM25} AS score FROM song_fts WHERE song_fts MATCH ?"
             " ORDER BY score LIMIT ?) AS s JOIN tracks t ON t.id = s.id JOIN files f ON f.id = t.file_id"
             " JOIN folders d ON d.id = f.folder_id JOIN roots r ON r.id = d.root_id ORDER BY s.score")


# The Songs view's sorts: (SQL, flips with a reversed sort?, descending?) for each column's first
# ORDER BY terms. Lossy files come first by quality (as the album list's Quality column).
_SONG_ORDER = {
    "title": (("ib_sort(t.title)", True, False),),
    "artist": (("ib_sort(t.artist)", True, False),),
    "album": (("ib_sort(t.album)", True, False),),
    "year": (("t.year IS NULL", False, False), ("t.year", True, False)),
    "quality": (("t.codec IN ('MP3', 'Vorbis', 'Opus', 'AAC')", True, True), ("t.bits", True, False),
                ("t.sample_rate", True, False)),
    "time": (("t.duration", True, False),),
}


def _hit(row) -> SongHit:
    """A SongHit (no search words) from a _TRACK_SELECT row: the track and its album folder."""
    return SongHit(row[3], _track(row), Path(_join(row[0], _album_rel(_from_db(row[1])))))


def _ranked(rows: list, words: list[str], whole_words: bool) -> list[SongHit]:
    """Search results in order: songs where every word is a whole word before ones where a word only
    starts one; then where the words were found (title, artist, album, album artist, composer,
    genre, year, file name — the index's own weighting); then by artist, album, disc and track,
    so an album's songs stay together in their order. `whole_words` False: the words were looked
    for anywhere in the text (the slower search)."""
    ranked = []
    for row in rows:
        track = _track(row)
        year = track.year
        stem = os.path.splitext(_from_db(row[2]))[0]
        texts = (track.title, track.artist, track.album, track.album_artist, track.composer, track.genre,
                 str(year) if year else "", stem)
        tokens = [searchfold.tokens(text) for text in texts]
        if whole_words:
            hit = [any(token.startswith(word) for token in found for word in words) for found in tokens]
        else:
            folded = [searchfold.fold(text) for text in texts]
            hit = [any(word in text for word in words) for text in folded]
        every = {token for found in tokens for token in found}
        whole = all(word in every for word in words)
        best = next((i for i, yes in enumerate(hit) if yes), len(texts))
        key = (not whole, best, sort_name(track.artist), natural_key(track.album), track.disc_no or 0,
               track.track_no if track.track_no is not None else 10_000, natural_key(track.title))
        album = Path(_join(row[0], _album_rel(_from_db(row[1]))))
        ranked.append((key, SongHit(row[3], track, album, FIELD_NAMES[best] if 3 <= best < len(texts) else "")))
    ranked.sort(key=lambda pair: pair[0])
    return [hit for _key, hit in ranked]


# -- reading a folder (no database: may run in a worker process) -------------------------------------------
def list_folder(folder: str | Path) -> tuple[dict[str, tuple[int, int, float]], str]:
    """A folder's music files as FolderResult.listing wants them — {name: (size, modified ns,
    created s)} — and its cue_signature, from one listing. OSError when it can't be listed."""
    folder = os.fspath(folder)
    music: dict[str, tuple[int, int, float]] = {}
    sheets = []
    with os.scandir(folder) as entries:
        for entry in entries:
            name = entry.name
            try:
                if not entry.is_file():
                    continue
                if is_music_name(name):
                    st = entry.stat()
                    music[name] = (st.st_size, st.st_mtime_ns, float(getattr(st, "st_birthtime", st.st_mtime)))
                elif name.lower().endswith(".cue") and not name.startswith("._"):
                    st = entry.stat()
                    sheets.append((name, st.st_size, st.st_mtime_ns))
            except OSError:
                continue
    return music, cue_signature_of(sheets)


def folder_result(root: str | Path, folder: str | Path, listing: dict[str, tuple[int, int, float]],
                  tracks: dict[str, list[Track]], retry: Iterable[str] = (), cue_sig: str = "",
                  listed_ok: bool = True) -> FolderResult:
    """A FolderResult from tracks already read: {file name: its tracks}."""
    return FolderResult(root=str(Path(root)), folder=os.fspath(folder), listing=dict(listing),
                        tracks={name: [track_row(t) for t in found] for name, found in tracks.items()},
                        retry=list(retry), cue_sig=cue_sig, listed_ok=listed_ok)


def read_folder(root: str | Path, folder: str | Path, listing: dict[str, tuple[int, int, float]],
                names: Iterable[str] | None = None, cue_sig: str = "",
                reader: Callable[[Path], Track] = read_track) -> FolderResult:
    """Read a folder's music files for LibraryDB.apply — `names` of them (None: all). A folder
    with CUE sheets (`cue_sig`), or a file with its own CUESHEET, is read whole, since how its
    files split into tracks depends on all of them (library.split_cue_images); pass names=None
    for a folder that held CUE tracks before. A file that couldn't be read just now (locked, a
    network drive dropping out) goes in `retry`: what's stored for it stays, read again next time."""
    folder = os.fspath(folder)
    whole = names is None or bool(cue_sig)
    todo = sorted(listing) if whole else sorted(set(names) & set(listing))
    tracks = _read_all(folder, todo, reader)
    if not whole and any(t.embedded_cue is not None for t in tracks):
        others = sorted(set(listing) - set(todo))
        tracks += _read_all(folder, others, reader)
        todo += others
        whole = True
    if whole:
        tracks = split_cue_images(tracks)
    found: dict[str, list[Track]] = {name: [] for name in todo}
    for track in tracks:
        found.setdefault(os.path.basename(os.fspath(track.path)), []).append(track)
    retry = [name for name, got in found.items() if not got or any(t.unreadable for t in got)]
    return folder_result(root, folder, listing, {name: got for name, got in found.items() if got}, retry, cue_sig)


def _read_all(folder: str, names: list[str], reader: Callable[[Path], Track]) -> list[Track]:
    tracks = []
    for name in names:
        try:
            tracks.append(reader(Path(folder, name)))
        except (OSError, RuntimeError, ValueError):
            continue   # (vanished meanwhile, or unreadable: tried again next time)
    return tracks

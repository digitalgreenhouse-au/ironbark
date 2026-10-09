"""The library database's schema: the tables, their upgrades, and the SQL the queries in library_db.py are built from. Nothing here opens a database; library_db.py does, and re-exports these names."""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from . import __version__, searchfold

SCHEMA_VERSION = 2          # PRAGMA user_version: bump with a step in _UPGRADES (else the library tables are remade)


# The search index's columns, and how much a match in each counts (bm25 weights).
FTS_COLUMNS = ("title", "artist", "album", "album_artist", "composer", "genre", "year", "path")


WEIGHTS = (10.0, 6.0, 5.0, 4.0, 3.0, 1.0, 1.0, 0.5)


FIELD_NAMES = ("title", "artist", "album", "album artist", "composer", "genre", "year", "file name")


class _NeedsFTS(sqlite3.DatabaseError):
    """The file has a search index this Python's SQLite can't open (no FTS5): it can't be used here."""


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
CREATE INDEX IF NOT EXISTS files_gone ON files(size, mtime_ns) WHERE missing_since IS NOT NULL;   -- moved files
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
# way up has its library tables made again (they're only a record of the music files). An older
# version opening a newer one's file keeps its plays, favourites and checks tables as they are, extra
# columns and all (_upgrade), and writes its own version number: so each step must cope with what a
# newer version already did (CREATE … IF NOT EXISTS; a column added only when it isn't there).
def _to_2(con: sqlite3.Connection) -> None:
    """1 → 2: the quality check's table."""
    for statement in _statements(_CHECK_TABLES):
        con.execute(statement)


_UPGRADES: dict[int, Callable[[sqlite3.Connection], None]] = {1: _to_2}


_TRACK_FIELDS = ("cue_sheet", "cue_number", "cue_start", "cue_end", "title", "artist", "album", "album_artist",
                 "composer", "genre", "date", "year", "track_no", "disc_no", "duration", "sample_rate", "bits",
                 "channels", "bitrate", "size", "codec", "md5", "encoder", "float_samples", "bitrate_mode",
                 "channel_mode", "misnamed", "unsupported", "compilation", "details", "haystack")


_INSERT_TRACK = (f"INSERT INTO tracks(id, file_id, album_id, {', '.join(_TRACK_FIELDS)}) "
                 f"VALUES ({', '.join('?' * (len(_TRACK_FIELDS) + 3))})")


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
    music files — keeping plays, favourites and checks. Those are never dropped: a newer version's
    extra columns stay (this version's writes name their columns); one this version can't write to
    at all (a column it needs isn't there) is set aside whole, for that newer version to find."""
    while version < SCHEMA_VERSION and version in _UPGRADES:
        _UPGRADES[version](con)
        version += 1
    if version == SCHEMA_VERSION:
        return
    con.execute("DROP TRIGGER IF EXISTS tracks_unindexed")
    for name in _LIBRARY_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {name}")
    for name, columns in _KEPT_TABLES.items():
        if name not in tables:
            continue
        info = con.execute(f"PRAGMA table_info({name})").fetchall()   # (cid, name, type, notnull, default, pk)
        have = {row[1] for row in info}
        if columns <= have and not any(row[1] not in columns and row[3] and row[4] is None for row in info):
            continue   # (usable as it is)
        for (index,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?"
                                    " AND sql IS NOT NULL", (name,)).fetchall():
            con.execute(f'DROP INDEX "{index}"')   # (this version makes its own, by the same names)
        aside, n = f"{name}_newer", 2
        while con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (aside,)).fetchone():
            aside, n = f"{name}_newer_{n}", n + 1
        con.execute(f'ALTER TABLE {name} RENAME TO "{aside}"')

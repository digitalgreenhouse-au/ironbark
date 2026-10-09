"""The cache's files and its small database (netcache.db). No network I/O here.

Folder (`settings.cache_folder() / "NetCache"`):
    netcache.db (+ -wal, -shm)     what's cached: one row per source file (an "item")
    owner.lock                     held by the one copy of the player using this cache
    Partial\\<id>.g<gen>.part       a download in progress: only ever the unbroken run of bytes from
                                   the start (never made full size up front: a full-size file read
                                   before it's filled would play silence as if it were complete)
    Partial\\<id>.g<gen>.seg-<n>    an "island": a run of bytes from offset n fetched ahead of that
                                   unbroken run (after a seek ahead, or the end of a file whose open
                                   reads its end). When the run from the start reaches an island, the
                                   island is appended to the .part (a local copy) and deleted.
    Tracks\\<name> [<id>].<ext>     complete, checked copies, in their original format (a later version
                                   of the source: [<id>-g<gen>], so an old copy still playing is kept)

`<id>` is the first 16 hex digits of the SHA-256 of the source's key; `<gen>` the version of the
source's bytes (a changed source starts a new version; old bytes are never rewritten).

The database follows library_db's conventions (WAL, synchronous=NORMAL, a busy timeout, the schema
version in `PRAGMA user_version`, damage = SQLite's CORRUPT / NOTADB codes only). It's a separate
file from library.db: the cache is disposable, library.db holds plays and favourites, and they
have different single writers. Only the cache's scheduler thread writes it. A damaged one is set
aside as netcache.db.damaged-dd-mm-yyyy and the cache emptied — everything can be downloaded again.

Write order (so the database never claims bytes that aren't safely on disk): the I/O thread
writes a chunk, flushes it (readers can see it), and every 4 MiB or 2 s fsyncs it (it's durable);
the scheduler stores the durable length as `prefix` (and each island's, in `islands`). After a
crash each .part and island is cut back to its stored length and the download carries on from there.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import IO

from .. import audiofile
from ..library_db import is_damage
from .growing import GrowingFile, open_shared

SCHEMA_VERSION = 1
BUSY_S = 5.0
NAME_STEM_MAX = 80
PATH_MAX = 240

# Item states
QUEUED, PROBING, DOWNLOADING, PARTIAL = "queued", "probing", "downloading", "partial"
DOWNLOADED, VERIFYING, READY, STALE, FAILED = "downloaded", "verifying", "ready", "stale", "failed"
CHECKING_STATES = {DOWNLOADED, VERIFYING}

_COLUMNS = ("id", "key", "kind", "source", "host", "ext", "gen", "state", "size", "mtime_ns", "etag",
            "last_modified", "prefix", "islands", "info", "final_name", "verified_by", "verified_at",
            "created", "completed", "last_used", "use_count", "pinned", "error", "fail_count", "retry_at")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
  source TEXT NOT NULL, host TEXT NOT NULL, ext TEXT NOT NULL,
  gen INTEGER NOT NULL, state TEXT NOT NULL,
  size INTEGER, mtime_ns INTEGER, etag TEXT, last_modified TEXT,
  prefix INTEGER NOT NULL DEFAULT 0,
  islands TEXT,
  info TEXT,
  final_name TEXT, verified_by TEXT, verified_at REAL,
  created REAL, completed REAL, last_used REAL, use_count INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0,
  error TEXT, fail_count INTEGER NOT NULL DEFAULT 0, retry_at REAL);
CREATE TABLE IF NOT EXISTS hosts (host TEXT PRIMARY KEY, speed REAL, gap REAL, failures INTEGER,
  quality TEXT, measured REAL);
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);
"""
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class Island:
    """Bytes [start, visible) of a source on disk in a file of their own, fetched ahead of the
    unbroken run from the start. `durable`: how far they are fsynced (what the database claims)."""

    __slots__ = ("start", "visible", "durable")

    def __init__(self, start: int, visible: int | None = None, durable: int | None = None):
        self.start = int(start)
        self.visible = int(self.start if visible is None else visible)
        self.durable = int(self.visible if durable is None else durable)

    def __repr__(self):
        return f"Island({self.start}, {self.visible})"


def _islands_from(saved) -> list[Island]:
    try:
        runs = json.loads(saved) if isinstance(saved, str) else (saved or [])
        islands = [Island(int(start), int(end), int(end)) for start, end in runs if int(end) > int(start) >= 0]
    except (TypeError, ValueError):
        return []
    return sorted(islands, key=lambda island: island.start)


def item_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:16]


class Item:
    """One source file in the cache (a CUE image is one item for all its tracks). The saved fields
    mirror the database row; the rest lives in memory. `cond` guards the byte counts and leases
    and is only ever held for bookkeeping, never during I/O."""

    def __init__(self, store: Store, *, key: str, kind: str, source: str, host: str, ext: str, **saved):
        self.store = store
        self.id = saved.pop("id", None) or item_id(key)
        self.key, self.kind, self.source, self.host, self.ext = key, kind, source, host, ext
        self.gen = int(saved.pop("gen", 1))
        self.state = saved.pop("state", QUEUED)
        self.size: int | None = saved.pop("size", None)
        self.mtime_ns: int | None = saved.pop("mtime_ns", None)
        self.etag = saved.pop("etag", None)
        self.last_modified = saved.pop("last_modified", None)
        self.prefix = int(saved.pop("prefix", 0) or 0)     # durable bytes (the database's claim)
        self.islands: list[Island] = _islands_from(saved.pop("islands", None))   # in order, apart, past the prefix
        info = saved.pop("info", None)
        self.info: dict = json.loads(info) if isinstance(info, str) else dict(info or {})
        self.final_name = saved.pop("final_name", None)
        self.verified_by = saved.pop("verified_by", None)
        self.verified_at = saved.pop("verified_at", None)
        now = time.time()
        self.created = saved.pop("created", None) or now
        self.completed = saved.pop("completed", None)
        self.last_used = saved.pop("last_used", None) or now
        self.use_count = int(saved.pop("use_count", 0) or 0)
        self.pinned = int(saved.pop("pinned", 0) or 0)
        self.error = saved.pop("error", None)
        self.fail_count = int(saved.pop("fail_count", 0) or 0)
        self.retry_at = saved.pop("retry_at", None)
        # In memory only
        self.cond = threading.Condition(threading.Lock())
        self.visible = self.prefix      # flushed bytes: what readers may read
        self.durable = self.prefix      # fsynced bytes
        self.leases: dict[int, int] = {}
        self.old: dict[int, int] = {}   # abandoned versions still being read: gen -> bytes they have
        self.old_sizes: dict[int, int] = {}   # ... and their full size
        self.old_files: dict[int, Path] = {}   # … and, for an old complete copy, its file
        self.old_islands: dict[int, list[Island]] = {}   # … and the islands it had
        self.failed_gens: set[int] = set()     # versions whose check failed (a play from one is warned of)
        self.rejected = ""              # the SHA-256 of the last copy whose sound failed its check
        self.lost_gens: set[int] = set()       # versions whose bytes went missing from this computer (see lose)
        self.last_failure: tuple | None = None   # (its server's comebacks, bytes here, times in a row) at a failed read
        self.open_ready = False         # the probe opened it and every byte that read is here
        self.open_end: int | None = self.info.get("open_end")
        self.scanner = None             # flacframes.Scanner, fed as bytes arrive (FLAC)
        self.priority: int | None = None
        self.transfer_ok = False        # every byte read as asked, same identity start to end
        self.permanent = False          # FAILED because the source is gone or refused
        self.room_check = False         # the I/O thread found no room for it: the scheduler looks
        self.plan_check = False         # its size was only just learnt: the scheduler looks before fetching it
        self.dirty = True
        self.saved_islands = self.island_claim()   # what the database last heard about the islands
        self.on_demand = None           # (item, gen, start, end, role): wakes the scheduler
        self.on_lost = None             # (item, gen): bytes it holds went missing (the scheduler looks)
        self.handed_at = -1e18          # when a reader was last handed a copy to open (the manager's clock)

    # -- what readers use (audiofile.LocalTrack, GrowingFile) -------------------------------
    def ready_path(self, gen: int, source):
        if gen != self.gen:
            copy = self.old_files.get(gen)   # an old complete copy, still being played
            if copy is None:
                return None
        elif self.state != READY or not self.final_name:
            return None
        else:
            copy = self.store.tracks / self.final_name
        cue = audiofile.cue_of(source)
        return audiofile.CuePath(copy, cue=cue) if cue is not None else copy

    def copy_gone(self, gen: int) -> bool:
        """Has the complete copy of version `gen` gone from this computer (deleted, or cut short,
        behind the cache's back)? A local look, for after it failed to open."""
        copy = self.ready_path(gen, None)
        if copy is None:
            return False
        try:
            return os.stat(copy).st_size < self.size_of(gen)
        except OSError:
            return True

    def reader(self, gen: int, gate, role: str = "play"):
        return GrowingFile(self, gen, gate, role)

    def acquire(self, gen: int) -> None:
        with self.cond:
            self.leases[gen] = self.leases.get(gen, 0) + 1

    def release(self, gen: int) -> None:
        with self.cond:
            left = self.leases.get(gen, 0) - 1
            if left > 0:
                self.leases[gen] = left
            else:
                self.leases.pop(gen, None)

    def leased(self, gen: int | None = None) -> bool:
        return bool(self.leases) if gen is None else self.leases.get(gen, 0) > 0

    def copy_state(self, gen: int) -> str:
        """"downloading", "checking", "verified", "stale" (an older version), "failed" (it failed its
        check: a fresh copy downloads) or "rejected" (it failed its check and the song is skipped for
        now: nothing downloads until it's asked for again a while later)."""
        if gen in self.failed_gens:
            return "rejected" if self.state == FAILED and self.permanent else "failed"
        if gen in self.lost_gens:
            return "downloading"   # (its bytes went missing here: fetched again)
        if gen != self.gen:
            return "stale"
        if self.state == READY:
            return "verified"
        if self.state in CHECKING_STATES:
            return "checking"
        if self.state == FAILED:
            return "failed"
        return "downloading"

    def has(self, gen: int, end: int) -> bool:
        """Bytes [0, end) of version `gen` are on disk and readable."""
        if gen in self.lost_gens:
            return False
        if gen == self.gen:
            return self.visible >= end or self.state == READY
        return end <= self.old.get(gen, 0)

    def _runs(self, gen: int) -> tuple[int, list[Island]]:
        """(the end of the unbroken run from the start, the islands) of version `gen`."""
        if gen in self.lost_gens:
            return 0, []
        if gen == self.gen:
            return (int(self.size or 0) if self.state == READY else self.visible), self.islands
        return self.old.get(gen, 0), self.old_islands.get(gen, [])

    def has_range(self, gen: int, start: int, end: int) -> bool:
        """Bytes [start, end) of version `gen` are on disk and readable (in the run from the start,
        in an island, or across both)."""
        prefix, islands = self._runs(gen)
        if end <= prefix:
            return True
        at = max(start, prefix)
        for island in list(islands):
            if island.start > at:
                break
            at = max(at, island.visible)
            if at >= end:
                return True
        return at >= end

    has_now = has_range   # (plain integer reads: safe without the lock for a quick look)

    def locate(self, gen: int, at: int) -> tuple[object, int] | None:
        """Where byte `at` of version `gen` is: ("part", end of that run) — the .part, or the
        complete copy once renamed — or (island start, island end); None if it isn't here."""
        prefix, islands = self._runs(gen)
        if at < prefix:
            return "part", prefix
        for island in list(islands):
            if island.start <= at < island.visible:
                return island.start, island.visible
        return None

    def island_bytes(self) -> int:
        return sum(island.visible - island.start for island in self.islands)

    def held(self) -> int:
        """Bytes of this version on disk."""
        if self.state == READY:
            return int(self.size or 0)
        return self.visible + self.island_bytes()

    def spans(self) -> list[tuple[int, int]]:
        """The byte runs of this version on disk, in order: [(start, end), …]."""
        if self.state == READY or self.complete:
            return [(0, int(self.size or self.visible))]
        runs = [(0, self.visible)] if self.visible else []
        return runs + [(island.start, island.visible) for island in self.islands if island.visible > island.start]

    def island_claim(self) -> list:
        """The islands as the database stores them: [[start, durable end], …]."""
        return [[island.start, island.durable] for island in self.islands if island.durable > island.start]

    def size_of(self, gen: int) -> int:
        """The full size of version `gen` (an old one keeps its own)."""
        if gen != self.gen and gen in self.old_sizes:
            return self.old_sizes[gen]
        return int(self.size or 0)

    def gen_failed(self, gen: int) -> bool:
        return gen != self.gen or self.state == FAILED or gen in self.lost_gens

    def lose(self, gen: int) -> None:
        """A reader couldn't read bytes of version `gen` that are recorded as here (the file was
        deleted or cut short behind the cache's back): nobody waits on them any more, and the cache
        is told, to look and fetch the song again. Called from the reader's own thread: quick."""
        with self.cond:
            if gen in self.lost_gens:
                return
            self.lost_gens.add(gen)
            self.cond.notify_all()
        hook = self.on_lost
        if hook is not None:
            hook(self, gen)

    def notify(self) -> None:
        with self.cond:
            self.cond.notify_all()

    def demand(self, gen: int, start: int, end: int, role: str) -> None:
        hook = self.on_demand
        if hook is not None:
            hook(self, gen, start, end, role)

    def open_bytes(self, gen: int):
        """The bytes of version `gen`, opened so they can still be renamed or deleted meanwhile."""
        part = self.store.part_path(self, gen)
        try:
            return open_shared(part)
        except FileNotFoundError:
            final = self.ready_path(gen, None) or self.old_files.get(gen)
            if final is None and gen == self.gen and self.final_name:
                final = self.store.final_path(self)   # (renamed to its final name a moment ago)
            if final is None:
                raise
            return open_shared(final)

    # -- bookkeeping ------------------------------------------------------------------------
    @property
    def complete(self) -> bool:
        return self.size is not None and self.visible >= self.size

    def fraction(self) -> float:
        if self.state == READY:
            return 1.0
        if not self.size:
            return 0.0
        return min(1.0, self.held() / self.size)

    def row(self) -> tuple:
        values = dict(id=self.id, key=self.key, kind=self.kind, source=self.source, host=self.host,
                      ext=self.ext, gen=self.gen, state=self.state, size=self.size, mtime_ns=self.mtime_ns,
                      etag=self.etag, last_modified=self.last_modified, prefix=self.prefix,
                      islands=json.dumps(self.island_claim()) if self.island_claim() else None,
                      info=json.dumps(self.info), final_name=self.final_name,
                      verified_by=self.verified_by, verified_at=self.verified_at, created=self.created,
                      completed=self.completed, last_used=self.last_used, use_count=self.use_count,
                      pinned=self.pinned, error=self.error, fail_count=self.fail_count, retry_at=self.retry_at)
        return tuple(values[c] for c in _COLUMNS)

    def __repr__(self):
        return f"Item({self.source!r}, gen {self.gen}, {self.state}, {self.visible}/{self.size})"


class OwnerLock:
    """Held (a byte lock on owner.lock) for as long as this copy of the player uses the cache, so
    two copies never download into one folder."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._file: IO[bytes] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            f.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._file = f
        return True

    def release(self) -> None:
        f, self._file = self._file, None
        if f is None:
            return
        try:
            f.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            f.close()


class Store:
    """The cache folder and netcache.db. Used by the scheduler thread only (one writer)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.partial = self.folder / "Partial"
        self.tracks = self.folder / "Tracks"
        self.db_path = self.folder / "netcache.db"
        self._con: sqlite3.Connection | None = None
        self.set_aside: Path | None = None   # where a damaged database went, this run

    # -- files ------------------------------------------------------------------------------
    def part_path(self, item: Item, gen: int | None = None) -> Path:
        return self.partial / f"{item.id}.g{item.gen if gen is None else gen}.part"

    def island_path(self, item: Item, start: int, gen: int | None = None) -> Path:
        return self.partial / f"{item.id}.g{item.gen if gen is None else gen}.seg-{int(start)}"

    def final_path(self, item: Item) -> Path | None:
        return self.tracks / item.final_name if item.final_name else None

    def final_name_for(self, item: Item) -> str:
        stem = _BAD_CHARS.sub("_", Path(item.source.replace("\\", "/").rsplit("/", 1)[-1]).stem).strip(" .")
        stem = (stem or "song")[:NAME_STEM_MAX]
        tag = item.id if item.gen == 1 else f"{item.id}-g{item.gen}"   # (an old version may still be read)
        suffix = f" [{tag}].{item.ext}" if item.ext else f" [{tag}]"
        room = PATH_MAX - len(str(self.tracks)) - 1 - len(suffix)
        if room < len(stem):
            stem = stem[:max(1, room)]
        return stem + suffix

    def make_folders(self) -> None:
        self.partial.mkdir(parents=True, exist_ok=True)
        self.tracks.mkdir(parents=True, exist_ok=True)

    # -- the database ---------------------------------------------------------------------
    def open(self) -> list[Item]:
        """Open (or make) netcache.db and load every item. A damaged database is set aside and the
        cache emptied (nothing in it can be trusted then)."""
        self.make_folders()
        try:
            return self._open()
        except sqlite3.DatabaseError as error:
            if not is_damage(error):
                raise
        self.close()
        self.set_aside = self._set_aside_damaged()
        self.empty_files()
        return self._open()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=BUSY_S, isolation_level=None, check_same_thread=False)
        try:
            con.execute(f"PRAGMA busy_timeout = {int(BUSY_S * 1000)}")
            con.execute("PRAGMA synchronous = NORMAL")
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA journal_size_limit = 8388608")
        except BaseException:
            con.close()
            raise
        return con

    def _open(self) -> list[Item]:
        con = self._connect()
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                con.execute("BEGIN")
                if version != 0:
                    # (a newer player's cache, or one this version has no upgrade for: start afresh)
                    for table in ("items", "hosts", "meta"):
                        con.execute(f"DROP TABLE IF EXISTS {table}")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        con.execute(statement)
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                con.execute("COMMIT")
            rows = con.execute(f"SELECT {', '.join(_COLUMNS)} FROM items").fetchall()
        except BaseException:
            con.close()
            raise
        self._con = con
        items = []
        for row in rows:
            values = dict(zip(_COLUMNS, row))
            key, kind, source, host, ext = (values.pop(k) for k in ("key", "kind", "source", "host", "ext"))
            item = Item(self, key=key, kind=kind, source=source, host=host, ext=ext, **values)
            item.dirty = False
            items.append(item)
        return items

    def _set_aside_damaged(self) -> Path | None:
        stamp = time.strftime("%d-%m-%Y")
        aside, n = self.db_path.with_name(f"{self.db_path.name}.damaged-{stamp}"), 2
        while aside.exists():
            aside, n = self.db_path.with_name(f"{self.db_path.name}.damaged-{stamp}-{n}"), n + 1
        try:
            os.replace(self.db_path, aside)
        except OSError:
            return None
        for extra in ("-wal", "-shm"):
            try:
                os.remove(str(self.db_path) + extra)
            except OSError:
                pass
        return aside

    def save(self, items) -> None:
        """Write these items' rows in one transaction."""
        items = list(items)
        if not items or self._con is None:
            return
        marks = ", ".join("?" for _ in _COLUMNS)
        con = self._con
        con.execute("BEGIN")
        try:
            con.executemany(f"INSERT OR REPLACE INTO items ({', '.join(_COLUMNS)}) VALUES ({marks})",
                            [item.row() for item in items])
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        for item in items:
            item.dirty = False

    def remove(self, items) -> None:
        items = list(items)
        if not items or self._con is None:
            return
        con = self._con
        con.execute("BEGIN")
        try:
            con.executemany("DELETE FROM items WHERE id = ?", [(item.id,) for item in items])
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise

    def close(self) -> None:
        con, self._con = self._con, None
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass

    # -- tidying ------------------------------------------------------------------------------
    def empty_files(self) -> None:
        """Delete every cached file (not the database, not owner.lock). Files still open are left."""
        for folder in (self.partial, self.tracks):
            if not folder.is_dir():
                continue
            for child in folder.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink()
                except OSError:
                    pass

    def delete_file(self, path: Path | None) -> bool:
        """Delete one cached file. False if it couldn't be (still open: tried again next sweep)."""
        if path is None:
            return True
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def tidy(self, items: list[Item]) -> tuple[list[Item], dict]:
        """The start-up tidy: put the files and the rows back in step after any crash. Returns the
        items to keep, and counts of what was done."""
        done = dict(truncated=0, promoted=0, dropped=0, orphans=0, stale=0)
        kept, removed = [], []
        for item in items:
            part = self.part_path(item)
            final = self.final_path(item)
            if item.state == STALE:
                for gen_part in self.partial.glob(f"{item.id}.g*"):
                    self.delete_file(gen_part)
                self.delete_file(final)
                removed.append(item)
                done["stale"] += 1
                continue
            final_size = _size(final)
            if item.islands and item.state in (READY, FAILED, DOWNLOADED, VERIFYING):
                item.islands, item.dirty = [], True   # (complete or failed: no runs ahead to keep)
            if item.state == READY:
                if final_size is not None and final_size == item.size:
                    kept.append(item)
                else:
                    removed.append(item)
                    done["dropped"] += 1
                continue
            if final_size is not None and item.size is not None and final_size == item.size \
                    and item.state in (DOWNLOADED, VERIFYING) and item.verified_by:
                # renamed into place, but the row wasn't updated before the crash
                item.state, item.prefix = READY, item.size
                item.visible = item.durable = item.size
                item.dirty = True
                kept.append(item)
                done["promoted"] += 1
                continue
            part_size = _size(part)
            if item.state == FAILED:
                self.delete_file(part)
                item.prefix = item.visible = item.durable = 0
                kept.append(item)
                continue
            if part_size is None:
                item.prefix = item.visible = item.durable = 0
                done["truncated"] += self._tidy_islands(item)
                if item.state != QUEUED and not item.islands:
                    item.state, item.dirty = QUEUED, True
                elif item.islands and item.state not in (PARTIAL, QUEUED):
                    item.state, item.dirty = PARTIAL, True
            else:
                if part_size > item.prefix:
                    try:
                        with open(part, "r+b") as f:
                            f.truncate(item.prefix)
                        done["truncated"] += 1
                    except OSError:
                        pass
                elif part_size < item.prefix:   # (never expected: claim only what's there)
                    item.prefix = part_size
                item.visible = item.durable = item.prefix
                done["truncated"] += self._tidy_islands(item)
                if item.state in (PROBING, DOWNLOADING, QUEUED):
                    item.state = PARTIAL if item.prefix or item.islands else QUEUED
                elif item.state == VERIFYING:
                    item.state = DOWNLOADED
                item.dirty = True
            kept.append(item)
        # Orphans: files no kept row accounts for, and anything left half-written.
        wanted_parts = {self.part_path(item).name for item in kept}
        wanted_parts |= {self.island_path(item, island.start).name for item in kept for island in item.islands}
        wanted_tracks = {item.final_name for item in kept if item.state == READY and item.final_name}
        for folder, wanted in ((self.partial, wanted_parts), (self.tracks, wanted_tracks)):
            if not folder.is_dir():
                continue
            for child in folder.iterdir():
                if child.name not in wanted or child.suffix == ".tmp":
                    if self.delete_file(child):
                        done["orphans"] += 1
        self.remove(removed)
        self.save([item for item in kept if item.dirty])
        for item in kept:
            item.saved_islands = item.island_claim()
        return kept, done

    def _tidy_islands(self, item: Item) -> int:
        """Cut each island back to its stored length; drop one whose file has gone, or that the
        run from the start (or an earlier island) already reaches. Returns how many were cut back."""
        cut = 0
        kept: list[Island] = []
        for island in item.islands:
            path = self.island_path(item, island.start)
            size = _size(path)
            if size is None or island.start <= item.prefix or (kept and island.start <= kept[-1].visible):
                self.delete_file(path)
                item.dirty = True
                continue
            claim = island.durable - island.start
            if size > claim:
                try:
                    with open(path, "r+b") as f:
                        f.truncate(claim)
                    cut += 1
                except OSError:
                    pass
            elif size < claim:   # (never expected: claim only what's there)
                island.durable = island.start + size
                item.dirty = True
            island.visible = island.durable
            kept.append(island)
        item.islands = kept
        return cut

    # -- per-server history (a warm start for the speed and the network's quality) ---------
    def load_hosts(self) -> list[dict]:
        if self._con is None:
            return []
        rows = self._con.execute("SELECT host, speed, gap, failures, quality, measured FROM hosts").fetchall()
        return [dict(zip(("host", "speed", "gap", "failures", "quality", "measured"), row)) for row in rows]

    def save_hosts(self, rows) -> None:
        rows = list(rows)
        if not rows or self._con is None:
            return
        con = self._con
        con.execute("BEGIN")
        try:
            con.executemany("INSERT OR REPLACE INTO hosts (host, speed, gap, failures, quality, measured) "
                            "VALUES (:host, :speed, :gap, :failures, :quality, :measured)", rows)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


def _size(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None

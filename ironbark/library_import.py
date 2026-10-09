"""The one-off move from the old library_index.json (0.15.0–0.30.0) into the database, run once on the first start with a database; the JSON file is renamed .migrated afterwards as the safety net."""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from .library_db import (
    READ_FORMAT,
    LibraryDB,
    _find_root,
    _folder,
    _from_db,
    _rel,
    _to_db,
    _TrackIds,
    cue_signature_of,
    track_from_dict,
    track_row,
)


class _NotMoved(Exception):
    """The JSON index couldn't be brought across exactly."""


def import_json_index(db: LibraryDB, path: str | Path,
                          progress: Callable[[int, int], None] | None = None) -> bool:
    """Bring a saved library_index.json (library_index.py) across: every file with exactly its
    size, modified time, `added` and `seen`, and the library folder's first-look time, so
    Recently added and new music carry on as they were. Only an index of the current format,
    for one of the library folders, into a library folder with nothing stored yet. Checked by
    counting the rows; then the JSON is renamed library_index.json.migrated (a backup).
    Returns whether it was brought across — if not (none, damaged, another folder, an older
    format), the library is simply looked through from scratch. `progress(done, total)`."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != READ_FORMAT:
            return False
        built = float(data.get("built", 0.0))
        entries = []
        for name, item in data["files"].items():
            entries.append((name, int(item["size"]), int(item["mtime"]), float(item["added"]), float(item["seen"]),
                            [track_from_dict(Path(name), t) for t in item["tracks"]]))
        cues = {str(folder): list(sheet) for folder, sheet in data.get("cues", {}).items()}
        json_root = Path(data["root"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    total, expected = len(entries), sum(len(tracks) for *_rest, tracks in entries)
    try:
        with db._writing() as con:
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
            ids = _TrackIds(con)
            for done, (name, size, mtime, added, seen, tracks) in enumerate(entries, 1):
                here, base = os.path.split(name)
                if here not in folders:
                    folders[here] = _folder(con, root_id, _rel(root, here))
                folder_id, album_id = folders[here]
                file_id = con.execute(
                    "INSERT INTO files(folder_id, name, size, mtime_ns, added, seen, read_format)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (folder_id, _to_db(base), size, mtime, added, seen, READ_FORMAT)).lastrowid
                db._insert_tracks(con, file_id, album_id, [track_row(t) for t in tracks], ids)
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
            db._rebuild_albums(con, albums)
            ids.save()
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

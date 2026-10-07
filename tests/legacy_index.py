"""Test helper: the library index as it was before the library database — one JSON file,
library_index.json, holding every file's details in memory — kept as a reference. Tests write
old-style index files with it (to check they're brought across exactly) and compare the
database's answers with the old code's (album rows, new music, tidying the opened list).

Only what those checks need: the stored entries, `save()` in the old format, and the old reads
(`tracks`, `albums`, `seen_times`), plus watcher.py's old new-music rules.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ironbark import __version__, settings
from ironbark.browse import albums_from
from ironbark.library import Album, Track, album_folder
from ironbark.library_index import FORMAT, Entry, track_to_dict


class JsonIndex:
    """The old library_index.LibraryIndex's data and reads (no looking at folders)."""

    def __init__(self, root: str | Path, path: Path):
        self.root = Path(root)
        self.path = path
        self.entries: dict[str, Entry] = {}
        self.cues: dict[str, list[list]] = {}   # folder -> [[name, size, modified ns], …] of its .cue files
        self.built = 0.0

    def save(self) -> None:
        """Write the index in the old format (as library_index.py did before 0.31)."""
        data = {"format": FORMAT, "app": __version__, "root": str(self.root), "built": self.built,
                "saved": time.time(), "cues": self.cues,
                "files": {name: {"size": e.size, "mtime": e.mtime, "added": e.added, "seen": e.seen,
                                 "tracks": [track_to_dict(t) for t in e.tracks]}
                          for name, e in self.entries.items()}}
        settings.write_file(self.path, json.dumps(data, ensure_ascii=True, separators=(",", ":")))

    def tracks(self) -> list[Track]:
        return [t for e in self.entries.values() for t in e.tracks]

    def seen_times(self) -> dict[Path, float]:
        return {Path(name): e.seen for name, e in self.entries.items()}

    def albums(self) -> list[Album]:
        added = {name: e.added for name, e in self.entries.items()}
        return albums_from(self.tracks(), added, self.root)


def new_albums(index: JsonIndex, since: float, opened: dict[str, float]) -> list[Path]:
    """The old watcher.new_albums: album folders with a file seen after max(since, built), not
    opened since; newest first."""
    floor = max(since, index.built)
    newest: dict[Path, float] = {}
    for path, seen in index.seen_times().items():
        if seen > floor:
            folder = album_folder(path)
            newest[folder] = max(seen, newest.get(folder, 0.0))
    fresh = [(seen, folder) for folder, seen in newest.items() if seen > opened.get(str(folder), 0.0)]
    return [folder for _seen, folder in sorted(fresh, key=lambda pair: (-pair[0], str(pair[1]).lower()))]


def tidy_opened(opened: dict[str, float], index: JsonIndex, since: float) -> dict[str, float]:
    """The old watcher.tidy_opened."""
    folders = {str(album_folder(path)) for path in index.seen_times()}
    return {folder: when for folder, when in opened.items() if folder in folders and when > since}

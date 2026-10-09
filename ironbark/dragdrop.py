"""Drag and drop rules: what happens to things dropped on the player. No UI code here, so the
rules can be tested with plain lists of paths (the window code in ui.py only finds out *where*
the drop landed and carries the plan out).

Dropped on the window (anywhere but a tab):
  - a folder → opens in a new tab, a tab each when several are dropped;
  - loose music files → one new tab holding just those files, in the order dropped;
  - a playlist (.m3u / .m3u8) → opens as a playlist tab;
  - a CUE sheet (.cue) → opens its album: the folder holding the music it describes (see
    `cue_folder`). A one-file rip is listed as the sheet's tracks (library.split_cue_images);
    a folder of separate track files plays those files.
Dropped on a tab → everything is added to the bottom of that tab (a playlist tab saves it into
its .m3u8), the same as the Library's "Add to tab".

Anything else is skipped and counted, for a one-line note in the status line ("2 files skipped —
not music"). Files are judged by name here; a music file whose contents don't match its name is
still read by what it really is, and flagged, exactly as when it's opened any other way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .artwork import first_audio_file
from .cuesheet import read
from .library import is_music_name
from .playlists import EXTENSIONS as PLAYLIST_EXTENSIONS

CUE_EXTENSIONS = {".cue"}


def cue_folder(cue: Path) -> Path:
    """The album folder a dropped .cue belongs to: the folder of the music it names, which is
    usually its own; rippers' leftovers often sit in an "Archive" subfolder with no music beside
    them (and stale file names), so then the folder above, if that holds music."""
    folder = cue.parent
    try:
        names = read(cue).files
    except OSError:
        names = []
    if any((folder / Path(name.replace("\\", "/")).name).is_file() for name in names):
        return folder
    if first_audio_file(folder) is None and folder.parent != folder and _has_music_files(folder.parent):
        return folder.parent
    return folder


def _has_music_files(folder: Path) -> bool:
    """Music files directly in `folder` (not searched below it: the folder above a stray .cue
    could be a whole drive)."""
    try:
        return any(is_music_name(p.name) and p.is_file() for p in folder.iterdir())
    except OSError:
        return False


@dataclass
class DropPlan:
    folders: list[Path] = field(default_factory=list)     # folders with music in them, in drop order
    files: list[Path] = field(default_factory=list)       # loose music files, in drop order
    playlists: list[Path] = field(default_factory=list)   # .m3u / .m3u8 files
    cues: list[Path] = field(default_factory=list)        # .cue sheets
    cue_folders: list[Path] = field(default_factory=list)  # the albums the cues belong to (cue_folder)
    not_music: list[Path] = field(default_factory=list)   # files that aren't music, playlists or cues
    missing: list[Path] = field(default_factory=list)     # gone by the time we looked
    empty: list[Path] = field(default_factory=list)       # folders with no music anywhere inside

    @property
    def nothing(self) -> bool:
        """Nothing usable at all (only skipped things)."""
        return not (self.folders or self.files or self.playlists or self.cues)

    def note(self) -> str:
        """The one-line status note about what was skipped ("" when nothing was)."""
        parts = []
        if self.not_music:
            n = len(self.not_music)
            parts.append(f"{n} file{'' if n == 1 else 's'} skipped — not music")
        if self.empty:
            n = len(self.empty)
            parts.append(f"{n} folder{'' if n == 1 else 's'} skipped — no music inside")
        if self.missing:
            n = len(self.missing)
            parts.append(f"{n} item{'' if n == 1 else 's'} not found")
        return " · ".join(parts)


def plan_drop(paths: list[Path | str], has_music: Callable[[Path], bool] | None = None) -> DropPlan:
    """Sort what was dropped into what the player can open. `has_music(folder)` says whether a
    folder holds any music (default: looks for the first audio file, stopping as soon as it finds
    one). Touches the disk (a dropped .cue is read here to find its album), so call it off the
    window's thread; `actions_for` then needs nothing more from the disk."""
    if has_music is None:
        has_music = lambda folder: first_audio_file(folder) is not None  # noqa: E731
    plan = DropPlan()
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        key = str(path).casefold()
        if key in seen:
            continue  # the same thing dropped twice counts once
        seen.add(key)
        try:
            is_dir, is_file = path.is_dir(), path.is_file()
        except OSError:
            is_dir = is_file = False
        if is_dir:
            (plan.folders if has_music(path) else plan.empty).append(path)
        elif not is_file:
            plan.missing.append(path)
        else:
            suffix = path.suffix.lower()
            if path.name.startswith("._"):  # macOS's companion files: Finder data, whatever the name
                plan.not_music.append(path)
            elif is_music_name(path.name):
                plan.files.append(path)
            elif suffix in PLAYLIST_EXTENSIONS:
                plan.playlists.append(path)
            elif suffix in CUE_EXTENSIONS:
                plan.cues.append(path)
                folder = cue_folder(path)
                if folder not in plan.cue_folders:
                    plan.cue_folders.append(folder)
            else:
                plan.not_music.append(path)
    return plan


@dataclass(frozen=True)
class Action:
    """One step of carrying a drop out. kind:
    "open_folder" (path) · "open_files" (paths, one new tab) · "open_playlist" (path) ·
    "add" (folders, files and playlists' songs go to the bottom of the target tab)."""
    kind: str
    path: Path | None = None
    paths: tuple[Path, ...] = ()
    folders: tuple[Path, ...] = ()
    playlists: tuple[Path, ...] = ()


def actions_for(plan: DropPlan, on_tab: bool) -> list[Action]:
    """What to do with a drop: on the window (on_tab False) or on a tab (True). No disk work:
    safe on the window's thread."""
    folders = plan.folders + [folder for folder in plan.cue_folders if folder not in plan.folders]
    if on_tab:
        if not (folders or plan.files or plan.playlists):
            return []
        return [Action("add", paths=tuple(plan.files), folders=tuple(folders), playlists=tuple(plan.playlists))]
    actions = [Action("open_folder", path=folder) for folder in folders]
    if plan.files:
        actions.append(Action("open_files", paths=tuple(plan.files)))
    actions += [Action("open_playlist", path=p) for p in plan.playlists]
    return actions


def move_item(items: list, source: int, target: int) -> list:
    """A new list with items[source] moved so it ends up *before* what is now at `target`
    (target == len(items) puts it last). Out-of-range or no-op moves return a plain copy.
    This is how dragging a song up or down a playlist reorders it: the drop line sits between
    rows, and `target` is the row below the line."""
    result = list(items)
    if not 0 <= source < len(result) or not 0 <= target <= len(result):
        return result
    item = result.pop(source)
    if target > source:
        target -= 1  # the list closed up behind the item that was lifted out
    result.insert(target, item)
    return result


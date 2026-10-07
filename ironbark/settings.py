"""Remembers a few things between runs (last folder, volume, window position).

The file is never left half-written (see `write_file`), and a bad one never stops the player
starting: a value of the wrong kind is dropped on its own (its default stands); a file that can't
be understood at all is set aside as `settings.json.damaged-<date-time>` and the last good copy,
`settings.json.bak` (taken each time the settings load cleanly), is used instead.

What the player can always make again (the library database) lives apart from the settings, in
`cache_folder()`: on Windows that's the local, non-roaming AppData folder, so it never travels
with a roaming profile or sits on a network share.

The file says which format it's in (`settings_version`). One from an older version of the player is
brought up to date step by step at load (MIGRATIONS); one written by a *newer* version is copied
aside first (`settings.json.v<N>.bak`), because this version will save over it in its own format.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from . import APP_NAME, OLD_APP_NAME

log = logging.getLogger(__name__)

# The settings file's format. Raise it, with a step in MIGRATIONS, whenever a setting changes its
# meaning or shape (renamed, split, a different kind of value): files from before are converted.
SETTINGS_VERSION = 1

DEFAULTS = {
    "settings_version": SETTINGS_VERSION,
    "last_folder": "",
    "last_album_only": False,  # last folder was opened from the Library (just that album)
    "last_mode": "",       # "folder", "album" (from the Library) or "library" (Load all)
    "last_filter": "",     # Library search used for "Load all", if any
    "volume": 0.8,
    "always_on_top": False,
    "mini_on_top": True,         # (0.25.0) the mini strip's own Keep on top (on by default)
    "exclusive": False,
    "output_device": "",  # "" = system default
    "library_folders": [],       # the folders the Library looks in ([] = Windows' own Music folder)
    "library_folder": "",        # (before library_folders) the one library folder: read into library_folders
    "now_playing_geometry": "",  # where the Now Playing window was last left
    "library_geometry": "",      # where (and how big) the Library window was last left
    "tabs": None,                # open tabs: [{"kind": "album"|"folder"|"library", "path", "query", "title"}]
    "current_tab": 0,
    "home_geometry": "",         # the favourite spot "Snap to home" returns to
    "open_at_home": False,       # start up at home rather than where it was last left
    "clip_guard": True,          # turn down recordings that would clip the converter (crackle)
    "clip_headroom": 0.0,        # (0.24.0) "My DAC has headroom": dB above full scale it can take (0 = off)
    "levelling": "off",          # Settings → Sound → Volume levelling: "off", "track", "album" or "smart"
    "levelling_preamp": 0.0,     # …its pre-amp, dB (−6 … +6), added to every levelling gain
    "lyrics_geometry": "",       # the Lyrics window (key L)
    "clip_geometry": "",         # (0.24.0) the Clipping window
    "clip_zoom": "wide",         # its live view: "wide" (50 ms), "mid" (5 ms) or "close" (30 samples)
    "show_art": True,            # the big album cover; off gives the track list the room
    "meters_geometry": "",       # the Meters window: where it was left
    "vu_align": "Auto (per album)",  # what 0 VU stands for: Auto, or a fixed dBFS level
    "vu_auto": None,             # Auto's measured 0 VU per album folder: {folder: dBFS}
    "vu_auto_loudness": None,    # Auto (loudness): {folder: [0 VU dBFS, album LUFS]}
    "scope_geometry": "",        # the Stereo scope window
    "scope_view": "lissajous",   # "lissajous" (the classic goniometer) or "polar" (half circle)
    "ppm_type": "I",             # IEC 60268-10 Type I (DIN) or II (BBC/EBU)
    "pcm_geometry": "",          # the PCM stream window
    "pcm_base": "Decimal",       # Decimal / Hex / Binary
    "pcm_view": "file",          # "file" (the decoded values) or "driver" (the 32-bit words sent)
    "spectrogram_geometry": "",  # the Spectrogram window
    "spectrogram_view": "live",  # "live" (scrolling, as heard) or "track" (the whole file)
    "compare_geometry": "",      # the Compare window
    "abx_geometry": "",          # the ABX blind test window
    "abx_trials": 16,            # trials per ABX test (fixed before it starts)
    "abx_length": 10,            # ABX excerpt length, seconds
    "testsignals_geometry": "",  # the Test signals (hardware test) window
    "testsignals_kind": "tone",  # "tone" (1 kHz: THD+N), "jtest" (jitter) or "sweep" (frequency response)
    "testsignals_level": -20.0,  # test level, dBFS (-20 by default)
    "testsignals_bits": 16,      # the J-test's bit depth (16 = Julian Dunn's original, or 24)
    "testsignals_output": "",    # "" = the player's output
    "testsignals_input": "",     # "" = Windows loopback of the output
    "shuffle": False,
    "repeat": "off",             # "off", "all" (the album / playlist) or "one" (the same song)
    "experiments": None,         # Settings → Experimental switches: {key: bool} (see experiments.py)
    "allow_network": False,      # Settings → Network: rip verification may look up checksums online
    "check_updates": False,      # Settings → Network: once a day, ask whether a newer version is out (updates.py)
    "update_checked": 0.0,       # when that was last asked (seconds since 1970; 0 = never)
    "media_controls": True,      # Settings → Window: media keys, Windows' media panel, taskbar buttons
    "detailed_logging": False,   # Settings → Window: the player's own notes in app.log too, not just problems
    "library_view": "albums",    # the Library's view: albums/artists/genres/years/composers/recent, or a song
                                 # view: songs/most_played/recently_played/favourites/needs_look
    "library_check": "off",      # the whole-library quality check: "off", "on" (keeps new music checked) or "paused"
    "track_columns": [],         # optional track-list columns on show: "artist", "album"
    "wide_track_columns": ["artist", "album", "quality"],   # …in a wide window (each shown only when
                                                            # its value varies between the tab's songs)
    "tags_geometry": "",         # the Edit tags window (Experimental: Tag editing)
    "tag_history_geometry": "",  # the Tag history window
    "help_geometry": "",         # the Keyboard shortcuts sheet (F1)
    "about_geometry": "",        # the About Ironbark window
    "watch_music": True,         # Settings → Library: look for new / changed / removed music every few minutes
    "new_music": None,           # {"since": when watching began, "opened": {album folder: when opened}}
    "theme": "dark",             # Settings → Window → Theme: "dark", "light" or "system" (follow Windows)
    "mini_mode": False,          # the window was last left as the mini strip (it opens that way again)
    "mini_geometry": "",         # where (and how big) the mini strip was last left
    # Network playback (the Experimental "Network playback cache"): songs on network drives play from a copy
    "net_mode": "intelligent",   # "intelligent", "fast" (start after the initial buffer) or "complete"
    "net_buffer": 30,            # the initial buffer, in seconds: 10, 30, 60 or 300
    "net_predownload": "next",   # "off", "next", "next2" or "queue": how far ahead songs download while one plays
    "net_keep": "limit",         # "none", "session" (until playback finishes), "day" or "limit" (until full)
    "net_cache_gb": 10,          # the most the cache holds, in GB (0 = no limit)
    "net_verify": True,          # check each finished download (a FLAC's own MD5 fingerprint too)
    "geometry": "",
    "maximised": False,          # the full window was maximised when last left: it opens maximised
}


def _base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def settings_path() -> Path:
    return _standard_settings_path()


def _standard_settings_path() -> Path:
    return _base() / APP_NAME / "settings.json"


def _cache_base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def cache_folder() -> Path:
    """Where the player keeps what it can always make again (the library database): Windows'
    local AppData folder (%LOCALAPPDATA%\\Ironbark), ~/Library/Caches/Ironbark on a Mac. When
    the settings have been pointed somewhere else (the self-check's empty folder, a test's), the
    cache goes with them, so nothing of the user's is touched."""
    here = settings_path()
    if here != _standard_settings_path():
        return here.parent
    return _cache_base() / APP_NAME


# What went wrong with the settings file this session ("" = nothing): set when a damaged file
# was set aside, for the window to mention once.
problem = ""


def write_file(path: Path, text: str, errors: str = "strict") -> None:
    """Write a text file (UTF-8) so that a crash or a power cut leaves the old file or the new one,
    never part of one: written beside it under a name no other writer is using, flushed to the
    disk, then swapped in. Raises OSError, or ValueError for text that can't be encoded."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", errors=errors) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def carry_over_old_folder(base: Path | None = None) -> bool:
    """(0.26.0) The player was renamed DG FLAC Player → Ironbark. On the first start under the new
    name, copy the old folder (settings, playlists, library index, tag history, ABX logs, crash
    log) to the new one, and point any paths inside settings.json that led into the old folder at
    the new one. The old folder is left exactly as it was, as a backup. Returns whether it copied.
    The copy is made under a temporary name and only takes the new name once it's complete; if
    anything can't be copied, nothing is kept, the player starts with its defaults, and the next
    start tries again."""
    base = base or _base()
    old, new = base / OLD_APP_NAME, base / APP_NAME
    if new.exists() or not old.is_dir():
        return False
    partial = base / f"{APP_NAME}.partial"
    try:
        shutil.rmtree(partial, ignore_errors=True)  # what an earlier try left behind
        shutil.copytree(old, partial)
        _point_paths_at(partial / "settings.json", old, new)
        os.replace(partial, new)
    except OSError:  # (shutil.Error is one: a file that couldn't be copied)
        shutil.rmtree(partial, ignore_errors=True)
        return False
    return True


def _point_paths_at(path: Path, old: Path, new: Path) -> None:
    """Paths inside a settings file that led into the folder `old` now lead into `new`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return

    def moved(value):
        if isinstance(value, str):
            for a, b in ((str(old), str(new)), (old.as_posix(), new.as_posix())):
                value = value.replace(a, b)
            return value
        if isinstance(value, list):
            return [moved(v) for v in value]
        if isinstance(value, dict):
            return {k: moved(v) for k, v in value.items()}
        return value

    write_file(path, json.dumps(moved(data), indent=2))


def load() -> dict:
    """The saved settings over the defaults (a bad file, or one from another version: see the
    module notes)."""
    path = settings_path()
    data = dict(DEFAULTS)
    try:
        saved, text = _read(path)
    except OSError:
        return data
    except ValueError:
        saved = _recover(path)
    else:
        _keep_backup(path, text)
    if saved is not None:
        data.update(_checked(_migrated(saved, path)))
    if not data["library_folders"] and data["library_folder"]:
        data["library_folders"] = [data["library_folder"]]   # (saved before there could be several)
    return data


def save(data: dict) -> bool:
    """Write the settings. Returns False when the write failed (the file briefly locked, a full
    disk), so the caller can try again later; losing preferences is not worth crashing over."""
    try:
        write_file(settings_path(), json.dumps(data, indent=2))
    except (OSError, ValueError):
        return False
    return True


def _read(path: Path) -> tuple[dict, str]:
    """A settings file's values and its text. OSError when it can't be read (or isn't there);
    ValueError when it's damaged: not JSON, or not a JSON object."""
    text = path.read_text(encoding="utf-8-sig")  # (Notepad may have added a byte-order mark)
    try:
        saved = json.loads(text)
    except RecursionError as error:  # brackets nested thousands deep: damage, not settings
        raise ValueError("nested too deeply") from error
    if not isinstance(saved, dict):
        raise ValueError("not a JSON object")
    return saved, text


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _keep_backup(path: Path, text: str) -> None:
    """The settings just loaded cleanly: keep them as the last good copy."""
    backup = _backup_path(path)
    try:
        same = backup.read_text(encoding="utf-8") == text
    except (OSError, ValueError):
        same = False
    if not same:
        try:
            write_file(backup, text)
        except (OSError, ValueError):
            pass


def _recover(path: Path) -> dict | None:
    """A settings file that can't be understood: set it aside under a new name (never written
    over), and go back to the last good copy if there is one — put back in its place, so the
    next load (and the next save) starts from it. None when there's no good copy."""
    global problem
    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside, n = path.with_name(f"{path.name}.damaged-{stamp}"), 2
    while aside.exists():
        aside, n = path.with_name(f"{path.name}.damaged-{stamp}-{n}"), n + 1
    try:
        os.replace(path, aside)
    except OSError:
        try:
            shutil.copyfile(path, aside)
        except OSError:
            aside = None
    try:
        saved, text = _read(_backup_path(path))
    except (OSError, ValueError):
        saved = None
    else:
        try:
            write_file(path, text)
        except (OSError, ValueError):
            pass
    kept = f" It was kept as {aside.name}." if aside is not None else ""
    problem = (f"The settings file was damaged.{kept} "
               + ("The last good copy is in use." if saved is not None else "The player started with its "
                                                                             "standard settings."))
    return saved


# -- the file's format ---------------------------------------------------------------------------------
def _to_1(saved: dict) -> dict:
    """Format 1, the first numbered one: the same settings as before; only the number is new."""
    return saved


# {format: the step from it to the next}. A step takes the saved values (a dict of its own) and
# returns them in the next format; it runs on files straight from disk, so it must cope with values
# of any kind (they're checked against DEFAULTS afterwards).
MIGRATIONS = {0: _to_1}


def _version_of(saved: dict) -> int:
    version = saved.get("settings_version", 0)
    return version if isinstance(version, int) and not isinstance(version, bool) and version >= 0 else 0


def _migrated(saved: dict, path: Path) -> dict:
    """The saved values in this version's format. A file from a newer version is copied aside first
    (it's about to be saved over in this version's format); its settings this version knows are used
    as they are, and the ones it doesn't are kept."""
    global problem
    version = _version_of(saved)
    if version > SETTINGS_VERSION:
        kept = _keep_newer_copy(path, version)
        if kept is not None and not problem:
            problem = (f"These settings were saved by a newer version of {APP_NAME}. A copy was kept as "
                       f"{kept.name} before this version saves over them.")
    while version < SETTINGS_VERSION:
        step = MIGRATIONS.get(version)
        if step is not None:
            try:
                saved = step(dict(saved))
            except Exception:  # noqa: BLE001 — a step that fails leaves the values as they were
                log.exception("settings: converting from format %d failed", version)
                break
        version += 1
    return {**saved, "settings_version": SETTINGS_VERSION}


def _keep_newer_copy(path: Path, version: int) -> Path | None:
    """Copy a newer version's settings file aside (settings.json.v<N>.bak, the latest such copy)."""
    copy = path.with_name(f"{path.name}.v{version}.bak")
    try:
        write_file(copy, path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        log.warning("settings: couldn't keep a copy of a newer version's settings", exc_info=True)
        return None
    return copy


# -- the kind of value each setting holds --------------------------------------------------------------
_BAD = object()


def _number(value) -> bool:
    """A finite number (not true / false, which Python also counts as numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # a whole number too big to be a decimal one
        return False


def _same_kind(value, default):
    """`value` if it's the kind of value `default` is (a whole number does for a decimal one),
    else _BAD. A setting whose default has no fixed kind is taken as it is."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else _BAD
    if isinstance(default, int):
        return value if isinstance(value, int) and not isinstance(value, bool) else _BAD
    if isinstance(default, float):
        return value if _number(value) else _BAD
    if isinstance(default, str):
        return value if isinstance(value, str) else _BAD
    return value


def _one_of(*allowed):
    """A check: the value must be one of these exactly (true / false never pass for a number)."""
    def check(value):
        if isinstance(value, bool) or value not in allowed:
            return _BAD
        return value
    return check


def _tabs(value):
    """Open tabs: each must say its kind and path as text. A tab that doesn't is dropped on its
    own; a resume point, search or extras of the wrong kind are dropped from their tab."""
    if value is None:
        return None
    if not isinstance(value, list):
        return _BAD
    tabs = []
    for tab in value:
        if not (isinstance(tab, dict) and isinstance(tab.get("kind"), str) and isinstance(tab.get("path"), str)):
            continue
        tab = dict(tab)
        if not isinstance(tab.get("query", ""), str):
            del tab["query"]
        if not isinstance(tab.get("extras", []), list):
            del tab["extras"]
        resume = tab.get("resume")
        if resume is not None and not (isinstance(resume, dict) and isinstance(resume.get("path", ""), str)
                                       and _number(resume.get("at", 0))):
            del tab["resume"]
        tabs.append(tab)
    return tabs


def _dict_or_none(value):
    return value if value is None or isinstance(value, dict) else _BAD


_CHECKS = {
    "tabs": _tabs,
    "experiments": _dict_or_none,
    "new_music": _dict_or_none,
    "vu_auto": _dict_or_none,
    "vu_auto_loudness": _dict_or_none,
    "track_columns": lambda value: [v for v in value if isinstance(v, str)] if isinstance(value, list) else _BAD,
    "wide_track_columns": lambda value: ([v for v in value if isinstance(v, str)] if isinstance(value, list)
                                         else _BAD),
    "library_folders": lambda value: ([v for v in value if isinstance(v, str) and v] if isinstance(value, list)
                                      else _BAD),
    "net_mode": _one_of("intelligent", "fast", "complete"),
    "net_buffer": _one_of(10, 30, 60, 300),
    "net_predownload": _one_of("off", "next", "next2", "queue"),
    "net_keep": _one_of("none", "session", "day", "limit"),
    "net_cache_gb": _one_of(1, 5, 10, 25, 50, 0),
    "library_check": lambda value: value if value in ("off", "on", "paused") else _BAD,
}


def _checked(saved: dict) -> dict:
    """The saved values that can be used. One of the wrong kind (a hand edit, a sync tool, another
    version of the player) is left out, so its default stands; settings this version doesn't know
    (written by a newer one) are kept as they are."""
    good = {}
    for key, value in saved.items():
        if key in DEFAULTS:
            value = _CHECKS[key](value) if key in _CHECKS else _same_kind(value, DEFAULTS[key])
        if value is not _BAD:
            good[key] = value
    return good

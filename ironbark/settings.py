"""Remembers a few things between runs (last folder, volume, window position)."""

from __future__ import annotations

import json
import shutil
import os
import sys
from pathlib import Path

from . import APP_NAME, OLD_APP_NAME

DEFAULTS = {
    "last_folder": "",
    "last_album_only": False,  # last folder was opened from the Library (just that album)
    "last_mode": "",       # "folder", "album" (from the Library) or "library" (Load all)
    "last_filter": "",     # Library search used for "Load all", if any
    "volume": 0.8,
    "always_on_top": False,
    "mini_on_top": True,         # (0.25.0) the mini strip's own Keep on top (on by default)
    "exclusive": False,
    "output_device": "",  # "" = system default
    "library_folder": "",  # "" = the Music folder in your home folder
    "now_playing_geometry": "",  # where the Now Playing window was last left
    "library_geometry": "",      # where (and how big) the Library window was last left
    "tabs": None,                # open tabs: [{"kind": "album"|"folder"|"library", "path", "query", "title"}]
    "current_tab": 0,
    "home_geometry": "",         # the favourite spot "Snap to home" returns to
    "open_at_home": False,       # start up at home rather than where it was last left
    "clip_guard": True,          # turn down recordings that would clip the converter (crackle)
    "clip_headroom": 0.0,        # (0.24.0) "My DAC has headroom": dB above full scale it can take (0 = off)
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
    "media_controls": True,      # Settings → Window: media keys, Windows' media panel, taskbar buttons
    "library_view": "albums",    # the Library's browse view: albums/artists/genres/years/composers/recent
    "track_columns": [],         # optional track-list columns on show: "artist", "album"
    "tags_geometry": "",         # the Edit tags window (Experimental: Tag editing)
    "tag_history_geometry": "",  # the Tag history window
    "watch_music": True,         # Settings → Library: look for new / changed / removed music every few minutes
    "new_music": None,           # {"since": when watching began, "opened": {album folder: when opened}}
    "theme": "dark",             # Settings → Window → Theme: "dark", "light" or "system" (follow Windows)
    "mini_mode": False,          # the window was last left as the mini strip (it opens that way again)
    "mini_geometry": "",         # where (and how big) the mini strip was last left
    "geometry": "",
}


def _base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def settings_path() -> Path:
    return _base() / APP_NAME / "settings.json"


def carry_over_old_folder(base: Path | None = None) -> bool:
    """(0.26.0) The player was renamed DG FLAC Player → Ironbark. On the first start under the new
    name, copy the old folder (settings, playlists, library index, tag history, ABX logs, crash
    log) to the new one, and point any paths inside settings.json that led into the old folder at
    the new one. The old folder is left exactly as it was, as a backup. Returns whether it copied."""
    base = base or _base()
    old, new = base / OLD_APP_NAME, base / APP_NAME
    if new.exists() or not old.is_dir():
        return False
    shutil.copytree(old, new)
    path = new / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True

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

    path.write_text(json.dumps(moved(data), indent=2), encoding="utf-8")
    return True


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(settings_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return data


def save(data: dict) -> None:
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # losing preferences is not worth crashing over

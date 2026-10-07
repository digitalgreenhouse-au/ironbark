"""The player window. Tabs run along the top where a title bar would be (one tab per
album, folder or library view), then what's playing, the transport, and the open tab's
track list. Output, bit-perfect, volume and the rest live under the gear; the home
button snaps the window back to a favourite spot.

Also here: drag and drop (folders, music files and playlists from Explorer; songs and Library
albums inside the player — the rules are in dragdrop.py) and the keyboard's media keys / Windows'
media panel / taskbar buttons (mediakeys.py).

Tag editing (0.16.0, Experimental — no menu, key or button for it while its switch is off):
right-click songs → Edit tags… (key E), or an album in the Library; Tag history… undoes. The
careful saving is tag_editor.py; a song that's playing waits until the player moves on (`_poll`).

Up next: right-click songs (or an album in the Library) → Play next / Add to queue; they play
before the tab carries on (engine.Player keeps the list, so they join on gaplessly). An
"Up next (n)" line above the status line opens the list. Albums and tabs are read through the
library database (library_index.py) when it's up to date, so nothing is read twice.

The library (0.31): Settings → Library lists the library folders (Add folder… / Remove, or
Change… while there's one). Nothing that reads the database runs on this thread: the start-up
look, the new albums and the Library window's album list are worked out on workers, and their
answers picked up by `_poll` (or the window's own loop). A song listened to for long enough is
counted as a play (`_count_listening`), stored from a worker.

Light / Dark (0.19.0): Settings → Window → Theme (Dark, Light, Follow Windows — Dark by default);
theme.use re-colours every open window at once. Mini mode (0.19.0): the same window shrunk to a
small strip (mini_view.py) — the Mini player button in Settings, key Z, or double-clicking the
cover; each of the two remembers its own position and size, and the music carries on.

The look (0.20.0): the seek bar, play button, round-hover transport buttons and the volume pill are
drawn controls (controls.py); the cover has softly rounded corners; with no tabs open the window
shows an "Open your library" button (Ctrl+L) instead of the empty layout.

Pop-ups (0.21.0): every question box is an in-app prompt and every right-click menu a
popups.PopupMenu (popups.py), so they follow Dark / Light; a prompt leaves the mini strip first
(`_for_prompt`). Settings scrolls when it's taller than the window (tabbar.Popover(scroll=True)).

Opening from outside: files, folders, playlists and CUE sheets named on the command line, or handed
over by a later launch (instance.py — "Open with", a double-click on a file while the player is
open) are opened like a drop on the window (`open_paths`); a file asked for this way plays at once.
Also: Settings → Help → Save diagnostics… (diagnostics.py), Detailed logging (logs.py) and Check for
updates (updates.py); Python's cycle collector runs on this thread (tkgc.py).

The keyboard: every shortcut is in one table (keys.py) — `_bind_keys` says what each does here, and
every window the player opens is attached to it (`_with_keys`), so the keys work wherever the
keyboard is. F1 (or ?) lists them all (help_window.py); Settings → Help has that list and About.
A playlist change (Delete, a move, Up next cleared) can be undone once: Ctrl+Z, or Undo in the
status line.

Wide windows (layout.py): from 780–880 logical px wide the window splits in two — the cover, the
now-playing text, the seek bar and the transport stacked on the left, the track list the window's full
height on the right — with larger type from 1280 and 1920. The same widgets are packed into other
frames (`_apply_layout`), never rebuilt; the narrow window is laid out exactly as before. A wide window
shows the cover at its own size at most (a smaller one on a mat), a caption over the title, a count in
the list's header row, and a maximise button on the strip. F11 is full screen on any size (borderless,
on the same window: chrome.enter_fullscreen); F11, Esc, double-clicking the strip or Z leave it.

Volume levelling (Settings → Sound, off by default; levelling.py): each song's gain is worked out
ahead of play beside the clipping guard's (`_apply_levelling`) and handed to the player per file.
Stop after the current song and the sleep timer (sleep.py): in the track menu and the Up next list,
and Ctrl+. / Ctrl+Shift+S, for this session only (`stop_after_current`, `set_sleep_timer`).
Lyrics: key L (lyrics_window.py).

The whole-library quality check (library_check.py, opt-in from the Library's Needs a look) runs on
its own thread and worker processes; it carries on CHECK_AFTER_MS after start-up when it was left
on, looks again when the watcher finds changed music, holds off while a blind test or test signals
run, and stops with the player. Its stored results serve Now Playing's Check and the clipping
guard's true peaks (`_stored_peak`)."""

from __future__ import annotations

import functools
import json
import logging
import math
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter import font as tkfont
from typing import Callable

from PIL import Image, ImageTk

from . import (APP_NAME, __version__, appicon, artwork, audiofile, browse, chrome, controls, devices, diagnostics,
               dragdrop, experiments, keys, layout, levelling, library_check, library_db, library_index,
               library_tracks, logs, loudness, mediakeys, placement, playlists, popups, quick_search, recycle,
               settings, shapes, sleep, tag_editor, theme, tkgc, updates, watcher)
from .abx_window import AbxWindow
from .analysis import GUARD_HEADROOM_CHOICES, guard_trim, headroom_index, headroom_label
from .compare_window import CompareWindow
from .testsignals_window import TestSignalsWindow
from .details import (COPY_FAILED, NowPlayingWindow, cache_mark, copy_note, network_hover, network_line,
                      network_now_line, output_line)
from .help_window import AboutWindow, HelpWindow
from .engine import Player, State
from .favourites import Favourites
from .library import (Track, album_folder, cue_track_path, find_albums, read_track, scan_album, scan_folder,
                      title_from_filename)
from .library_window import LibraryWindow, adding_folder
from .lyrics_window import LyricsWindow
from .meter_window import MeterWindow
from .meters import album_files
from .mini_view import MIN_SIZE as MINI_MIN_SIZE, DEFAULT_SIZE as MINI_DEFAULT_SIZE, MiniView, click_or_double
from .pcm_window import PcmWindow
from .clip_window import ClipWindow
from .scope_window import ScopeWindow
from .spectrogram_window import SpectrogramWindow
from .tag_window import TagHistoryWindow, TagWindow
from .playlist_view import PlaylistView, _fold
from .quick_search import QuickSearch
from .tabbar import MenuRow, Popover, SettingRow, TabBar, Tooltip, glyph

log = logging.getLogger(__name__)

POLL_MS = 250
HEARD_STEP_S = 2.0  # a move further than this between polls is a seek, not listening


MEDIA_POLL_MS = 60   # how often presses from media keys / Windows' media panel are picked up
AUTOSAVE_EVERY = 8   # polls, i.e. settings are saved within ~2 s of a change
SAFE_VOLUME = 0.3    # player volume when bit-perfect is switched on without a device volume control
SEEK_STEP = 5.0
LONG_SEEK_STEP = 30.0  # Shift+← / →
VOLUME_STEP = 0.05   # per mouse-wheel notch over the transport, and per Ctrl+↑ / ↓
UNDO_NOTE_MS = 10000  # how long "Removed … · Undo" stays in the status line (Ctrl+Z works until the next change)
WHEEL_NOTCH = 120 if sys.platform == "win32" else 1   # Tk's wheel delta for one notch (Windows: 120)
NETWORK_CACHE = "network_cache"   # the Experimental switch (experiments.py)
ALBUM_AGAIN_S = 30.0   # a network album not all downloaded yet: its loudness is measured again after this long
SLOW_LOOK_S = 2.0      # a playlist's song looked for this long and not found: its drive or server isn't answering
# Settings -> Network playback (the network playback cache): (saved value, what the choice says)
NET_MODES = (("intelligent", "Intelligent"), ("fast", "Fast start"), ("complete", "Complete track"))
NET_BUFFERS = ((10, "10 seconds"), (30, "30 seconds"), (60, "1 minute"), (300, "5 minutes"))
NET_PREDOWNLOADS = (("off", "Off"), ("next", "Next track"), ("next2", "Next 2 tracks"), ("queue", "Entire queue"))
NET_KEEPS = (("none", "Don't keep"), ("session", "Until playback finishes"), ("day", "For 1 day"),
             ("limit", "Until the cache is full"))
NET_SIZES = ((1, "1 GB"), (5, "5 GB"), (10, "10 GB"), (25, "25 GB"), (50, "50 GB"), (0, "Unlimited"))
QUIT_WAIT_MS = 30000  # longest closing waits for a tag save to stop after the file in hand
MIN_LIST_HEIGHT = 190  # px (at 100% scaling) the track list keeps, however big the cover wants to be
INDEX_AFTER_MS = 3000  # the library index is brought up to date this long after start-up
CHECK_AFTER_MS = 60000  # a library quality check left on carries on this long after start-up
FAVOURITES_WAIT_S = 6.0  # longest closing waits for a ♥ to be stored (the database can be busy for 5 s)
NEW_MUSIC_NOTE_MS = 12000  # how long "New music: …" stays in the status line
UP_NEXT_SHOWN = 12     # songs listed in the Up next pop-over (the rest are counted)
UP_NEXT_CHARS = 44     # an Up next row's words are cut with "…" past this many characters…
UP_NEXT_MARK_CHARS = 8 # …this many fewer while a column of network-download marks shares the row
THEME_POLL_MS = 2000   # Theme: Follow Windows looks at Windows' light/dark setting this often
FULL_MIN_SIZE = (300, 260)  # px at 100%: the full window's smallest size (the mini strip has its own)
SETTINGS_WIDTH = 310       # px at 100%: the Settings panel (less in a narrow window)
SETTINGS_CHOICE_W = 120    # px at 100%: Settings' drop-downs beside their names (their left edges line up)
DROP_GAP = 4               # px at 100%: a pop-over from the strip opens this far below it
HEAD_ICON_SLOT = 24        # px at 100%: the lock's and ⓘ's slots (and round hover discs) beside the format line
INFO_INK_INSET = 6         # px at 100%: the ⓘ glyph's ink stops this far inside its slot (measured from Segoe
                           # Fluent Icons / MDL2 Assets at 10 pt), so the slot reaches this far into the margin
STATUS_DOT = 8             # px at 100%: the status dot
UPDATE_FIRST_MS = 30000    # Check for updates: the first look this long after start-up…
UPDATE_EVERY_MS = 3600000  # …then this often (updates.due decides whether a day has passed)
UPDATE_NOTE_MS = 30000     # how long "Ironbark X.Y.Z is available" stays in the status line
COVER_SETTLE_MS = 120      # while the window is resized, the cover is fetched afresh once it pauses this long

# The now-playing text in each layout (layout.py): larger in the wider windows.
TITLE_STYLES = {layout.COLUMN: "Display.TLabel", layout.SPLIT: "Display.TLabel",
                layout.SPLIT_M: "DisplayWide.TLabel", layout.SPLIT_L: "DisplayLarge.TLabel"}
SUB_STYLES = {layout.COLUMN: "Subtitle.TLabel", layout.SPLIT: "Subtitle.TLabel",
              layout.SPLIT_M: "SubtitleLarge.TLabel", layout.SPLIT_L: "SubtitleLarge.TLabel"}
INFO_STYLES = {layout.COLUMN: "Info.TLabel", layout.SPLIT: "Info.TLabel", layout.SPLIT_M: "Info.TLabel",
               layout.SPLIT_L: "InfoLarge.TLabel"}
# The caption over the title in a wide window: what the top of the window is showing.
NOW_PLAYING = "NOW PLAYING"
CAPTIONS = {"album": "ALBUM", "folder": "FOLDER", "library": "LIBRARY", "playlist": "PLAYLIST", "files": "FILES"}
KIND_NAMES = {"album": "Album", "folder": "Folder", "library": "Library", "playlist": "Playlist", "files": "Files"}


def _layout_scale() -> float:
    """Windows' display scaling for working out the layout: theme.scale() to the nearest percent
    (Tk measures 96.05 dots an inch at 100%, which would put a 1920 px window just under 1920)."""
    return round(theme.scale(), 2)


def plural(count: int, word: str) -> str:
    """ "1 song", "13 songs"."""
    return f"{count} {word}{'' if count == 1 else 's'}"


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:  # a double album: 2:02:36, not 122:36
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def volume_words(volume: float) -> str:
    """The volume as Settings and the status line say it: "100% · untouched", "75% · −5.0 dB" (the
    change in level: the slider is squared, see engine.Player.set_volume), "0% · muted"."""
    gain = volume ** 2
    if gain >= 1.0:
        return "100% · untouched"
    if gain <= 0:
        return "0% · muted"
    return theme.figures(f"{volume:.0%} · {20 * math.log10(gain):+.1f} dB")


def _known_music_folder() -> Path | None:
    """Windows' own Music folder (SHGetKnownFolderPath, FOLDERID_Music): it may have been moved
    (Properties → Location, e.g. to D:\\Music) or redirected by backup or policy. None elsewhere,
    or if Windows can't say."""
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes as wt
    import uuid

    try:
        # Private handles, so setting their types can't change anyone else's calls.
        shell32, ole32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("ole32")
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        shell32.SHGetKnownFolderPath.argtypes = (ctypes.c_char_p, wt.DWORD, wt.HANDLE, ctypes.POINTER(ctypes.c_void_p))
        ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
        music = uuid.UUID("{4BD8D571-6D19-48D3-BE97-422220080E43}").bytes_le   # FOLDERID_Music
        found = ctypes.c_void_p()
        try:
            if shell32.SHGetKnownFolderPath(music, 0, None, ctypes.byref(found)) == 0 and found.value:
                return Path(ctypes.wstring_at(found.value))
        finally:
            ole32.CoTaskMemFree(found)   # (Windows allocates the text even when it fails)
    except (OSError, AttributeError, ValueError):
        log.debug("Windows' Music folder couldn't be looked up", exc_info=True)
    return None


@functools.cache
def default_music_folder() -> Path:
    """The library folder until one is chosen: the system's Music folder, else ~/Music."""
    return _known_music_folder() or Path.home() / "Music"


def library_folders(prefs: dict) -> list[Path]:
    """The library folders in the settings (`library_folders`; before there could be several,
    `library_folder`), else the system's Music folder. Each once, and none inside another
    (library_db.outermost: its albums would be listed twice)."""
    folders = prefs.get("library_folders") or ([prefs["library_folder"]] if prefs.get("library_folder") else [])
    found = library_db.outermost(folder for folder in folders if isinstance(folder, str) and folder)
    return found or [default_music_folder()]


def _refresh_library(index: library_index.LibraryIndex) -> None:
    """Bring the library database up to date (a worker thread's work: nothing of the window's)."""
    try:
        index.refresh()
    except Exception:
        log.exception("couldn't bring the library up to date")
    finally:
        index.db.release()


def _find_new_albums(index: library_index.LibraryIndex, since: float, results: queue.SimpleQueue) -> None:
    """The new albums, worked out on a worker thread (it reads the database) and handed to
    _watch_reports as a report with nothing changed."""
    try:
        results.put(watcher.Report(new=index.new_album_times(since), album_folders=frozenset(index.album_folders())))
    except Exception as error:
        index.db.note_damage(error)   # (a damaged file is put right at the next start)
        log.exception("couldn't work out the new albums")
    finally:
        index.db.release()


_plays_stored = [0]   # plays stored this session (bumped by the worker that stores one)


def _keys_held(tracks, keys: list[str]) -> set[str]:
    """Which of these songs (play keys: audiofile.key) a tab's list holds. A list read from the
    library database a page at a time asks the database rather than reading every song."""
    finder = getattr(tracks, "keys_in", None)
    if finder is not None:
        return finder(keys)
    have = {audiofile.key(t.path) for t in tracks}
    return {key for key in keys if key in have}


def _index_of_key(tracks, key: str) -> int:
    """Where the song with this play key is in a tab's list (-1: not there)."""
    finder = getattr(tracks, "index_of_key", None)
    if finder is not None:
        return finder(key)
    return next((i for i, t in enumerate(tracks) if audiofile.key(t.path) == key), -1)


def _held_in_memory(tracks) -> list[Track]:
    """A tab's songs as Track objects held now (a list read a page at a time: the songs read)."""
    loaded = getattr(tracks, "loaded", None)
    return loaded() if loaded is not None else list(tracks or [])


def _tracks_at(tracks, rows: list[int]) -> list[Track]:
    """These songs of a tab's list (a list from the library database reads them in one go)."""
    many = getattr(tracks, "many", None)
    return many(rows) if many is not None else [tracks[i] for i in rows]


def _store_play(index: library_index.LibraryIndex, key: str, heard: float, duration: float) -> None:

    """Count a play (LibraryDB.record_play) on a worker thread."""
    try:
        if index.db.record_play(key, heard, duration):
            _plays_stored[0] += 1   # (the window's poll sees it: the Plays columns read again)
    except Exception as error:
        index.db.note_damage(error)
        log.exception("couldn't count a play of %s", key)
    finally:
        index.db.release()


@dataclass
class _Listening:
    """The song playing now, and how much of it has been heard (seeks aren't counted)."""
    key: str
    duration: float
    position: float
    heard: float = 0.0


class _Unwanted:
    """A measuring worker's cancel flag: set once the song it measures isn't wanted any more."""

    def __init__(self, test: Callable[[], bool]):
        self.is_set = test


def _queued_alone(playing_tab: int | None, status) -> bool:
    """A song from Up next playing (or paused) with no tab behind it — queued before anything had
    played, or after its tab was closed. The window then treats it as a playing tab's song."""
    return playing_tab is None and status.track is not None and status.state is not State.STOPPED


@dataclass
class Tab:
    kind: str                         # "album" (from the Library), "folder" (everything under it), "library",
                                      # "playlist" (an .m3u8 file; path is the file) or "files" (music files
                                      # dropped from Explorer: they're its extras; path = the first one's folder)
    path: str
    query: str = ""                   # Library search, for a "library" tab
    tracks: list[Track] | None = None  # None until loaded
    loading: bool = False
    album_name: str = ""               # a one-album tab's name from the tags, looked up in the background
    artist: str = ""
    first_album: str = ""              # for a tab of many albums: the first one's name
    first_file: Path | None = None     # stands in for the album (its cover) before the tab is loaded
    info: tuple | None = None          # (album name, artist, first file) once looked up
    info_requested: bool = False
    last_index: int = -1               # the track this tab last played
    resume_at: float = 0.0             # how far into it (seconds), to carry on from
    resume_path: str = ""              # that track's file, to find it again after a restart
    extras: list[str] = field(default_factory=list)  # album folders (Library, drops) or single files added, played after
    play_when_loaded: bool = False     # play was pressed while it was loading
    unavailable: bool = False          # its folder / file wasn't there at the last load (a drive not connected)
    load_progress: dict | None = field(default=None, repr=False, compare=False)  # the load under way
    summary: tuple | None = field(default=None, repr=False, compare=False)  # (tracks, key, text) of the album line
    numbers: tuple | None = field(default=None, repr=False, compare=False)  # (tracks, count, (songs, albums, seconds))

    def view_track(self) -> Track | None:
        """The track that represents this tab: the one it last played, else its first."""
        if not self.tracks:
            return None
        return self.tracks[self.last_index] if 0 <= self.last_index < len(self.tracks) else self.tracks[0]

    @property
    def title(self) -> str:
        if self.kind == "playlist":
            return Path(self.path).stem
        if self.album_name:
            return self.album_name  # "Madvillainy", not "Madvillain-Madvillainy (2004) FLAC"
        if self.kind == "library" and self.last_index < 0:
            return f"Library: {self.query}" if self.query else "Library"
        track = self.view_track()
        if track and track.album:
            return track.album  # a tab of many albums is named after the one it's on
        return self.first_album or Path(self.path).name or self.path

    def source(self) -> dict:
        saved = {"kind": self.kind, "path": self.path, "query": self.query}
        if self.extras:
            saved["extras"] = list(self.extras)
        track =self.view_track() if self.last_index >= 0 else None
        if track is not None:
            # Whole 5-second steps, so settings aren't rewritten every moment a song plays.
            saved["resume"] = {"path": audiofile.key(track.path), "at": int(self.resume_at // 5 * 5)}
        elif self.resume_path:
            saved["resume"] = {"path": self.resume_path, "at": int(self.resume_at)}
        return saved

    def same(self, kind: str, path, query: str = "") -> bool:
        return self.kind == kind and Path(self.path) == Path(path) and self.query == query


def tab_numbers(tab: Tab) -> tuple[int, int, float]:
    """(songs, albums, seconds) in a tab — worked out once per change of its list (a list read from the
    library database a page at a time knows them already)."""
    tracks = tab.tracks or []
    if tab.numbers is not None and tab.numbers[0] is tab.tracks and tab.numbers[1] == len(tracks):
        return tab.numbers[2]
    if isinstance(tracks, library_tracks.LibraryTrackList):
        numbers = (len(tracks), len(tracks.groups), tracks.seconds)
    else:
        numbers = (len(tracks), len({album_folder(t.path) for t in tracks}), sum(t.duration for t in tracks))
    tab.numbers = (tab.tracks, len(tracks), numbers)
    return numbers


class PlayerWindow:
    def __init__(self, root: tk.Tk, player: Player, folder: str | None = None, paths: list[str] | None = None,
                 requests: queue.Queue | None = None, on_close: Callable[[], None] | None = None):
        """folder: an album folder to open and play (as before); paths: files / folders / playlists /
        CUE sheets named on the command line (`open_paths`); requests: where later launches' requests
        arrive (instance.Instance.requests), taken by `_poll`; on_close: called as soon as closing
        starts (instance.Instance.stop_listening: from then on a later launch waits for this one to
        go, rather than handing its files to a window that's closing)."""
        self.root = root
        self.player = player
        self.prefs = settings.load()
        self._requests = requests
        self._on_close = on_close
        self._seek_target_playing = True  # does the seek bar show the playing song? (set by _poll)
        self._shown_key = None
        self._now_playing: NowPlayingWindow | None = None
        self._meters: MeterWindow | None = None
        self._pcm: PcmWindow | None = None
        self._clip: ClipWindow | None = None
        self._spectrogram: SpectrogramWindow | None = None
        self._scope: ScopeWindow | None = None
        self._compare: CompareWindow | None = None
        self._abx: AbxWindow | None = None
        self._testsignals: TestSignalsWindow | None = None
        self._library: LibraryWindow | None = None
        self._quick: QuickSearch | None = None       # the song search panel (Ctrl+K), made when first opened
        # Favourite songs (♥): read from the library database on a worker, then kept here.
        self.favourites = Favourites(lambda: self._index().db)
        self.favourites.changed.append(self._favourites_changed)
        # The whole-library quality check: off until chosen in the Library; the choice is kept.
        self.checker = library_check.Checker(lambda: self._index().db, state=self.prefs.get("library_check", "off"),
                                             on_state=lambda state: self.prefs.update(library_check=state))
        self._tag_window: TagWindow | None = None
        self._tag_history: TagHistoryWindow | None = None
        self._help: HelpWindow | None = None
        self._about: AboutWindow | None = None
        # The one playlist change Ctrl+Z can undo: (notice, what undoing says, undo, still possible?)
        self._undo: tuple | None = None
        # The volume before Ctrl+Shift+↓ muted it, and what it was heard through (_ear_setup); None
        # once the level has been changed any other way.
        self._muted_from: tuple[float, tuple] | None = None
        self._tag_waiting: list[tag_editor.TagSave] = []  # saves with songs waiting for the player to move on
        self._index_updated = False  # set by the thread that re-reads edited files into the index
        self._notice = ""            # a UI message that overrides the player's for a while
        self._netcache = None        # the network playback cache's manager, while it runs (Experimental)
        self._notice_job = None
        self._window_title = ""
        self.tabs: list[Tab] = []
        self.current = -1                      # the tab on show
        self.playing_tab: int | None = None    # the tab the player is playing from

        self.mini_mode = False                 # showing the mini strip instead of the full layout
        self._mini_state: dict | None = None   # what the strip shows (worked out by _poll)
        # (0.25.0) Settings opened from the mini strip: where the strip was before the window grew
        # to hold the panel (None the rest of the time). Saved in its place, and put back on close.
        self._mini_settings_from: str | None = None
        # The theme comes first, so everything is built in its colours. (Dark by default.)
        if self.prefs.get("theme") not in dict(theme.THEME_CHOICES):
            self.prefs["theme"] = theme.DEFAULT_THEME
        self._windows_light = theme.windows_uses_light() if self.prefs["theme"] == "system" else None
        theme.use(theme.palette_for(self.prefs["theme"], self._windows_light))

        root.withdraw()  # set the window up out of sight, then show it once
        root.title(APP_NAME)
        appicon.apply(root)   # (0.23.0) the app icon, on this window and every window opened after it
        root.minsize(theme.px(FULL_MIN_SIZE[0]), theme.px(FULL_MIN_SIZE[1]))

        self.on_top = tk.BooleanVar(value=self.prefs["always_on_top"])
        root.attributes("-topmost", self.on_top.get())
        try:
            player.set_volume(float(self.prefs["volume"]))
        except (TypeError, ValueError):   # (a hand-edited settings file)
            player.set_volume(settings.DEFAULTS["volume"])
        self.volume_var = tk.DoubleVar(value=player.volume * 100)
        self.can_exclusive = devices.exclusive_supported()
        self.exclusive = tk.BooleanVar(value=bool(self.prefs["exclusive"]) and self.can_exclusive)
        self.open_at_home = tk.BooleanVar(value=bool(self.prefs["open_at_home"]))
        self.clip_guard = tk.BooleanVar(value=bool(self.prefs["clip_guard"]))
        self.show_art = tk.BooleanVar(value=bool(self.prefs["show_art"]))
        self.allow_network = tk.BooleanVar(value=bool(self.prefs["allow_network"]))
        self.media_controls = tk.BooleanVar(value=bool(self.prefs["media_controls"]))
        self.watch_music = tk.BooleanVar(value=bool(self.prefs["watch_music"]))
        self.check_updates = tk.BooleanVar(value=bool(self.prefs["check_updates"]))
        self.detailed_logging = tk.BooleanVar(value=bool(self.prefs["detailed_logging"]))
        self._update_found: updates.Release | None = None   # set by the update check's thread
        self._update_checking = False
        self.update_available: updates.Release | None = None
        self._update_job = None
        # Spotting new music (0.17.0): a thread looks at the library folder every few minutes;
        # its reports are picked up by _poll (never handled on the watcher's thread).
        self.watcher = watcher.Watcher(self._index)
        self._new_albums: list[Path] = []        # the new albums, newest first (watcher.new_albums)
        self._listening: _Listening | None = None   # the song being heard, for its play count
        self._plays_seen = 0   # plays stored (_plays_stored) when the Plays columns last read their counts
        self._held_key = None    # what the songs held round the one playing were worked out for

        self._opened_key = None                  # the song last marked as opening its album
        self.media = mediakeys.MediaKeys()        # presses from media keys / Windows arrive here
        self._windows_media = None                # mediakeys.WindowsMedia while switched on
        self._drop_overlay: list[tk.Frame] = []   # the green edge shown while something is dragged over
        self._drop_shown = None                   # what's lit up for a drag now: ("tab", i) / ("window", None)
        experiments.tidy(self.prefs)  # forget switches for experiments since rolled in or dropped
        self.experiment_vars: dict[str, tk.BooleanVar] = {}
        # Clipping guard: true peaks are measured in the background, one file at a time — only
        # the song playing and the next (others queued meanwhile are skipped when their turn comes).
        self._peaks: dict[Path, float] = {}
        self._peak_jobs: queue.Queue = queue.Queue()
        self._peak_wanted: set[Path] = set()
        self._guard_for: Path | None = None
        # Volume levelling (off by default): ReplayGain tags, else the measured loudness, worked out
        # on the same worker (tags read, loudness and true peak measured in one pass); an album's own
        # loudness on a worker of its own. Kept in memory for the session.
        self._gain_tags: dict[Path, levelling.GainTags | None] = {}
        self._lufs: dict[Path, float | None] = {}
        self._level_wanted: set[Path] = set()
        # (the choices each file's gain was worked out with, and whether its true peak was known then)
        self._level_set_with: dict[Path, tuple] = {}
        self._album_lufs: dict[Path, float | None] = {}
        self._album_jobs: queue.Queue = queue.Queue()
        self._album_wanted: Path | None = None
        self._album_later: dict[Path, float] = {}   # network albums to measure again after this (monotonic)
        self._album_cancel = threading.Event()
        threading.Thread(target=self._measure_peaks, name="true-peak", daemon=True).start()
        threading.Thread(target=self._measure_albums, name="album-loudness", daemon=True).start()
        # Stop after this song and the sleep timer: for this session only.
        self.sleep_timer: sleep.SleepTimer | None = None
        self._lyrics: LyricsWindow | None = None
        self._closed_tabs: list[tuple[int, Tab]] = []  # for Ctrl+Shift+T
        self._writing_playlists: set[Path] = set()    # playlists being added to on a thread
        self._wheel_delta = 0                          # mouse-wheel movement not yet a whole notch
        self._poll_errors: set[tuple[str, str]] = set()  # kinds of error _poll has logged already
        self._closing = False
        player.set_output_device(self.prefs["output_device"] or None)
        player.set_shuffle(bool(self.prefs["shuffle"]))
        player.set_repeat(self.prefs["repeat"])
        hardware_volume = None
        if self.exclusive.get():
            hardware_volume = player.refresh_hardware_volume()
            player.set_exclusive(True)

        # The layout (layout.py): the narrow column until the window's size says otherwise.
        self._tier = layout.COLUMN
        self._layout_job = None
        self._plan: layout.Plan | None = None
        self._heights: layout.Heights | None = None   # measured once per layout (_measure_heights)
        self._measured: dict[str, layout.Heights] = {}   # each wide layout's heights when last measured
        self._opening_size: tuple[int, int] | None = None   # (real px) the size it opens at, before it shows
        self._head_full = ("Nothing playing", "Open your library: Ctrl+L")   # title and artist line, uncut
        self._head_texts = ("", "")    # the caption and the format line
        self._head_cut = (False, False)  # whether a wide window shows the title / artist line shortened
        self._fits: dict[tuple, str] = {}   # shortened text, by (text, style, width)
        self._cover_tip = ""           # a wide window's cover tooltip: its own size, and how it's shown
        self._art_image = None         # the last cover fetched, for a quick stand-in while resizing
        self._art_hidden = False       # a wide window too short for a cover: the cover box is put away

        self._build()
        self._bind_keys()

        chrome.remove_caption(root)   # before restoring the size, so it isn't counted twice
        chrome.keep_maximise_in_work_area(root)
        home = self.prefs["home_geometry"]
        start = home if self.open_at_home.get() and home else self.prefs["geometry"]
        if self.prefs["mini_mode"]:   # left as the mini strip last time: open that way
            self._show_mini(True)
            start = self._mini_geometry(start)
        if not placement.restore(root, start) and placement.parse(start) is None and self.mini_mode:
            root.geometry(f"{theme.px(MINI_DEFAULT_SIZE[0])}x{theme.px(MINI_DEFAULT_SIZE[1])}")
        parsed = placement.parse(start) if start else None
        if parsed and parsed[0]:   # laid out for its size before it's shown: no narrow layout first
            self._check_layout(size=(parsed[0], parsed[1]))
        root.deiconify()
        self._settle_size(start)
        if self.prefs["maximised"] and not self.mini_mode:   # it was left maximised: so it opens
            root.state("zoomed")
        root._dg_escape = self._escape_pressed   # Esc with no pop-over open (Popover._escape)
        theme.dark_title_bar(root)
        self._setup_drop()
        if self.media_controls.get():
            self._start_media()
        self._media_job = root.after(MEDIA_POLL_MS, self._media_poll)
        self._theme_job = root.after(THEME_POLL_MS, self._follow_windows)

        self._polls = 0
        self._saved_snapshot = json.dumps(self.prefs, sort_keys=True)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        if self.exclusive.get():
            # Bit-perfect from the start: the device may not be last time's (a different Windows
            # default, or the chosen one unplugged), so the same ear check as switching it on —
            # once the window is up, and before anything can start playing.
            root.after_idle(self._protect_ears, hardware_volume)
        self._restore_tabs(folder)
        # Named on the command line: opened by the first poll, with any later launch's that has
        # arrived meanwhile (`_take_requests`).
        self._named: list[tuple[str, ...]] = [tuple(paths)] if paths else []
        if settings.problem:   # a damaged settings file was set aside at start: said once
            self._set_notice(settings.problem, 12000)
        self._poll_job = self.root.after(POLL_MS, self._poll)
        self.favourites.load()
        if self.watch_music.get():
            self._start_watching(INDEX_AFTER_MS / 1000)
        else:
            self.root.after(INDEX_AFTER_MS, self._refresh_index)
        self._update_new_albums()
        self._update_job = self.root.after(UPDATE_FIRST_MS, self._update_tick)
        if experiments.enabled(self.prefs, NETWORK_CACHE):
            self.root.after_idle(self._start_cache)   # (once the window is up: no I/O to speak of)
        self._check_job = self.root.after(CHECK_AFTER_MS, self.checker.kick)   # (nothing unless it was left on)
        # Up and running: from here Python's cycle collector runs only on this thread (tkgc.py).
        self.collector = tkgc.Collector(root)
        self.collector.start()

    # -- layout --------------------------------------------------------------
    def _build(self):
        theme.apply(self.root)
        theme.on_change(self._palette_changed)
        self.bar = TabBar(self.root, self.root, on_select=self.select_tab, on_close=self.close_tab,
                          on_new=lambda: self.new_menu.toggle(self.bar.add_btn, self._below_strip()),
                          on_settings=lambda: self._open_settings(),
                          on_double=self._bar_double_click, on_quit=self.quit,
                          on_tab_menu=self._tab_menu, on_move=self.move_tab, on_maximise=self._toggle_maximise)
        self.bar.pack(fill="x")

        body = ttk.Frame(self.root, padding=theme.pad(theme.GAP_WIDE, theme.GAP_LARGE, theme.GAP_WIDE, theme.GAP_MEDIUM))
        body.pack(fill="both", expand=True)
        self.body = body
        # A wide window's two panes (layout.py), not packed in the narrow window: on the left the
        # now-playing stack (cover, text, seek bar, transport) centred in its pane, on the right the
        # track list. Made before the widgets they hold: a widget packed into a frame other than its
        # parent must be above that frame in Tk's stacking order.
        self.split = ttk.Frame(body)
        self.left = tk.Frame(self.split, bg=theme.BG, width=1)
        self.left.pack_propagate(False)
        self.left.pack(side="left", fill="y")
        self.stack = ttk.Frame(self.left)
        self.right = ttk.Frame(self.split)
        self.right.pack(side="left", fill="both", expand=True)

        # Now playing: a square cover the full width of the window, the details underneath.
        # It follows the window's width, but never takes more than half its height, so the
        # track list always keeps room. (Packed by _pack_column / _pack_split.)
        head = ttk.Frame(body)
        self.head = head
        self.art_box = tk.Frame(head, bg=theme.BG, height=1)
        self.art_box.pack(fill="x")
        self.art_box.pack_propagate(False)
        self.art_label = tk.Label(self.art_box, bg=theme.BG, bd=0, cursor="hand2", fg=theme.MUTED,
                                  compound="center")
        self.art_label.place(relx=0.5, rely=0, anchor="n")
        # Click: Now Playing; double-click: the mini strip (0.19.0).
        click_or_double(self.art_label, self.show_details, self.toggle_mini)
        Tooltip(self.art_label, lambda: self._cover_tip)   # (a wide window: the cover's size)
        self.art_box.bind("<Configure>", lambda e: self._fit_art())
        self.root.bind("<Configure>", self._on_resize, add="+")
        text = ttk.Frame(head)
        text.pack(fill="x", pady=theme.pad(theme.GAP_LARGE, 0))
        self.head_text = text
        # A wide window's caption over the title: NOW PLAYING, or what the tab is (ALBUM, PLAYLIST, …).
        self.kind_label = ttk.Label(text, text="", style="Section.TLabel", anchor="w")
        if not self.show_art.get():
            self.art_box.pack_forget()
            text.pack_configure(pady=0)
        self.title_label = ttk.Label(text, text="Nothing playing", style="Display.TLabel", anchor="w")
        self.title_label.pack(fill="x")
        self.sub_label = ttk.Label(text, text="Open your library: Ctrl+L", style="Subtitle.TLabel",
                                   anchor="w")
        self.sub_label.pack(fill="x")
        # Shortened with "…" (a wide window): the whole text in a tooltip.
        Tooltip(self.title_label, lambda: self._head_full[0] if self._head_cut[0] else "")
        Tooltip(self.sub_label, lambda: self._head_full[1] if self._head_cut[1] else "")
        info_row = ttk.Frame(text)
        info_row.pack(fill="x", pady=theme.pad(theme.GAP_SMALL, 0))
        # Right of the format line, one group in one size and style: the status dot, the
        # bit-perfect lock and Details, each icon in a slot with a round hover disc. The group sits
        # over the row (placed, not packed in it) so the ⓘ's ink can end on the content edge, its
        # slot reaching into the window's margin; a spacer keeps the format line clear of it.
        self.head_icons = tk.Frame(body, bg=theme.BG)
        self.info_button = controls.RoundIconButton(self.head_icons, self.root, "info", self.show_details,
                                                    size=theme.ICON, diameter=HEAD_ICON_SLOT, width=HEAD_ICON_SLOT,
                                                    bg="BG", label="Details: live stats, file info, lossless check")
        self.info_button.pack(side="right")
        self.lock_button = None
        if self.can_exclusive:
            self.lock_button = controls.RoundIconButton(self.head_icons, self.root, "unlock", self._toggle_lock,
                                                        size=theme.ICON, diameter=HEAD_ICON_SLOT,
                                                        width=HEAD_ICON_SLOT, bg="BG", label="Bit-perfect")
            self.lock_button.pack(side="right")
            self.lock_button.tip.text = self._lock_tip
        # One dot instead of a line of device detail: green = all good, amber = something is
        # changing the sound, grey = fine but not bit-perfect. Hover for the detail, click for Details.
        self.status_dot = tk.Label(self.head_icons, bg=theme.BG, bd=0, cursor="hand2")
        self.status_dot.pack(side="right", padx=theme.pad(0, theme.GAP_SMALL))
        self.status_dot.bind("<Button-1>", lambda e: self.show_details())
        self._status_detail = ""
        self._status_colour = "BG"
        Tooltip(self.status_dot, lambda: self._status_detail)
        self._paint_status_dot("BG")
        self._paint_lock()
        overhang = theme.px(INFO_INK_INSET)
        self.head_icons.update_idletasks()
        ttk.Frame(info_row, width=max(1, self.head_icons.winfo_reqwidth() - overhang), height=1).pack(side="right")
        self.head_icons.place(in_=info_row, relx=1.0, x=overhang, rely=0.5, anchor="e")
        self.format_label = ttk.Label(info_row, text="", style="Info.TLabel", anchor="w", cursor="hand2")
        self.format_label.pack(side="left", fill="x", expand=True)
        self.format_label.bind("<Button-1>", lambda e: self.show_details())
        self.art_size = 0                    # set by _fit_art once the window has a size
        self._art_track: Path | None = None  # the track whose cover we want
        self._art_key: tuple | None = None   # (track, size) of the cover on show or on its way
        self._art_result: tuple | None = None
        self._art_job = None

        # (0.20.0) The seek bar: a drawn slider (controls.Slider, shared with the mini strip), the
        # time so far under its left end and the time left under its right. Its track runs exactly
        # from content edge to content edge, lined up with the times and the search box: the slider
        # is wider than the row by the track's inset each side (the knob may reach into the margin),
        # so it sits over the row's first line rather than in it.
        seek_row = ttk.Frame(body)
        self.seek_row = seek_row
        self.seek = controls.Slider(body, on_release=self._seek_released, on_drag=self._seek_dragged, step=SEEK_STEP)
        track = ttk.Frame(seek_row, height=int(self.seek.cget("height")))
        track.pack(fill="x")
        inset = round(self.seek.track_inset())
        self.seek.place(in_=track, x=-inset, y=0, relwidth=1.0, width=2 * inset, relheight=1.0)
        times = ttk.Frame(seek_row)
        times.pack(fill="x")
        self.pos_label = ttk.Label(times, text="0:00", style="Info.TLabel", font=theme.numbers(theme.SIZE_SMALL))
        self.pos_label.pack(side="left")
        self.remain_label = ttk.Label(times, text="0:00", style="Info.TLabel", anchor="e",
                                      font=theme.numbers(theme.SIZE_SMALL))
        self.remain_label.pack(side="right")

        # Transport: no volume slider here. It lives under the gear, and a badge
        # appears beside the buttons only while the volume is below 100%.
        self.transport = tk.Frame(body, bg=theme.BG, height=theme.px(52))
        self.transport.pack_propagate(False)
        buttons = tk.Frame(self.transport, bg=theme.BG)
        buttons.place(relx=0.5, rely=0.5, anchor="center")
        # (0.20.0) The play button is a drawn green circle; the others light up with a soft round
        # disc. Previous and next are drawn, filled like play (no icon font needed); shuffle and
        # repeat are line icons that turn green with a dot under them when on.
        self.shuffle_button = controls.RoundIconButton(buttons, self.root, "shuffle", self.toggle_shuffle,
                                                       size=theme.ICON_MEDIUM, diameter=32, width=36, bg="BG",
                                                       label=f"Shuffle ({keys.hint('shuffle')})")
        self.shuffle_button.pack(side="left", fill="y", padx=theme.pad(0, theme.GAP_SMALL))
        self.previous_button = controls.RoundIconButton(buttons, self.root, "prev", self.player.previous, diameter=36,
                                                        width=40, bg="BG", drawn="prev",
                                                        label=f"Previous ({keys.hint('previous')})")
        self.previous_button.pack(side="left", fill="y")
        self.play_button = controls.PlayButton(buttons, self.toggle_play, label=f"Play / pause ({keys.hint('play')})")
        self.play_button.pack(side="left", padx=max(0, theme.px(theme.GAP_MEDIUM) - self.play_button.margin))
        self.next_button = controls.RoundIconButton(buttons, self.root, "next", self.player.next, diameter=36,
                                                    width=40, bg="BG", drawn="next", label=f"Next ({keys.hint('next')})")
        self.next_button.pack(side="left", fill="y")
        self.repeat_button = controls.RoundIconButton(buttons, self.root, "repeat", self.cycle_repeat,
                                                      size=theme.ICON_MEDIUM, diameter=32, width=36, bg="BG",
                                                      label=f"Repeat ({keys.hint('repeat')})")
        self.repeat_button.pack(side="left", fill="y", padx=theme.pad(theme.GAP_SMALL, 0))
        self._paint_order_buttons()
        self.volume_badge = controls.PillButton(self.transport, "", self._open_settings, kind="badge", small=True,
                                                font=theme.font(theme.SIZE_SMALL),
                                                label="Volume below 100% · click for Settings, or use the mouse "
                                                      "wheel over the buttons, or Ctrl+↑ / ↓")
        for widget in (self.transport, buttons, *buttons.winfo_children(), self.volume_badge):
            widget.bind("<MouseWheel>", self._wheel_volume)

        # The status line is packed first, from the bottom, so a short window squeezes the
        # track list rather than pushing the status line out of sight. It keeps one height, the
        # body text's line, whatever it says (a name in another script can bring a taller fallback
        # font), so the list above never jumps when a message comes or goes.
        line = tkfont.nametofont("TkDefaultFont", root=self.root).metrics("linespace")
        self.status_row = ttk.Frame(body, height=line)
        self.status_row.pack(side="bottom", fill="x", pady=theme.pad(theme.GAP, 0))
        self.status_row.pack_propagate(False)
        self.status_label = ttk.Label(self.status_row, text="", style="Sub.TLabel", anchor="w")
        self.status_label.pack(fill="both", expand=True)
        self.status_label.bind("<Button-1>", self._status_clicked)
        # "Undo (Ctrl+Z)" at the right-hand end of the status line, while it's telling of a playlist
        # change that can be undone (placed over the line, so nothing else moves).
        self.undo_link = ttk.Label(body, text=f"Undo ({keys.hint('undo')})", style="UpNext.TLabel", cursor="hand2")
        self.undo_link.bind("<Button-1>", lambda e: self.undo())
        self.playlist = PlaylistView(body, on_play=lambda i: self._play_tab(self.current, i),
                                     on_menu=self._track_menu, on_drag=self._song_drag,
                                     on_heading_menu=self._heading_menu,
                                     on_search_library=lambda query: self.find_in_library(query),
                                     is_favourite=lambda track: track in self.favourites,
                                     plays_db=lambda: self._index().db)
        self.playlist.set_columns(self.prefs.get("track_columns") or ())
        self.playlist.set_wide_columns(self.prefs.get("wide_track_columns") or ())
        # "Up next (3) · Song": shown above the status line only while songs are queued; click
        # for the list. (Packed from the bottom, ahead of the track list, like the status line.)
        self.up_next_label = ttk.Label(body, text="", style="UpNext.TLabel", anchor="w", cursor="hand2")
        self.up_next_label.bind("<Button-1>", lambda e: self._show_up_next())
        self._up_next_key = None
        self._marks_shown: list = []   # the Up next pop-over's network-song marks as last drawn
        self.up_next_pop = Popover(self.root, width=theme.px(300), keys=True, padding=(0, theme.GAP_LARGE))
        self._pack_column()

        # (0.20.0) The empty window (no tabs open): instead of "Nothing playing", a quiet ♫, one
        # clear button and the shortcut. Shown in place of the cover / transport / list by
        # _show_empty (the status line stays, for "Closed … click to reopen" and drop notes).
        self.empty_view = tk.Frame(body, bg=theme.BG)
        middle = tk.Frame(self.empty_view, bg=theme.BG)
        middle.place(relx=0.5, rely=0.45, anchor="center")
        self.empty_mark = tk.Label(middle, bg=theme.BG, bd=0)
        self.empty_mark.pack(pady=theme.pad(0, theme.GAP_WIDE))
        self.empty_button = controls.PillButton(middle, "Open your library", self.open_library,
                                                label="Your albums, artists and playlists (Ctrl+L)")
        self.empty_button.pack()
        self.empty_hint = ttk.Label(middle, text="Ctrl+L  ·  or drop a folder or music files here",
                                    style="Info.TLabel", anchor="center")
        self.empty_hint.pack(pady=theme.pad(theme.GAP_MEDIUM, 0))
        self._empty_shown = False
        self._paint_empty_mark()

        self.settings_pop = self._build_settings()
        self.settings_pop.on_hide = self._settings_closed
        self.new_menu = self._build_new_menu()

        # Mini mode: built now, shown in place of the strip + body by toggle_mini.
        self.mini = MiniView(self.root, self.root, on_play=self.toggle_play, on_previous=self.player.previous,
                             on_next=self.player.next, on_seek=self._seek_to, on_full=self.toggle_mini,
                             on_details=self.show_details, on_quit=self.quit, on_settings=self._open_settings)
        for widget in (self.mini.previous_button, self.mini.play_button, self.mini.next_button):
            widget.bind("<MouseWheel>", self._wheel_volume)

    def _caption(self, parent, text: str, first: bool = False):
        """A section heading; every section after the first gets a faint line above it."""
        if not first:
            tk.Frame(parent, bg=theme.BORDER, height=1).pack(fill="x", pady=theme.pad(theme.GAP_LARGE, 0))
        ttk.Label(parent, text=text.upper(), style="PopCaption.TLabel").pack(
            anchor="w", pady=theme.pad(theme.GAP, theme.GAP_HAIR))

    def _setting(self, parent, title: str, description: str, variable: tk.BooleanVar, command=None):
        """An on/off setting: a slider switch on the right, a line underneath saying what it does (wrapped
        at the text column's real edge, which SettingRow follows)."""
        column = SETTINGS_WIDTH - 2 * theme.GAP_WIDE - SettingRow.SWITCH_ROOM
        row = SettingRow(parent, self.root, title, description, variable, command, guess=theme.px(column))
        row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
        return row

    def _pop_button(self, parent, text: str, command, label: str = "") -> controls.PillButton:
        """(0.21.0) A button in a pop-over (Settings): a small plain pill, like the rest of the app's
        buttons instead of a stock ttk button."""
        return controls.PillButton(parent, text, command, kind="plain", small=True, bg="SURFACE", label=label)

    def _choice_box(self, parent, label: str, values, *, state: str = "readonly", full: bool = False,
                    gap: int = theme.GAP, width: int | None = None) -> ttk.Combobox:
        """A Settings drop-down on its own row: its name on the left, the box SETTINGS_CHOICE_W wide on
        the right (so the boxes' left edges line up), or the whole width (`full`, no name). `width`:
        another width, in real px, for a section whose choices need more room. Tab reaches it; ↓
        opens it."""
        if full:
            box = ttk.Combobox(parent, state=state, takefocus=True, style=theme.CARD_COMBOBOX)
            box.pack(fill="x")
            return box
        row = ttk.Frame(parent, style="Pop.TFrame")
        row.pack(fill="x", pady=theme.pad(gap, 0))
        holder = tk.Frame(row, bg=theme.SURFACE, width=width or theme.px(SETTINGS_CHOICE_W))
        holder.pack(side="right", fill="y")   # (first: in a narrow window the name gives way, not the box)
        ttk.Label(row, text=label, style="Pop.TLabel").pack(side="left")
        box = ttk.Combobox(holder, state=state, takefocus=True, style=theme.CARD_COMBOBOX, values=list(values),
                           width=1)
        box.pack(fill="x")
        holder.configure(height=box.winfo_reqheight())
        holder.pack_propagate(False)
        return box

    def _build_settings(self) -> Popover:
        """Grouped into Sound / Window / Library / Network / Experimental / Help, every on/off choice
        a slider switch with one plain line saying what it does.

        (0.21.0) It scrolls when it doesn't fit the window (it's ~1300 px tall at 150%
        scaling) — mouse wheel anywhere over it, and a slim scrollbar that shows only then. Its
        buttons are flat pills and the volume is the same drawn slider as the seek bar."""
        pop = Popover(self.root, width=theme.px(SETTINGS_WIDTH), scroll=True, controls=True, at_edge=True)
        self.settings_area = pop.area
        b = pop.body
        row = ttk.Frame(b, style="Pop.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Settings", style="PopTitle.TLabel").pack(side="left")
        self.mini_button = self._pop_button(row, "Mini player", self.toggle_mini,   # (it closes Settings first)
                                            "Shrink the player to a small strip. Also Z, or double-click the cover")
        self.mini_button.pack(side="right")

        self._caption(b, "Sound", first=True)
        self._output_choices: list[str | None] = []
        self.output_box = self._choice_box(b, "", (), full=True)
        self.output_box.configure(postcommand=self._refresh_outputs)
        self.output_box.bind("<<ComboboxSelected>>", self._change_output)
        Tooltip(self.output_box, "Output: where the sound goes")
        self._refresh_outputs()
        if self.can_exclusive:
            self._setting(b, "Bit-perfect", "Sound exactly as recorded. Other apps go quiet.",
                          self.exclusive, self._toggle_exclusive)
        self._setting(b, "Clipping guard", "Lowers only tracks that would crackle, by just enough.",
                      self.clip_guard, self._guard_changed)
        # (0.24.0) "My DAC has headroom": room some converters have above full scale. Off by default.
        self.headroom_box = self._choice_box(b, "My DAC has headroom",
                                             [headroom_label(v) for v in GUARD_HEADROOM_CHOICES])
        self.headroom_box.current(headroom_index(self.prefs.get("clip_headroom", 0.0)))
        self.headroom_box.bind("<<ComboboxSelected>>", self._headroom_chosen)
        Tooltip(self.headroom_box, "Only if your converter is known to take overs: the clipping guard then lets "
                                   "the wave go this far past full scale before it turns a track down. "
                                   "Measured converters range from none to about 3.5 dB. Off = none.")
        # Volume levelling: off by default (any change of level means the device no longer gets the
        # file's own samples); the pre-amp goes with it.
        self.levelling_box = self._choice_box(b, "Volume levelling", [label for _, label in levelling.MODES])
        self.levelling_box.current([key for key, _ in levelling.MODES].index(self._levelling_setting()))
        self.levelling_box.bind("<<ComboboxSelected>>", self._levelling_chosen)
        Tooltip(self.levelling_box, "Plays songs at about the same loudness, from their ReplayGain tags (or "
                                    "Ironbark measures them). Track: each song on its own. Album: an album's "
                                    "songs keep their own balance. Smart: Album while an album plays in order, "
                                    "else Track. Not bit-perfect while it's on.")
        self.preamp_box = self._choice_box(b, "Levelling pre-amp",
                                           [levelling.preamp_label(v) for v in levelling.PREAMP_CHOICES],
                                           state="disabled" if self._levelling_setting() == "off" else "readonly")
        self.preamp_box.current(levelling.PREAMP_CHOICES.index(self._preamp_db()))
        self.preamp_box.bind("<<ComboboxSelected>>", self._preamp_chosen)
        Tooltip(self.preamp_box, "Added to every levelling gain. A song is only turned up as far as its "
                                 "peak allows (it stays under −0.1 dB true peak).")
        row = ttk.Frame(b, style="Pop.TFrame")
        row.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
        ttk.Label(row, text="Volume", style="Pop.TLabel").pack(side="left", padx=theme.pad(0, theme.GAP_MEDIUM))
        self.volume_text = ttk.Label(row, text="", style="PopMuted.TLabel", width=15, anchor="e")
        self.volume_text.pack(side="right", padx=theme.pad(theme.GAP_MEDIUM, 0))
        # (0.21.0) The drawn slider (controls.Slider), following volume_var however it's changed.
        self.volume_slider = controls.Slider(row, on_release=self._volume_slid, on_drag=self._volume_slid,
                                             bg="SURFACE", step=VOLUME_STEP * 100)
        self.volume_slider.pack(side="left", fill="x", expand=True)
        self.volume_slider.set(self.volume_var.get(), 100.0)
        self.volume_var.trace_add("write", lambda *_: self.volume_slider.set(self.volume_var.get(), 100.0))

        self._caption(b, "Window")
        self.theme_box = self._choice_box(b, "Theme", [label for _, label in theme.THEME_CHOICES], gap=0)
        self.theme_box.current([key for key, _ in theme.THEME_CHOICES].index(self.prefs["theme"]))
        self.theme_box.bind("<<ComboboxSelected>>", self._theme_chosen)
        Tooltip(self.theme_box, "Follow Windows: light or dark, as Windows' own app setting is "
                                "(Personalisation → Colours)")
        self._setting(b, "Album art", "Off gives the track list more room.", self.show_art, self._toggle_art)
        self._setting(b, "Keep on top", "Stay in front of other windows. The mini player has its own (on at first).",
                      self.on_top, self._toggle_on_top)
        self._setting(b, "Media keys", "Play/pause, next, previous keys and Windows' media panel.",
                      self.media_controls, self._toggle_media)
        row = ttk.Frame(b, style="Pop.TFrame")
        row.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
        self.home_text = ttk.Label(row, text="", style="Pop.TLabel")
        self.home_text.pack(side="left", fill="x", expand=True)
        self.snap_button = self._pop_button(row, "Snap to home", lambda: (Popover.close_open(), self.snap_home()),
                                            "Also Ctrl+Shift+H, or double-click the empty strip at the top")
        self.snap_button.pack(side="right")
        self._pop_button(row, "Use current", self.make_home, "Make the window's current spot and size its home").pack(
            side="right", padx=theme.pad(0, theme.GAP))
        self._setting(b, "Open at home", "Start up at the home position.", self.open_at_home)
        self._update_home_text()

        self._caption(b, "Library")
        self.library_rows = ttk.Frame(b, style="Pop.TFrame")   # one row per library folder
        self.library_rows.pack(fill="x")
        self._paint_library_folders()
        row = ttk.Frame(b, style="Pop.TFrame")
        row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
        self._pop_button(row, "Add folder…", self._add_library_folder,
                         "Add another folder of music to the Library").pack(side="right")
        self._setting(b, "Watch for new music", "Checks the folders every few minutes.",
                      self.watch_music, self._toggle_watch)

        self._caption(b, "Network")
        network_row = self._setting(b, "Allow network access", "Lets rip verification look up checksums online "
                                    "(AccurateRip, CUETools DB). The CUETools DB lookup isn't encrypted, so "
                                    "others on the network can see which disc is looked up. Otherwise only Check "
                                    "for updates (Help, below) uses the internet.",
                                    self.allow_network, self._network_changed)
        # Network playback (only while its Experimental switch is on): how songs on network drives
        # are fetched into the copy on this computer they play from.
        self.netplay_box = ttk.Frame(b, style="Pop.TFrame")
        self._netplay_after = network_row
        self._build_netplay(self.netplay_box)
        if experiments.enabled(self.prefs, NETWORK_CACHE):
            self.netplay_box.pack(fill="x", after=network_row)

        if experiments.EXPERIMENTS:
            self._caption(b, "Experimental")
        else:  # one line, to keep the panel short: the switches appear when there's something to try
            tk.Frame(b, bg=theme.BORDER, height=1).pack(fill="x", pady=theme.pad(theme.GAP_LARGE, 0))
            row = ttk.Frame(b, style="Pop.TFrame")
            row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
            ttk.Label(row, text="EXPERIMENTAL", style="PopCaption.TLabel").pack(side="left")
            note = ttk.Label(row, text="none right now", style="PopMuted.TLabel")
            note.pack(side="left", padx=theme.pad(theme.GAP_MEDIUM, 0))
            Tooltip(note, "New features appear here first, off until you switch them on")
        for experiment in experiments.EXPERIMENTS:
            var = tk.BooleanVar(value=experiments.enabled(self.prefs, experiment.key))
            self.experiment_vars[experiment.key] = var
            self._setting(b, experiment.title, experiment.description, var,
                          lambda key=experiment.key: self._toggle_experiment(key))
        self._caption(b, "Help")
        self.help_box = ttk.Frame(b, style="Pop.TFrame")
        self.help_box.pack(fill="x")
        row = ttk.Frame(self.help_box, style="Pop.TFrame")
        row.pack(fill="x")
        self._pop_button(row, f"Keyboard shortcuts ({keys.hint('help')})", lambda: self._from_settings(self.show_help),
                         "Every key, with a filter box. Also F1 or ? in any Ironbark window").pack(side="left")
        self._pop_button(row, "About Ironbark", lambda: self._from_settings(self.show_about),
                         "Version, licences, privacy and the website").pack(side="right")
        self._add_help_buttons(self.help_box)
        self.version_label = ttk.Label(b, text="", style="PopMuted.TLabel")
        self.version_label.pack(anchor="e", pady=theme.pad(theme.GAP_LARGE, 0))
        self._paint_version()
        return pop

    def _choice(self, parent, label: str, choices, key: str, tip: str, on_change=None,
                width: int | None = None) -> ttk.Combobox:
        """A drop-down setting in a row (Settings' style, _choice_box): `choices` are (saved value,
        words); `on_change()` runs after a new choice is saved. `width`: the box's, in real px."""
        box = self._choice_box(parent, label, [words for _, words in choices], width=width)
        values = [value for value, _ in choices]
        box.current(values.index(self.prefs[key]) if self.prefs.get(key) in values else
                    values.index(settings.DEFAULTS[key]))

        def chosen(_event=None):
            self.prefs[key] = values[box.current()]   # (the cache reads it live)
            box.selection_clear()
            if on_change is not None:
                on_change()
        box.bind("<<ComboboxSelected>>", chosen)
        Tooltip(box, tip)
        return box

    def _build_netplay(self, box: ttk.Frame):
        """Settings -> Network playback: buffering, pre-download, checking, and the cache itself."""
        self._caption(box, "Network playback")
        # One width for the section's drop-downs (their left edges line up), wide enough for the
        # longest choice: the words, their inset, and the chevron's room.
        font = tkfont.nametofont("TkTextFont", root=self.root)
        longest = max(font.measure(words) for choices in (NET_MODES, NET_BUFFERS, NET_PREDOWNLOADS, NET_KEEPS, NET_SIZES)
                      for _, words in choices)
        width = max(theme.px(SETTINGS_CHOICE_W),
                    longest + theme.px(theme.FIELD_INSET + theme.SEARCH_CLEAR_ROOM + theme.GAP_SMALL))
        self.net_mode_box = self._choice(
            box, "Playback buffering", NET_MODES, "net_mode",
            "Intelligent: measures your network and waits only as long as it needs to. Fast start: starts as "
            "soon as the initial buffer is downloaded. Complete track: downloads and checks the whole song "
            "before it plays, so the network isn't needed once it starts.", width=width)
        self.net_buffer_box = self._choice(
            box, "Initial buffer", NET_BUFFERS, "net_buffer",
            "Intelligent uses this as its starting point and waits less on a fast, steady network.", width=width)
        self.net_predownload_box = self._choice(
            box, "Pre-download", NET_PREDOWNLOADS, "net_predownload",
            "How far ahead songs download while one plays, so they follow on without a wait. Songs not "
            "played yet use at most half the cache; on a slow network only the next one is fetched.",
            self._net_settings_changed, width=width)
        self.net_keep_box = self._choice(
            box, "Keep cached tracks", NET_KEEPS, "net_keep",
            "Don't keep: a copy goes about a minute after it has played. Until playback finishes: kept while "
            "the song is in the list playing or Up next. For 1 day: a day after it last played. Until the "
            "cache is full: the copies played longest ago make room for new ones.", self._net_settings_changed,
            width=width)
        self.net_size_box = self._choice(
            box, "Maximum cache size", NET_SIZES, "net_cache_gb",
            "The most the copies may take on this computer. Whatever you choose, at least 2 GB (or 5% of the "
            "drive) is always left free.", self._net_settings_changed, width=width)
        self.net_verify = tk.BooleanVar(value=bool(self.prefs.get("net_verify", True)))
        self._setting(box, "Check finished downloads", "Makes sure each downloaded copy is whole and plays "
                      "correctly (a FLAC file's own fingerprint is checked too).", self.net_verify,
                      self._net_verify_changed)
        self.net_now_text = ttk.Label(box, text="", style="PopMuted.TLabel", wraplength=theme.px(250),
                                      justify="left")
        self.net_now_text.pack(fill="x", pady=theme.pad(theme.GAP, 0))
        Tooltip(self.net_now_text, "Excellent, Good, Poor or Unstable: how well the network keeps up with the "
                "music. On a poorer network songs wait a little longer before they start, so they don't "
                "pause later")
        row = ttk.Frame(box, style="Pop.TFrame")
        row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
        self.cache_text = ttk.Label(row, text="", style="PopMuted.TLabel")
        self.cache_text.pack(side="left", fill="x", expand=True)
        row = ttk.Frame(box, style="Pop.TFrame")
        row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
        self._pop_button(row, "Empty the cache", self._empty_cache,
                         "Delete the copies of network songs kept on this computer (not the songs themselves). "
                         "Songs playing now are kept.").pack(side="right")
        self._pop_button(row, "Open folder", self._open_cache_folder,
                         "Where the copies of network songs are kept").pack(side="right", padx=theme.pad(0, theme.GAP))
        self._paint_cache()

    def _net_verify_changed(self):
        self.prefs["net_verify"] = self.net_verify.get()

    def _net_settings_changed(self):
        """Pre-download, Keep cached tracks or the cache's size changed: the cache plans and tidies
        again now (it reads the settings live)."""
        changed = getattr(self._netcache, "settings_changed", None)
        if changed is not None:
            changed()
        self._paint_cache()

    def _paint_cache(self):
        """Settings -> Network playback's lines about the network now and the cache: how full it is."""
        manager = self._netcache
        if manager is None:
            on = experiments.enabled(self.prefs, NETWORK_CACHE)
            self.cache_text.configure(text="Cache: starts when Ironbark next starts" if on else "Cache: off")
            self.net_now_text.configure(text="")
            return
        view = manager.snapshot()
        limit = self.prefs.get("net_cache_gb") or 0
        of = f" of {limit}" if limit else ""
        songs = f"{view.songs} song{'' if view.songs == 1 else 's'}"
        text = f"Cache: {view.used / 1e9:.1f}{of} GB used · {songs}"
        if self.cache_text.cget("text") != text:
            self.cache_text.configure(text=text)
        now = getattr(manager, "network_now", None)
        line = network_now_line(now()) if now is not None else ""
        if self.net_now_text.cget("text") != line:
            self.net_now_text.configure(text=line)

    def _open_cache_folder(self):
        from . import crash, netcache

        Popover.close_open()
        folder = netcache.folder()
        folder.mkdir(parents=True, exist_ok=True)
        crash.open_folder(folder)

    def _empty_cache(self):
        manager = self._netcache
        if manager is None:
            self._set_notice("The network playback cache isn't running", 4000)
            return
        self._for_prompt()
        if not popups.confirm(self.root, "Empty the cache?",
                              "The copies of network songs on this computer are deleted (the songs on the "
                              "network drive aren't touched). Songs playing now, or about to, are kept.",
                              ok="Empty", danger=True):
            return
        manager.empty()
        self._set_notice("Emptying the cache…", 3000)
        self.root.after(1000, self._paint_cache)

    # -- the network playback cache (Experimental) --------------------------------------------
    def _start_cache(self):
        """Experimental -> Network playback cache is on: start it (read when Ironbark starts)."""
        from . import netcache

        try:
            self._netcache = netcache.install(self.prefs, self.player)
        except Exception:
            log.exception("starting the network playback cache")
            self._netcache = None
        if self._netcache is None:
            self._set_notice("The network playback cache didn't start: songs on network drives play directly", 8000)
        if hasattr(self, "cache_text"):
            self._paint_cache()

    def _stop_cache(self):
        if self._netcache is not None:
            from . import netcache

            netcache.uninstall()
            self._netcache = None

    def _take_cache_notices(self):
        """One-off messages from the cache (a copy that failed its check, a full disk, a server not
        answering for a while), one at a time on the status line."""
        manager = self._netcache
        if manager is None or not manager.notices or self._notice:
            return
        try:
            text = manager.notices.popleft()
        except IndexError:
            return
        self._set_notice(text, 8000)

    def _add_help_buttons(self, box: ttk.Frame):
        """The rest of Settings → Help, under Keyboard shortcuts / About: what to reach for when
        something's wrong (Save diagnostics…, Open log folder, Detailed logging), and Check for
        updates."""
        row = ttk.Frame(box, style="Pop.TFrame")
        row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
        self.diagnostics_button = self._pop_button(row, "Save diagnostics…", self.save_diagnostics,
                                                   "Save a .zip to send with a problem report: versions, sound "
                                                   "outputs, logs and settings (folder names left out). Nothing "
                                                   "is sent anywhere.")
        self.diagnostics_button.pack(side="left")
        self._pop_button(row, "Open log folder", self._open_log_folder,
                         "Where crash.log and app.log are kept").pack(side="right")
        self._setting(box, "Detailed logging", "Notes more of what the player does in its log, to help track "
                      "down a problem. Off normally.", self.detailed_logging, self._detailed_logging_changed)
        self._setting(box, "Check for updates", "Once a day, asks digitalgreenhouse.com.au whether a newer "
                      f"{APP_NAME} is out. The site sees only your internet address and this version number. "
                      "Needs Allow network access (Network, above).",
                      self.check_updates, self._updates_changed)

    def _from_settings(self, show):
        """A Settings button that opens a window: Settings closes first (the mini strip's grown
        window goes back to the strip)."""
        Popover.close_open()
        show()

    def _volume_slid(self, value: float):
        """The Settings volume slider moved (while dragging, and let go)."""
        self._muted_from = None   # (a level chosen since: unmuting no longer goes back to the old one)
        self.player.set_volume(value / 100)
        self.volume_var.set(self.player.volume * 100)   # (the slider ignores this while it's being dragged)
        self._update_volume_text()

    def _network_changed(self):
        """Off by default; read live by rip verification (ripcheck.network_allowed) and the update
        check, the only parts of the player that go online."""
        self.prefs["allow_network"] = self.allow_network.get()
        if self._now_playing is not None and self._now_playing.win.winfo_exists():
            self._now_playing.network_changed()
        self._check_for_update_if_due()

    def _updates_changed(self):
        """Check for updates: off by default, and only switched on while network access is allowed."""
        if self.check_updates.get() and not self.allow_network.get():
            self.check_updates.set(False)
            self._set_notice("Check for updates needs network access: switch on Allow network access first", 8000)
        self.prefs["check_updates"] = self.check_updates.get()
        self._check_for_update_if_due()

    def _detailed_logging_changed(self):
        self.prefs["detailed_logging"] = self.detailed_logging.get()
        logs.set_detailed(self.detailed_logging.get())

    def _toggle_experiment(self, key: str):
        experiments.set_enabled(self.prefs, key, self.experiment_vars[key].get())
        self._paint_version()
        if key == NETWORK_CACHE:   # (its settings show with it; the cache itself starts or stops with Ironbark)
            on = self.experiment_vars[key].get()
            if on:
                self.netplay_box.pack(fill="x", after=self._netplay_after)
            else:
                self.netplay_box.pack_forget()
            self._paint_cache()
            self._set_notice(f"The network playback cache {'starts' if on else 'stops'} when Ironbark next starts",
                             5000)
        if key == tag_editor.EXPERIMENT and not self._tag_editing():  # switched off: its windows go too
            for window in (self._tag_window, self._tag_history):
                if window is not None and window.win.winfo_exists():
                    window.close()  # (one in the middle of saving stops after the file in hand)

    def _paint_version(self):
        """The version, and how many experiments are on — so it's never a mystery why the player
        behaves differently from the release."""
        on = len(experiments.switched_on(self.prefs))
        extra = f" · {on} experiment{'' if on == 1 else 's'} on" if on else ""
        if self.update_available is not None:
            extra += f" · {self.update_available.version} available"
        self.version_label.configure(text=f"{APP_NAME} {__version__}{extra}")

    def _build_new_menu(self) -> Popover:
        pop = Popover(self.root, width=theme.px(240), keys=True, padding=(0, theme.GAP_SMALL))
        MenuRow(pop.body, "Browse library…", self.open_library, keys.hint("library")).pack(fill="x")
        MenuRow(pop.body, "Open a folder…", self.choose_folder, keys.hint("open_folder")).pack(fill="x")
        MenuRow(pop.body, "New playlist…", self.new_playlist, keys.hint("new_playlist")).pack(fill="x")
        MenuRow(pop.body, "Open a playlist file…", self.open_playlist_file, keys.hint("open_playlist")).pack(fill="x")
        return pop

    def _bind_keys(self):
        """What each shortcut in keys.ACTIONS does in the player (the Library and Edit tags add their
        own: their `key_handlers`). Looked up when the key is pressed, so the methods are the live ones."""
        def call(name):
            return lambda *key: getattr(self, name)(*key)

        def void(_result) -> None:   # a handler that always acted (returning False would pass the key on)
            return None

        def side(key: str) -> int:   # -1 for the backwards key of a pair (←, ↓, PgUp), 1 for the other
            return -1 if key.endswith(("Left", "Down", "PgUp")) else 1

        self.keys = keys.Keys(self.root, {
            "play": call("toggle_play"),
            "next": lambda: self.player.next(),
            "previous": lambda: self.player.previous(),
            "seek": lambda key: self._nudge(side(key) * SEEK_STEP),
            "seek_long": lambda key: self._nudge(side(key) * LONG_SEEK_STEP),
            "jump": lambda key: self._jump(int(key) / 10),
            "shuffle": call("toggle_shuffle"),
            "repeat": call("cycle_repeat"),
            "volume": lambda key: self._step_volume(side(key)),
            "mute": call("toggle_mute"),
            "full_volume": call("full_volume"),
            "new_tab": call("open_new_menu"),
            "close_tab": lambda: self.close_tab(self.current) if self.tabs else None,
            "reopen_tab": call("reopen_tab"),
            "next_tab": lambda: self._cycle_tab(1),
            "previous_tab": lambda: self._cycle_tab(-1),
            "move_tab": lambda key: self.move_tab(self.current, self.current + side(key)),
            "go_tab": lambda key: self.select_tab(int(key[-1]) - 1),
            "last_tab": lambda: self.select_tab(len(self.tabs) - 1),
            "open_folder": call("choose_folder"),
            "open_playlist": call("open_playlist_file"),
            "new_playlist": lambda: self.new_playlist(),
            "search_tab": call("search_this_tab"),
            "list_play": lambda: self.playlist.activate(),
            "list_queue": lambda: self._queue_selected(front=False),
            "list_play_next": lambda: self._queue_selected(front=True),
            "select_all": lambda: self.playlist.select_all(),
            "now_playing": call("show_details"),
            "meters": call("show_meters"),
            "pcm": call("show_pcm"),
            "clipping": call("show_clipping"),
            "spectrogram": call("show_spectrogram"),
            "scope": call("show_scope"),
            "compare": call("show_compare"),
            "abx": call("show_abx"),
            "test_signals": call("show_testsignals"),
            "mini": call("toggle_mini"),
            "settings": call("settings_from_key"),
            "snap_home": call("snap_home"),
            "help": call("show_help"),
            "quit": call("quit"),
            "full_screen": call("toggle_full_screen"),
            "library": lambda: self.open_library(),
            "find": call("find_in_library"),
            "remove": call("_remove_selected"),
            "move_song": lambda key: self._move_selected(1 if key.endswith("Down") else -1),
            "undo": call("undo"),
            "rename": call("rename_playlist_on_show"),
            "edit_tags": call("edit_selected_tags"),
            "lyrics": call("show_lyrics"),
            "stop_after": lambda: void(self.stop_after_current()),   # (False only means "carry on" now)
            "sleep_timer": call("open_sleep_menu"),
        }, held=lambda: bool(popups.Prompt.stack))   # (no key acts while a question box waits)
        self.keys.attach(self.root, "main")
        self.keys.mark(self.playlist.tree, "list")

    def _with_keys(self, window, scope: str = "app", handlers: dict | None = None):
        """Every window the player opens answers the shortcuts (keys.py), and its own: attached once,
        as it's made. Returns the window."""
        self.keys.attach(window.win, scope, handlers)
        return window

    # -- what the keys do (keys.py has which key is which) ------------------------------------------
    def open_new_menu(self):
        """Ctrl+T: the + button's menu (Browse library, Open a folder, New playlist, Open a playlist file)."""
        if self.mini_mode:
            self.toggle_mini()
        self.new_menu.toggle(self.bar.add_btn, self._below_strip())

    def search_this_tab(self):
        """Ctrl+F: the track list's search box."""
        if self.mini_mode:
            self.toggle_mini()
        self.playlist.focus_search()

    def settings_from_key(self):
        """Ctrl+, / Ctrl+P, in any window: Settings, with the player window brought to the front."""
        if self.root.state() == "iconic":
            self.root.deiconify()
        self.root.lift()
        self._open_settings()

    def find_in_library(self, query: str | None = None):
        """Ctrl+K / Ctrl+Shift+F, from any window: the song search panel over this window
        (quick_search.py), ready to type in. `query`: search for that (the track list's "Search the
        whole library")."""
        if self.mini_mode:
            self.toggle_mini()
        if self.root.state() == "iconic":
            self.root.deiconify()
        self.root.lift()
        if self._quick is None:
            self._quick = QuickSearch(self.root, self._index, self.song_actions(), top=self._below_strip)
        self._quick.open(query)

    def song_actions(self) -> quick_search.Actions:
        """What the search panel and the Library's song views can ask of the player."""
        return quick_search.Actions(
            play_song=self.play_song, queue_tracks=self._queue_tracks,
            open_album=lambda album, play: self.open_tab("album", album, play=play, interrupt=play),
            queue_album=self.queue_album, playlists=self._known_playlists, add_to_playlist=self.add_to_playlist,
            new_playlist=self.new_playlist, show_in_library=lambda album: self.open_library(album=album),
            show_artist=lambda name: self.open_library(view="artists", search=name),
            drag=self._found_drag, favourites=self.favourites)

    def play_song(self, track: Track, album: Path):
        """Play a song found in the library: its album opens in a tab (or its tab comes to the front)
        and plays from that song, the album carrying on after it. Asked for by name, so it plays
        even if something else is playing."""
        album, key = Path(album), audiofile.key(track.path)
        self._mark_opened(album)
        index = next((i for i, tab in enumerate(self.tabs) if tab.same("album", album)), None)
        if index is None:
            self.tabs.append(Tab("album", str(album)))
            index = len(self.tabs) - 1
        tab = self.tabs[index]
        if tab.tracks is not None and not tab.loading:
            self.select_tab(index)
            found = next((i for i, t in enumerate(tab.tracks) if audiofile.key(t.path) == key), None)
            if found is not None:
                self._play_tab(index, found)
                return
            self._forget_songs(tab)   # (not in the tab as it was read: read it again, then play)
        tab.resume_path, tab.resume_at, tab.last_index = key, 0.0, -1
        self.select_tab(index, then_play=True)

    def _found_drag(self, carried, x_root: int, y_root: int, phase: str):
        """A song (a list of one) or an album (its folder) dragged out of the search panel or the
        Library's song views: onto a tab adds it there (nowhere else: the panel covers the window)."""
        target = self._drop_target(x_root, y_root) if phase != "cancel" else None
        if target is not None and target[0] != "tab":
            target = None
        album = carried if isinstance(carried, Path) else None
        what = (f"“{album.name}”" if album is not None
                else "the song" if len(carried) == 1 else f"{len(carried)} songs")
        if phase == "move":
            self._show_drop_target(target, what)
            return
        self._clear_drop_target()
        if phase != "drop" or target is None:
            return
        tab = self.tabs[target[1]]
        if album is not None:
            self.add_album_to_current(album, tab=tab)
        else:
            tracks = [t for t in carried if not t.missing]
            self._set_notice(self._add_tracks_to_tab(tab, tracks, [audiofile.key(t.path) for t in tracks]))

    def _favourites_changed(self):
        """A song was made a favourite (or not), or the stored list arrived: the ♥ marks follow."""
        self.playlist.refresh_titles()
        if self._library is not None and self._library.win.winfo_exists():
            self._library.favourites_changed()

    def toggle_full_screen(self):
        """F11: full screen on the window's monitor (borderless, the same window: chrome.py), and back.
        Never remembered: the next start opens as the window was before. (Not from the mini strip.)"""
        if self.mini_mode:
            return
        if chrome.is_fullscreen(self.root):
            self._leave_full_screen()
            return
        Popover.close_open()
        chrome.enter_fullscreen(self.root)
        self.bar.paint_max(True)

    def _leave_full_screen(self) -> bool:
        if not chrome.exit_fullscreen(self.root):
            return False
        self.bar.paint_max(self.root.state() == "zoomed")
        return True

    def _escape_pressed(self, event):
        """Esc with no pop-over open: leaves full screen — but not while typing in a box (Esc clears
        the search there) or dragging (Esc calls the drag off)."""
        if not chrome.is_fullscreen(self.root) or keys.is_text_box(event.widget):
            return
        if self.playlist.drag.active or (self.bar._drag is not None and self.bar._drag["moving"]):
            return
        self._leave_full_screen()

    def _toggle_maximise(self):
        """The strip's maximise / restore button (a wide window): full screen counts as maximised."""
        if not self._leave_full_screen():
            chrome.toggle_maximise(self.root)

    def _jump(self, fraction: float):
        """0 – 9: to that tenth of the song on show (0: back to its start)."""
        if self._seek_target_playing:
            duration = self.player.status().duration
        else:
            tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
            view = tab.view_track() if tab is not None else None
            duration = view.duration if view is not None else 0.0
        if duration > 0:
            self._seek_to(duration * fraction)

    def _set_volume(self, volume: float, notice: str = "", ms: int = 1500):
        """The volume from a key: the Settings slider follows, and the status line says it."""
        self._muted_from = None
        self.player.set_volume(volume)
        self.volume_var.set(self.player.volume * 100)
        self._set_notice(notice or f"Volume {volume_words(self.player.volume)}", ms)

    def _step_volume(self, steps: int):
        """Ctrl+↑ / ↓: a step up or down (as a notch of the mouse wheel over the transport)."""
        self._set_volume(self.player.volume + steps * VOLUME_STEP)

    def _ears_at_risk(self) -> bool:
        """Bit-perfect on a device that can't turn itself down (or that the player couldn't confirm
        can): the player's volume is all there is between the file and the ears (see _protect_ears)."""
        return bool(self.exclusive.get()) and not self.player.hardware_volume

    def _ear_setup(self) -> tuple:
        """What the level is heard through: a level kept from one setup isn't safe to bring back in another."""
        return bool(self.exclusive.get()), self.player.output_device, self.player.hardware_volume

    def full_volume(self):
        """Ctrl+Shift+↑: volume 100% (untouched). Where the player's volume is the only one (bit-perfect
        on a device without its own), that's the file at full level into the headphones: asked first."""
        if self.player.volume < 1.0 and self._ears_at_risk():
            reason = ("This device has no volume control of its own" if self.player.hardware_volume is False
                      else "The player couldn't confirm this device has a volume control of its own")
            self._for_prompt()
            if not popups.confirm(self.root, "Turn the volume up to 100%?",
                                  f"{reason}, and bit-perfect goes round the Windows volume: the music will "
                                  f"play at full level (the volume is {volume_words(self.player.volume)} now).",
                                  detail="Turn your headphone or amp volume down first.", ok="Turn it up",
                                  tone="warn"):
                return
        self._set_volume(1.0)

    def toggle_mute(self):
        """Ctrl+Shift+↓: silence; again, back to the level it was (the default level if it was 0
        already). Never straight back up past the safe level where that would be loud: bit-perfect
        on a device that can't turn itself down, switched on (or another device chosen) since."""
        if self.player.volume > 0:
            remembered = (self.player.volume, self._ear_setup())
            self._set_volume(0.0, f"Muted · {keys.hint('mute')} again to unmute")
            self._muted_from = remembered
            return
        level, setup = self._muted_from or (settings.DEFAULTS["volume"], None)
        if level > SAFE_VOLUME and self._ears_at_risk() and setup != self._ear_setup():
            self._set_volume(SAFE_VOLUME, f"Unmuted at {SAFE_VOLUME:.0%}: this device can't turn itself down in "
                                          f"bit-perfect mode — raise it slowly", 6000)
        else:
            self._set_volume(level)

    def _queue_selected(self, front: bool):
        """Ctrl+Enter / Ctrl+Shift+Enter in the track list: the selected songs (an album header: its
        songs) into Up next — at the end, or to play next."""
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        rows = self.playlist.selected_rows() if tab is not None and tab.tracks else []
        if not rows:
            return False
        self._queue_tracks(_tracks_at(tab.tracks, rows), front)
        return True

    def _remove_selected(self):
        """Delete in a playlist's track list: the selected songs out of it, all undone at once (other
        tabs: nothing — their songs aren't a list you edit)."""
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        if tab is None or tab.kind != "playlist" or not tab.tracks:
            return
        rows = self.playlist.selected_rows()
        if rows:
            self._remove_from_playlist(rows)

    def _move_selected(self, step: int):
        """Alt+↑ / ↓ in a playlist's track list: the selected song a place up or down (one song only)."""
        index = self.playlist.selected_track()
        if index is not None and self.playlist.selected_count() == 1:
            self._move_in_playlist(index, step)

    def rename_playlist_on_show(self):
        """F2: rename the playlist on show."""
        if 0 <= self.current < len(self.tabs) and self.tabs[self.current].kind == "playlist":
            self.rename_playlist(self.current)
        elif self.tabs:
            self._set_notice(f"{keys.hint('rename')} renames a playlist: this tab isn't one", 3000)

    # -- undo (one playlist change) ----------------------------------------------------------------------
    def _remember_edit(self, notice: str, undone: str, undo, possible):
        """Keep a playlist change to undo (Ctrl+Z, or Undo in the status line), and say what it was.
        `undo()` puts it back; `possible()` says whether it still can (nothing else changed since)."""
        self._undo = (notice, undone, undo, possible)
        self._set_notice(notice, UNDO_NOTE_MS)

    def _edit_playlist(self, tab: Tab, tracks: list[Track], notice: str, undone: str, select: int | None,
                       select_back: int | list[int] | None) -> bool:
        """Save a change to a playlist tab's songs and keep it to undo; `select` (and, undone,
        `select_back`: one song, or several) is the song to select after. False when it couldn't be
        saved."""
        before = list(tab.tracks)
        if not self._save_playlist(Path(tab.path), tracks):
            return False
        after = tab.tracks   # (every later change to the tab's songs makes a new list: see _save_playlist)

        def possible() -> bool:
            return self._tab_index(tab) >= 0 and tab.tracks is after

        def undo() -> bool:
            if not self._save_playlist(Path(tab.path), before):
                return False
            if select_back is not None and self._tab_index(tab) == self.current:
                if isinstance(select_back, list):
                    self.playlist.select_tracks(select_back)
                else:
                    self.playlist.select(select_back)
            return True

        self._remember_edit(notice, undone, undo, possible)
        if select is not None and tracks:
            self.playlist.select(select)
        return True

    def clear_up_next(self):
        """Up next's Clear: every song out of it — Ctrl+Z puts them back."""
        queued = self.player.up_next()
        self.player.clear_queue()
        self._paint_up_next()
        if queued:
            n = len(queued)
            self._remember_edit(f"Cleared Up next ({n} song{'' if n == 1 else 's'})",
                                f"Put {n} song{'' if n == 1 else 's'} back in Up next",
                                lambda: (self.player.play_next(queued), self._paint_up_next()), lambda: True)

    def undo(self):
        """Ctrl+Z / Undo in the status line: put the last playlist change back (once). If the playlist
        file couldn't be written (the drive gone, say — the reason has been shown), it can be tried again."""
        if self._undo is None:
            self._set_notice("Nothing to undo", 2500)
            return
        _notice, undone, undo, possible = self._undo
        if not possible():
            self._undo = None
            self._set_notice("Can't undo: the playlist has changed since", 5000)
            return
        if undo() is False:
            return
        self._undo = None
        self._set_notice(undone, 3000)

    def _paint_undo_link(self):
        """Undo shows at the end of the status line while the line tells of a change it can undo."""
        show = self._undo is not None and self._notice == self._undo[0] and not self.mini_mode
        if show and not self.undo_link.winfo_manager():
            self.undo_link.place(in_=self.status_label, relx=1.0, rely=0.5, anchor="e")
        elif not show and self.undo_link.winfo_manager():
            self.undo_link.place_forget()

    # -- tabs ----------------------------------------------------------------
    def _restore_tabs(self, folder: str | None):
        saved = self.prefs.get("tabs")
        if saved is None:  # settings from before tabs existed
            saved = []
            if self.prefs["last_folder"]:
                kind = self.prefs["last_mode"] or ("album" if self.prefs["last_album_only"] else "folder")
                saved = [{"kind": kind, "path": self.prefs["last_folder"], "query": self.prefs["last_filter"]}]
        # Every tab comes back, even one on a drive that isn't connected yet: it's kept, with its
        # place in it, and is read once its folder is there (the loader checks, off this thread).
        for source in saved if isinstance(saved, list) else []:
            try:
                path, kind = source.get("path", ""), source.get("kind")
                if not (path and isinstance(path, str) and kind in ("album", "folder", "library", "playlist", "files")):
                    continue
                tab = Tab(kind, path, str(source.get("query") or ""))
                resume = source.get("resume") or {}
                tab.resume_path, tab.resume_at = str(resume.get("path", "")), float(resume.get("at", 0))
                tab.extras = [p for p in source.get("extras", []) if isinstance(p, str)]
            except (AttributeError, TypeError, ValueError):   # (a hand-edited settings file)
                continue
            self.tabs.append(tab)
        if folder:
            self.open_tab("folder", folder, play=True)
        elif self.tabs:
            try:
                current = int(self.prefs["current_tab"])
            except (TypeError, ValueError):
                current = 0
            self.select_tab(min(max(0, current), len(self.tabs) - 1))
        else:
            self._refresh_tabs()

    def open_tab(self, kind: str, path, query: str = "", play: bool = False, interrupt: bool = False):
        """Open (or switch to, if it's already open) a tab for this album / folder / library view.
        play: start it once it's loaded — but only when nothing is playing or paused. Music
        already going carries on while you set up other tabs; press play in the new one when ready.
        interrupt: play it even so (it was asked for by name: "Open with", a double-click)."""
        if kind == "album":
            self._mark_opened(Path(str(path)))
        if play and not interrupt and self.player.status().state is not State.STOPPED:
            play = False
            self._set_notice(f"Opened “{Path(str(path)).name}” · the music playing carries on: press play "
                             "there when you're ready", 6000)
        for i, tab in enumerate(self.tabs):
            if tab.same(kind, path, query):
                self.select_tab(i, then_play=play)
                return
        self.tabs.append(Tab(kind, str(path), query))
        self.select_tab(len(self.tabs) - 1, then_play=play)

    def select_tab(self, index: int, then_play: bool = False):
        if not 0 <= index < len(self.tabs):
            return
        if index != self.current and self.playlist.searching:
            self.playlist.query.set("")  # a search belongs to the tab it was typed in
        self.current = index
        tab = self.tabs[index]
        if tab.tracks is None:
            self._load_tab(tab, then_play)
        elif then_play:
            self._play_tab(index, 0)
        self._show_current_tab()

    def close_tab(self, index: int, remember: bool = True):
        if not 0 <= index < len(self.tabs):
            return
        closing = self.tabs[index]
        if closing.loading:   # the load under way is forgotten: reopened, the tab is read afresh
            self._forget_songs(closing)
        closing.play_when_loaded = False
        if remember:
            self._closed_tabs = (self._closed_tabs + [(index, closing)])[-10:]
            self._set_notice(f"Closed “{closing.title}” · click here or press Ctrl+Shift+T to reopen", 8000)
        if index == self.playing_tab:
            self.player.stop()
            self.player.set_tracks([])
            self.playing_tab = None
        elif self.playing_tab is not None and self.playing_tab > index:
            self.playing_tab -= 1
        del self.tabs[index]
        if not self.tabs:
            self.current = -1
            self._show_current_tab()
            return
        if self.current > index or self.current >= len(self.tabs):
            self.current -= 1
        self.select_tab(max(0, self.current))

    def reopen_tab(self):
        """Bring back the last closed tab, where it was (and where its song was)."""
        if not self._closed_tabs:
            return
        index, tab = self._closed_tabs.pop()
        if tab.kind == "playlist":   # songs may have been added to it while it was closed
            self._forget_songs(tab)
        index = min(index, len(self.tabs))
        self.tabs.insert(index, tab)
        if self.playing_tab is not None and self.playing_tab >= index:
            self.playing_tab += 1
        self._set_notice(f"Reopened “{tab.title}”", 2500)
        self.select_tab(index)

    def move_tab(self, source: int, target: int):
        """Move a tab to a new place along the strip (dragging, the tab menu or Ctrl+Shift+PgUp/PgDn).
        Nothing else changes: the same tab stays on show and the music carries on."""
        n = len(self.tabs)
        if not (0 <= source < n and 0 <= target < n) or source == target:
            return "break"
        self.tabs.insert(target, self.tabs.pop(source))

        def moved(i):
            if i is None:
                return None
            if i == source:
                return target
            if source < i <= target:
                return i - 1
            if target <= i < source:
                return i + 1
            return i

        self.current, self.playing_tab = moved(self.current), moved(self.playing_tab)
        self._shown_key = None
        self._refresh_tabs()
        return "break"

    def _cycle_tab(self, step: int):
        if self.tabs:
            self.select_tab((self.current + step) % len(self.tabs))
        return "break"

    def _tab_index(self, tab: Tab) -> int:
        return next((i for i, t in enumerate(self.tabs) if t is tab), -1)

    def _load_tab(self, tab: Tab, then_play: bool = False):
        """Scan a tab's files in the background (a whole library can take a few seconds). Play
        pressed meanwhile isn't lost: the tab plays once it's loaded."""
        tab.play_when_loaded = tab.play_when_loaded or then_play
        if tab.loading:
            return
        tab.loading = True
        progress = {"done": 0, "total": 0, "tracks": None, "missing": 0, "unavailable": False}
        tab.load_progress = progress
        index = self._index()
        if tab.kind in ("album", "folder") and index.indexer.running:
            index.indexer.prioritise(tab.path)   # (a first look under way reads this one next)

        def work():
            try:
                root = Path(tab.path)
                if not (root.is_file() if tab.kind == "playlist" else root.is_dir()):
                    progress["unavailable"] = True   # a drive that isn't connected (or since moved)
                    tracks = []
                elif tab.kind == "playlist":
                    tracks = self._read_playlist(root)
                    progress["missing"] = sum(1 for t in tracks if t.missing)
                elif tab.kind == "library":
                    albums = self._library_albums(root)
                    if tab.query:  # the same search as the Library window's
                        words = [_fold(w) for w in tab.query.split()]
                        albums = [a for a in albums if browse.matches(a, words, _fold)]
                    progress["total"] = len(albums)
                    tracks = self._library_tracks(albums)
                    progress["done"] = len(albums)
                elif tab.kind == "files":
                    tracks = []  # dropped files: all in extras
                else:
                    tracks = self._scan(root, whole_tree=tab.kind != "album")
                if not progress["unavailable"]:
                    # Albums / songs added later (a drive may be unplugged). Each song once: one
                    # added from an album the tab already holds isn't listed twice.
                    extra_tracks = [track for extra in list(tab.extras) for track in self._extra_tracks(extra)]
                    have = _keys_held(tracks, [audiofile.key(t.path) for t in extra_tracks]) if extra_tracks else set()
                    added = []
                    for track in extra_tracks:
                        if audiofile.key(track.path) not in have:
                            have.add(audiofile.key(track.path))
                            added.append(track)
                    if added:
                        tracks = tracks + added
                if tab.resume_path and tab.last_index < 0 and tracks:
                    progress["resume"] = _index_of_key(tracks, tab.resume_path)   # (here: it may ask the database)
            except OSError:
                log.warning("couldn't read the tab %s", tab.path, exc_info=True)
                tracks = []
            except Exception:   # (a file no reader expected: the tab ends up empty, never stuck loading)
                log.exception("couldn't load the tab %s", tab.path)
                tracks = []
            progress["tracks"] = tracks

        def start():
            if tab.load_progress is not progress:
                return   # closed meanwhile
            if tab.kind == "playlist" and Path(tab.path) in self._writing_playlists:
                self.root.after(50, start)   # songs are being added to it: read it once they're in
                return
            threading.Thread(target=work, name="tab-load", daemon=True).start()
            self.root.after(50, self._finish_load, tab, progress)

        start()

    def _extra_tracks(self, extra: str) -> list[Track]:
        """The songs an extra stands for: an album folder's, a file, or one track of a CUE image
        (saved as "<file>#<track number>"). None while its drive isn't connected."""
        path = Path(extra)
        if path.is_dir():
            return self._scan(path)
        if path.is_file():
            return [read_track(path)]
        file, mark, number = extra.rpartition("#")
        if mark and number.isdecimal() and Path(file).is_file():
            return [read_track(cue_track_path(Path(file), int(number)))]
        return []

    @staticmethod
    def _forget_songs(tab: Tab):
        """Forget a tab's songs (and any load under way), so it's read afresh when next shown. Its
        place is kept as the song's path, found again once it's read."""
        if tab.tracks is not None and tab.last_index >= 0:
            tab.resume_path = audiofile.key(tab.view_track().path)
        tab.last_index = -1
        tab.tracks, tab.loading, tab.load_progress, tab.play_when_loaded = None, False, None, False

    def _finish_load(self, tab: Tab, progress: dict):
        if tab.load_progress is not progress:
            return  # closed while loading (it's read afresh if reopened)
        index = self._tab_index(tab)
        if progress["tracks"] is None:
            if tab.kind == "library":
                self._set_notice(f"Loading library… {progress['done']} of {progress['total'] or '?'} albums", 0)
            self.root.after(100, self._finish_load, tab, progress)
            return
        tab.loading, tab.load_progress = False, None
        play, tab.play_when_loaded = tab.play_when_loaded, False
        tab.unavailable = progress["unavailable"]
        if tab.unavailable:
            # Kept as it was, place and all (nothing is saved over it), and read again when it's
            # next chosen — by then its drive may be connected.
            self._set_notice(f"“{tab.title}” isn't connected right now: {tab.path}", 8000)
            if index == self.current:
                self._show_current_tab()
            return
        tab.tracks = progress["tracks"]
        if tab.resume_path and tab.last_index < 0:  # where it got to last time
            tab.last_index = progress["resume"] if "resume" in progress else _index_of_key(tab.tracks, tab.resume_path)
            if tab.last_index < 0:
                tab.resume_at = 0.0
        if tab.kind == "playlist" and not tab.tracks:
            self._set_notice("Empty playlist: right-click songs in any tab and choose Add to playlist", 8000)
        elif progress["missing"]:
            self._set_notice(f"{progress['missing']} song(s) in “{tab.title}” can't be found: moved, or on a drive "
                             "that isn't plugged in?", 8000)
        elif not tab.tracks:
            self._set_notice(f"No music files in {tab.title}")
        elif tab.kind == "library":
            self._set_notice(f"Loaded {len(tab.tracks)} tracks from {progress['total']} albums")
        if index == self.current:
            self._show_current_tab()
        if play and tab.tracks:
            if tab.last_index >= 0:
                self._play_tab(index, tab.last_index, start_at=tab.resume_at)
            else:
                self._play_tab(index, 0)

    def _show_current_tab(self):
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        self.playlist.set_tracks(tab.tracks if tab and tab.tracks else [],
                                 flat=bool(tab and tab.kind == "playlist"))
        self._shown_key = None
        self._refresh_tabs()

    def _refresh_tabs(self):
        for tab in self.tabs:
            if not tab.info_requested:
                self._load_tab_info(tab)
        self.bar.set_tabs([t.title for t in self.tabs], self.current, self.playing_tab)
        self._show_empty(not self.tabs)

    def _load_tab_info(self, tab: Tab):
        """Look up a tab's album name without loading the whole album, so every tab is
        labelled properly (and shows its cover when flicked to) before you've opened it."""
        tab.info_requested = True
        if tab.kind in ("library", "playlist"):
            return

        def work():
            name, artist, first, found = "", "", None, None
            try:
                if tab.kind == "files":
                    first = next((Path(e) for e in tab.extras if Path(e).is_file()), None)
                else:
                    first = artwork.first_audio_file(Path(tab.path))
                if first:
                    found = read_track(first)
                    artist = found.artist
                    # A folder of several albums is named after whichever one it's playing.
                    if tab.kind == "album" or album_folder(first) == Path(tab.path):
                        name = found.album
            except OSError:
                log.info("couldn't look up the tab %s", tab.path, exc_info=True)
            except Exception:
                log.exception("couldn't look up the tab %s", tab.path)
            finally:   # always published: the tab's label waits for it
                tab.info = (name, artist, first, found.album if found is not None else "")

        threading.Thread(target=work, name="tab-info", daemon=True).start()
        self.root.after(80, self._finish_tab_info, tab)

    def _finish_tab_info(self, tab: Tab):
        if self._tab_index(tab) < 0:
            return  # closed meanwhile
        if tab.info is None:
            self.root.after(80, self._finish_tab_info, tab)
            return
        tab.album_name, tab.artist, tab.first_file, tab.first_album = tab.info
        self._refresh_tabs()

    def _play_tab(self, index: int, track: int, start_at: float = 0.0):
        """Play a tab's track. Switching tabs leaves the old one paused where it was: its
        song and position are already noted (see _poll), ready to carry on later."""
        if not 0 <= index < len(self.tabs) or not self.tabs[index].tracks:
            return
        tab = self.tabs[index]
        if self.playing_tab != index:
            self.player.set_tracks(tab.tracks)
            self.playing_tab = index
        tab.last_index, tab.resume_at = track, start_at
        ready = getattr(tab.tracks, "ready", None)
        if ready is not None:   # (a list read a page at a time: the song to start, and the ones round it)
            ready(track, self.player.order_around(track))
        self.player.play(track, start_at=start_at)

        self._shown_key = None
        self._refresh_tabs()

    def toggle_play(self):
        """Pause/resume what's playing; or, when you've flicked to another album, play that."""
        if (self.playing_tab is not None and self.current == self.playing_tab
                or _queued_alone(self.playing_tab, self.player.status())):
            self.player.toggle_pause()
            return
        if not 0 <= self.current < len(self.tabs):
            return
        tab = self.tabs[self.current]
        if tab.tracks is None:
            self.select_tab(self.current, then_play=True)  # loads it, then carries on
        elif tab.last_index >= 0:
            self._play_tab(self.current, tab.last_index, start_at=tab.resume_at)  # carry on where it was
        else:
            self._play_tab(self.current, 0)

    # -- shuffle and repeat ----------------------------------------------------
    def toggle_shuffle(self):
        self.player.set_shuffle(not self.player.shuffle)
        self._paint_order_buttons()
        self._set_notice("Shuffle on" if self.player.shuffle else "Shuffle off", 2000)

    def cycle_repeat(self):
        following = {"off": "all", "all": "one", "one": "off"}[self.player.repeat]
        self.player.set_repeat(following)
        self._paint_order_buttons()
        self._set_notice({"off": "Repeat off", "all": "Repeat the album / playlist",
                          "one": "Repeat this song"}[following], 2000)

    # -- stop after this song, and the sleep timer (for this session only) ------------------
    def stop_after_current(self) -> bool:
        """Stop at the end of the song playing, or — asked again — carry on after all. Returns
        whether it's now set. (Taking it away leaves a sleep timer's "end of this album" in place.)"""
        if self.player.stop_after == "song":
            album = self.sleep_timer is not None and self.sleep_timer.state == sleep.AT_ALBUM_END
            self.player.set_stop_after("album" if album else None)
            if self.sleep_timer is not None and self.sleep_timer.state == sleep.AT_BOUNDARY:
                self.sleep_timer.cancel()
                self.sleep_timer = None
            self._set_notice("Carries on after this song", 2500)
            return False
        self.player.set_stop_after("song")
        self._set_notice("Stops after this song", 2500)
        return True

    def set_sleep_timer(self, minutes):
        """Sleep timer: a number of minutes (sleep.CHOICES are offered), "album" (the end of the
        album playing), or None / 0 = off."""
        if self.sleep_timer is not None:
            self.sleep_timer.cancel()
            self.sleep_timer = None
        if not minutes:
            self._set_notice("Sleep timer off", 2500)
            return
        self.sleep_timer = sleep.SleepTimer(self.player, minutes)
        exclusive = self.exclusive.get()
        detail = (" (bit-perfect: it stops at the end of the song playing then, no fade)" if exclusive
                  and minutes != sleep.ALBUM else "")
        self._set_notice(f"Sleep timer: {sleep.choice_label(minutes).lower()}{detail}", 4000)

    def _tick_sleep(self, status):
        timer = self.sleep_timer
        if timer is not None:
            timer.tick(status)
            if not timer.active:
                self.sleep_timer = None

    def _sleep_line(self, status) -> str:
        """The status line while a sleep timer or "stop after this song" is set (with time left)."""
        timer = self.sleep_timer if self.sleep_timer is not None and self.sleep_timer.active else None
        after_song = getattr(status, "stop_after", "") == "song" and status.state is not State.STOPPED
        if after_song and (timer is None or timer.state == sleep.AT_ALBUM_END):
            return "Stops after this song"   # (sooner than the end of the album, so it's what happens)
        return timer.describe() if timer is not None else ""

    def open_sleep_menu(self):
        """Ctrl+Shift+S, in any window: the Stop after / Sleep timer menu, over the window the
        keyboard is in (the main window: at the status line)."""
        try:
            focus = self.root.focus_get()
        except (KeyError, tk.TclError):   # (a widget Tk can't name, e.g. a menu's own)
            focus = None
        window = focus.winfo_toplevel() if focus is not None else self.root
        if window is self.root and not self.mini_mode:
            x, y = self.status_label.winfo_rootx(), self.status_label.winfo_rooty()
        else:
            x, y = window.winfo_rootx() + theme.px(theme.GAP_LARGE), window.winfo_rooty() + theme.px(theme.GAP_LARGE)
        self._sleep_menu(self._menu()).popup(x, y)

    def _sleep_menu(self, menu):
        """Fill `menu` (a PopupMenu or a submenu of one) with "Stop after the current song" and the
        sleep timer's choices, ticked as they are now."""
        menu.add_check("Stop after the current song", self.player.stop_after == "song", self.stop_after_current,
                       keys.hint("stop_after"))
        timer = self.sleep_timer.target if self.sleep_timer is not None and self.sleep_timer.active else None
        sub = menu.add_submenu("Sleep timer")
        for choice in (*sleep.CHOICES, sleep.ALBUM):
            sub.add_check(sleep.choice_label(choice), timer == choice,
                          lambda c=choice: self.set_sleep_timer(c))
        sub.add_separator()
        sub.add("Off", lambda: self.set_sleep_timer(None), enabled=timer is not None)
        return menu

    def _paint_order_buttons(self):
        self.shuffle_button.set_on(self.player.shuffle)
        mode = self.player.repeat
        self.repeat_button.configure(text=glyph(self.root, "repeat_one" if mode == "one" else "repeat"))
        self.repeat_button.set_on(mode != "off")

    def _paint_status_dot(self, role: str):
        """The status dot in `role`'s colour (GOOD / WARN / MUTED; BG: nothing to say, no dot)."""
        self._status_colour = role
        self._status_photo = shapes.disc_photo(self.status_dot, theme.px(STATUS_DOT), role, "BG")
        self.status_dot.configure(image=self._status_photo, bg=theme.BG)

    # -- clipping guard ------------------------------------------------------------
    def _headroom_db(self) -> float:
        try:
            value = float(self.prefs.get("clip_headroom") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if value in GUARD_HEADROOM_CHOICES else 0.0

    def _measure_peaks(self):
        """Background worker: true peak of each file it's given (a few seconds per track, longer
        for hi-res). Songs skipped past meanwhile are dropped when their turn comes, so the one
        playing is never left waiting behind them; and no error stops the worker."""
        from .analysis import true_peak

        while True:
            path = self._peak_jobs.get()
            if isinstance(path, tuple):   # ("level", path): volume levelling's tags, loudness and peak
                self._measure_for_levelling(path[1])
                continue
            if path in self._peaks or path not in self._peak_wanted:
                continue
            stored = self._stored_peak(path)
            if stored is not None:   # (the library check read it already: known at once)
                self._peaks[path] = stored
                continue
            unwanted = _Unwanted(lambda path=path: path not in self._peak_wanted)
            try:
                peak = true_peak(path, unwanted)
            except audiofile.NotCached as error:   # (a network song not downloaded yet: nothing recorded)
                self._again_later(path, error, unwanted)
                continue
            except (OSError, RuntimeError, ValueError):
                log.info("couldn't measure the true peak of %s", path, exc_info=True)
                peak = 0.0
            except Exception:
                log.exception("couldn't measure the true peak of %s", path)
                peak = 0.0
            if peak is not None:   # (None: skipped past while it was measured)
                self._peaks[path] = peak

    def _again_later(self, job, error: audiofile.NotCached, unwanted):
        """A network song had no complete copy here in time (it's downloading): measured again
        while it's still wanted, rather than remembered as having no peak."""
        if error.reason != "cancel" and not unwanted.is_set():
            self._peak_jobs.put(job)

    def _measure_for_levelling(self, path: Path):
        """(On the worker.) A file's ReplayGain tags, and — in one read of the file — its loudness
        (only when the tags have no track gain) and true peak (unless already measured)."""
        if path not in self._level_wanted:
            return   # skipped past meanwhile
        try:
            if path not in self._gain_tags:
                self._gain_tags[path] = levelling.read_tags(path)
            tags = self._gain_tags[path]
            need_lufs = path not in self._lufs and (tags is None or tags.track_gain is None)
            need_peak = path not in self._peaks
            if need_peak:
                stored = self._stored_peak(path)
                if stored is not None:
                    self._peaks[path] = stored
                    need_peak = False
            if need_lufs or need_peak:
                unwanted = _Unwanted(lambda: path not in self._level_wanted)
                try:
                    measured = levelling.measure(path, peak=need_peak, loudness=need_lufs, cancel=unwanted)
                except audiofile.NotCached as error:   # (a network song not downloaded yet: nothing recorded)
                    self._again_later(("level", path), error, unwanted)
                    return
                if measured is None:
                    return   # skipped past while it was measured
                peak, lufs = measured
                if need_peak:
                    self._peaks[path] = peak or 0.0
                if need_lufs:
                    self._lufs[path] = lufs
            return
        except (OSError, RuntimeError, ValueError):
            pass
        except Exception:
            log.exception("couldn't work out the volume levelling of %s", path)
        self._gain_tags.setdefault(path, None)   # (not again: it can't be read)
        self._lufs.setdefault(path, None)
        self._peaks.setdefault(path, 0.0)

    def _measure_albums(self):
        """Background worker: an album's loudness, all its tracks as one programme (for Album
        levelling when its tags have no album gain). Given up when another album is wanted."""
        while True:
            folder, track_path = self._album_jobs.get()
            if folder in self._album_lufs or folder != self._album_wanted:
                continue
            cancel = self._album_cancel
            try:
                files = album_files(track_path)[1]
                # (a folder of more songs than an album holds, singles say, isn't measured as one)
                result = ((None, None) if len(files) > levelling.ALBUM_MOST_FILES
                          else loudness.album_loudness(files, cancel))
            except audiofile.NotCached:
                # A network album isn't all downloaded yet: measured again in a while (never from a part of it).
                self._album_later[folder] = time.monotonic() + ALBUM_AGAIN_S
                if self._album_wanted == folder:
                    self._album_wanted = None
                continue
            except (OSError, RuntimeError, ValueError):
                result = (None, None)
            except Exception:
                log.exception("couldn't measure the album %s", folder)
                result = (None, None)
            if result is not None:   # (None: cancelled — asked again if it's wanted again)
                self._album_lufs[folder] = result[0]

    def _stored_peak(self, path: Path) -> float | None:
        """(On a worker.) A song's true peak from the library quality check, when it has been checked
        and its file hasn't changed since; None to measure it."""
        try:
            return library_check.stored_peak(self._index().db, path)
        except Exception:   # (a database that can't be read just now: measured as before)
            log.info("couldn't read the stored check of %s", path, exc_info=True)
            return None

    def _stored_check(self, path: Path):
        """(On a short-lived worker, for Now Playing.) A song's stored check and when it was made, or None."""
        db = self._index().db
        try:
            return library_check.stored(db, path)
        except Exception:
            log.info("couldn't read the stored check of %s", path, exc_info=True)
            return None
        finally:
            db.release()

    def _remember_check(self, track: Track, result, file) -> None:
        """(On Now Playing's worker.) Keep its check, so the library check needn't read the file again."""
        db = self._index().db
        try:
            library_check.remember(db, track, result, file)
        except Exception:
            log.info("couldn't keep the check of %s", track.path, exc_info=True)
        finally:
            db.release()

    def _want_album(self, folder: Path, track_path: Path):
        if folder == self._album_wanted or self._album_later.get(folder, 0.0) > time.monotonic():
            return
        self._album_cancel.set()   # (the album being measured isn't wanted any more)
        self._album_cancel = threading.Event()
        self._album_wanted = folder
        self._album_jobs.put((folder, track_path))

    def _levelling_setting(self) -> str:
        setting = self.prefs.get("levelling")
        return setting if setting in dict(levelling.MODES) else "off"

    def _preamp_db(self) -> float:
        try:
            value = float(self.prefs.get("levelling_preamp") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if value in levelling.PREAMP_CHOICES else 0.0

    def _apply_levelling(self, track: Track | None, status):
        """Keep volume levelling in step with what's playing, as the clipping guard does: this
        song's gain and the next one's, worked out ahead so it's in place when that song joins on.
        A song's gain isn't changed once it's playing (unless the choice in Settings changes), so a
        late measurement never jumps the level part-way through; it counts from the next song. The
        one exception is downwards only: a gain worked out before the song's true peak was measured
        (from its tags' peak, which is often just the highest sample) is held down if the measured
        peak shows it would go past the ceiling (levelling.held), as the clipping guard would.
        Off: every gain is taken away and playback is exactly as without levelling."""
        setting = self._levelling_setting()
        if setting == "off" or track is None:
            if self._level_wanted or self._level_set_with:
                self.player.set_levelling(None)
                self._level_wanted, self._level_set_with = set(), {}
            return
        playing = self.playing_tab
        tab = self.tabs[playing] if playing is not None and playing < len(self.tabs) else None
        following = self.player.following_path()   # (Up next first, as the engine plays it)
        up_next = self.player.up_next()
        entries = [(track.path, status.index < 0)]
        if following is not None and following != track.path:
            entries.append((following, bool(up_next) and up_next[0].path == following))
        wanted = {path for path, _ in entries}
        if wanted != self._level_wanted:
            self._level_wanted = wanted   # (anything else still queued is skipped)
            for path, _ in entries:
                self._peak_jobs.put(("level", path))   # (does nothing for what's already known)
        preamp = self._preamp_db()
        for path, queued in entries:
            mode = levelling.mode_for(setting, tab.kind if tab else None, self.player.shuffle, queued)
            choices = (setting, preamp, mode)
            current, set_with = self.player.levelling_for(path), self._level_set_with.get(path)
            peak_known = path in self._peaks
            if path == track.path and current is not None and set_with is not None and set_with[0] == choices:
                if peak_known and not set_with[1]:   # its true peak has come in since: only ever down
                    level = levelling.held(current, self._gain_tags.get(path), self._peaks.get(path) or None)
                    if level != current:
                        self.player.set_levelling(path, level)
                    self._level_set_with[path] = (choices, True)
                continue   # playing: keep its level
            tags = self._gain_tags.get(path)
            album = None
            if mode == "album" and path in self._gain_tags and (tags is None or tags.album_gain is None):
                folder = album_folder(Path(audiofile.real_path(path)))
                if folder in self._album_lufs:
                    album = self._album_lufs[folder]
                elif path == track.path:
                    self._want_album(folder, path)
            level = levelling.level_for(mode, tags, self._lufs.get(path), album, self._peaks.get(path) or None,
                                        preamp)
            if level != current:
                self.player.set_levelling(path, level)
            if level is not None:
                self._level_set_with.pop(path, None)   # (newest last: the oldest are let go below)
                self._level_set_with[path] = (choices, peak_known)
        while len(self._level_set_with) > 16:
            self._level_set_with.pop(next(iter(self._level_set_with)))

    def _levelling_chosen(self, _event=None):
        """Settings → Volume levelling: Off / Track / Album / Smart."""
        self.prefs["levelling"] = levelling.MODES[self.levelling_box.current()][0]
        self.levelling_box.selection_clear()
        self._level_set_with = {}   # every song worked out again, the one playing too
        self.preamp_box.configure(state="disabled" if self.prefs["levelling"] == "off" else "readonly")

    def _preamp_chosen(self, _event=None):
        self.prefs["levelling_preamp"] = levelling.PREAMP_CHOICES[self.preamp_box.current()]
        self.preamp_box.selection_clear()
        self._level_set_with = {}

    def _guard_changed(self):
        self._guard_for = None  # re-evaluate on the next refresh
        if not self.clip_guard.get():
            self.player.set_trim(None, 1.0)

    def _headroom_chosen(self, _event=None):
        """(0.24.0) Settings → My DAC has headroom: the guard's ceiling moves; tracks are re-judged
        on the next refresh (their true peaks are already measured)."""
        self.prefs["clip_headroom"] = GUARD_HEADROOM_CHOICES[self.headroom_box.current()]
        self.headroom_box.selection_clear()
        self._guard_for = None

    def _apply_guard(self, track: Track | None, index: int):
        """Keep the clipping guard in step with what's playing: measure this track and the
        next one ahead of time, and turn each down only if its peaks go over full scale. The
        next one's setting goes in early, so it's in place when that track joins on gaplessly."""
        if not self.clip_guard.get() or track is None:
            return
        paths = [track.path]
        following = self.player.following_path()  # (Up next first, as the engine plays it)
        if following is not None:
            paths.append(following)
        if track.path != self._guard_for or set(paths) != self._peak_wanted:
            self._guard_for = track.path
            self._peak_wanted = set(paths)   # (anything else still queued is skipped)
            for path in paths:
                if path not in self._peaks:
                    self._peak_jobs.put(path)
        for path in paths:
            peak = self._peaks.get(path)
            if peak is None:
                continue
            # (0.24.0) Measured at 8x; just under full scale (plus any headroom the DAC has), with
            # 0.17 dB spare — the most 8x can under-read (analysis.guard_trim).
            trim = guard_trim(peak, self._headroom_db())
            if abs(self.player.guard_for(path) - trim) > 1e-6:   # (the guard's own part: not levelling's)
                self.player.set_trim(path, trim)

    # -- playlists -------------------------------------------------------------
    @staticmethod
    def _read_playlist(path: Path) -> list[Track]:
        """A playlist's songs, in order. Files that aren't there stay listed (marked missing),
        so a playlist never loses songs just because a drive was unplugged. A drive or server
        that doesn't answer is asked once: when looking for one of its songs fails slowly (a NAS
        switched off can take half a minute a look), its other songs are listed as missing without
        asking again."""
        silent: set[str] = set()   # drives / servers (path anchors) that didn't answer
        tracks = []
        for song in playlists.read(path):
            anchor = song.anchor.lower()
            here = False
            if anchor not in silent:
                started = time.monotonic()
                here = song.is_file()
                if not here and time.monotonic() - started > SLOW_LOOK_S:
                    silent.add(anchor)
            tracks.append(read_track(song) if here else Track(song, title_from_filename(song), missing=True))
        return tracks

    def _playlist_tab(self, path: Path) -> Tab | None:
        return next((t for t in self.tabs if t.kind == "playlist" and Path(t.path) == Path(path)), None)

    def _known_playlists(self) -> list[Path]:
        """Saved playlists plus any open from elsewhere."""
        found = playlists.saved()
        for tab in self.tabs:
            if tab.kind == "playlist" and Path(tab.path) not in found:
                found.append(Path(tab.path))
        return found

    def _for_prompt(self):
        """(0.21.0) In-app boxes sit over the full window: leave the mini strip first."""
        if self.mini_mode:
            self.toggle_mini()

    @staticmethod
    def playlist_name_problem(name: str, current: Path | None = None) -> str | None:
        """What's wrong with this name for a new playlist (or for renaming `current`), or None.
        Shown under the name box, which stays open so the name can be changed."""
        if not name.strip():
            return "Type a name for the playlist."
        target = (playlists.path_for(name) if current is None
                  else current.with_name(f"{playlists.safe_name(name)}{current.suffix}"))
        if target.exists() and (current is None or target != current):
            # (Windows ignores case: "rock" clashes with "Rock" — name the one that's there.)
            there = next((p for p in target.parent.iterdir() if p.name.lower() == target.name.lower()), target)
            return f"There's already a playlist called “{there.stem}”."
        return None

    def new_playlist(self, tracks: list[Track] | None = None):
        self._for_prompt()
        count = len(tracks) if tracks else 0
        name = popups.ask_text(self.root, "New playlist", "Name for the new playlist:",
                               initial=playlists.unused_name(), ok="Make playlist",
                               detail=f"{count} song{'' if count == 1 else 's'} will go in it." if count else "",
                               validate=self.playlist_name_problem)
        if not name:
            return
        path = playlists.path_for(name)
        try:
            playlists.write(path, tracks or [])
        except (OSError, ValueError) as exc:   # (ValueError: a name the file's encoding can't hold)
            popups.tell(self.root, "Couldn't make the playlist", str(exc), tone="warn")
            return
        self.open_tab("playlist", path)
        if tracks:
            self._set_notice(f"Made “{path.stem}” with {len(tracks)} song{'' if len(tracks) == 1 else 's'}")

    def open_playlist_file(self):
        start = playlists.folder() if playlists.folder().is_dir() else self._library_root()
        file = filedialog.askopenfilename(title="Open a playlist", initialdir=str(start), parent=self.root,
                                          filetypes=[("Playlists", "*.m3u8 *.m3u"), ("All files", "*.*")])
        if file:
            self.open_tab("playlist", file, play=True)

    def add_to_playlist(self, path: Path, tracks: list[Track]):
        """Add songs to the end of a playlist. A tab of it that's loaded is the list added to; one
        still loading gets them once it has loaded (else its list, read before, would later be saved
        over them). With no tab loaded, the file is read and written on a thread: reading every
        song's tags first can take seconds on a slow drive. One change to a playlist at a time."""
        path = Path(path)
        tab = self._playlist_tab(path)
        if (tab is not None and tab.loading) or path in self._writing_playlists:
            self.root.after(50, self.add_to_playlist, path, tracks)
            return
        added = f"Added {len(tracks)} song{'' if len(tracks) == 1 else 's'} to “{path.stem}”"
        if tab is not None and tab.tracks is not None:
            if self._save_playlist(path, tab.tracks + list(tracks)):
                self._set_notice(added)
            return
        if not self._may_rewrite(path):
            return
        self._writing_playlists.add(path)
        result: dict = {}

        def work():
            try:
                playlists.write(path, self._read_playlist(path) + list(tracks))
            except Exception as error:   # (OSError mostly: the file locked, or the disk full)
                if not isinstance(error, OSError):
                    log.exception("couldn't add to the playlist %s", path)
                result["error"] = error
            finally:
                result["done"] = True

        def finish():
            if "done" not in result:
                self.root.after(50, finish)
                return
            self._writing_playlists.discard(path)
            if "error" in result:
                self._for_prompt()
                popups.tell(self.root, "Couldn't save the playlist", str(result["error"]), tone="warn")
            else:
                self._set_notice(added)

        threading.Thread(target=work, name="playlist-add", daemon=True).start()
        self.root.after(50, finish)

    def _may_rewrite(self, path: Path) -> bool:
        """A playlist another program saved in an older encoding (a Windows code page) is written back
        as UTF-8, which could keep a misread name wrong for good: ask first. True to go ahead."""
        encoding = playlists.fallback_encoding(path)
        if not encoding:
            return True
        self._for_prompt()
        return popups.confirm(
            self.root, f"Save “{path.stem}” as UTF-8?",
            f"This playlist was saved by another program in an older text format ({encoding}). Saving this "
            "change writes it as UTF-8, which today's players read.",
            detail=("If a song's name has letters that format can't hold, it may have been misread when the "
                    "playlist was opened (the song shows as missing) — and it would be saved that way. "
                    "Don't save leaves the file exactly as it is."),
            ok="Save as UTF-8", cancel="Don't save", tone="warn")

    def _save_playlist(self, path: Path, tracks: list[Track]) -> bool:
        """Write the playlist, and update its tab (and the music, if it's playing from it). False
        when it couldn't be written (the reason has been shown) or wasn't (a playlist in an older
        encoding, left as it is)."""
        if not self._may_rewrite(path):
            return False
        try:
            playlists.write(path, tracks)
        except (OSError, ValueError) as exc:   # (ValueError: a name the file's encoding can't hold)
            self._for_prompt()
            popups.tell(self.root, "Couldn't save the playlist", str(exc), tone="warn")
            return False
        tab = self._playlist_tab(path)
        if tab is None or tab.tracks is None:
            return True
        index = self._tab_index(tab)
        # A tab that isn't playing keeps its place on the same song (the same Track, wherever it has
        # moved to); if that song was taken out, the tab starts again from the top.
        resume = tab.view_track() if index != self.playing_tab and tab.last_index >= 0 else None
        tab.tracks = list(tracks)
        if resume is not None:
            tab.last_index = next((i for i, t in enumerate(tab.tracks) if t is resume), -1)
            if tab.last_index < 0:
                tab.resume_at, tab.resume_path = 0.0, ""
        if index == self.playing_tab:
            self.player.set_tracks(tab.tracks, keep_playing=True)
        if index == self.current:
            self._show_current_tab()
        return True

    def _remove_from_playlist(self, index: int | list[int]):
        """Take songs out of the playlist on show (Delete, or the songs' menu) — one Ctrl+Z puts them
        all back."""
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        if tab is None or tab.kind != "playlist" or not tab.tracks:
            return
        asked = [index] if isinstance(index, int) else index
        indices = sorted({i for i in asked if 0 <= i < len(tab.tracks)})
        if not indices:
            return
        gone = set(indices)
        tracks = [track for i, track in enumerate(tab.tracks) if i not in gone]
        if len(indices) == 1:
            song = tab.tracks[indices[0]]
            notice, undone = f"Removed “{song.title}” from “{tab.title}”", f"Put “{song.title}” back"
        else:
            notice, undone = f"Removed {len(indices)} songs from “{tab.title}”", f"Put {len(indices)} songs back"
        self._edit_playlist(tab, tracks, notice, undone, select=min(indices[0], len(tracks) - 1),
                            select_back=indices if len(indices) > 1 else indices[0])


    def _move_in_playlist(self, index: int, step: int):
        """A song a place up or down the playlist on show (Alt+↑ / ↓, or its menu) — Ctrl+Z moves it back."""
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        if tab is None or tab.kind != "playlist" or not tab.tracks:
            return
        target = index + step
        if not (0 <= index < len(tab.tracks) and 0 <= target < len(tab.tracks)):
            return
        tracks = list(tab.tracks)
        tracks[index], tracks[target] = tracks[target], tracks[index]
        song = tracks[target]
        self._edit_playlist(tab, tracks, f"Moved “{song.title}” {'up' if step < 0 else 'down'}",
                            f"Moved “{song.title}” back", select=target, select_back=index)

    def _menu(self) -> popups.PopupMenu:
        """(0.21.0) A right-click menu in the app's colours (popups.PopupMenu), over the main window."""
        return popups.PopupMenu(self.root)

    def _track_menu(self, rows: list[int], event):
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        if tab is None or not tab.tracks or not rows:
            return
        tracks = _tracks_at(tab.tracks, rows)
        single = len(rows) == 1
        album = getattr(rows, "album", not single)   # (an album header's songs, not songs picked one by one)
        menu = self._menu()
        menu.add("Play this album" if album else "Play", lambda: self._play_tab(self.current, rows[0]),
                 keys.hint("list_play"))
        menu.add("Play next", lambda: self._queue_tracks(tracks, front=True), keys.hint("list_play_next"))
        menu.add("Add to queue", lambda: self._queue_tracks(tracks, front=False), keys.hint("list_queue"))
        menu.add_separator()
        add = menu.add_submenu("Add to playlist" if single else f"Add these {len(rows)} songs to playlist")
        targets = [p for p in self._known_playlists() if not (tab.kind == "playlist" and Path(tab.path) == p)]
        for path in targets:
            add.add(path.stem, lambda p=path: self.add_to_playlist(p, tracks))
        add.add_separator()
        add.add("New playlist…", lambda: self.new_playlist(tracks))   # (Ctrl+N makes an empty one)
        favourite = all(track in self.favourites for track in tracks)
        menu.add("Remove from favourites" if favourite else "Add to favourites",
                 lambda: self.favourites.set(tracks, not favourite), quick_search.HEART)
        if self._tag_editing():
            menu.add_separator()
            menu.add("Edit tags…" if single else f"Edit tags of these {len(rows)} songs…",
                     lambda: self.edit_tags(tracks), keys.hint("edit_tags"))
            menu.add("Tag history (undo)…", self.show_tag_history)
        if tab.kind == "playlist" and single:
            index = rows[0]
            menu.add_separator()
            menu.add("Move up", lambda: self._move_in_playlist(index, -1), keys.hint("move_song", 0),
                     enabled=index > 0)
            menu.add("Move down", lambda: self._move_in_playlist(index, 1), keys.hint("move_song", 1),
                     enabled=index < len(tab.tracks) - 1)
            menu.add("Remove from playlist", lambda: self._remove_from_playlist(index), keys.hint("remove"))
        elif tab.kind == "playlist":
            menu.add_separator()
            menu.add(f"Remove these {len(rows)} songs from playlist", lambda: self._remove_from_playlist(rows),
                     keys.hint("remove"))
        menu.add_separator()
        self._sleep_menu(menu)
        menu.add_separator()
        self._columns_menu(menu.add_submenu("Columns"))
        menu.popup(event.x_root, event.y_root)

    def _columns_menu(self, menu):
        """Artist / Album / Plays columns in the track list (off by default: the window is narrow).
        Fills `menu` (a PopupMenu, or a submenu of one) with a tick item per column. A wide window has
        its own setting, with Format too (the columns its layout has room for); a column ticked but
        hidden because its value is the same on every row of the tab says so."""
        if self._tier == layout.COLUMN:
            choices = (("artist", "Artist"), ("album", "Album"), ("plays", "Plays"))
        else:
            allowed = layout.ALLOWED[self._tier]
            choices = tuple(c for c in (("artist", "Artist"), ("album", "Album"), ("quality", "Format"),
                                        ("plays", "Plays")) if c[0] in allowed)
        wanted = self.playlist.wanted()
        for column, label in choices:
            on = column in wanted
            if on and self._tier != layout.COLUMN and column not in self.playlist.shown:
                label += " (same on every row)"
            menu.add_check(label, on, lambda c=column, on=on: self._set_track_column(c, not on))
        return menu

    def _heading_menu(self, event):
        menu = self._menu()
        self._columns_menu(menu)
        menu.popup(event.x_root, event.y_root)

    def _set_track_column(self, column: str, on: bool):
        columns = [c for c in self.playlist.wanted() if c != column] + ([column] if on else [])
        if self._tier == layout.COLUMN:
            self.playlist.set_columns(columns)
            self.prefs["track_columns"] = list(self.playlist.columns)
        else:
            self.playlist.set_wide_columns(columns)
            self.prefs["wide_track_columns"] = list(self.playlist.wide_columns)

    def _tab_menu(self, index: int, event):
        if not 0 <= index < len(self.tabs):
            return
        tab = self.tabs[index]
        menu = self._menu()
        on_show = index == self.current   # (the keys act on the tab on show: their hints only there)
        if tab.kind == "playlist":
            menu.add("Rename playlist…", lambda: self.rename_playlist(index), keys.hint("rename") if on_show else "")
            menu.add("Delete playlist…", lambda: self.delete_playlist(index))
            menu.add_separator()
        if len(self.tabs) > 1:
            menu.add("Move left", lambda: self.move_tab(index, index - 1), keys.hint("move_tab", 0) if on_show else "",
                     enabled=index > 0)
            menu.add("Move right", lambda: self.move_tab(index, index + 1), keys.hint("move_tab", 1) if on_show else "",
                     enabled=index < len(self.tabs) - 1)
            menu.add_separator()
        menu.add("Close tab", lambda: self.close_tab(index), keys.hint("close_tab") if on_show else "")
        menu.popup(event.x_root, event.y_root)

    def rename_playlist(self, index: int):
        tab = self.tabs[index]
        self._for_prompt()
        current = Path(tab.path)
        name = popups.ask_text(self.root, "Rename playlist", "New name for the playlist:", initial=current.stem,
                               ok="Rename", validate=lambda text: self.playlist_name_problem(text, current))
        if not name or tab not in self.tabs:
            return
        try:
            tab.path = str(playlists.rename(current, name))
        except OSError as exc:
            popups.tell(self.root, "Couldn't rename the playlist", str(exc), tone="warn")
            return
        self._refresh_tabs()

    def delete_playlist(self, index: int):
        """Delete a playlist: to the Recycle Bin, so it can be restored — or, on a drive that has
        none (a network drive, a USB stick), for good once that's been said."""
        tab = self.tabs[index]
        path = Path(tab.path)
        name = path.stem
        self._for_prompt()
        binned = recycle.can_recycle(path)
        if binned:
            asked = popups.confirm(self.root, f"Delete the playlist “{name}”?",
                                   f"It goes to the {recycle.BIN_NAME}, so it can be restored from there. The songs "
                                   "themselves aren't touched.", ok="Delete", danger=True)
        else:
            asked = popups.confirm(self.root, f"Delete the playlist “{name}” for good?",
                                   f"It's on a drive with no {recycle.BIN_NAME} (a network drive or a USB stick), so "
                                   "it can't be restored. The songs themselves aren't touched.",
                                   ok="Delete for good", danger=True)
        if not asked or tab not in self.tabs:   # (closed while the box was up)
            return
        index = self.tabs.index(tab)
        self.close_tab(index, remember=False)
        if not binned:
            try:
                path.unlink()
            except OSError as exc:
                self._set_notice(f"Couldn't delete the playlist file: {exc}")
                return
            self._set_notice(f"Deleted the playlist “{name}”")
            return
        result: dict = {}

        def work():   # (the shell's delete runs a message loop of its own: never on this thread)
            try:
                recycle.recycle(path)
            except OSError as exc:
                result["error"] = exc
            finally:
                result["done"] = True

        def finish():
            if "done" not in result:
                self.root.after(50, finish)
            elif "error" in result:
                self._set_notice(f"Couldn't move the playlist to the {recycle.BIN_NAME} ({result['error']}): "
                                 "it's still where it was", 10000)
            else:
                self._set_notice(f"Moved the playlist “{name}” to the {recycle.BIN_NAME}: restore it from there "
                                 "to have it back", 10000)

        threading.Thread(target=work, name="playlist-delete", daemon=True).start()
        self.root.after(50, finish)

    # -- opening things ------------------------------------------------------
    def choose_folder(self):
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        initial = tab.path if tab else str(self._library_root())
        folder = filedialog.askdirectory(title="Choose a folder of music", initialdir=initial, mustexist=True)
        if folder:
            self.open_tab("folder", folder, play=True)

    def _library_root(self) -> Path:
        """The (first) library folder."""
        return library_folders(self.prefs)[0]

    def _library_roots(self) -> list[Path]:
        return library_folders(self.prefs)

    def _current_tab_title(self) -> str | None:
        return self.tabs[self.current].title if 0 <= self.current < len(self.tabs) else None

    def add_album_to_current(self, album: Path, on_done=lambda message: None, tab: Tab | None = None):
        """Add an album (from the Library) to the bottom of the tab on show (or `tab`, when it was
        dragged onto one). A playlist saves it into its file; any other tab remembers it, so it's
        still there after a restart. If the tab is playing, the music carries on uninterrupted."""
        if tab is None and not 0 <= self.current < len(self.tabs):
            self.open_tab("album", album)
            on_done(f"Opened “{album.name}” in a new tab")
            return
        tab = tab or self.tabs[self.current]
        self._mark_opened(album)
        result: dict = {}

        def work():
            result["tracks"] = self._scan_or_nothing(album)

        threading.Thread(target=work, name="add-album", daemon=True).start()
        self.root.after(50, self._finish_add_album, tab, album, result, on_done)

    def _finish_add_album(self, tab: Tab, album: Path, result: dict, on_done):
        if self._tab_index(tab) < 0:
            return  # the tab was closed meanwhile
        if "tracks" not in result or tab.loading:  # (a tab still loading gets it once it's loaded)
            self.root.after(50, self._finish_add_album, tab, album, result, on_done)
            return
        tracks = result["tracks"]
        name = tracks[0].album if tracks and tracks[0].album else album.name
        have = _keys_held(tab.tracks, [audiofile.key(t.path) for t in tracks]) if tab.tracks else set()
        if not tracks:
            message = f"No FLAC, WAV or MP3 files in “{album.name}”"
        elif tab.kind != "playlist" and (Path(tab.path) == album or str(album) in tab.extras
                                         or all(audiofile.key(t.path) in have for t in tracks)):
            message = f"“{name}” is already in “{tab.title}”"
        elif tab.kind == "playlist":
            self.add_to_playlist(Path(tab.path), tracks)
            message = f"Added “{name}” ({len(tracks)} songs) to “{tab.title}”"
        else:
            tab.extras.append(str(album))
            if tab.tracks is not None:  # (not loaded yet: loading it will include the album)
                tab.tracks = tab.tracks + tracks
                index = self._tab_index(tab)
                if index == self.playing_tab:
                    self.player.set_tracks(tab.tracks, keep_playing=True)
                if index == self.current:
                    self._show_current_tab()
            message = f"Added “{name}” ({len(tracks)} songs) to “{tab.title}”"
        self._set_notice(message)
        on_done(message)

    def open_library(self, view: str | None = None, album: Path | None = None, search: str | None = None):
        """The Library window (one at a time): an open one is brought to the front as it was, search
        and all (Ctrl+L) — unless the library folders have been changed in Settings since: then it's
        made afresh for the new folders. `view`: show that view (e.g. "recent" for new music);
        `album`: show (and select) that album; `search`: search the Library for that."""
        if view is not None:
            self.prefs["library_view"] = view
        if (self._library is not None and self._library.win.winfo_exists()
                and list(self._library.roots) != self._library_roots()):
            self._library.close()
        if self._library is not None and self._library.win.winfo_exists():
            if view is not None:
                self._library.set_view(view)
            if album is not None or search is not None:
                self._library.show(album=album, search=search)
            self._library.lift()
            return
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        current = Path(tab.path) if tab and tab.kind == "album" else None
        if album is not None:
            current = Path(album)
        library = LibraryWindow(self.root, self._library_roots(), current,
                                on_choose=lambda path: self.open_tab("album", path, play=True),
                                on_root_changed=self._set_library_folders,
                                on_load_all=lambda root, query: self.open_tab("library", root, query),
                                on_add=self.add_album_to_current, add_target=self._current_tab_title,
                                on_drag=self._album_drag, on_queue=self.queue_album,
                                view=self.prefs.get("library_view") or "albums",
                                on_view=lambda view: self.prefs.update(library_view=view),
                                geometry=self.prefs["library_geometry"],
                                on_moved=lambda geometry: self.prefs.update(library_geometry=geometry),
                                on_edit_tags=self.edit_album_tags, can_edit_tags=self._tag_editing,
                                new_albums=lambda: self._new_albums, actions=self.song_actions(),
                                on_add_tracks=self._add_tracks_to_current, checker=self.checker,
                                song_columns=tuple(self.prefs.get("library_song_columns") or ()),
                                on_song_columns=lambda columns: self.prefs.update(library_song_columns=columns))
        self._library = self._with_keys(library, "library", library.key_handlers())
        if album is not None or search:
            library.show(album=album, search=search or None)

    # -- the library index --------------------------------------------------------------------------
    def _index(self) -> library_index.LibraryIndex:
        """The library (nothing is read here: safe on this thread; its reads belong on workers)."""
        return library_index.shared(self._library_roots())

    def _refresh_index(self):
        """Bring the library database up to date in the background (only changed files are
        read; the first time ever, every file once), so the Library opens at once. (Whether the
        folders are there is found out on the worker: a sleeping network drive can take a while.)"""
        index = self._index()
        if index.refreshed:
            return
        threading.Thread(target=_refresh_library, args=(index,), name="library-start", daemon=True).start()

    def _library_albums(self, root: Path) -> list:
        """Every album in the library, from the database, when `root` is one of the library
        folders (a "library" tab: the Library's "Open all albums in a tab"); else every album found
        under `root`. (A worker thread's work.)"""
        index = self._index()
        if Path(root) in index.roots:
            if not index.built:
                index.refresh()   # the first look isn't finished: wait for it (it may be under way)
            return index.albums()
        return find_albums(root) if root.is_dir() else []

    def _library_tracks(self, albums: list) -> list[Track]:
        """Every song of these albums, in this order: from the database (nothing on disk is looked at
        again, so a whole library loads quickly), else read from the files. (A worker thread's work.)
        When the database holds every one of the albums, the list holds just the songs' ids and reads
        Tracks a page at a time (library_tracks.LibraryTrackList): a whole library opens without a
        Track held for every song."""
        index = self._index()
        light = library_tracks.build(index.db, albums)
        if len(light.groups) == len(albums):
            return light
        stored = index.albums_tracks([album.path for album in albums])
        tracks: list[Track] = []
        for album in albums:
            found = stored.get(album.path)
            tracks += found if found is not None else self._scan(album.path)
        return tracks

    def _scan(self, folder: Path, whole_tree: bool = False) -> list[Track]:
        """An album's (or a whole folder's) tracks in play order: from the index when it's up to
        date for that folder, else read from the files."""
        tracks = self._index().album_tracks(Path(folder), whole_tree=whole_tree)
        if tracks is not None:
            return tracks
        return scan_folder(folder) if whole_tree else scan_album(folder)

    def _scan_or_nothing(self, album: Path) -> list[Track]:
        """An album's tracks, for a worker thread that must always answer: none if it can't be read."""
        try:
            return self._scan(album)
        except OSError:
            log.info("couldn't read %s", album, exc_info=True)
            return []
        except Exception:
            log.exception("couldn't read %s", album)
            return []

    # -- tag editing (Experimental) -----------------------------------------------------------------
    def _tag_editing(self) -> bool:
        """Is Settings → Experimental → Tag editing on? Every way in checks this first."""
        return experiments.enabled(self.prefs, tag_editor.EXPERIMENT)

    def edit_selected_tags(self):
        """Key E: edit the selected song (or the album whose header is selected)."""
        if not self._tag_editing():
            return
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        rows = self.playlist.selected_rows() if tab is not None and tab.tracks else []
        if rows:
            self.edit_tags(_tracks_at(tab.tracks, rows))

    def edit_tags(self, tracks: list[Track]):
        if not self._tag_editing() or not tracks:
            return
        if self._tag_window is not None and self._tag_window.win.winfo_exists():
            if self._tag_window.saving is not None:
                self._tag_window.lift()
                self._set_notice("Still saving the last tag edit…")
                return
            if not self._tag_window.may_discard():   # (edits typed there, not saved: asked first)
                return
            self._tag_window.close()
        window = TagWindow(self.root, tracks, self.prefs, on_saved=self._tags_saved,
                           busy=self._busy_files, on_history=self.show_tag_history)
        self._tag_window = self._with_keys(window, "tags", window.key_handlers())

    def edit_album_tags(self, album: Path):
        """From the Library: edit a whole album's tags."""
        if not self._tag_editing():
            return
        result: dict = {}

        def work():
            result["tracks"] = self._scan_or_nothing(album)

        def finish():
            if "tracks" not in result:
                self.root.after(50, finish)
            elif result["tracks"]:
                self.edit_tags(result["tracks"])
            else:
                self._set_notice(f"No music found in “{album.name}”")

        threading.Thread(target=work, name="tag-album", daemon=True).start()
        self.root.after(50, finish)

    def show_tag_history(self):
        if not self._tag_editing():
            return
        if self._tag_history is not None and self._tag_history.win.winfo_exists():
            self._tag_history.reload()
            self._tag_history.lift()
            return
        self._tag_history = self._with_keys(TagHistoryWindow(self.root, self.prefs, on_saved=self._tags_saved,
                                                             busy=self._busy_files))

    def _busy_files(self) -> set[Path]:
        """Files the player has open (the song playing or paused, and the one lined up after it):
        Windows won't let them be replaced, so their new tags wait."""
        status = self.player.status()
        if status.state is State.STOPPED or status.track is None:
            return set()
        busy = {Path(audiofile.real_path(status.track.path))}
        following = self.player.following_path()
        if following is not None:
            busy.add(Path(audiofile.real_path(following)))
        return busy

    def _tags_saved(self, save: tag_editor.TagSave):
        """A tag save (or undo) finished: show the new tags everywhere, and look after any waiting."""
        if save.saved:
            self._apply_new_tags(save.saved)
        if save.waiting and save not in self._tag_waiting:
            self._tag_waiting.append(save)
            names = ", ".join(f"“{p.stem}”" for p in save.waiting[:2])
            self._set_notice(f"{save.describe()} — {names} {'is' if len(save.waiting) == 1 else 'are'} playing: "
                             "saved once the player moves on", 10000)
        else:
            self._set_notice(save.describe(), 6000)
        if self._tag_history is not None and self._tag_history.win.winfo_exists() and self._tag_history.saving is None:
            self._tag_history.reload()

    def _retry_tag_saves(self):
        """Songs whose new tags wait for the player to let go of them: try again (a quick swap —
        they were checked already)."""
        busy = self._busy_files()
        for save in list(self._tag_waiting):
            done = save.retry(lambda path: path in busy)
            if done:
                self._apply_new_tags(done)
                self._set_notice(f"Saved the new tags for “{done[0].stem}”" if len(done) == 1
                                 else f"Saved the new tags for {len(done)} songs", 5000)
            if not save.waiting:
                self._tag_waiting.remove(save)

    TAG_FIELDS = ("title", "artist", "album", "track_no", "disc_no", "album_artist", "genre", "composer", "date",
                  "compilation", "size")

    def _apply_new_tags(self, paths: list[Path]):
        """Re-read edited files and update every open tab's songs in place (the player, Up next and
        playlists hold the same Track objects), then the library index (on a thread)."""
        changed = {Path(p) for p in paths}
        if getattr(self, "_netcache", None) is not None:   # a network song: its cached copy is out of date now
            for path in changed:
                self._netcache.invalidate(path)
        fresh: dict[Path, Track] = {}
        touched = []
        # (A list read a page at a time: the songs it holds now. The rest are read from the library
        # database when next needed, by then up to date.)
        tracks = [t for tab in self.tabs for t in _held_in_memory(tab.tracks)] + self.player.up_next()

        for track in tracks:
            real = Path(audiofile.real_path(track.path))
            if real not in changed or audiofile.cue_of(track.path) is not None:
                continue
            if real not in fresh:
                try:
                    fresh[real] = read_track(real)
                except OSError:
                    log.info("couldn't re-read %s after its tags were saved", real, exc_info=True)
                    continue
            for name in self.TAG_FIELDS:
                setattr(track, name, getattr(fresh[real], name))
            touched.append(track)
        touched_ids = {id(t) for t in touched}   # (by identity: comparing Tracks field by field is slow)
        for tab in self.tabs:
            if tab.kind not in ("library", "playlist") and any(id(t) in touched_ids
                                                               for t in _held_in_memory(tab.tracks)):


                tab.info_requested = False  # the tab's name comes from the album tag: look it up again
        if touched:
            self._show_current_tab()
            self._window_title = ""   # the taskbar title follows on the next poll
            if self._art_track is not None and Path(audiofile.real_path(self._art_track)) in changed:
                self._request_art(self._art_track, force=True)
        index = self._index()

        def work():
            try:
                index.update(sorted(changed))
            finally:
                self._index_updated = True

        threading.Thread(target=work, name="library-index", daemon=True).start()

    # -- Up next ----------------------------------------------------------------------------------
    def queue_album(self, album: Path, front: bool):
        """Line up a whole album (from the Library) in Up next."""
        self._mark_opened(album)
        result: dict = {}

        def work():
            result["tracks"] = self._scan_or_nothing(album)

        def finish():
            if "tracks" not in result:
                self.root.after(50, finish)
                return
            if result["tracks"]:
                self._queue_tracks(result["tracks"], front)
            else:
                self._set_notice(f"No music found in “{album.name}”")

        threading.Thread(target=work, name="queue-album", daemon=True).start()
        self.root.after(50, finish)

    def _queue_tracks(self, tracks: list[Track], front: bool):
        added = self.player.play_next(tracks) if front else self.player.add_to_queue(tracks)
        if not added:
            self._set_notice("Nothing to add: the song can't be played here (missing or unsupported)")
            return
        what = f"“{tracks[0].title}”" if added == 1 else f"{added} songs"
        self._set_notice(f"{what} will play next" if front else f"Added {what} to Up next", 3000)
        self._paint_up_next()

    def _paint_up_next(self):
        queue = self.player.up_next()
        key = (len(queue), id(queue[0]) if queue else None)
        if key == self._up_next_key:
            if (self.up_next_pop.is_open and self._netcache is not None
                    and self._up_next_marks(queue) != self._marks_shown):
                self._fill_up_next()   # (a network song's download moved on)
            return
        self._up_next_key = key
        if queue:
            first = queue[0]
            artist = f" — {first.artist}" if first.artist else ""
            self.up_next_label.configure(text=f"Up next ({len(queue)}) · {first.title}{artist}")
            if not self.up_next_label.winfo_ismapped():
                # (Above the track list; with no tabs open the list is hidden: just above the status line.)
                place = (dict(before=self.playlist.frame) if self.playlist.frame.winfo_manager()
                         else dict(after=self.status_row))
                self.up_next_label.pack(side="bottom", fill="x", pady=theme.pad(theme.GAP_SMALL, 0), **place)
        else:
            self.up_next_label.pack_forget()
            if self.up_next_pop.is_open:
                self.up_next_pop.hide()
        if self.up_next_pop.is_open:
            self._fill_up_next()

    def _show_up_next(self):
        """The Up next list, just above its line: click a song to play it now, × to take it out."""
        if self.up_next_pop.is_open:
            self.up_next_pop.hide()
            return
        self._fill_up_next()
        self.up_next_pop.toggle(self.up_next_label, self._up_next_top())

    def _up_next_top(self) -> int:
        self.up_next_pop.update_idletasks()
        top = self.up_next_label.winfo_rooty() - self.root.winfo_rooty() - self.up_next_pop.winfo_reqheight() - 4
        return max(0, top)

    def _fill_up_next(self):
        body = self.up_next_pop.body
        for child in body.winfo_children():
            child.destroy()
        queue = self.player.up_next()
        inset = theme.px(theme.MENU_INSET)
        head = tk.Frame(body, bg=theme.SURFACE)
        head.pack(fill="x", padx=inset, pady=theme.pad(0, theme.GAP_SMALL))
        ttk.Label(head, text="Up next", style="PopTitle.TLabel").pack(side="left")
        ttk.Label(head, text=f"{len(queue)} song{'' if len(queue) == 1 else 's'}", style="PopMuted.TLabel").pack(
            side="left", padx=theme.pad(theme.GAP_MEDIUM, 0))
        # (0.22.0) Clear is a small plain pill like Settings' buttons (it was a plain word).
        controls.PillButton(head, "Clear", lambda: self.clear_up_next(), kind="plain", small=True, bg="SURFACE",
                            label="Take every song out of Up next").pack(side="right")
        marks = self._up_next_marks(queue)
        self._marks_shown = marks
        tones = {"accent": theme.ACCENT_TEXT, "warn": theme.WARN}
        column = any(mark for mark, _, _ in marks)   # (a column of marks only when there are network songs)
        longest = UP_NEXT_CHARS - (UP_NEXT_MARK_CHARS if column else 0)   # (the marks take some of the row)
        for position, track in enumerate(queue[:UP_NEXT_SHOWN]):
            label = f"{track.title} — {track.artist}" if track.artist else track.title
            mark, tone, meaning = marks[position]
            row = MenuRow(body, label if len(label) <= longest else label[:longest - 1] + "…",
                          lambda p=position: self.player.play_queued(p), lead=mark if column else None,
                          lead_fg=tones.get(tone, theme.MUTED))
            row.pack(fill="x")
            row.remove_button = controls.RoundIconButton(
                row.plate, self.root, "x", lambda p=position: self._unqueue(p), size=theme.TAB_CLOSE,
                diameter=theme.CLOSE_DISC, width=theme.CLOSE_SLOT, bg="SURFACE", hover=("HOVER", "TEXT", 0.16),
                pressed=("HOVER", "TEXT", 0.26), label="Take it out of Up next")
            row.remove_button.set_focusable(False)   # (the list's keys work the rows)
            row.remove_button.bind("<Enter>", lambda e, r=row: r._entered(), add="+")   # (the row stays lit)
            row.keys.pack_forget()
            row.remove_button.pack(side="right", padx=(0, theme.px(theme.GAP_SMALL)))
            if meaning:
                Tooltip(row.lead, meaning)
        if len(queue) > UP_NEXT_SHOWN:
            ttk.Label(body, text=f"and {len(queue) - UP_NEXT_SHOWN} more", style="PopMuted.TLabel").pack(
                anchor="w", padx=inset, pady=theme.pad(theme.GAP_HAIR, 0))
        if self._netcache is not None and getattr(self._netcache, "narrowed", False) and any(m[0] for m in marks):
            ttk.Label(body, text="The next song may need a short wait: the network is slower than the music "
                      "right now", style="PopMuted.TLabel", wraplength=theme.px(300) - 2 * inset,
                      justify="left").pack(anchor="w", padx=inset, pady=theme.pad(theme.GAP_HAIR, 0))
        # When to stop: after the song playing, or a sleep timer (its choices in a menu of their own).
        stopping = tk.Frame(body, bg=theme.SURFACE)
        stopping.pack(fill="x")
        popups.separator(stopping)
        MenuRow(stopping, "Stop after the current song", self.stop_after_current, keys.hint("stop_after"),
                check=self.player.stop_after == "song").pack(fill="x")
        timer = self.sleep_timer if self.sleep_timer is not None and self.sleep_timer.active else None
        row = MenuRow(stopping, f"Sleep timer: {sleep.choice_label(timer.target).lower()}" if timer
                      else "Sleep timer…", lambda: None, closes=False, ticks=True, chevron="right")
        row.command = lambda r=row: self._sleep_menu(self._menu()).popup(r.winfo_rootx(), r.winfo_rooty())
        row.pack(fill="x")
        if self.up_next_pop.is_open:  # re-seat it: the list's height changed
            self.up_next_pop.place_configure(y=self._up_next_top())
            self.up_next_pop.wire_rows()

    def _up_next_marks(self, queue) -> list[tuple[str, str, str]]:
        """For each song shown in Up next: (mark, tone, meaning) — how much of a network song is
        on this computer (the network playback cache); ("", "", "") for a song on this computer."""
        manager = self._netcache
        marks = []
        for track in queue[:UP_NEXT_SHOWN]:
            try:
                progress = manager.progress(track.path) if manager is not None else None
            except Exception:   # noqa: BLE001 — only a mark
                progress = None
            marks.append(cache_mark(progress))
        return marks

    def _unqueue(self, position: int):
        self.player.remove_from_queue(position)
        self._paint_up_next()
        return "break"

    def _change_library_folder(self):
        """Settings → Library → Change… (while there's one library folder): another in its place."""
        folder = filedialog.askdirectory(title="Choose your music library folder",
                                         initialdir=str(self._library_root()), mustexist=True)
        if folder:
            self._set_library_folders([Path(folder)])

    def _add_library_folder(self):
        folder = filedialog.askdirectory(title="Add a folder of music to the Library",
                                         initialdir=str(self._library_root()), mustexist=True)
        if not folder:
            return
        roots, note = adding_folder(self._library_roots(), Path(folder))
        if roots != self._library_roots():
            self._set_library_folders(roots)
        if note:
            self._set_notice(note, 8000)

    def _remove_library_folder(self, folder: Path):
        folders = [f for f in self._library_roots() if f != folder]
        if folders:
            self._set_library_folders(folders)
            self._set_notice(f"Took “{folder}” out of the Library (its music files are untouched)", 6000)

    def _set_library_folders(self, folders: list[Path]):
        """The library folders are now these. The look under way for the old ones is stopped first
        (library_index.LibraryIndex.set_roots), then the library is looked through again on a worker."""
        folders = library_db.outermost(folders)
        self.prefs["library_folders"] = [str(f) for f in folders]
        self.prefs["library_folder"] = str(folders[0])   # (what a version before several folders reads)
        index = self._index()   # (takes the new folders on: a look still under way for the old ones stops)
        self.root.after_idle(self._paint_library_folders)   # (not now: the Remove clicked is one of the rows)
        if self._library is not None and self._library.win.winfo_exists():
            self._library.set_roots(folders)
        if self.watcher.running:
            self.watcher.look_now()
        elif not (self._library is not None and self._library.win.winfo_exists()):
            threading.Thread(target=_refresh_library, args=(index,), name="library-folders", daemon=True).start()
        self._update_new_albums()

    # -- spotting new music (watcher.py) -----------------------------------------------------------
    def _new_music_state(self) -> dict:
        """{"since": when watching began, "opened": {album folder: when}} — kept in the settings."""
        state = self.prefs.get("new_music")
        if not isinstance(state, dict) or not isinstance(state.get("opened"), dict):
            state = {"since": None, "opened": {}}
            self.prefs["new_music"] = state
        return state

    def _start_watching(self, delay_s: float = 0.0):
        state = self._new_music_state()
        if not isinstance(state.get("since"), (int, float)):
            state["since"] = time.time()   # nothing already in the library counts as new
        self.watcher.since = state["since"]   # (its reports then carry the new albums)
        self.watcher.start(delay_s)

    def _toggle_watch(self):
        self.prefs["watch_music"] = self.watch_music.get()
        if self.watch_music.get():
            self._start_watching()
        else:
            self.watcher.stop()

    def _update_new_albums(self):
        """Work the new albums out afresh — on a worker thread, as it reads the database;
        _watch_reports takes the answer in."""
        state = self._new_music_state()
        since = state.get("since")
        if not isinstance(since, (int, float)):
            self._new_albums = []
            return
        threading.Thread(target=_find_new_albums, args=(self._index(), since, self.watcher.results),
                         name="new-albums", daemon=True).start()

    def _watch_reports(self):
        """Take in what the watcher found, and new albums worked out on a worker (called from
        _poll, on the Tk thread: the reading was done on theirs, so this is only showing it). A
        look that changed nothing brings nothing to do."""
        reports = self.watcher.drain()
        if not reports:
            return
        state = self._new_music_state()
        found = next((report for report in reversed(reports) if report.new is not None), None)
        if found is not None:
            if isinstance(state.get("since"), (int, float)) and found.album_folders is not None:
                state["opened"] = watcher.tidy_opened(state["opened"], found.album_folders, state["since"])
            self._new_albums = watcher.still_new(found.new, state["opened"])
        changed = False
        for report in reports:
            changed = changed or not report.diff.nothing
        if changed:
            self.checker.kick()   # (new or changed music: checked as it arrives, when the check is on)
        for report in reports:
            text = watcher.note(report, len(watcher.arrived(report, self._new_albums)))
            if text:
                self._set_notice(text, NEW_MUSIC_NOTE_MS)
        if self._library is not None and self._library.win.winfo_exists():
            if changed:
                self._library.reload()
            elif found is not None:
                self._library.refill()

    def _mark_opened(self, folder: Path):
        """An album was opened (a tab, added to one, queued, or one of its songs played): it's
        no longer new."""
        folder = Path(folder)
        if folder not in self._new_albums:
            return
        self._new_music_state()["opened"][str(folder)] = time.time()
        self._new_albums = [f for f in self._new_albums if f != folder]
        if self._library is not None and self._library.win.winfo_exists():
            self._library.refill()

    def _status_clicked(self, _event=None):
        """The status line: "Closed …" reopens the tab; "New music: …" shows Recently added; a
        playlist change that can be undone is undone; "Ironbark X.Y.Z is available" opens the
        download page."""
        if self._undo is not None and self._notice == self._undo[0]:
            self.undo()
        elif self._notice.startswith("Closed"):
            self.reopen_tab()
        elif self._notice.startswith("New music"):
            self.open_library(view="recent")
        elif self.update_available is not None and self._notice == self._update_text(self.update_available):
            self.open_download_page()

    def _paint_library_folders(self):
        """Settings → Library: a row per library folder — Change… while there's one, Remove when
        there are several."""
        rows = getattr(self, "library_rows", None)
        if rows is None or not rows.winfo_exists():
            return
        for child in self.library_rows.winfo_children():
            child.destroy()
        folders = self._library_roots()
        for folder in folders:
            row = ttk.Frame(self.library_rows, style="Pop.TFrame")
            row.pack(fill="x", pady=theme.pad(0, theme.GAP_HAIR))
            text = str(folder)
            label = ttk.Label(row, text=text if len(text) <= 30 else "…" + text[-29:], style="Pop.TLabel")
            label.pack(side="left", fill="x", expand=True)
            Tooltip(label, text)
            if len(folders) == 1:
                self._pop_button(row, "Change…", self._change_library_folder,
                                 "Choose the folder your music is in").pack(side="right")
            else:
                self._pop_button(row, "Remove", lambda folder=folder: self._remove_library_folder(folder),
                                 "Take this folder out of the Library (its music files stay where they are)").pack(
                    side="right")

    # -- home position -------------------------------------------------------
    def make_home(self):
        geometry = placement.current(self.root)
        if geometry:
            self.prefs["home_geometry"] = geometry
            self._update_home_text()
            self._set_notice("Home set: the home button (or Ctrl+Shift+H) brings the player back here")

    def snap_home(self):
        if self.mini_mode:   # home is a place for the full window
            self.toggle_mini()
        home = self.prefs["home_geometry"]
        if not home:
            self._set_notice("No home yet: choose “Make this spot home” first")
            return
        self._leave_full_screen()
        if self.root.state() == "zoomed":
            self.root.state("normal")
        if placement.restore(self.root, home):
            self._settle_size(home)
        else:
            self._set_notice("Home is on a monitor that isn't connected right now")

    def _settle_size(self, geometry: str):
        """Without the standard title bar, Windows and Tk disagree by a pixel or two on the
        frame, so a restored size can come out slightly bigger (and creep on every restart).
        Measure what we got and correct it once."""
        parsed = placement.parse(geometry) if geometry else None
        if not parsed or not parsed[0]:
            return
        want_w, want_h = parsed[0], parsed[1]
        self.root.update_idletasks()
        dw, dh = self.root.winfo_width() - want_w, self.root.winfo_height() - want_h
        if (dw or dh) and abs(dw) < 40 and abs(dh) < 40:
            self.root.geometry(f"{want_w - dw}x{want_h - dh}")

    def _bar_double_click(self):
        if self._leave_full_screen():
            return
        if self.prefs["home_geometry"]:
            self.snap_home()
        else:
            chrome.toggle_maximise(self.root)

    def _update_home_text(self):
        parsed = placement.parse(self.prefs["home_geometry"]) if self.prefs["home_geometry"] else None
        if parsed and parsed[0]:
            self.home_text.configure(text=f"Home: {parsed[0]} × {parsed[1]}")
        else:
            self.home_text.configure(text="Home: not set")
        self.snap_button.set_enabled(bool(parsed))

    def _open_settings(self):
        self._update_volume_text()
        if self.netplay_box.winfo_manager():
            self._paint_cache()
        if self.mini_mode:   # (0.25.0) the strip's own gear: Settings opens under the strip
            self._open_mini_settings()
            return
        self.settings_pop.toggle(self.bar.settings_btn, self._below_strip())

    def _below_strip(self) -> int:
        """Where a pop-over from the strip's buttons opens: DROP_GAP below the strip."""
        return self.bar.height + theme.px(DROP_GAP)

    def _open_mini_settings(self):
        """(0.25.0) Settings from the mini strip, without leaving it: the window grows to hold
        the panel under the strip — downwards, or upwards when the strip sits low on the screen —
        and `_settings_closed` puts the strip back to its own size and place. The strip keeps its
        height meanwhile (pack_propagate off), so nothing in it moves."""
        if self.settings_pop.is_open:
            self.settings_pop.hide()
            return
        Popover.close_open()
        here = placement.current(self.root)
        parsed = placement.parse(here) if here else None
        if parsed is None:   # minimised: nothing sensible to grow
            return
        width, strip, x, y = parsed
        self._mini_settings_from = here
        self.mini.pack_forget()
        self.mini.configure(height=strip)
        self.mini.pack_propagate(False)
        self.mini.pack(side="top", fill="x")
        self.root.update_idletasks()
        margin = theme.px(Popover.MARGIN)
        panel = self.settings_pop.natural_height()
        if panel < theme.px(200):   # not measured yet: the panel then scrolls in whatever it gets
            panel = theme.px(640)
        left, top, right, bottom = chrome.work_area(self.root)
        want_w = min(max(width, self.settings_pop.width + 2 * margin), right - left)
        want_h = min(strip + panel + 2 * margin, bottom - top)
        x = max(left, min(x, right - want_w))
        y = max(top, min(y, bottom - want_h))
        self.root.minsize(theme.px(MINI_MIN_SIZE[0]), theme.px(MINI_MIN_SIZE[1]))
        self.root.geometry(f"{want_w}x{want_h}+{x}+{y}")
        self.root.update_idletasks()
        self.mini_button.set_text("Full player")
        self.settings_pop.toggle(self.mini.settings_button, strip + theme.px(DROP_GAP))

    def _settings_closed(self):
        """(0.25.0) Settings closed. If it was opened from the mini strip, the window goes back to
        the strip's own size and place."""
        before, self._mini_settings_from = self._mini_settings_from, None
        if before is None:
            return
        self.mini_button.set_text("Mini player")
        self.mini.pack_forget()
        self.mini.pack_propagate(True)
        self.mini.configure(height=0)
        self.mini.pack(fill="both", expand=True)
        placement.restore(self.root, before)
        self._settle_size(before)   # Tk and Windows disagree by ~2 px without the standard caption

    # -- settings actions ----------------------------------------------------
    def _set_notice(self, text: str, ms: int = 5000):
        """Show `text` in the status line instead of the player's message (for `ms`; 0 = until replaced)."""
        self._notice = text
        if self._notice_job:
            self.root.after_cancel(self._notice_job)
            self._notice_job = None
        if ms:
            self._notice_job = self.root.after(ms, lambda: setattr(self, "_notice", ""))

    def _seek_released(self, seconds: float):
        """The seek bar let go: go there (or, on another tab, set where that tab carries on from)."""
        self._seek_to(seconds)
        self._show_times(seconds, self.seek.maximum)

    def _seek_dragged(self, seconds: float):
        """While the seek bar is dragged, the times under it follow the knob."""
        self._show_times(seconds, self.seek.maximum)

    def _show_times(self, position: float, duration: float):
        """Under the seek bar: the time so far (left) and the time left (right, with a minus)."""
        self.pos_label.configure(text=fmt_time(position))
        self.remain_label.configure(text=f"−{fmt_time(max(0.0, duration - position))}" if duration > 0 else "0:00")

    def _nudge(self, delta: float):
        if self._seek_target_playing:
            self._seek_to(self.player.status().position + delta)
        else:
            tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
            self._seek_to((tab.resume_at if tab and tab.last_index >= 0 else 0.0) + delta)

    def _seek_to(self, seconds: float):
        """Seek the song on show: the playing one, or, on another tab, set where that tab
        will carry on from when you press play (the music playing elsewhere isn't touched)."""
        if self._seek_target_playing:
            self.player.seek(seconds)
            return
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        view = tab.view_track() if tab is not None else None
        if view is None:
            return
        if tab.last_index < 0:
            tab.last_index = 0  # the song on show is its first
        tab.resume_at = max(0.0, min(seconds, max(view.duration - 1, 0.0)))

    def _wheel_volume(self, event):
        """A step per notch. Fine-grained wheels and touchpads send a notch's worth in many small
        messages: they add up, so one swipe can't race the volume to the top."""
        if not event.delta:
            return
        if (event.delta > 0) != (self._wheel_delta > 0):
            self._wheel_delta = 0   # turned the other way: start counting afresh
        self._wheel_delta += event.delta
        steps = int(self._wheel_delta / WHEEL_NOTCH)
        if not steps:
            return
        self._wheel_delta -= steps * WHEEL_NOTCH
        self._muted_from = None   # (a level chosen since: unmuting no longer goes back to the old one)
        self.player.set_volume(self.player.volume + steps * VOLUME_STEP)
        self.volume_var.set(self.player.volume * 100)
        self._set_notice(f"Volume {volume_words(self.player.volume)}", 1500)

    def _update_volume_text(self):
        self.volume_text.configure(text=volume_words(self.player.volume))

    def _toggle_on_top(self):
        """(0.25.0) The switch is for the mode on show: the full window and the mini strip each
        remember their own (the strip is on top at first, the full window isn't)."""
        self.prefs["mini_on_top" if self.mini_mode else "always_on_top"] = self.on_top.get()
        self.root.attributes("-topmost", self.on_top.get())

    def _toggle_exclusive(self):
        on = self.exclusive.get()
        if on:
            self._protect_ears(self.player.refresh_hardware_volume())
        self.player.set_exclusive(on)
        self._paint_lock()

    def _toggle_lock(self):
        """The lock beside the format line: bit-perfect on/off without opening the gear."""
        self.exclusive.set(not self.exclusive.get())
        self._toggle_exclusive()
        self._set_notice("Bit-perfect on: other apps go quiet while this plays" if self.exclusive.get()
                         else "Bit-perfect off: sound goes through the Windows mixer", 3000)

    def _paint_lock(self):
        if self.lock_button is not None:
            on = self.exclusive.get()
            self.lock_button.configure(text=glyph(self.root, "lock" if on else "unlock"))
            self.lock_button.set_colour("GOOD" if on else "MUTED")

    def _lock_tip(self) -> str:
        setting = self._levelling_setting()
        levelled = (f" Volume levelling is on ({levelling.mode_label(setting)}): it changes every song's level, "
                    "so nothing is bit-perfect until it's off (Settings → Sound)." if setting != "off" else "")
        if self.exclusive.get():
            guard = (" The clipping guard comes first: a track that would clip the converter is turned down "
                     "just enough, so that one track isn't bit-perfect." if self.clip_guard.get() else "")
            return ("Bit-perfect is locked on: the device gets the file's exact samples, and other apps "
                    f"go quiet while this plays.{guard}{levelled} Click to turn it off.")
        return f"Bit-perfect is off: Windows mixes the sound and may resample it.{levelled} Click to lock it on."

    def _refresh_outputs(self):
        """Re-read the connected devices (runs each time the dropdown opens)."""
        names = devices.output_names()
        chosen = self.player.output_device
        default = devices.default_output_name()
        self._output_choices = [None] + names
        labels = [f"System default ({default})" if default else "System default"] + names
        if chosen is not None and chosen not in names:
            self._output_choices.append(chosen)
            labels.append(f"{chosen} (not connected)")
        self.output_box.configure(values=labels)
        self.output_box.current(self._output_choices.index(chosen))

    def _change_output(self, _event=None):
        chosen = self._output_choices[self.output_box.current()]
        if chosen == self.player.output_device:
            return
        if self.exclusive.get():
            # A new device may not have its own volume control: check before it can play loud.
            self._protect_ears(self.player.refresh_hardware_volume(chosen))
        self.player.set_output_device(chosen)
        self.prefs["output_device"] = chosen or ""
        self.output_box.selection_clear()

    def _protect_ears(self, hardware_volume: bool | None):
        """Bit-perfect bypasses the Windows mixer. If the device can't turn itself down,
        the system volume slider stops working — so drop the player volume first."""
        if hardware_volume or self.player.volume <= SAFE_VOLUME:
            return
        self.player.set_volume(SAFE_VOLUME)
        self.volume_var.set(SAFE_VOLUME * 100)
        reason = ("This device has no volume control of its own"
                  if hardware_volume is False
                  else "The player couldn't confirm this device has a volume control of its own")
        # (0.21.0) An in-app box, but as unmissable as the Windows one was: modal, alone over a plain
        # background, amber edge and warning sign. It waits for OK.
        self._for_prompt()
        popups.tell(self.root, "Volume turned down to protect your ears",
                    f"{reason}, so the Windows volume slider may not work in bit-perfect mode. "
                    f"The player's volume is now {SAFE_VOLUME:.0%}.",
                    detail="Raise it slowly, or use your headphone or amp volume.",
                    tone="warn", cover=True, ok="I understand")

    def show_details(self):
        """The live Now Playing window (one at a time)."""
        if self._now_playing is not None and self._now_playing.win.winfo_exists():
            self._now_playing.lift()
        else:
            self._now_playing = self._with_keys(NowPlayingWindow(
                self.root, self.player, self.prefs["now_playing_geometry"],
                on_moved=lambda geometry: self.prefs.update(now_playing_geometry=geometry),
                on_meters=self.show_meters, on_pcm=self.show_pcm, on_clipping=self.show_clipping,
                on_spectrogram=self.show_spectrogram,
                on_scope=self.show_scope, on_compare=self.show_compare, on_abx=self.show_abx,
                on_testsignals=self.show_testsignals, on_lyrics=self.show_lyrics,
                prefs=self.prefs, stored=self._stored_check, remember=self._remember_check))

    def show_meters(self):
        """VU, PPM, RMS and true-peak meters (one window at a time)."""
        if self._meters is not None and self._meters.win.winfo_exists():
            self._meters.lift()
        else:
            self._meters = self._with_keys(MeterWindow(self.root, self.player, self.prefs))

    def show_pcm(self):
        """The sample values being played, in decimal, hex or binary (one window at a time)."""
        if self._pcm is not None and self._pcm.win.winfo_exists():
            self._pcm.lift()
        else:
            self._pcm = self._with_keys(PcmWindow(self.root, self.player, self.prefs,
                                                  analysis_for=NowPlayingWindow.cached_analysis))

    def show_clipping(self):
        """(0.24.0) Inter-sample overs: live, the whole song, and (Experimental) hear them."""
        if self._clip is not None and self._clip.win.winfo_exists():
            self._clip.lift()
        else:
            self._clip = self._with_keys(ClipWindow(self.root, self.player, self.prefs))

    def show_spectrogram(self):
        """A picture of the sound, live or the whole track (one window at a time)."""
        if self._spectrogram is not None and self._spectrogram.win.winfo_exists():
            self._spectrogram.lift()
        else:
            self._spectrogram = self._with_keys(SpectrogramWindow(self.root, self.player, self.prefs,
                                                                  analysis_for=NowPlayingWindow.cached_analysis))

    def show_lyrics(self):
        """The playing song's lyrics, from its file or the .lrc / .txt beside it (one window at a time)."""
        if self._lyrics is not None and self._lyrics.win.winfo_exists():
            self._lyrics.lift()
        else:
            self._lyrics = self._with_keys(LyricsWindow(self.root, self.player, self.prefs))

    def show_scope(self):
        """The stereo scope: vectorscope, correlation, balance, DC offset (one window at a time)."""
        if self._scope is not None and self._scope.win.winfo_exists():
            self._scope.lift()
        else:
            self._scope = self._with_keys(ScopeWindow(self.root, self.player, self.prefs))

    def _playing_file(self) -> tuple[Path | None, float]:
        """The song playing (or paused) and how far in, to start Compare / ABX from."""
        status = self.player.status()
        if status.track is None or status.state is State.STOPPED:
            return None, 0.0
        return status.track.path, status.position

    def show_compare(self):
        """Are two files the same music, sample for sample? (one window at a time)"""
        if self._compare is not None and self._compare.win.winfo_exists():
            self._compare.lift()
        else:
            self._compare = self._with_keys(CompareWindow(self.root, self.prefs, path_a=self._playing_file()[0]))

    def show_abx(self):
        """The ABX blind test (one window at a time); A starts as the song playing."""
        if self._abx is not None and self._abx.win.winfo_exists():
            self._abx.lift()
        else:
            path, position = self._playing_file()
            self._abx = self._with_keys(AbxWindow(self.root, self.player, self.prefs, path_a=path, start_s=position))
            self._hold_check_while(self._abx.win, "abx")

    def show_testsignals(self):
        """Hardware test: test signals, THD+N, J-test, frequency response (one window at a time)."""
        if self._testsignals is not None and self._testsignals.win.winfo_exists():
            self._testsignals.lift()
        else:
            self._testsignals = self._with_keys(TestSignalsWindow(self.root, self.player, self.prefs))
            self._hold_check_while(self._testsignals.win, "testsignals")

    def _hold_check_while(self, window: tk.Misc, reason: str):
        """The library quality check holds off while a blind test or a measurement has this window open
        (its worker processes would compete for the computer); it carries on once it closes."""
        self.checker.hold(reason)
        window.bind("<Destroy>", lambda e: self.checker.hold(reason, False) if e.widget is window else None,
                    add="+")

    def show_help(self):
        """F1 / ?: every keyboard shortcut, with a filter box (one window at a time)."""
        if self._help is not None and self._help.win.winfo_exists():
            self._help.lift()
        else:
            self._help = self._with_keys(HelpWindow(self.root, self.prefs, on_about=self.show_about))

    def show_about(self):
        """About Ironbark: version, licences, privacy, the website (one window at a time)."""
        if self._about is not None and self._about.win.winfo_exists():
            self._about.lift()
        else:
            self._about = self._with_keys(AboutWindow(self.root, self.prefs))

    # -- media keys, Windows' media panel, taskbar buttons (mediakeys.py) -------------------------
    def _start_media(self):
        """Hand the window to Windows' media controls (and the taskbar's buttons). Anything that
        fails is logged and left off; the player works the same without it."""
        if self._windows_media is not None or not mediakeys.AVAILABLE:
            return
        try:
            self._windows_media = mediakeys.start_windows(self.media, chrome._hwnd(self.root))
        except Exception:
            log.exception("media controls: couldn't start")
            self._windows_media = None

    def _stop_media(self):
        self.media.close()
        self._windows_media = None

    def _toggle_media(self):
        self.prefs["media_controls"] = self.media_controls.get()
        if self.media_controls.get():
            self._start_media()
        else:
            self._stop_media()

    def _media_state(self, status=None) -> str:
        """What the player is doing, for the media keys (a song from Up next counts, tab or not)."""
        status = status or self.player.status()
        if status.track is None:
            return "stopped"
        return {State.PLAYING: "playing", State.PAUSED: "paused"}.get(status.state, "stopped")

    def _media_now(self, track: Track | None, status) -> mediakeys.NowPlaying:
        """What Windows' media panel shows: the song playing (or paused), else nothing."""
        if track is None:
            return mediakeys.NowPlaying("closed")
        return mediakeys.NowPlaying(self._media_state(status), title=track.title, artist=track.artist,
                                    album=track.album, album_artist=track.artist, track_no=track.track_no or 0,
                                    path=track.path)

    def _media_poll(self):
        """Presses from the media keys, Windows' media panel or the taskbar buttons arrive on
        other threads; they only queue a word, picked up and carried out here, on Tk's thread."""
        try:
            for command in self.media.drain():
                self._media_command(command)
        finally:
            self._media_job = self.root.after(MEDIA_POLL_MS, self._media_poll)

    def _media_command(self, command: str):
        mediakeys.dispatch(command, self._media_state(), {
            "toggle_pause": self.player.toggle_pause, "pause": self.player.pause, "start": self.toggle_play,
            "next": self.player.next, "previous": self.player.previous})

    # -- drag and drop (dragdrop.py has the rules) ---------------------------------------------
    def _setup_drop(self):
        """Accept files dragged from Explorer. Tk can't do this by itself: the tkdnd extension
        (tkinterdnd2 package) does, inside Tk's own event loop, so drops arrive as ordinary Tk
        events. If it can't load, the player works the same, just without drops from outside."""
        try:
            from tkinterdnd2 import TkinterDnD

            TkinterDnD.require(self.root)
            call = self.root.tk.call
            call("tkdnd::drop_target", "register", self.root._w, ("DND_Files",))

            def bind(event, handler):
                command = self.root.register(handler)
                call("bind", self.root._w, event, f"{command} %X %Y %D")

            bind("<<DropEnter>>", self._dnd_over)
            bind("<<DropPosition>>", self._dnd_over)
            bind("<<DropLeave>>", lambda x, y, data: self._clear_drop_target())
            bind("<<Drop>>", self._dnd_drop)
        except Exception as exc:
            log.warning("drag and drop from Explorer unavailable: %s", exc)

    def _drop_target(self, x_root, y_root):
        """Where a drop at this screen point would land: ("tab", index), ("window", None), or
        None when it isn't over the player's main window."""
        x_root, y_root = int(x_root), int(y_root)
        try:
            widget = self.root.winfo_containing(x_root, y_root)
        except (KeyError, tk.TclError):
            widget = None
        if widget is None or widget.winfo_toplevel() is not self.root:
            return None
        index = self.bar.tab_at(x_root, y_root) if not self.mini_mode else None
        return ("tab", index) if index is not None else ("window", None)

    def _show_drop_target(self, target, what: str = ""):
        """Light up where a drag would land: a tab gets an outline, the window a green edge.
        The status line says what will happen."""
        if target == self._drop_shown:
            return
        self._drop_shown = target
        self.bar.highlight(target[1] if target and target[0] == "tab" else None)
        for frame in self._drop_overlay:
            frame.destroy()
        self._drop_overlay = []
        if target is None:
            self._set_notice("", 1)
            return
        if target[0] == "tab":
            self._set_notice(f"Drop to add {what or 'it all'} to the bottom of “{self.tabs[target[1]].title}”", 0)
            return
        edge = max(3, theme.px(3))
        for spec in (dict(x=0, y=0, relwidth=1.0, height=edge), dict(x=0, rely=1.0, y=-edge, relwidth=1.0, height=edge),
                     dict(x=0, y=0, width=edge, relheight=1.0), dict(relx=1.0, x=-edge, y=0, width=edge, relheight=1.0)):
            frame = tk.Frame(self.root, bg=theme.ACCENT)
            frame.place(**spec)
            frame.lift()
            self._drop_overlay.append(frame)
        self._set_notice(f"Drop to open {what} in a new tab" if what else "Drop to open in a new tab (folders: a tab each)", 0)

    def _clear_drop_target(self):
        self._show_drop_target(None)
        return "none"

    def _dnd_over(self, x_root, y_root, _data=""):
        target = self._drop_target(x_root, y_root)
        self._show_drop_target(target)
        return "copy" if target is not None else "refuse_drop"

    def _dnd_drop(self, x_root, y_root, data):
        """Files dropped from Explorer. Sorting them out touches the disk (a dropped drive could
        take a while), so it runs on a thread; _finish_drop carries the plan out."""
        target = self._drop_target(x_root, y_root)
        self._clear_drop_target()
        try:
            paths = [Path(p) for p in self.root.tk.splitlist(data)]
        except tk.TclError:
            log.warning("a drop's list of files couldn't be read: %r", data, exc_info=True)
            paths = []
        if target is None or not paths:
            return "refuse_drop"
        tab = self.tabs[target[1]] if target[0] == "tab" else None
        result: dict = {}

        def work():
            try:
                result["plan"] = dragdrop.plan_drop(paths)
                if tab is not None:  # adding to a tab: read the songs here too
                    result["songs"] = self._read_dropped(result["plan"])
            except Exception:
                log.exception("drop failed")
                result["plan"] = dragdrop.DropPlan(missing=paths)

        self._set_notice("Opening what was dropped…", 0)
        threading.Thread(target=work, name="drop", daemon=True).start()
        self.root.after(50, self._finish_drop, tab, result)
        return "copy"

    def _read_dropped(self, plan: dragdrop.DropPlan) -> tuple[list[Track], list[str]]:
        """(songs, extras) for adding a drop to a tab. extras is what a non-playlist tab keeps to
        find them again after a restart: each album folder the folders' songs came from, and
        single files for the rest."""
        tracks: list[Track] = []
        extras: list[str] = []
        for action in dragdrop.actions_for(plan, on_tab=True):
            for folder in action.folders:
                found = scan_folder(folder)
                tracks += found
                for t in found:
                    if str(album_folder(t.path)) not in extras:
                        extras.append(str(album_folder(t.path)))
            for file in action.paths:
                tracks.append(read_track(file))
                extras.append(str(file))
            for playlist in action.playlists:
                songs = [t for t in self._read_playlist(playlist) if not t.missing]
                tracks += songs
                extras += [audiofile.key(t.path) for t in songs]
        return tracks, extras

    def _finish_drop(self, tab: Tab | None, result: dict):
        if "plan" not in result or (tab is not None and tab.loading and self._tab_index(tab) >= 0):
            self.root.after(50, self._finish_drop, tab, result)
            return
        plan = result["plan"]
        note = plan.note()
        if plan.nothing:
            self._set_notice(f"Nothing to open · {note}" if note else "Nothing to open", 8000)
            return
        if tab is not None:
            if self._tab_index(tab) < 0:
                self._set_notice("The tab it was dropped on has been closed", 6000)
                return
            tracks, extras = result.get("songs", ([], []))
            message = self._add_tracks_to_tab(tab, tracks, extras)
        else:
            message = self._open_dropped(plan)
        self._set_notice(f"{message} · {note}" if note else message, 8000)

    def _open_dropped(self, plan: dragdrop.DropPlan, asked: bool = False) -> str:
        """A drop on the window: new tabs. Only the first may start playing, and only when
        nothing is playing or paused (as for any new tab).
        asked: they were named ("Open with", a double-click, the command line): a file among them —
        music, a playlist or a CUE sheet — is what was asked for, so it plays, even over music
        already playing. Folders alone open as a drop's do."""
        actions = dragdrop.actions_for(plan, on_tab=False)
        chosen = self._asked_for(actions, plan) if asked else None
        play = chosen is None and self.player.status().state is State.STOPPED
        interrupt = {"interrupt": True} if chosen is not None else {}
        opened = []
        for action in actions:
            if chosen is not None:
                play = action is chosen
            if action.kind == "open_folder":
                self.open_tab("folder", action.path, play=play, **interrupt)
                opened.append(action.path.name)
            elif action.kind == "open_playlist":
                self.open_tab("playlist", action.path, play=play, **interrupt)
                opened.append(action.path.stem)
            elif action.kind == "open_files":
                self.open_files_tab(list(action.paths), play=play)
                opened.append(f"{len(action.paths)} song{'' if len(action.paths) == 1 else 's'}")
            play = False
        if plan.cues:  # CUE sheet dropped: see dragdrop.cue_folder
            opened.append("CUE sheet as its folder (track-by-track CUE playback comes later)")
        if len(opened) == 1:
            return f"Opened {opened[0]}"
        return f"Opened {len(opened)} tabs"

    def open_files_tab(self, files: list[Path], play: bool = False):
        """One new tab holding just these music files, in this order (saved with the tab). The same
        files opened again (a second double-click) go back to their tab."""
        extras = [str(f) for f in files]
        for i, tab in enumerate(self.tabs):
            if tab.kind == "files" and tab.extras == extras:
                self.select_tab(i, then_play=play)
                return
        tab = Tab("files", str(files[0].parent))
        tab.extras = extras
        self.tabs.append(tab)
        self.select_tab(len(self.tabs) - 1, then_play=play)

    @staticmethod
    def _asked_for(actions: list, plan: dragdrop.DropPlan):
        """Of what was named, the action that plays: the music files, else the first playlist, else the
        first CUE sheet's album. None when only folders were named."""
        for kind in ("open_files", "open_playlist"):
            found = next((a for a in actions if a.kind == kind), None)
            if found is not None:
                return found
        cues = [folder for folder in plan.cue_folders if folder not in plan.folders]
        return next((a for a in actions if a.kind == "open_folder" and a.path in cues), None)

    # -- opening from outside (the command line, "Open with", a later launch) ---------------------------
    def open_paths(self, paths: list[str]):
        """Open what was named outside the player, as a drop on the window would (dragdrop.py): folders
        as tabs, music files together in one tab, playlists and CUE sheets as theirs. A file asked for
        this way plays at once (`_open_dropped`, asked). Sorting them out touches the disk, so it runs
        on a thread; `_finish_open_paths` carries it out."""
        named = [Path(p) for p in paths if p]
        if not named:
            return
        result: dict = {}

        def work():
            try:
                result["plan"] = dragdrop.plan_drop(named)
            except Exception:
                log.exception("couldn't sort out the files named")
                result["plan"] = dragdrop.DropPlan(missing=named)

        self._set_notice("Opening…", 0)
        threading.Thread(target=work, name="open-paths", daemon=True).start()
        self.root.after(50, self._finish_open_paths, result)

    def _finish_open_paths(self, result: dict):
        if self._closing:   # (sorted out only after closing began: nothing to open it in)
            return
        if "plan" not in result:
            self.root.after(50, self._finish_open_paths, result)
            return
        plan = result["plan"]
        note = plan.note()
        if plan.nothing:
            self._set_notice(f"Nothing to open · {note}" if note else "Nothing to open", 8000)
            return
        message = self._open_dropped(plan, asked=True)
        self._set_notice(f"{message} · {note}" if note else message, 8000)

    def _take_requests(self):
        """Later launches' requests (instance.py), handed over by its thread through a queue. Several
        files chosen in Explorer start a launch each, all within a moment, so what's named (this
        launch's own command line too) is gathered until a poll brings nothing new, then opened
        together — one tab, one start — in name order when several launches named it. Left alone
        once the window is closing: what they ask for would only close with it."""
        if self._closing:
            return
        front, arrived = False, False
        while self._requests is not None:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                break
            arrived = True
            front = front or request.front
            if request.paths:
                self._named.append(request.paths)
            log.info("a later launch handed over %d path(s)%s", len(request.paths),
                     " and asked to come to the front" if request.front else "")
        if front:
            self.bring_to_front()
        if arrived or not self._named:
            return
        named, self._named = self._named, []
        paths = [path for group in named for path in group]
        if len(named) > 1:
            paths.sort(key=str.casefold)
        self.open_paths(paths)

    def bring_to_front(self):
        """Show the window in front of the others (from the taskbar if it was minimised)."""
        if self.root.state() in ("iconic", "withdrawn"):
            self.root.deiconify()
        self.root.lift()
        if not self.on_top.get():   # in front once, without staying on top
            self.root.attributes("-topmost", True)
            self.root.after_idle(lambda: self.root.attributes("-topmost", self.on_top.get()))
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    # -- help: diagnostics, the log folder ----------------------------------------------------------
    def save_diagnostics(self, destination: str | None = None):
        """Save a diagnostics .zip where the user chooses (diagnostics.py). Nothing is sent anywhere."""
        Popover.close_open()
        if destination is None:
            destination = filedialog.asksaveasfilename(
                parent=self.root, title="Save diagnostics", defaultextension=".zip",
                initialfile=diagnostics.default_name(), filetypes=[("Zip file", "*.zip")])
        if not destination:
            return
        prefs = json.loads(json.dumps(self.prefs))   # (a copy of its own for the thread)
        result: dict = {}

        def work():
            try:
                result["path"] = diagnostics.write_zip(Path(destination), prefs, settings.settings_path().parent)
            except Exception as error:   # reported in the window
                log.warning("couldn't save diagnostics", exc_info=True)
                result["error"] = error

        self._set_notice("Saving diagnostics…", 0)
        threading.Thread(target=work, name="diagnostics", daemon=True).start()
        self.root.after(100, self._finish_diagnostics, result)

    def _finish_diagnostics(self, result: dict):
        if not result:
            self.root.after(100, self._finish_diagnostics, result)
            return
        if "path" in result:
            self._set_notice(f"Diagnostics saved as {result['path'].name} · nothing has been sent anywhere", 10000)
            return
        self._set_notice("", 1)
        self._for_prompt()
        popups.tell(self.root, "Diagnostics weren't saved", f"{result['error']}", tone="warn")

    def _open_log_folder(self):
        from . import crash

        Popover.close_open()
        crash.open_folder(settings.settings_path().parent)

    # -- Check for updates (updates.py) ----------------------------------------------------------------
    def _update_tick(self):
        try:
            self._check_for_update_if_due()
        finally:
            self._update_job = self.root.after(UPDATE_EVERY_MS, self._update_tick)

    def _check_for_update_if_due(self):
        """Start a check on a thread when one is due (switched on, network allowed, a day since)."""
        if self._update_checking or not updates.due(self.prefs):
            return
        self._update_checking = True
        self.prefs["update_checked"] = time.time()   # noted now: a failed check also waits a day
        prefs = {key: self.prefs.get(key) for key in ("check_updates", "allow_network")}

        def work():
            try:
                self._update_found = updates.check(prefs)
            finally:
                self._update_checking = False

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _take_update(self):
        found, self._update_found = self._update_found, None
        if found is None:
            return
        self.update_available = found
        self._paint_version()
        self._set_notice(self._update_text(found), UPDATE_NOTE_MS)

    @staticmethod
    def _update_text(release: updates.Release) -> str:
        return f"{APP_NAME} {release.version} is available · click here for the download page"

    def open_download_page(self):
        """Opens the page in the web browser; nothing is downloaded by the player."""
        release = self.update_available
        webbrowser.open(release.url if release is not None else updates.DOWNLOAD_PAGE)

    def _add_tracks_to_current(self, tracks: list[Track]) -> str:
        """Songs (from the Library's song views) onto the bottom of the tab on show, or a new tab of
        their own when none is open. Returns what was done, to say."""
        tracks = [t for t in tracks if not t.missing]
        if not tracks:
            return "Nothing to add"
        if not 0 <= self.current < len(self.tabs):
            self.open_files_tab([t.path for t in tracks])
            return "Opened in a new tab"
        message = self._add_tracks_to_tab(self.tabs[self.current], tracks, [audiofile.key(t.path) for t in tracks])
        self._set_notice(message)
        return message

    def _add_tracks_to_tab(self, tab: Tab, tracks: list[Track], extras: list[str]) -> str:
        """Add songs to the bottom of a tab (a drop, or songs dragged onto it). A playlist saves
        them into its .m3u8; any other tab keeps `extras` so they're there after a restart, and
        skips songs it already has. Music playing from the tab carries on uninterrupted."""
        if not tracks:
            return f"Nothing to add to “{tab.title}”"
        if tab.kind == "playlist":
            self.add_to_playlist(Path(tab.path), tracks)
            count = len(tracks)
            return f"Added {count} song{'' if count == 1 else 's'} to “{tab.title}”"
        # Songs are told apart by audiofile.key: a CUE track's path is its whole image file.
        have = _keys_held(tab.tracks, [audiofile.key(t.path) for t in tracks]) if tab.tracks else set()
        new = [t for t in tracks if audiofile.key(t.path) not in have]
        if not new:
            return f"Already in “{tab.title}”"
        # Only the extras that brought something in (a song, or the album folder of one), so the
        # tab comes back the same after a restart.
        brought = {audiofile.key(t.path) for t in new} | {str(album_folder(t.path)) for t in new}
        for extra in extras:
            if extra in brought and extra not in tab.extras and Path(extra) != Path(tab.path):
                tab.extras.append(extra)
        if tab.tracks is not None:  # (not loaded yet: loading it will include them)
            tab.tracks = tab.tracks + new
            index = self._tab_index(tab)
            if index == self.playing_tab:
                self.player.set_tracks(tab.tracks, keep_playing=True)
            if index == self.current:
                self._show_current_tab()
        count = len(new)
        return f"Added {count} song{'' if count == 1 else 's'} to “{tab.title}”"

    def _song_drag(self, rows: list[int], x_root: int, y_root: int, phase: str):
        """Songs dragged from the track list: up and down a playlist to reorder it, or onto
        another tab to add them there."""
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        if tab is None or not tab.tracks:
            return
        rows = [r for r in rows if 0 <= r < len(tab.tracks)]
        reorder = (tab.kind == "playlist" and len(rows) == 1 and not self.playlist.searching
                   and self.playlist.contains(x_root, y_root))
        target = None if reorder else self._drop_target(x_root, y_root)
        if target is not None and (target[0] != "tab" or target[1] == self.current):
            target = None  # songs go onto another tab, nowhere else
        if phase == "move":
            self.playlist.show_drop_line(self.playlist.drop_index(y_root) if reorder else None)
            self._show_drop_target(target, "the song" if len(rows) == 1 else f"these {len(rows)} songs")
            return
        self.playlist.show_drop_line(None)
        self._clear_drop_target()
        if phase != "drop" or not rows:
            return
        if reorder:
            self._reorder_playlist(rows[0], self.playlist.drop_index(y_root))
        elif target is not None:
            tracks = [track for track in _tracks_at(tab.tracks, rows) if not track.missing]

            self._set_notice(self._add_tracks_to_tab(self.tabs[target[1]], tracks,
                                                     [audiofile.key(t.path) for t in tracks]))

    def _reorder_playlist(self, source: int, target: int):
        """Move one song within the playlist on show (dragged up or down); saved straight away."""
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        if tab is None or tab.kind != "playlist" or not tab.tracks:
            return
        tracks = dragdrop.move_item(tab.tracks, source, target)
        if [t.path for t in tracks] == [t.path for t in tab.tracks]:
            return
        song = tab.tracks[source]
        self._edit_playlist(tab, tracks, f"Moved “{song.title}”", f"Moved “{song.title}” back",
                            select=target - 1 if target > source else target, select_back=source)

    def _album_drag(self, album: Path, x_root: int, y_root: int, phase: str):
        """An album dragged from the Library window: onto a tab adds it there, anywhere else on
        the player opens it in a new tab."""
        target = self._drop_target(x_root, y_root) if phase != "cancel" else None
        if phase == "move":
            self._show_drop_target(target, f"“{album.name}”")
            return
        self._clear_drop_target()
        if phase != "drop" or target is None:
            return
        if target[0] == "tab":
            self.add_album_to_current(album, tab=self.tabs[target[1]])
        else:
            self.open_tab("album", album, play=True)

    # -- theme (0.19.0) -----------------------------------------------------------------------
    def _theme_chosen(self, _event=None):
        self.prefs["theme"] = theme.THEME_CHOICES[self.theme_box.current()][0]
        self.theme_box.selection_clear()
        self._apply_theme()

    def _apply_theme(self):
        """Switch to the palette the Theme setting asks for now (nothing happens if it's on show)."""
        choice = self.prefs["theme"]
        self._windows_light = theme.windows_uses_light() if choice == "system" else None
        palette = theme.palette_for(choice, self._windows_light)
        if palette is not theme.current:
            theme.use(palette, self.root)

    def _follow_windows(self):
        """Theme: Follow Windows notices Windows switching light / dark while the player runs — a
        quick registry read every couple of seconds on Tk's own timer (nothing blocks or waits)."""
        try:
            if self.prefs["theme"] == "system" and theme.windows_uses_light() != self._windows_light:
                self._apply_theme()
        finally:
            self._theme_job = self.root.after(THEME_POLL_MS, self._follow_windows)

    # -- mini mode (0.19.0) ---------------------------------------------------------------------
    def toggle_mini(self):
        """Full window ⇄ mini strip. Only the layout and the window's place change: the player
        isn't touched, so the music carries on, and Keep on top / media keys / drops (all tied
        to this one window) keep working. Each mode returns to where it was last left."""
        grown_from = self._mini_settings_from   # Settings open from the strip: the strip's own place
        Popover.close_open()
        self._leave_full_screen()
        if self.root.state() == "zoomed":
            self.root.state("normal")
        here = grown_from or placement.current(self.root)
        if here:   # remember where the mode we're leaving was
            self.prefs["mini_geometry" if self.mini_mode else "geometry"] = here
        going_mini = not self.mini_mode
        self._show_mini(going_mini)
        target = self._mini_geometry(here or "") if going_mini else self.prefs["geometry"]
        if not placement.restore(self.root, target) and going_mini and placement.parse(target) is None:
            self.root.geometry(f"{theme.px(MINI_DEFAULT_SIZE[0])}x{theme.px(MINI_DEFAULT_SIZE[1])}")
        self._settle_size(target)
        if going_mini:
            if self._mini_state is not None:
                self.mini.show(**self._mini_state)
        else:
            self.art_size = 0   # size the cover afresh for the full window
            self._check_layout(force=True)
            self.root.after_idle(self._fit_art)

    def _show_mini(self, on: bool):
        """Swap the layouts (no moving: toggle_mini and start-up place the window). (0.25.0) Keep
        on top swaps too: each mode has its own (prefs `always_on_top` / `mini_on_top`)."""
        self.prefs["mini_on_top" if self.mini_mode else "always_on_top"] = self.on_top.get()
        self.mini_mode = on
        self.prefs["mini_mode"] = on
        self.on_top.set(bool(self.prefs["mini_on_top" if on else "always_on_top"]))
        self.root.attributes("-topmost", self.on_top.get())
        if on:
            self.bar.pack_forget()
            self.body.pack_forget()
            self.mini.pack(fill="both", expand=True)
            self.root.minsize(theme.px(MINI_MIN_SIZE[0]), theme.px(MINI_MIN_SIZE[1]))
        else:
            self.mini.pack_forget()
            self.bar.pack(fill="x")
            self.body.pack(fill="both", expand=True)
            self.root.minsize(theme.px(FULL_MIN_SIZE[0]), theme.px(FULL_MIN_SIZE[1]))

    def _mini_geometry(self, full: str) -> str:
        """Where the mini strip goes: where it was last left; the first time, its default size
        at the full window's top-left corner (or just the size, if that isn't known)."""
        if self.prefs["mini_geometry"]:
            return self.prefs["mini_geometry"]
        size = f"{theme.px(MINI_DEFAULT_SIZE[0])}x{theme.px(MINI_DEFAULT_SIZE[1])}"
        parsed = placement.parse(full) if full else None
        return f"{size}+{parsed[2]}+{parsed[3]}" if parsed else size

    # -- wide windows (layout.py) -------------------------------------------------------------------
    def _pack_column(self):
        """The narrow window's layout: the one place it's packed (at start, after a wide window, and
        when a tab opens in an empty window)."""
        self.head.pack(fill="x", before=self.status_row)
        self.seek_row.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0), before=self.status_row)
        self.transport.pack(fill="x", pady=theme.pad(theme.GAP_SMALL, theme.GAP), before=self.status_row)
        self.playlist.frame.pack(fill="both", expand=True, after=self.status_row)
        self._place_up_next()

    def _pack_split(self):
        """A wide window's layout: the two panes between the strip and the status line, the
        now-playing parts in the left one's stack, the track list in the right one."""
        self.split.pack(fill="both", expand=True, after=self.status_row)
        self.head.pack(in_=self.stack, fill="x")
        self.seek_row.pack(in_=self.stack, fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
        self.transport.pack(in_=self.stack, fill="x", pady=theme.pad(theme.GAP_SMALL, theme.GAP))
        self.playlist.frame.pack(in_=self.right, fill="both", expand=True)
        self._place_up_next()
        # Each above the frame it's packed in (raised in the order they were made, which is also the
        # order Tab goes through them; the pieces placed over them come after).
        for widget in (self.head, self.head_icons, self.seek_row, self.seek, self.transport, self.playlist.frame,
                       self.playlist.scrollbar, self.up_next_label):
            self.root.tk.call("raise", str(widget))   # (a canvas's own lift() raises its items instead)

    def _place_up_next(self):
        """The Up next line (only while songs are queued): above the track list, or with no tab open,
        just above the status line."""
        if not self.player.up_next():
            return
        place = (dict(before=self.playlist.frame) if self.playlist.frame.winfo_manager()
                 else dict(after=self.status_row))
        self.up_next_label.pack(side="bottom", fill="x", pady=theme.pad(theme.GAP_SMALL, 0), **place)

    def _choose_tier(self, size: tuple[int, int] | None = None) -> str:
        width, height = size or (self.root.winfo_width(), self.root.winfo_height())
        scale = _layout_scale()
        return layout.choose(width / scale, height / scale, self._tier, self.show_art.get())

    def _on_resize(self, event):
        """The window's size changed: another layout (once, when Tk is next idle), or the same one
        fitted to the new size."""
        if event.widget is not self.root:
            return
        self._opening_size = None   # (shown: its own size from now on)
        self.bar.paint_max(chrome.is_fullscreen(self.root) or self.root.state() == "zoomed")
        if self.mini_mode or self._mini_settings_from is not None:
            return
        if self._choose_tier() != self._tier:
            if self._layout_job is None:
                self._layout_job = self.root.after_idle(self._apply_layout)
            return
        self._fit_art()

    def _check_layout(self, force: bool = False, size: tuple[int, int] | None = None):
        """Put the window in the layout its size calls for now, rather than at the next resize.
        force: a wide window staying wide is measured and fitted afresh (the cover switched on or off,
        back from the mini strip). size: the window's size to go by (real px), before it's shown."""
        if self.mini_mode or self._mini_settings_from is not None:
            return
        if size is not None:
            self._opening_size = size   # (fitted to it while the window can't be measured yet)
        tier = self._choose_tier(size)
        if tier != self._tier:
            self._apply_layout(tier)
        elif force and tier != layout.COLUMN:
            self._heights, self._plan, self.art_size = None, None, 0
            self._fit_split()

    def _apply_layout(self, tier: str | None = None):
        """Move into another layout, in one go so Tk lays out and paints once. Nothing is rebuilt —
        the same widgets are packed into other frames — so the search, the selection, the list's
        scroll position and the keyboard's place all stay."""
        if self._layout_job is not None:
            self.root.after_cancel(self._layout_job)
            self._layout_job = None
        if self.mini_mode or self._mini_settings_from is not None:
            return
        tier = tier or self._choose_tier()
        if tier == self._tier:
            return
        try:
            focus = self.root.focus_get()
        except (KeyError, tk.TclError):   # (a part of Tk's own, e.g. an open drop-down's list)
            focus = None
        was, self._tier = self._tier, tier
        wide = tier != layout.COLUMN
        if wide != (was != layout.COLUMN):
            moved = (self.head, self.seek_row, self.transport, self.up_next_label, self.playlist.frame)
            self.split.pack_forget()
            for widget in moved:
                widget.pack_forget()
            if not wide:
                self._art_hidden = False
                if self.show_art.get():
                    self.art_box.pack(fill="x", before=self.head_text)
                    self.head_text.pack_configure(pady=theme.pad(theme.GAP_LARGE, 0))
            if self._empty_shown:
                self._place_up_next()
            elif wide:
                self._pack_split()
            else:
                self._pack_column()
        self._style_for(tier)
        self._heights, self._plan, self.art_size = None, None, 0
        self._fits.clear()
        self._fit_art()
        self._paint_head()
        if focus is not None and focus.winfo_exists():
            focus.focus_set()

    def _style_for(self, tier: str):
        """A layout's type sizes, caption, list style and columns, and the strip's extras."""
        wide = tier != layout.COLUMN
        self.title_label.configure(style=TITLE_STYLES[tier])
        self.sub_label.configure(style=SUB_STYLES[tier])
        self.format_label.configure(style=INFO_STYLES[tier])
        info_row = self.format_label.master
        if wide:
            self.kind_label.pack(anchor="w", pady=theme.pad(0, theme.GAP_SMALL), before=self.title_label)
            info_row.pack_configure(fill="none", anchor="w")   # the dot, lock and ⓘ right after the text
        else:
            self.kind_label.pack_forget()
            info_row.pack_configure(fill="x", anchor="center")
        self.playlist.set_tier(tier)
        self.bar.set_wide(wide, layout.TAB_MAX[tier])
        self._cover_tip = ""

    def _measure_heights(self) -> layout.Heights:
        """The heights a wide layout is planned from (logical px), measured with its styles on."""
        self.root.update_idletasks()
        scale = _layout_scale()
        tree = self.playlist.tree
        row = ttk.Style(self.root).lookup(str(tree.cget("style")) or "Treeview", "rowheight")
        base = layout.DEFAULT_HEIGHTS[self._tier]

        def logical(real: float) -> int:
            return math.ceil(real / scale)

        chrome_h = (self.bar.winfo_reqheight() + theme.px(theme.GAP_LARGE) + theme.px(theme.GAP_MEDIUM)
                    + self.status_row.winfo_reqheight() + theme.px(theme.GAP))
        return layout.Heights(
            text=logical(self.head_text.winfo_reqheight()),
            seek=logical(self.seek_row.winfo_reqheight() + theme.px(theme.GAP_MEDIUM)),
            transport=logical(self.transport.winfo_reqheight() + theme.px(theme.GAP_SMALL) + theme.px(theme.GAP)),
            chrome=logical(chrome_h),
            list_head=logical(self.playlist.header.winfo_reqheight() + theme.px(theme.GAP)),
            heading=base.heading,
            row=logical(float(row)) if row else base.row)

    def _fit_split(self):
        """Fit a wide layout to the window: the panes' widths, the margins past CONTENT_MAX, the
        cover's slot (snapped to whole steps; while resizing, the last cover is scaled quickly as a
        stand-in until the window settles) and the shortened title. Reads only the window's size and
        the measured heights — never the panes themselves — so nothing feeds back."""
        if self.mini_mode or self._tier == layout.COLUMN:
            return
        # Until the window has been shown and told its size, Tk calls it 200 px square: go by the
        # size it opens at instead (or its first paint would be laid out for that).
        width, height = self._opening_size or (self.root.winfo_width(), self.root.winfo_height())
        if width < 10 or height < 10:
            return
        if self._heights is None:
            # Measuring makes Tk lay out and paint the window as it is at that moment, so it's first
            # fitted with the heights this layout had last time (or typical ones): that paint already
            # shows the panes in place, not a list across the whole window with nothing beside it.
            self._heights = self._measured.get(self._tier) or layout.DEFAULT_HEIGHTS[self._tier]
            self._fit_plan(width, height)
            self._heights = self._measured[self._tier] = self._measure_heights()
        self._fit_plan(width, height)

    def _fit_plan(self, width: int, height: int):
        """Fit the panes, the cover's slot and the title to a window `width` × `height` real px, planned
        from the heights in hand."""
        scale = _layout_scale()
        plan = layout.plan(width / scale, height / scale, self._tier, self.show_art.get(), self._heights)
        old, self._plan = self._plan, plan
        px = theme.px

        def moved(name: str) -> bool:
            return old is None or abs(px(getattr(old, name)) - px(getattr(plan, name))) > 2

        if moved("margin"):
            self.split.pack_configure(padx=px(plan.margin))
        if moved("left_w"):
            self.left.configure(width=px(plan.left_w))
        if moved("stack_w"):
            self.stack.place(relx=0.5, rely=0.5, anchor="center", width=px(plan.stack_w))
            self._fits.clear()
        self._fit_info_row()
        if self.show_art.get():
            hide = plan.slot == 0
            if hide != self._art_hidden:   # too short for a cover: text, seek bar and transport only
                self._art_hidden = hide
                if hide:
                    self.art_box.pack_forget()
                    self.head_text.pack_configure(pady=0)
                else:
                    self.art_box.pack(fill="x", before=self.head_text)
                    self.head_text.pack_configure(pady=theme.pad(theme.GAP_LARGE, 0))
            slot = px(plan.slot)
            if slot and slot != self.art_size:
                self.art_size = slot
                self.art_box.configure(height=slot)
                self._stand_in_art()
                if self._art_job:
                    self.root.after_cancel(self._art_job)
                self._art_job = self.root.after(COVER_SETTLE_MS, self._art_settled)
        self._paint_head()

    def _fit_info_row(self):
        """The format line with the status dot, lock and ⓘ right after it — unless it's wider than the
        stack, when the row takes the stack's width (the icons at its end, the text cut)."""
        if self._plan is None:
            return
        info_row = self.format_label.master
        fill = "x" if info_row.winfo_reqwidth() > theme.px(self._plan.stack_w) else "none"
        if str(info_row.pack_info().get("fill")) != fill:
            info_row.pack_configure(fill=fill)

    def _stand_in_art(self):
        """The last cover scaled quickly (bilinear, about a millisecond) to the new slot while the
        window is being resized; the proper one is fetched once it settles. Never past the picture's
        own size."""
        image = self._art_image
        if image is None or not self.art_size:
            return
        source = image.info.get("source_size", image.size)
        side = max(1, min(self.art_size, *source))
        quick = image if image.width == side else image.resize((side, side), Image.Resampling.BILINEAR)
        quick.info["source_size"] = source
        self._show_art(quick, fetched=False)

    def _art_settled(self):
        self._art_job = None
        self._request_art(self._art_track, force=True)

    def _set_head(self, caption: str, title: str, sub: str, fmt: str):
        """The top of the window: the caption (a wide window only), title, artist line and format line.
        The whole title and artist line are kept — the mini strip and the tooltips show them — and a
        wide window shortens them with "…" to its stack's width."""
        self._head_full = (title, sub)
        self._head_texts = (caption, fmt)
        self._paint_head()

    def _paint_head(self):
        (title, sub), (caption, fmt) = self._head_full, self._head_texts
        shown_title, shown_sub = title, sub
        if self._tier != layout.COLUMN and self._plan is not None:
            width = theme.px(self._plan.stack_w)
            shown_title = self._fit_text(title, TITLE_STYLES[self._tier], width)
            shown_sub = self._fit_text(sub, SUB_STYLES[self._tier], width)
        self._head_cut = (shown_title != title, shown_sub != sub)
        for label, text in ((self.kind_label, caption), (self.title_label, shown_title),
                            (self.sub_label, shown_sub), (self.format_label, fmt)):
            if str(label.cget("text")) != text:
                label.configure(text=text)

    def _fit_text(self, text: str, style: str, width: int) -> str:
        """`text` shortened with "…" to `width` px in a label style's font (remembered)."""
        key = (text, style, width)
        shown = self._fits.get(key)
        if shown is None:
            if len(self._fits) > 400:
                self._fits.clear()
            font = controls._measuring_font(self.root, ttk.Style(self.root).lookup(style, "font"))
            shown = text
            if font.measure(text) > width:
                while shown and font.measure(shown + "…") > width:
                    shown = shown[:-1]
                shown = shown.rstrip() + "…" if shown.strip() else text[:1] + "…"
            self._fits[key] = shown
        return shown

    def _tab_count(self, tab: Tab) -> str:
        """A wide window's count over the list, e.g. "Album · 13 songs · 51:57", or for a tab of many
        albums "Library · 655 songs · 50 albums · 41:22:10"."""
        if tab.tracks is None:
            return "Loading…" if tab.loading else ""
        songs, albums, seconds = tab_numbers(tab)
        if not songs:
            return ""
        parts = [KIND_NAMES.get(tab.kind, ""), plural(songs, "song")]
        if albums > 1 and tab.kind != "playlist":
            parts.append(plural(albums, "album"))
        parts.append(fmt_time(seconds))
        return " · ".join(p for p in parts if p)

    # -- cover art -------------------------------------------------------------
    def _fit_art(self):
        """Size the cover square to the window: the full width, but never so tall that the
        track list drops below a few rows. (A wide window: _fit_split.)"""
        if self._tier != layout.COLUMN:
            self._fit_split()
            return
        width = self.art_box.winfo_width()
        if width < 10 or not self.show_art.get() or self.mini_mode:
            return
        # What everything except the cover and the list needs (their requested heights,
        # plus the gaps between them), so the sum is right even mid-resize.
        parts = (self.bar, self.head_text, self.seek_row, self.transport, self.status_row)
        gaps = theme.px(48)  # body padding and the space between rows
        try:
            everything_else = sum(w.winfo_reqheight() for w in parts) + gaps
        except tk.TclError:  # the window is closing: its parts go one by one, each resizing the rest
            return
        room = self.root.winfo_height() - everything_else - theme.px(MIN_LIST_HEIGHT)
        size = max(theme.px(64), min(width, room))
        if abs(size - self.art_size) <= 2:
            return
        self.art_size = size
        self.art_box.configure(height=size)
        if self._art_job:
            self.root.after_cancel(self._art_job)
        # While the window is being dragged bigger or smaller, redraw once it settles.
        self._art_job = self.root.after(COVER_SETTLE_MS, self._art_settled)

    def _toggle_art(self):
        """Show or hide the big cover. Hidden, the track list takes the room."""
        if self.show_art.get():
            self.art_box.pack(fill="x", before=self.head_text)
            self.head_text.pack_configure(pady=theme.pad(theme.GAP_LARGE, 0))
            self.art_size = 0  # size it afresh; _fit_art then fetches the cover
            self._art_hidden = False
            self.root.after_idle(self._fit_art)
        else:
            self.art_box.pack_forget()
            self.head_text.pack_configure(pady=0)
        self._check_layout(force=True)   # (a 780–879 px window splits only with a cover to show)

    def _request_art(self, path: Path | None, force: bool = False):
        """Find and shrink the cover off the UI thread; _poll picks the result up."""
        self._art_track = path
        if not self.show_art.get():
            return  # remembered, so switching the cover back on shows the right one
        # The narrow window enlarges a small cover to fill its square; a wide one never does.
        upscale = self._tier == layout.COLUMN
        key = (path, self.art_size, upscale)
        if (key == self._art_key and not force) or not self.art_size:
            return
        if not upscale and self._art_job is not None and not force:
            return   # being resized: the stand-in shows until it settles (_art_settled)
        self._art_key = key
        if path is None:
            self._show_art(None)
            return
        size = self.art_size

        def work():
            try:
                image = artwork.cover_for(path, size, upscale=upscale)
            except Exception:   # (a damaged picture: the placeholder instead)
                log.exception("couldn't read the cover for %s", path)
                image = None
            if self._art_key == key:   # asked for another since: this one is stale (it may finish last)
                self._art_result = (key, image)

        threading.Thread(target=work, name="cover-art", daemon=True).start()

    def _show_art(self, image, fetched: bool = True):
        """Put a cover up with softly rounded corners (0.20.0); no cover: a quiet ♫ placeholder
        the same size, so nothing shifts. A wide window shows a cover at most at its own size: a
        smaller one sits on a mat the size of the slot (and the cover's tooltip says so).
        fetched=False: a quick stand-in while the window is resized."""
        if fetched:
            self._art_image = image
        if self._tier != layout.COLUMN:
            self._show_wide_art(image)
            return
        self._cover_tip = ""
        size = image.width if image is not None else self.art_size
        if size < 2:
            return
        if image is None:
            image = controls.placeholder(size)
        image = controls.round_corners(image, controls.corner_radius(size), "BG")
        self._art_photo = ImageTk.PhotoImage(image, master=self.root)  # keep a reference or Tk drops it
        self.art_label.configure(image=self._art_photo, text="")

    def _show_wide_art(self, image):
        slot = self.art_size
        if slot < 2:
            return
        if image is None:
            self._cover_tip = ""
            picture = controls.round_corners(controls.placeholder(slot), controls.corner_radius(slot), "BG")
        else:
            width, height = image.info.get("source_size", image.size)
            if image.width < slot:
                picture = controls.matted(image, slot)
                self._cover_tip = f"Cover {width} × {height} px · shown 1:1, not enlarged"
            else:
                picture = controls.round_corners(image, controls.corner_radius(image.width), "BG")
                self._cover_tip = f"Cover {width} × {height} px · shown at {image.width}"
        self._art_photo = ImageTk.PhotoImage(picture, master=self.root)
        self.art_label.configure(image=self._art_photo, text="")

    def _paint_empty_mark(self):
        """The empty window's ♫ tile (a picture: drawn again on a palette change)."""
        size = theme.px(72)
        tile = controls.round_corners(controls.placeholder(size), controls.corner_radius(size), "BG")
        self._empty_photo = ImageTk.PhotoImage(tile, master=self.root)
        self.empty_mark.configure(image=self._empty_photo)

    def _show_empty(self, empty: bool):
        """No tabs open: the empty view in place of the cover, transport and track list (the status
        line stays), in any layout. A tab opening packs them back for the layout of the moment."""
        if empty == self._empty_shown:
            return
        self._empty_shown = empty
        if empty:
            for w in (self.split, self.head, self.seek_row, self.transport, self.playlist.frame):
                w.pack_forget()
            if self.up_next_label.winfo_manager() and str(self.up_next_label.pack_info()["in"]) != str(self.body):
                self._place_up_next()   # (it was in the wide window's list pane)
            self.empty_view.pack(fill="both", expand=True, after=self.status_row)
            return
        self.empty_view.pack_forget()
        if self._tier == layout.COLUMN:
            self._pack_column()
        else:
            self._pack_split()
        self.art_size = 0   # size the cover afresh
        self.root.after_idle(self._fit_art)

    def _palette_changed(self, _palette):
        """After a palette change (theme.use): redraw what keeps its colours — the cover's
        placeholder (a picture), the status dot and the buttons that show a state. (The tab strip
        repaints itself.)"""
        self._refresh_tabs()
        self._paint_status_dot(self._status_colour)
        if self._art_track is not None or self.art_size:
            self.art_size = 0   # (a wide window's mat, too, is drawn again in the new colours)
            self._fit_art()
        self._paint_order_buttons()
        self._paint_lock()
        self._paint_empty_mark()

    # -- refresh -------------------------------------------------------------
    def _album_summary(self, tab: Tab) -> str:
        """e.g. "FLAC · 24-bit / 96 kHz · 10 tracks · 41:07" for an album you've flicked to. Worked
        out once per change of the tab's list or song, not on every poll: a tab can hold a whole
        library."""
        track = tab.view_track()
        if track is None:
            if tab.unavailable:
                return f"Not connected: {tab.path}"
            return "Loading…" if tab.loading else ("Empty playlist" if tab.kind == "playlist" else "")
        key = (len(tab.tracks), tab.last_index)
        if tab.summary is not None and tab.summary[0] is tab.tracks and tab.summary[1] == key:
            return tab.summary[2]
        if tab.kind == "library":   # the whole tab, not its first album
            songs, albums, seconds = tab_numbers(tab)
            text = f"Library · {plural(songs, 'song')} · {plural(albums, 'album')} · {fmt_time(seconds)}"
        elif tab.kind == "playlist":
            count = len(tab.tracks)
            text = f"Playlist · {count} song{'' if count == 1 else 's'} · {fmt_time(sum(t.duration for t in tab.tracks))}"
        else:
            if track.lossy or not track.sample_rate:
                quality = track.codec
            else:
                quality = f"{track.codec} · {track.bits}-bit / {track.sample_rate / 1000:g} kHz"
            folder = album_folder(track.path)
            album = [t for t in tab.tracks if album_folder(t.path) == folder]
            text = f"{quality} · {len(album)} tracks · {fmt_time(sum(t.duration for t in album))}"
        tab.summary = (tab.tracks, key, text)
        return text

    def _poll(self):
        """Every POLL_MS: the window follows the player, settings are autosaved, waiting tag saves
        are retried and the watcher's reports taken in. Each part is guarded on its own and the
        loop always carries on: one error (logged once per kind) mustn't freeze the window or stop
        the autosave for the rest of the session."""
        try:
            self._guarded(self._refresh_view)
            self._guarded(self._count_listening)
            self._guarded(self._follow_plays)
            self._guarded(self._hold_playing)
            self._polls += 1
            if self._polls % AUTOSAVE_EVERY == 0:
                self._guarded(self._save_if_changed)
                if self._tag_waiting:
                    self._guarded(self._retry_tag_saves)
            if self._index_updated:
                self._index_updated = False
                self.checker.kick()   # (a tag edit changed the files: checked again)
                if self._library is not None and self._library.win.winfo_exists():
                    self._guarded(self._library.reload)
            self._guarded(self._watch_reports)
            if not self.favourites.ready:
                self._guarded(self.favourites.take_loaded)
            self._guarded(self._take_requests)
            self._guarded(self._take_cache_notices)
            self._guarded(self._take_update)
        finally:
            self._poll_job = self.root.after(POLL_MS, self._poll)

    def _count_listening(self):
        """Keep count of how much of the song playing has been heard; when it ends or is left (another
        song, stopped, started again), store it as a play if it counts as one (library_db.counts_as_play)
        — on a worker thread."""
        status = self.player.status()
        track = status.track if status.state is not State.STOPPED else None
        key = audiofile.key(track.path) if track is not None else None
        listening = self._listening
        if listening is not None and (key != listening.key or status.position < listening.position - HEARD_STEP_S):
            self._record_play()
            listening = None
        if key is None:
            return
        if listening is None:
            listening = self._listening = _Listening(key, float(track.duration or 0.0), status.position)
        step = status.position - listening.position
        if status.state is State.PLAYING and 0 < step <= HEARD_STEP_S:
            listening.heard += step
        listening.position = status.position

    def _record_play(self) -> threading.Thread | None:
        """Store the song just heard as a play (when it counts as one), on a worker thread."""
        listening, self._listening = self._listening, None
        if listening is None or not library_db.counts_as_play(listening.heard, listening.duration):
            return None
        thread = threading.Thread(target=_store_play, args=(self._index(), listening.key, listening.heard,
                                                            listening.duration), name="library-play", daemon=True)
        thread.start()
        return thread

    def _hold_playing(self):
        """(The poll.) A list read a page at a time keeps the songs round the one playing read — the
        one before it and the next few in play order — so the engine never waits for the database."""
        player = self.player
        tracks = getattr(player, "tracks", None)
        hold = getattr(tracks, "hold", None)
        if hold is None:
            return
        key = (id(tracks), player.index, player.shuffle, player.repeat, id(player._order))
        if key != self._held_key:
            self._held_key = key
            hold(player.order_around(player.index))

    def _follow_plays(self):
        """(The poll.) A play has been stored since: the Plays columns read their counts again."""
        stored = _plays_stored[0]
        if stored == self._plays_seen:
            return
        self._plays_seen = stored
        self.playlist.plays_changed()
        if self._library is not None and self._library.win.winfo_exists():
            self._library.plays_changed()



    def _record_last_play(self):
        """Closing: the song being heard counts too (given a moment to be stored)."""
        thread = self._record_play()
        if thread is not None:
            thread.join(1.0)

    def _guarded(self, step):
        """Run one part of the poll; an error is logged (the first of its kind) and the rest goes on."""
        try:
            step()
        except Exception as error:
            kind = (getattr(step, "__name__", repr(step)), type(error).__name__)
            if kind not in self._poll_errors:
                self._poll_errors.add(kind)
                log.exception("%s failed: %s (carrying on; more of these aren't logged)", *kind)

    def _refresh_view(self):
        """The top of the window, the seek bar, the transport and the track list's marker, the
        taskbar title, Windows' media panel and the mini strip: all as the player is now."""
        status = self.player.status()
        from_queue = _queued_alone(self.playing_tab, status)
        track = status.track if self.playing_tab is not None or from_queue else None
        if self.playing_tab is not None and status.index >= 0:
            playing = self.tabs[self.playing_tab]
            # Noted continuously, so switching tabs leaves it paused right here (and an album
            # that played to the end goes back to the start: the engine reports 0 there).
            playing.resume_at = status.position
            if playing.last_index != status.index:
                playing.last_index = status.index
                self._refresh_tabs()  # a tab of many albums takes the name of the one it's on
        # (Levelling first: its look-ahead measures loudness and true peak in one read of a file,
        # and the guard's own measurement then finds the peak already known.)
        self._apply_levelling(track, status)
        self._apply_guard(track, status.index)
        self._tick_sleep(status)
        shown = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        # A song from Up next with no tab behind it is shown as a playing tab's song would be.
        viewing_playing = (shown is not None and self.current == self.playing_tab) or from_queue

        # The top of the window follows the tab you're looking at. On the playing tab that's
        # the song; flick to another album and you see that album (press play to start it).
        if viewing_playing and track:
            self._request_art(track.path)
            self._set_head(NOW_PLAYING, track.title, track.display_artist_album or track.path.parent.name,
                           track.format_line)
            text, tone = output_line(status.output, status.gain, track.float_samples, track.lossy, status.trim,
                                     track.bits, track.codec, status.levelling, status.fading)
            # The proof (see proof.py) backs up — or contradicts — what the line above claims.
            proof = status.proof
            copy = getattr(proof, "copy", "")   # (played from the network playback cache: its copy)
            if proof is not None and proof.mismatch_at is not None:
                text, tone = f"{text} · ✗ the audio sent differed from the file at {fmt_time(proof.mismatch_at)}", "warn"
            elif copy == "failed":
                text, tone = f"{text} · {COPY_FAILED}", "warn"
            elif tone == "good" and proof is not None and proof.complete and proof.untouched:
                text += f" · proven sample for sample ({fmt_time(proof.checked)} checked)"
                if copy:
                    text += f" · {copy_note(copy)}"
            if status.dropouts:
                text, tone = f"{text} · {status.dropouts} dropout(s) this track: see ⓘ", "warn"
            showing_playing = True
            # (With no tab open the top of the window is hidden: the status line says it instead.)
            elsewhere = "" if shown is not None else (f"♪ Playing from Up next: {track.title}"
                                                      + (f" — {track.artist}" if track.artist else ""))
        elif shown is not None:
            view = shown.view_track()
            self._request_art(view.path if view else shown.first_file)
            caption = CAPTIONS.get(shown.kind, "")
            if view is not None and shown.last_index >= 0 and shown.resume_at >= 1:
                # Left paused part-way through a song: show where, ready to carry on.
                self._set_head(caption, view.title, view.display_artist_album or view.path.parent.name,
                               f"Paused at {fmt_time(shown.resume_at)} of {fmt_time(view.duration)} · press play "
                               "to carry on")
            elif shown.kind == "library" and shown.last_index < 0:   # (its first album's artist would mislead)
                self._set_head(caption, shown.title, "Press play to start", self._album_summary(shown))
            else:
                artist = (view.artist if view else shown.artist) or ""
                self._set_head(caption, shown.title, artist or "Press play to start this album",
                               self._album_summary(shown))
            text, tone = "", "plain"
            showing_playing = False
            elsewhere = (f"♪ Playing in another tab: {track.title}" + (f" — {track.album}" if track.album else "")
                         if track else "")
        else:
            self._request_art(None)
            self._set_head("", "Nothing playing", "Open your library: Ctrl+L", "")
            text, tone = "", "plain"
            showing_playing = False
            elsewhere = ""
        if self._art_result is not None and self._art_result[0] == self._art_key:
            self._show_art(self._art_result[1])
            self._art_result = None

        if track is not None and track.path != self._opened_key:  # a song of a new album plays: opened
            self._opened_key = track.path
            self._mark_opened(album_folder(Path(audiofile.real_path(track.path))))
        window_title = (f"{track.title} — {track.artist}" if track.artist else track.title) if track else APP_NAME
        if window_title != self._window_title:  # the taskbar always shows what's playing
            self.root.title(window_title)
            self._window_title = window_title
        self.media.show(self._media_now(track, status))  # Windows' media panel: the same song

        # The status dot: the colour says it all; hovering gives the detail.
        if text:
            role = {"good": "GOOD", "warn": "WARN"}.get(tone, "MUTED")
            verdict = {"good": "All good", "warn": "Heads up"}.get(tone, "Fine")
            self._status_detail = f"{verdict}: {text.removeprefix('→ ')}"
        else:
            role, self._status_detail = "BG", ""
        # A song on a network drive (the network playback cache): how its copy is getting on.
        network = getattr(status, "network", None)
        network_text, network_tone = network_line(network, track is not None and track.codec == "MP3")
        hover = network_hover(network) if track is not None else ""
        if hover:
            self._status_detail = f"{self._status_detail} · {hover}" if self._status_detail else hover
            if role == "BG":
                role = "WARN" if network_tone == "warn" else "MUTED"
        if role != self._status_colour:
            self._paint_status_dot(role)
        # Preparing or waiting for the network with the intent to play: the transport shows pause.
        playing_now = showing_playing and (status.state is State.PLAYING or bool(network is not None and network.auto))
        self.play_button.set_playing(playing_now)
        # The seek bar belongs to the tab you're looking at too: on another tab it shows
        # that tab's song and where it was left, not the time of the song playing elsewhere.
        self._seek_target_playing = showing_playing
        if showing_playing:
            position, duration = status.position, status.duration
        else:
            view = shown.view_track() if shown is not None else None
            position = shown.resume_at if view is not None and shown.last_index >= 0 else 0.0
            duration = view.duration if view is not None else 0.0
        # Always told: while dragged, the bar holds the value (it notices a song change mid-drag),
        # and the knob and the times follow the pointer. A network song shows what's downloaded.
        self.seek.set(position, duration)
        band = tuple(network.spans) if showing_playing and network is not None else ()
        self.seek.set_band(band)
        if not self.seek.dragging:
            self._show_times(position, duration)
        self.status_label.configure(text=self._notice or status.message or network_text or self._sleep_line(status)
                                    or elsewhere)
        self._paint_undo_link()
        # The mini strip shows the same as the top of the full window.
        if self._tier != layout.COLUMN:
            self.playlist.set_count(self._tab_count(shown) if shown is not None else "")
        self._mini_state = dict(title=self._head_full[0], artist=self._head_full[1],
                                position=position, duration=duration, playing=playing_now,
                                status_colour=role, status_detail=self._status_detail, notice=self._notice,
                                band=band)
        if self.mini_mode:
            self.mini.show(**self._mini_state)

        if self.player.volume < 0.995:
            self.volume_badge.set_text(f"{self.player.volume:.0%}")
            if not self.volume_badge.winfo_ismapped():
                self.volume_badge.place(relx=1.0, rely=0.5, anchor="e")
        else:
            self.volume_badge.place_forget()
        if self.settings_pop.is_open:
            self._update_volume_text()
            if self._netcache is not None and self.netplay_box.winfo_manager():
                self._paint_cache()   # (the network's speed and the cache's size move on while it's open)

        key = (self.current, status.index if self.current == self.playing_tab else None)
        if key != self._shown_key:
            if key[1] is not None and key[1] >= 0:
                self.playlist.show_current(key[1])
            else:
                self.playlist.clear_current()
            self._shown_key = key

        self._paint_up_next()

    def _save_if_changed(self) -> bool:
        """Settings are written as they change, not only on close — so a crash, Windows
        restarting or Task Manager can't lose your window position or choices. A write that
        fails (the file briefly locked, a full disk) isn't taken as saved: it's tried again at
        the next autosave. Returns whether the settings on disk are up to date."""
        self.prefs.update(
            volume=self.player.volume,
            exclusive=self.exclusive.get(),
            output_device=self.player.output_device or "",
            open_at_home=self.open_at_home.get(),
            tabs=[t.source() for t in self.tabs],
            current_tab=max(0, self.current),
            shuffle=self.player.shuffle,
            repeat=self.player.repeat,
            clip_guard=self.clip_guard.get(),
            show_art=self.show_art.get(),
            allow_network=self.allow_network.get(),
            media_controls=self.media_controls.get(),
            watch_music=self.watch_music.get(),
            check_updates=self.check_updates.get(),
            detailed_logging=self.detailed_logging.get(),
        )
        self.prefs["mini_on_top" if self.mini_mode else "always_on_top"] = self.on_top.get()
        # (0.25.0) While Settings has grown the mini strip's window, the strip's own place is kept.
        geometry = self._mini_settings_from or placement.current(self.root)
        if geometry:  # not while minimised, maximised or full screen; the mini strip keeps its own
            self.prefs["mini_geometry" if self.mini_mode else "geometry"] = geometry
        if not self.mini_mode and self._mini_settings_from is None:
            full = getattr(self.root, "_dg_fullscreen", None)   # (full screen: as it was before it)
            state = self.root.state()
            if full is not None:
                self.prefs["maximised"] = bool(full["zoomed"])
            elif state in ("normal", "zoomed"):   # (minimised: it keeps what it was)
                self.prefs["maximised"] = state == "zoomed"
        snapshot = json.dumps(self.prefs, sort_keys=True)
        if snapshot == self._saved_snapshot:
            return True
        if settings.save(self.prefs) is False:
            return False
        self._saved_snapshot = snapshot
        return True

    def quit(self):
        """Close the player. A tag save under way is first asked to stop after the file in hand
        (a file is never left half swapped, nor its checked copy left beside it), and songs being
        added to a playlist are written; then every step of closing runs, even if one before it
        fails, and the window always closes. Edits typed in Edit tags and not saved are asked about
        first (Ctrl+Q can be pressed in that window too): kept, the player stays open."""
        if self._closing:   # already on its way
            return
        tags = self._tag_window
        if tags is not None and tags.saving is None and tags.win.winfo_exists() and not tags.may_discard():
            return
        self._closing = True
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                log.exception("while closing: on_close failed")
        self._close_when_saved(time.monotonic() + QUIT_WAIT_MS / 1000)

    def _tag_saves_running(self) -> list:
        """The tag windows (Edit tags, Tag history) with a save on its way."""
        return [w for w in (self._tag_window, self._tag_history)
                if w is not None and getattr(w, "saving", None) is not None]

    def _close_when_saved(self, deadline: float):
        running = self._tag_saves_running()
        if (running or self._writing_playlists) and time.monotonic() < deadline:
            for window in running:
                if not window.saving.cancel.is_set():
                    window.close()   # (while saving, this only asks it to stop after the file in hand)
            self._set_notice("Closing once the tag save has finished the file in hand…" if running
                             else "Closing once the playlist is saved…", 0)
            self.root.after(100, self._close_when_saved, deadline)
            return
        try:
            for step in (self._save_on_close, self._stop_timers, self._stop_media, self.watcher.stop,
                         self._record_last_play, self._stop_indexing, self._stop_checking, self._store_favourites,
                         self.player.close, self._stop_cache, self._last_tag_tries):
                try:
                    step()
                except Exception:
                    log.exception("while closing: %s failed", getattr(step, "__name__", step))
        finally:
            self.root.destroy()

    def _save_on_close(self):
        if not self._save_if_changed():
            time.sleep(0.2)   # (a file locked for a moment by antivirus or backup): once more
            self._save_if_changed()

    def _stop_indexing(self):
        """A look at the library under way stops after the folder in hand (the next start carries on)."""
        self._index().indexer.cancel()

    def _stop_checking(self):
        """The library quality check stops the songs in hand (stored results stay; it carries on next time)."""
        self.checker.close(timeout=1.0)

    def _store_favourites(self):
        """A ♥ given or taken just before closing is stored before the player goes: its writer is a
        background thread, which would otherwise stop with the player and the change be lost."""
        if not self.favourites.flush(timeout=FAVOURITES_WAIT_S):
            log.warning("closing: a favourite change wasn't stored in time")

    def _stop_timers(self):
        for job in (self._poll_job, self._media_job, self._theme_job, self._update_job, self._check_job,
                    self._layout_job, self._art_job):
            if job is not None:
                self.root.after_cancel(job)

    def _last_tag_tries(self):
        """The player has let go of its files: a last quick try for the songs still waiting (a save
        that has just finished but not yet reported included); any still in use are given up."""
        for window in self._tag_saves_running():
            save = window.saving.save
            if window.saving.finished and save.waiting and save not in self._tag_waiting:
                self._tag_waiting.append(save)
        for save in self._tag_waiting:
            save.retry()
            save.abandon()

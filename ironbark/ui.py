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
saved library index (library_index.py) when it's up to date, so nothing is read twice.

Light / Dark (0.19.0): Settings → Window → Theme (Dark, Light, Follow Windows — Dark by default);
theme.use re-colours every open window at once. Mini mode (0.19.0): the same window shrunk to a
small strip (mini_view.py) — the Mini player button in Settings, key Z, or double-clicking the
cover; each of the two remembers its own position and size, and the music carries on.

The look (0.20.0): the seek bar, play button, round-hover transport buttons and the volume pill are
drawn controls (controls.py); the cover has softly rounded corners; with no tabs open the window
shows an "Open your library" button (Ctrl+L) instead of the empty layout.

Pop-ups (0.21.0): every question box is an in-app prompt and every right-click menu a
popups.PopupMenu (popups.py), so they follow Dark / Light; a prompt leaves the mini strip first
(`_for_prompt`). Settings scrolls when it's taller than the window (tabbar.Popover(scroll=True))."""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, ttk

from PIL import ImageTk

from . import (APP_NAME, __version__, appicon, artwork, audiofile, browse, chrome, controls, devices, dragdrop,
               experiments, library_index, mediakeys, placement, playlists, popups, settings, tag_editor, theme,
               watcher)
from .abx_window import AbxWindow
from .analysis import GUARD_HEADROOM_CHOICES, guard_trim, headroom_index, headroom_label
from .compare_window import CompareWindow
from .testsignals_window import TestSignalsWindow
from .details import NowPlayingWindow, output_line
from .engine import Player, State
from .library import Track, album_folder, find_albums, read_track, scan_album, scan_folder, title_from_filename
from .library_window import LibraryWindow
from .meter_window import MeterWindow
from .mini_view import MIN_SIZE as MINI_MIN_SIZE, DEFAULT_SIZE as MINI_DEFAULT_SIZE, MiniView, click_or_double
from .pcm_window import PcmWindow
from .clip_window import ClipWindow
from .scope_window import ScopeWindow
from .spectrogram_window import SpectrogramWindow
from .tag_window import TagHistoryWindow, TagWindow
from .playlist_view import PlaylistView, _fold
from .tabbar import IconButton, MenuRow, Popover, SettingRow, TabBar, Tooltip, glyph

log = logging.getLogger(__name__)

POLL_MS = 250
MEDIA_POLL_MS = 60   # how often presses from media keys / Windows' media panel are picked up
AUTOSAVE_EVERY = 8   # polls, i.e. settings are saved within ~2 s of a change
SAFE_VOLUME = 0.3    # player volume when bit-perfect is switched on without a device volume control
SEEK_STEP = 5.0
VOLUME_STEP = 0.05   # per mouse-wheel notch over the transport
MIN_LIST_HEIGHT = 190  # px (at 100% scaling) the track list keeps, however big the cover wants to be
INDEX_AFTER_MS = 3000  # the library index is brought up to date this long after start-up
NEW_MUSIC_NOTE_MS = 12000  # how long "New music: …" stays in the status line
UP_NEXT_SHOWN = 12     # songs listed in the Up next pop-over (the rest are counted)
THEME_POLL_MS = 2000   # Theme: Follow Windows looks at Windows' light/dark setting this often
FULL_MIN_SIZE = (300, 260)  # px at 100%: the full window's smallest size (the mini strip has its own)


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:  # a double album: 2:02:36, not 122:36
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


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


class PlayerWindow:
    def __init__(self, root: tk.Tk, player: Player, folder: str | None = None):
        self.root = root
        self.player = player
        self.prefs = settings.load()
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
        self._tag_window: TagWindow | None = None
        self._tag_history: TagHistoryWindow | None = None
        self._tag_waiting: list[tag_editor.TagSave] = []  # saves with songs waiting for the player to move on
        self._index_updated = False  # set by the thread that re-reads edited files into the index
        self._notice = ""            # a UI message that overrides the player's for a while
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
        player.set_volume(float(self.prefs["volume"]))
        self.volume_var = tk.DoubleVar(value=player.volume * 100)
        self.can_exclusive = devices.exclusive_supported()
        self.exclusive = tk.BooleanVar(value=bool(self.prefs["exclusive"]) and self.can_exclusive)
        self.open_at_home = tk.BooleanVar(value=bool(self.prefs["open_at_home"]))
        self.clip_guard = tk.BooleanVar(value=bool(self.prefs["clip_guard"]))
        self.show_art = tk.BooleanVar(value=bool(self.prefs["show_art"]))
        self.allow_network = tk.BooleanVar(value=bool(self.prefs["allow_network"]))
        self.media_controls = tk.BooleanVar(value=bool(self.prefs["media_controls"]))
        self.watch_music = tk.BooleanVar(value=bool(self.prefs["watch_music"]))
        # Spotting new music (0.17.0): a thread looks at the library folder every few minutes;
        # its reports are picked up by _poll (never handled on the watcher's thread).
        self.watcher = watcher.Watcher(self._index)
        self._new_albums: list[Path] = []        # the new albums, newest first (watcher.new_albums)
        self._opened_key = None                  # the song last marked as opening its album
        self.media = mediakeys.MediaKeys()        # presses from media keys / Windows arrive here
        self._windows_media = None                # mediakeys.WindowsMedia while switched on
        self._drop_overlay: list[tk.Frame] = []   # the green edge shown while something is dragged over
        self._drop_shown = None                   # what's lit up for a drag now: ("tab", i) / ("window", None)
        experiments.tidy(self.prefs)  # forget switches for experiments since rolled in or dropped
        self.experiment_vars: dict[str, tk.BooleanVar] = {}
        # Clipping guard: true peaks are measured in the background, one file at a time.
        self._peaks: dict[Path, float] = {}
        self._peak_jobs: queue.Queue = queue.Queue()
        self._guard_for: Path | None = None
        threading.Thread(target=self._measure_peaks, name="true-peak", daemon=True).start()
        self._closed_tabs: list[tuple[int, Tab]] = []  # for Ctrl+Shift+T
        player.set_output_device(self.prefs["output_device"] or None)
        player.set_shuffle(bool(self.prefs["shuffle"]))
        player.set_repeat(self.prefs["repeat"])
        if self.exclusive.get():
            player.refresh_hardware_volume()
            player.set_exclusive(True)

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
        root.deiconify()
        self._settle_size(start)
        theme.dark_title_bar(root)
        self._setup_drop()
        if self.media_controls.get():
            self._start_media()
        self._media_job = root.after(MEDIA_POLL_MS, self._media_poll)
        self._theme_job = root.after(THEME_POLL_MS, self._follow_windows)

        self._polls = 0
        self._saved_snapshot = json.dumps(self.prefs, sort_keys=True)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._restore_tabs(folder)
        self._poll_job = self.root.after(POLL_MS, self._poll)
        if self.watch_music.get():
            self._start_watching(INDEX_AFTER_MS / 1000)
        else:
            self.root.after(INDEX_AFTER_MS, self._refresh_index)
        self._update_new_albums()

    # -- layout --------------------------------------------------------------
    def _build(self):
        theme.apply(self.root)
        theme.on_change(self._palette_changed)
        self.bar = TabBar(self.root, self.root, on_select=self.select_tab, on_close=self.close_tab,
                          on_new=lambda: self.new_menu.toggle(self.bar.add_btn, self.bar.height),
                          on_settings=lambda: self._open_settings(),
                          on_double=self._bar_double_click, on_quit=self.quit,
                          on_tab_menu=self._tab_menu, on_move=self.move_tab)
        self.bar.pack(fill="x")

        body = ttk.Frame(self.root, padding=theme.pad(theme.GAP_WIDE, theme.GAP_LARGE, theme.GAP_WIDE, theme.GAP_MEDIUM))
        body.pack(fill="both", expand=True)
        self.body = body

        # Now playing: a square cover the full width of the window, the details underneath.
        # It follows the window's width, but never takes more than half its height, so the
        # track list always keeps room.
        head = ttk.Frame(body)
        head.pack(fill="x")
        self.art_box = tk.Frame(head, bg=theme.BG, height=1)
        self.art_box.pack(fill="x")
        self.art_box.pack_propagate(False)
        self.art_label = tk.Label(self.art_box, bg=theme.BG, bd=0, cursor="hand2", fg=theme.MUTED,
                                  compound="center")
        self.art_label.place(relx=0.5, rely=0, anchor="n")
        # Click: Now Playing; double-click: the mini strip (0.19.0).
        click_or_double(self.art_label, self.show_details, self.toggle_mini)
        self.art_box.bind("<Configure>", lambda e: self._fit_art())
        self.root.bind("<Configure>", lambda e: self._fit_art() if e.widget is self.root else None, add="+")
        text = ttk.Frame(head)
        text.pack(fill="x", pady=theme.pad(theme.GAP_LARGE, 0))
        self.head_text = text
        if not self.show_art.get():
            self.art_box.pack_forget()
            text.pack_configure(pady=0)
        self.title_label = ttk.Label(text, text="Nothing playing", style="Title.TLabel", anchor="w")
        self.title_label.pack(fill="x")
        self.sub_label = ttk.Label(text, text="Open your library: Ctrl+L", style="Sub.TLabel",
                                   anchor="w")
        self.sub_label.pack(fill="x")
        info_row = ttk.Frame(text)
        info_row.pack(fill="x", pady=theme.pad(theme.GAP_SMALL, 0))
        # Right of the format line: the status square, the bit-perfect lock and Details
        # (packed first, so a long format line can't squeeze them).
        IconButton(info_row, self.root, "info", self.show_details, size=theme.ICON_SMALL, width=26, bg="BG",
                   label="Details: live stats, file info, lossless check").pack(side="right")
        self.lock_button = None
        if self.can_exclusive:
            self.lock_button = IconButton(info_row, self.root, "unlock", self._toggle_lock, size=theme.ICON, width=26,
                                          bg="BG", label="Bit-perfect")
            self.lock_button.pack(side="right")
            self.lock_button.tip.text = self._lock_tip
        # One square instead of a line of device detail: green = all good, amber = something
        # is changing the sound, grey = fine but not bit-perfect. Hover for the detail.
        square = theme.px(10)
        self.status_square = tk.Frame(info_row, bg=theme.BG, width=square, height=square, cursor="hand2")
        self.status_square.pack(side="right", padx=theme.pad(theme.GAP_MEDIUM, theme.GAP_SMALL))
        self.status_square.bind("<Button-1>", lambda e: self.show_details())
        self._status_detail = ""
        Tooltip(self.status_square, lambda: self._status_detail)
        self._paint_lock()
        self.format_label = ttk.Label(info_row, text="", style="Info.TLabel", anchor="w", cursor="hand2")
        self.format_label.pack(side="left", fill="x", expand=True)
        self.format_label.bind("<Button-1>", lambda e: self.show_details())
        self.art_size = 0                    # set by _fit_art once the window has a size
        self._art_track: Path | None = None  # the track whose cover we want
        self._art_key: tuple | None = None   # (track, size) of the cover on show or on its way
        self._art_result: tuple | None = None
        self._art_job = None

        # (0.20.0) The seek bar: a drawn slider (controls.Slider, shared with the mini strip), the
        # time so far under its left end and the time left under its right.
        seek_row = ttk.Frame(body)
        seek_row.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
        self.seek_row = seek_row
        self.seek = controls.Slider(seek_row, on_release=self._seek_released, on_drag=self._seek_dragged)
        self.seek.pack(fill="x")
        times = ttk.Frame(seek_row)
        times.pack(fill="x")
        self.pos_label = ttk.Label(times, text="0:00", style="Info.TLabel")
        self.pos_label.pack(side="left")
        self.remain_label = ttk.Label(times, text="0:00", style="Info.TLabel", anchor="e")
        self.remain_label.pack(side="right")

        # Transport: no volume slider here. It lives under the gear, and a badge
        # appears beside the buttons only while the volume is below 100%.
        self.transport = tk.Frame(body, bg=theme.BG, height=theme.px(52))
        self.transport.pack(fill="x", pady=theme.pad(theme.GAP_SMALL, theme.GAP))
        self.transport.pack_propagate(False)
        buttons = tk.Frame(self.transport, bg=theme.BG)
        buttons.place(relx=0.5, rely=0.5, anchor="center")
        # (0.20.0) The play button is a drawn green circle; the others light up with a soft round disc.
        self.shuffle_button = controls.RoundIconButton(buttons, self.root, "shuffle", self.toggle_shuffle,
                                                       size=theme.ICON_MEDIUM, diameter=32, width=36, bg="BG",
                                                       label="Shuffle (S)")
        self.shuffle_button.pack(side="left", fill="y", padx=theme.pad(0, theme.GAP_SMALL))
        controls.RoundIconButton(buttons, self.root, "prev", self.player.previous, size=theme.ICON_LARGE, diameter=36,
                                 width=40, bg="BG", label="Previous (P)").pack(side="left", fill="y")
        self.play_button = controls.PlayButton(buttons, self.toggle_play, label="Play / pause (Space)")
        self.play_button.pack(side="left", padx=theme.px(theme.GAP_MEDIUM))
        controls.RoundIconButton(buttons, self.root, "next", self.player.next, size=theme.ICON_LARGE, diameter=36,
                                 width=40, bg="BG", label="Next (N)").pack(side="left", fill="y")
        self.repeat_button = controls.RoundIconButton(buttons, self.root, "repeat", self.cycle_repeat,
                                                      size=theme.ICON_MEDIUM, diameter=32, width=36, bg="BG",
                                                      label="Repeat (R)")
        self.repeat_button.pack(side="left", fill="y", padx=theme.pad(theme.GAP_SMALL, 0))
        self._paint_order_buttons()
        self.volume_badge = controls.PillButton(self.transport, "", self._open_settings, fill=None, fg="WARN",
                                                outline="WARN", hover_fill="SURFACE_HI",
                                                font=theme.font(theme.SIZE_SMALL), pad_x=theme.GAP, pad_y=1,
                                                label="Volume below 100% · click for Settings, or use the mouse "
                                                      "wheel over the buttons")
        for widget in (self.transport, buttons, *buttons.winfo_children(), self.volume_badge):
            widget.bind("<MouseWheel>", self._wheel_volume)

        # The status line is packed first, from the bottom, so a short window squeezes the
        # track list rather than pushing the status line out of sight.
        self.status_label = ttk.Label(body, text="", style="Sub.TLabel", anchor="w")
        self.status_label.pack(side="bottom", fill="x", pady=theme.pad(theme.GAP, 0))
        self.status_label.bind("<Button-1>", self._status_clicked)
        self.playlist = PlaylistView(body, on_play=lambda i: self._play_tab(self.current, i),
                                     on_menu=self._track_menu, on_remove=self._remove_from_playlist,
                                     on_move=self._move_in_playlist, on_drag=self._song_drag,
                                     on_heading_menu=self._heading_menu)
        self.playlist.set_columns(self.prefs.get("track_columns") or ())
        # "Up next (3) · Song": shown above the status line only while songs are queued; click
        # for the list. (Packed from the bottom, ahead of the track list, like the status line.)
        self.up_next_label = ttk.Label(body, text="", style="UpNext.TLabel", anchor="w", cursor="hand2")
        self.up_next_label.bind("<Button-1>", lambda e: self._show_up_next())
        self._up_next_key = None
        self.up_next_pop = Popover(self.root, width=theme.px(300), keys=True)

        # (0.20.0) The empty window (no tabs open): instead of "Nothing playing", a quiet ♫, one
        # clear button and the shortcut. Shown in place of the cover / transport / list by
        # _show_empty (the status line stays, for "Closed … click to reopen" and drop notes).
        self.head = head
        self.empty_view = tk.Frame(body, bg=theme.BG)
        middle = tk.Frame(self.empty_view, bg=theme.BG)
        middle.place(relx=0.5, rely=0.45, anchor="center")
        self.empty_mark = tk.Label(middle, bg=theme.BG, bd=0)
        self.empty_mark.pack(pady=theme.pad(0, theme.GAP_WIDE))
        self.empty_button = controls.PillButton(middle, "Open your library", self.open_library,
                                                font=theme.font(theme.SIZE_BODY, "bold"),
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
        """An on/off setting: a slider switch on the right, a line underneath saying what it does."""
        SettingRow(parent, self.root, title, description, variable, command,
                   wrap=theme.px(212)).pack(fill="x", pady=theme.pad(theme.GAP, 0))

    def _pop_button(self, parent, text: str, command, label: str = "") -> controls.PillButton:
        """(0.21.0) A flat button in a pop-over (Settings): a soft grey pill, darker under the pointer —
        drawn like the rest of the app's buttons instead of a stock ttk button."""
        return controls.PillButton(parent, text, command, fill="SURFACE_HI", fg="TEXT", hover_fill="SURFACE_DOWN",
                                   bg="SURFACE", font=theme.font(theme.SIZE_BODY), pad_x=theme.GAP_MEDIUM,
                                   pad_y=theme.GAP_HAIR, label=label)

    def _build_settings(self) -> Popover:
        """Grouped into Sound / Window / Library / Network / Experimental, every on/off choice a slider
        switch with one plain line saying what it does.

        (0.21.0) It scrolls when it doesn't fit the window (it's ~1300 px tall at 150%
        scaling) — mouse wheel anywhere over it, and a slim scrollbar that shows only then. Its
        buttons are flat pills and the volume is the same drawn slider as the seek bar."""
        pop = Popover(self.root, width=theme.px(310), scroll=True)
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
        self.output_box = ttk.Combobox(b, state="readonly", takefocus=False, postcommand=self._refresh_outputs)
        self.output_box.pack(fill="x")
        self.output_box.bind("<<ComboboxSelected>>", self._change_output)
        Tooltip(self.output_box, "Output: where the sound goes")
        self._refresh_outputs()
        if self.can_exclusive:
            self._setting(b, "Bit-perfect", "Sound exactly as recorded. Other apps go quiet.",
                          self.exclusive, self._toggle_exclusive)
        self._setting(b, "Clipping guard", "Lowers only tracks that would crackle, by just enough.",
                      self.clip_guard, self._guard_changed)
        # (0.24.0) "My DAC has headroom": room some converters have above full scale. Off by default.
        row = ttk.Frame(b, style="Pop.TFrame")
        row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
        ttk.Label(row, text="My DAC has headroom", style="Pop.TLabel").pack(side="left")
        self.headroom_box = ttk.Combobox(row, state="readonly", takefocus=False, width=8,
                                         values=[headroom_label(v) for v in GUARD_HEADROOM_CHOICES])
        self.headroom_box.pack(side="right")
        self.headroom_box.current(headroom_index(self.prefs.get("clip_headroom", 0.0)))
        self.headroom_box.bind("<<ComboboxSelected>>", self._headroom_chosen)
        Tooltip(self.headroom_box, "Only if your converter is known to take overs: the clipping guard then lets "
                                   "the wave go this far past full scale before it turns a track down. "
                                   "Measured converters range from none to about 3.5 dB. Off = none.")
        row = ttk.Frame(b, style="Pop.TFrame")
        row.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
        ttk.Label(row, text="Volume", style="Pop.TLabel").pack(side="left", padx=theme.pad(0, theme.GAP_MEDIUM))
        self.volume_text = ttk.Label(row, text="", style="PopMuted.TLabel", width=15, anchor="e")
        self.volume_text.pack(side="right", padx=theme.pad(theme.GAP_MEDIUM, 0))
        # (0.21.0) The drawn slider (controls.Slider), following volume_var however it's changed.
        self.volume_slider = controls.Slider(row, on_release=self._volume_slid, on_drag=self._volume_slid,
                                             bg="SURFACE")
        self.volume_slider.pack(side="left", fill="x", expand=True)
        self.volume_slider.set(self.volume_var.get(), 100.0)
        self.volume_var.trace_add("write", lambda *_: self.volume_slider.set(self.volume_var.get(), 100.0))

        self._caption(b, "Window")
        row = ttk.Frame(b, style="Pop.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Theme", style="Pop.TLabel").pack(side="left")
        self.theme_box = ttk.Combobox(row, state="readonly", takefocus=False, width=16,
                                      values=[label for _, label in theme.THEME_CHOICES])
        self.theme_box.pack(side="right")
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
        row = ttk.Frame(b, style="Pop.TFrame")
        row.pack(fill="x")
        self.library_text = ttk.Label(row, text="", style="Pop.TLabel")
        self.library_text.pack(side="left", fill="x", expand=True)
        self._pop_button(row, "Change…", self._change_library_folder, "Choose the folder your music is in").pack(
            side="right")
        self._update_library_text()
        self._setting(b, "Watch for new music", "Checks the folder every few minutes.",
                      self.watch_music, self._toggle_watch)

        self._caption(b, "Network")
        self._setting(b, "Allow network access", "Lets rip verification look up checksums online "
                      "(AccurateRip, CUETools DB). Nothing else uses the internet.",
                      self.allow_network, self._network_changed)

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
        self.version_label = ttk.Label(b, text="", style="PopMuted.TLabel")
        self.version_label.pack(anchor="e", pady=theme.pad(theme.GAP_LARGE, 0))
        self._paint_version()
        return pop

    def _volume_slid(self, value: float):
        """The Settings volume slider moved (while dragging, and let go)."""
        self.player.set_volume(value / 100)
        self.volume_var.set(self.player.volume * 100)   # (the slider ignores this while it's being dragged)
        self._update_volume_text()

    def _network_changed(self):
        """Off by default; read live by rip verification (ripcheck.network_allowed), the only
        part of the player that goes online."""
        self.prefs["allow_network"] = self.allow_network.get()
        if self._now_playing is not None and self._now_playing.win.winfo_exists():
            self._now_playing.network_changed()

    def _toggle_experiment(self, key: str):
        experiments.set_enabled(self.prefs, key, self.experiment_vars[key].get())
        self._paint_version()
        if key == tag_editor.EXPERIMENT and not self._tag_editing():  # switched off: its windows go too
            for window in (self._tag_window, self._tag_history):
                if window is not None and window.win.winfo_exists():
                    window.close()  # (one in the middle of saving stops after the file in hand)

    def _paint_version(self):
        """The version, and how many experiments are on — so it's never a mystery why the player
        behaves differently from the release."""
        on = len(experiments.switched_on(self.prefs))
        extra = f" · {on} experiment{'' if on == 1 else 's'} on" if on else ""
        self.version_label.configure(text=f"{APP_NAME} {__version__}{extra}")

    def _build_new_menu(self) -> Popover:
        pop = Popover(self.root, width=theme.px(240), keys=True, padding=(0, theme.GAP_SMALL))
        MenuRow(pop.body, "Browse library…", self.open_library, "Ctrl+L").pack(fill="x")
        MenuRow(pop.body, "Open a folder…", self.choose_folder, "Ctrl+O").pack(fill="x")
        MenuRow(pop.body, "New playlist…", self.new_playlist, "Ctrl+N").pack(fill="x")
        MenuRow(pop.body, "Open a playlist file…", self.open_playlist_file).pack(fill="x")
        return pop

    def _typing(self) -> bool:
        """Is the search box being typed in? Then single-key shortcuts are just letters."""
        return isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry))

    def _key(self, action):
        def handler(event):
            if self._typing():
                return None
            action()
            return "break"
        return handler

    def _bind_keys(self):
        bind = self.root.bind
        bind("<Control-f>", lambda e: self.playlist.focus_search())
        bind("<space>", self._key(self.toggle_play))
        bind("<Control-o>", lambda e: self.choose_folder())
        bind("<Control-n>", lambda e: self.new_playlist())
        bind("s", self._key(self.toggle_shuffle))
        bind("r", self._key(self.cycle_repeat))
        bind("<Control-l>", lambda e: self.open_library())
        bind("<Control-t>", lambda e: self.open_library())
        bind("<Control-w>", lambda e: self.close_tab(self.current) if self.tabs else None)
        bind("<Control-T>", lambda e: self.reopen_tab())  # Ctrl+Shift+T, as in browsers
        bind("<Control-Tab>", lambda e: self._cycle_tab(1))
        bind("<Control-Shift-Tab>", lambda e: self._cycle_tab(-1))
        bind("<Control-Shift-Prior>", lambda e: self.move_tab(self.current, self.current - 1))  # PgUp
        bind("<Control-Shift-Next>", lambda e: self.move_tab(self.current, self.current + 1))   # PgDn
        bind("<Control-H>", lambda e: self.snap_home())   # Ctrl+Shift+H
        bind("<Control-comma>", lambda e: self._open_settings())
        bind("<Left>", self._key(lambda: self._nudge(-SEEK_STEP)))
        bind("<Right>", self._key(lambda: self._nudge(SEEK_STEP)))
        bind("n", self._key(self.player.next))
        bind("p", self._key(self.player.previous))
        bind("i", self._key(self.show_details))
        bind("m", self._key(self.show_meters))
        bind("b", self._key(self.show_pcm))   # b for bits
        bind("c", self._key(self.show_clipping))   # c for clipping (inter-sample overs)
        bind("f", self._key(self.show_spectrogram))   # f for frequencies
        bind("o", self._key(self.show_scope))   # o: the scope's round picture
        bind("d", self._key(self.show_compare))   # d for difference
        bind("x", self._key(self.show_abx))   # x: the ABX test's mystery version
        bind("t", self._key(self.show_testsignals))   # t for test signals
        bind("e", self._key(self.edit_selected_tags))   # e: edit tags (only while that experiment is on)
        bind("z", self._key(self.toggle_mini))   # z: zoom the window down to the mini strip, and back

    # -- tabs ----------------------------------------------------------------
    def _restore_tabs(self, folder: str | None):
        saved = self.prefs.get("tabs")
        if saved is None:  # settings from before tabs existed
            saved = []
            if self.prefs["last_folder"]:
                kind = self.prefs["last_mode"] or ("album" if self.prefs["last_album_only"] else "folder")
                saved = [{"kind": kind, "path": self.prefs["last_folder"], "query": self.prefs["last_filter"]}]
        for source in saved:
            path = source.get("path", "")
            kind = source.get("kind")
            exists = Path(path).is_file() if kind == "playlist" else Path(path).is_dir()
            if path and exists and kind in ("album", "folder", "library", "playlist", "files"):
                tab = Tab(source["kind"], path, source.get("query", ""))
                resume = source.get("resume") or {}
                tab.resume_path, tab.resume_at = str(resume.get("path", "")), float(resume.get("at", 0))
                tab.extras = [p for p in source.get("extras", []) if isinstance(p, str)]
                self.tabs.append(tab)
        if folder:
            self.open_tab("folder", folder, play=True)
        elif self.tabs:
            self.select_tab(min(max(0, int(self.prefs["current_tab"])), len(self.tabs) - 1))
        else:
            self._refresh_tabs()

    def open_tab(self, kind: str, path, query: str = "", play: bool = False):
        """Open (or switch to, if it's already open) a tab for this album / folder / library view.
        play: start it once it's loaded — but only when nothing is playing or paused. Music
        already going carries on while you set up other tabs; press play in the new one when ready."""
        if kind == "album":
            self._mark_opened(Path(str(path)))
        if play and self.player.status().state is not State.STOPPED:
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
        if remember:
            self._closed_tabs = (self._closed_tabs + [(index, self.tabs[index])])[-10:]
            self._set_notice(f"Closed “{self.tabs[index].title}” · click here or press Ctrl+Shift+T to reopen", 8000)
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
        """Scan a tab's files in the background (a whole library can take a few seconds)."""
        if tab.loading:
            return
        tab.loading = True
        progress = {"done": 0, "total": 0, "tracks": None, "missing": 0}

        def work():
            try:
                if tab.kind == "playlist":
                    tracks = self._read_playlist(Path(tab.path))
                    progress["missing"] = sum(1 for t in tracks if t.missing)
                elif tab.kind == "library":
                    albums = self._library_albums(Path(tab.path))
                    if tab.query:  # the same search as the Library window's
                        words = [_fold(w) for w in tab.query.split()]
                        albums = [a for a in albums if browse.matches(a, words, _fold)]
                    progress["total"] = len(albums)
                    tracks = []
                    for album in albums:
                        tracks += self._scan(album.path)
                        progress["done"] += 1
                elif tab.kind == "files":
                    tracks = []  # dropped files: all in extras
                else:
                    tracks = self._scan(Path(tab.path), whole_tree=tab.kind != "album")
                for extra in list(tab.extras):  # albums / files added later (a drive may be unplugged)
                    if Path(extra).is_dir():
                        tracks += self._scan(Path(extra))
                    elif Path(extra).is_file():
                        tracks.append(read_track(Path(extra)))
            except OSError:
                tracks = []
            progress["tracks"] = tracks

        threading.Thread(target=work, name="tab-load", daemon=True).start()
        self.root.after(50, self._finish_load, tab, progress, then_play)

    def _finish_load(self, tab: Tab, progress: dict, then_play: bool):
        index = self._tab_index(tab)
        if index < 0:
            return  # closed while loading
        if progress["tracks"] is None:
            if tab.kind == "library":
                self._set_notice(f"Loading library… {progress['done']} of {progress['total'] or '?'} albums", 0)
            self.root.after(100, self._finish_load, tab, progress, then_play)
            return
        tab.tracks, tab.loading = progress["tracks"], False
        if tab.resume_path and tab.last_index < 0:  # where it got to last time
            tab.last_index = next((i for i, t in enumerate(tab.tracks) if audiofile.key(t.path) == tab.resume_path), -1)
            if tab.last_index < 0:
                tab.resume_at = 0.0
        if tab.kind == "playlist" and not tab.tracks:
            self._set_notice("Empty playlist: right-click songs in any tab and choose Add to playlist", 8000)
        elif progress["missing"]:
            self._set_notice(f"{progress['missing']} song(s) in “{tab.title}” can't be found: moved, or on a drive "
                             "that isn't plugged in?", 8000)
        elif not tab.tracks:
            self._set_notice(f"No FLAC, WAV or MP3 files in {tab.title}")
        elif tab.kind == "library":
            self._set_notice(f"Loaded {len(tab.tracks)} tracks from {progress['total']} albums")
        if index == self.current:
            self._show_current_tab()
        if then_play and tab.tracks:
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
                pass
            tab.info = (name, artist, first, found.album if first else "")

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
        self.player.play(track, start_at=start_at)
        self._shown_key = None
        self._refresh_tabs()

    def toggle_play(self):
        """Pause/resume what's playing; or, when you've flicked to another album, play that."""
        if self.playing_tab is not None and self.current == self.playing_tab:
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

    def _paint_order_buttons(self):
        self.shuffle_button.set_colour("ACCENT" if self.player.shuffle else "MUTED")
        mode = self.player.repeat
        self.repeat_button.configure(text=glyph(self.root, "repeat_one" if mode == "one" else "repeat"))
        self.repeat_button.set_colour("MUTED" if mode == "off" else "ACCENT")

    # -- clipping guard ------------------------------------------------------------
    def _headroom_db(self) -> float:
        try:
            value = float(self.prefs.get("clip_headroom") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if value in GUARD_HEADROOM_CHOICES else 0.0

    def _measure_peaks(self):
        """Background worker: true peak of each file it's given (a second or two per track)."""
        from .analysis import true_peak

        while True:
            path = self._peak_jobs.get()
            if path in self._peaks:
                continue
            try:
                self._peaks[path] = true_peak(path) or 0.0
            except (OSError, RuntimeError, ValueError):
                self._peaks[path] = 0.0

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
        if track.path != self._guard_for:
            self._guard_for = track.path
            for path in paths:
                self._peak_jobs.put(path)
        for path in paths:
            peak = self._peaks.get(path)
            if peak is None:
                continue
            # (0.24.0) Measured at 8x; just under full scale (plus any headroom the DAC has), with
            # 0.17 dB spare — the most 8x can under-read (analysis.guard_trim).
            trim = guard_trim(peak, self._headroom_db())
            if abs(self.player.trim_for(path) - trim) > 1e-6:
                self.player.set_trim(path, trim)

    # -- playlists -------------------------------------------------------------
    @staticmethod
    def _read_playlist(path: Path) -> list[Track]:
        """A playlist's songs, in order. Files that aren't there stay listed (marked missing),
        so a playlist never loses songs just because a drive was unplugged."""
        return [read_track(song) if song.is_file()
                else Track(song, title_from_filename(song), missing=True)
                for song in playlists.read(path)]

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
        except OSError as exc:
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
        tab = self._playlist_tab(path)
        existing = list(tab.tracks) if tab and tab.tracks is not None else self._read_playlist(path)
        self._save_playlist(path, existing + list(tracks))
        self._set_notice(f"Added {len(tracks)} song{'' if len(tracks) == 1 else 's'} to “{path.stem}”")

    def _save_playlist(self, path: Path, tracks: list[Track]):
        """Write the playlist, and update its tab (and the music, if it's playing from it)."""
        try:
            playlists.write(path, tracks)
        except OSError as exc:
            self._for_prompt()
            popups.tell(self.root, "Couldn't save the playlist", str(exc), tone="warn")
            return
        tab = self._playlist_tab(path)
        if tab is None or tab.tracks is None:
            return
        tab.tracks = list(tracks)
        index = self._tab_index(tab)
        if index == self.playing_tab:
            self.player.set_tracks(tab.tracks, keep_playing=True)
        if index == self.current:
            self._show_current_tab()

    def _remove_from_playlist(self, index: int):
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        if tab is None or tab.kind != "playlist" or not tab.tracks or not 0 <= index < len(tab.tracks):
            return
        tracks = list(tab.tracks)
        tracks.pop(index)
        self._save_playlist(Path(tab.path), tracks)
        if tracks:
            self.playlist.select(min(index, len(tracks) - 1))

    def _move_in_playlist(self, index: int, step: int):
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        if tab is None or tab.kind != "playlist" or not tab.tracks:
            return
        target = index + step
        if not (0 <= index < len(tab.tracks) and 0 <= target < len(tab.tracks)):
            return
        tracks = list(tab.tracks)
        tracks[index], tracks[target] = tracks[target], tracks[index]
        self._save_playlist(Path(tab.path), tracks)
        self.playlist.select(target)

    def _menu(self) -> popups.PopupMenu:
        """(0.21.0) A right-click menu in the app's colours (popups.PopupMenu), over the main window."""
        return popups.PopupMenu(self.root)

    def _track_menu(self, rows: list[int], event):
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        if tab is None or not tab.tracks or not rows:
            return
        tracks = [tab.tracks[i] for i in rows]
        single = len(rows) == 1
        menu = self._menu()
        menu.add("Play" if single else "Play this album", lambda: self._play_tab(self.current, rows[0]), "Enter")
        menu.add("Play next", lambda: self._queue_tracks(tracks, front=True))
        menu.add("Add to queue", lambda: self._queue_tracks(tracks, front=False))
        menu.add_separator()
        add = menu.add_submenu("Add to playlist" if single else f"Add these {len(rows)} songs to playlist")
        targets = [p for p in self._known_playlists() if not (tab.kind == "playlist" and Path(tab.path) == p)]
        for path in targets:
            add.add(path.stem, lambda p=path: self.add_to_playlist(p, tracks))
        add.add_separator()
        add.add("New playlist…", lambda: self.new_playlist(tracks), "Ctrl+N")
        if self._tag_editing():
            menu.add_separator()
            menu.add("Edit tags…" if single else f"Edit tags of these {len(rows)} songs…",
                     lambda: self.edit_tags(tracks), "E")
            menu.add("Tag history (undo)…", self.show_tag_history)
        if tab.kind == "playlist" and single:
            index = rows[0]
            menu.add_separator()
            menu.add("Move up", lambda: self._move_in_playlist(index, -1), "Ctrl+Up", enabled=index > 0)
            menu.add("Move down", lambda: self._move_in_playlist(index, 1), "Ctrl+Down",
                     enabled=index < len(tab.tracks) - 1)
            menu.add("Remove from playlist", lambda: self._remove_from_playlist(index), "Del")
        menu.add_separator()
        self._columns_menu(menu.add_submenu("Columns"))
        menu.popup(event.x_root, event.y_root)

    def _columns_menu(self, menu):
        """Artist / Album columns in the track list (off by default: the window is narrow). Fills
        `menu` (a PopupMenu, or a submenu of one) with a tick item per column."""
        for column, label in (("artist", "Artist"), ("album", "Album")):
            on = column in self.playlist.columns
            menu.add_check(label, on, lambda c=column, on=on: self._set_track_column(c, not on))
        return menu

    def _heading_menu(self, event):
        menu = self._menu()
        self._columns_menu(menu)
        menu.popup(event.x_root, event.y_root)

    def _set_track_column(self, column: str, on: bool):
        columns = [c for c in self.playlist.columns if c != column] + ([column] if on else [])
        self.playlist.set_columns(columns)
        self.prefs["track_columns"] = list(self.playlist.columns)

    def _tab_menu(self, index: int, event):
        if not 0 <= index < len(self.tabs):
            return
        tab = self.tabs[index]
        menu = self._menu()
        if tab.kind == "playlist":
            menu.add("Rename playlist…", lambda: self.rename_playlist(index))
            menu.add("Delete playlist…", lambda: self.delete_playlist(index))
            menu.add_separator()
        if len(self.tabs) > 1:
            menu.add("Move left", lambda: self.move_tab(index, index - 1), "Ctrl+Shift+PgUp", enabled=index > 0)
            menu.add("Move right", lambda: self.move_tab(index, index + 1), "Ctrl+Shift+PgDn",
                     enabled=index < len(self.tabs) - 1)
            menu.add_separator()
        menu.add("Close tab", lambda: self.close_tab(index), "Ctrl+W")
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
        tab = self.tabs[index]
        name = Path(tab.path).stem
        self._for_prompt()
        if not popups.confirm(self.root, f"Delete the playlist “{name}”?",
                              "Only the playlist goes; the songs themselves aren't touched.",
                              ok="Delete", danger=True):
            return
        if tab not in self.tabs:   # (closed while the box was up)
            return
        index = self.tabs.index(tab)
        self.close_tab(index, remember=False)
        try:
            Path(tab.path).unlink()
        except OSError as exc:
            self._set_notice(f"Couldn't delete the playlist file: {exc}")
            return
        self._set_notice(f"Deleted the playlist “{name}”")

    # -- opening things ------------------------------------------------------
    def choose_folder(self):
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        initial = tab.path if tab else str(self._library_root())
        folder = filedialog.askdirectory(title="Choose a folder of music", initialdir=initial, mustexist=True)
        if folder:
            self.open_tab("folder", folder, play=True)

    def _library_root(self) -> Path:
        return Path(self.prefs["library_folder"] or Path.home() / "Music")

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
            try:
                result["tracks"] = self._scan(album)
            except OSError:
                result["tracks"] = []

        threading.Thread(target=work, name="add-album", daemon=True).start()
        self.root.after(50, self._finish_add_album, tab, album, result, on_done)

    def _finish_add_album(self, tab: Tab, album: Path, result: dict, on_done):
        if "tracks" not in result or tab.loading:  # (a tab still loading gets it once it's loaded)
            self.root.after(50, self._finish_add_album, tab, album, result, on_done)
            return
        if self._tab_index(tab) < 0:
            return  # the tab was closed meanwhile
        tracks = result["tracks"]
        name = tracks[0].album if tracks and tracks[0].album else album.name
        have = {str(t.path) for t in tab.tracks} if tab.tracks else set()
        if not tracks:
            message = f"No FLAC, WAV or MP3 files in “{album.name}”"
        elif tab.kind != "playlist" and (Path(tab.path) == album or str(album) in tab.extras
                                         or all(str(t.path) in have for t in tracks)):
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

    def open_library(self):
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        current = Path(tab.path) if tab and tab.kind == "album" else None
        if self._library is not None and self._library.win.winfo_exists():
            self._library.close()
        self._library = LibraryWindow(self.root, self._library_root(), current,
                      on_choose=lambda path: self.open_tab("album", path, play=True),
                      on_root_changed=self._set_library_folder,
                      on_load_all=lambda root, query: self.open_tab("library", root, query),
                      on_add=self.add_album_to_current, add_target=self._current_tab_title,
                      on_drag=self._album_drag, on_queue=self.queue_album,
                      view=self.prefs.get("library_view") or "albums",
                      on_view=lambda view: self.prefs.update(library_view=view),
                      geometry=self.prefs["library_geometry"],
                      on_moved=lambda geometry: self.prefs.update(library_geometry=geometry),
                      on_edit_tags=self.edit_album_tags, can_edit_tags=self._tag_editing,
                      new_albums=lambda: self._new_albums)

    # -- the library index --------------------------------------------------------------------------
    def _index(self) -> library_index.LibraryIndex:
        return library_index.shared(self._library_root())

    def _refresh_index(self):
        """Bring the saved library index up to date in the background (only changed files are
        read; the first time ever, every file once), so the Library opens at once."""
        index = self._index()
        if not index.root.is_dir() or index.refreshed:
            return
        threading.Thread(target=index.refresh, name="library-index", daemon=True).start()

    def _library_albums(self, root: Path) -> list:
        """Every album under `root`, from the index when `root` is the library folder."""
        index = self._index()
        if index.root == root and root.is_dir():
            index.refresh()  # (quick when nothing has changed: a walk of names, sizes and times)
            return index.albums()
        return find_albums(root) if root.is_dir() else []

    def _scan(self, folder: Path, whole_tree: bool = False) -> list[Track]:
        """An album's (or a whole folder's) tracks in play order: from the index when it's up to
        date for that folder, else read from the files."""
        tracks = self._index().album_tracks(Path(folder), whole_tree=whole_tree)
        if tracks is not None:
            return tracks
        return scan_folder(folder) if whole_tree else scan_album(folder)

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
            self.edit_tags([tab.tracks[i] for i in rows])

    def edit_tags(self, tracks: list[Track]):
        if not self._tag_editing() or not tracks:
            return
        if self._tag_window is not None and self._tag_window.win.winfo_exists():
            if self._tag_window.saving is not None:
                self._tag_window.lift()
                self._set_notice("Still saving the last tag edit…")
                return
            self._tag_window.close()
        self._tag_window = TagWindow(self.root, tracks, self.prefs, on_saved=self._tags_saved,
                                     busy=self._busy_files, on_history=self.show_tag_history)

    def edit_album_tags(self, album: Path):
        """From the Library: edit a whole album's tags."""
        if not self._tag_editing():
            return
        result: dict = {}

        def work():
            try:
                result["tracks"] = self._scan(album)
            except OSError:
                result["tracks"] = []

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
        self._tag_history = TagHistoryWindow(self.root, self.prefs, on_saved=self._tags_saved, busy=self._busy_files)

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
        fresh: dict[Path, Track] = {}
        touched = []
        tracks = [t for tab in self.tabs for t in (tab.tracks or [])] + self.player.up_next()
        for track in tracks:
            real = Path(audiofile.real_path(track.path))
            if real not in changed or audiofile.cue_of(track.path) is not None:
                continue
            if real not in fresh:
                try:
                    fresh[real] = read_track(real)
                except OSError:
                    continue
            for name in self.TAG_FIELDS:
                setattr(track, name, getattr(fresh[real], name))
            touched.append(track)
        for tab in self.tabs:
            if tab.kind not in ("library", "playlist") and any(t in touched for t in (tab.tracks or [])):
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
            try:
                result["tracks"] = self._scan(album)
            except OSError:
                result["tracks"] = []

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
            return
        self._up_next_key = key
        if queue:
            first = queue[0]
            artist = f" — {first.artist}" if first.artist else ""
            self.up_next_label.configure(text=f"Up next ({len(queue)}) · {first.title}{artist}")
            if not self.up_next_label.winfo_ismapped():
                # (Above the track list; with no tabs open the list is hidden: just above the status line.)
                place = (dict(before=self.playlist.frame) if self.playlist.frame.winfo_manager()
                         else dict(after=self.status_label))
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
        head = tk.Frame(body, bg=theme.SURFACE)
        head.pack(fill="x", pady=theme.pad(0, theme.GAP_SMALL))
        ttk.Label(head, text="Up next", style="PopTitle.TLabel").pack(side="left")
        ttk.Label(head, text=f"{len(queue)} song{'' if len(queue) == 1 else 's'}", style="PopMuted.TLabel").pack(
            side="left", padx=theme.pad(theme.GAP_MEDIUM, 0))
        # (0.22.0) Clear is a flat pill like Settings' buttons (it was a plain word).
        controls.PillButton(head, "Clear", lambda: (self.player.clear_queue(), self._paint_up_next()),
                            fill="SURFACE_HI", fg="TEXT", hover_fill="SURFACE_DOWN", bg="SURFACE",
                            font=theme.font(theme.SIZE_BODY), pad_x=theme.GAP_MEDIUM, pad_y=theme.GAP_HAIR,
                            label="Take every song out of Up next").pack(side="right")
        for position, track in enumerate(queue[:UP_NEXT_SHOWN]):
            label = f"{track.title} — {track.artist}" if track.artist else track.title
            row = MenuRow(body, label if len(label) <= 44 else label[:43] + "…",
                          lambda p=position: self.player.play_queued(p), "×")
            row.pack(fill="x")
            row.keys.configure(cursor="hand2", font=theme.font(theme.SIZE_LARGE))
            row.keys.bind("<ButtonRelease-1>", lambda e, p=position: self._unqueue(p))
            Tooltip(row.keys, "Take it out of Up next")
        if len(queue) > UP_NEXT_SHOWN:
            ttk.Label(body, text=f"and {len(queue) - UP_NEXT_SHOWN} more", style="PopMuted.TLabel").pack(
                anchor="w", padx=theme.px(theme.GAP_MEDIUM), pady=theme.pad(theme.GAP_HAIR, 0))
        if self.up_next_pop.is_open:  # re-seat it: the list's height changed
            self.up_next_pop.place_configure(y=self._up_next_top())
            self.up_next_pop.wire_rows()

    def _unqueue(self, position: int):
        self.player.remove_from_queue(position)
        self._paint_up_next()
        return "break"

    def _change_library_folder(self):
        folder = filedialog.askdirectory(title="Choose your music library folder",
                                         initialdir=str(self._library_root()), mustexist=True)
        if folder:
            self._set_library_folder(Path(folder))

    def _set_library_folder(self, path: Path):
        self.prefs["library_folder"] = str(path)
        self._update_library_text()
        self._update_new_albums()
        if self.watcher.running:
            self.watcher.look_now()

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
        self.watcher.start(delay_s)

    def _toggle_watch(self):
        self.prefs["watch_music"] = self.watch_music.get()
        if self.watch_music.get():
            self._start_watching()
        else:
            self.watcher.stop()

    def _update_new_albums(self):
        state = self._new_music_state()
        since = state.get("since")
        if not isinstance(since, (int, float)):
            self._new_albums = []
            return
        self._new_albums = watcher.new_albums(self._index(), since, state["opened"])

    def _watch_reports(self):
        """Take in what the watcher found (called from _poll, on the Tk thread)."""
        reports = self.watcher.drain()
        if not reports:
            return
        state = self._new_music_state()
        index = self._index()
        if isinstance(state.get("since"), (int, float)) and index.entries:
            state["opened"] = watcher.tidy_opened(state["opened"], index, state["since"])
        self._update_new_albums()
        changed = False
        for report in reports:
            changed = changed or not report.diff.nothing
            text = watcher.note(report, len(watcher.arrived(report, self._new_albums)))
            if text:
                self._set_notice(text, NEW_MUSIC_NOTE_MS)
        if changed and self._library is not None and self._library.win.winfo_exists():
            self._library.reload()

    def _mark_opened(self, folder: Path):
        """An album was opened (a tab, added to one, queued, or one of its songs played): it's
        no longer new."""
        folder = Path(folder)
        if folder not in self._new_albums:
            return
        self._new_music_state()["opened"][str(folder)] = time.time()
        self._new_albums = [f for f in self._new_albums if f != folder]
        if self._library is not None and self._library.win.winfo_exists():
            self._library.reload()

    def _status_clicked(self, _event=None):
        """The status line: "Closed …" reopens the tab; "New music: …" shows Recently added."""
        if self._notice.startswith("Closed"):
            self.reopen_tab()
        elif self._notice.startswith("New music"):
            self.prefs["library_view"] = "recent"
            self.open_library()

    def _update_library_text(self):
        root = str(self._library_root())
        self.library_text.configure(text=root if len(root) <= 30 else "…" + root[-29:])

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
        if self.mini_mode:   # (0.25.0) the strip's own gear: Settings opens under the strip
            self._open_mini_settings()
            return
        self.settings_pop.toggle(self.bar.settings_btn, self.bar.height)

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
        self.settings_pop.toggle(self.mini.settings_button, strip)

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
        step = VOLUME_STEP if event.delta > 0 else -VOLUME_STEP
        self.player.set_volume(self.player.volume + step)
        self.volume_var.set(self.player.volume * 100)
        self._set_notice(f"Volume {self.player.volume:.0%}", 1500)

    def _update_volume_text(self):
        gain = self.player.volume ** 2
        if gain >= 1.0:
            text = "100% · untouched"
        elif gain <= 0:
            text = "muted"
        else:
            text = f"{self.player.volume:.0%} · {20 * math.log10(gain):+.1f} dB"
        self.volume_text.configure(text=text)

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
        if self.exclusive.get():
            guard = (" The clipping guard comes first: a track that would clip the converter is turned down "
                     "just enough, so that one track isn't bit-perfect." if self.clip_guard.get() else "")
            return ("Bit-perfect is locked on: the device gets the file's exact samples, and other apps "
                    f"go quiet while this plays.{guard} Click to turn it off.")
        return "Bit-perfect is off: Windows mixes the sound and may resample it. Click to lock it on."

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
            self._now_playing = NowPlayingWindow(
                self.root, self.player, self.prefs["now_playing_geometry"],
                on_moved=lambda geometry: self.prefs.update(now_playing_geometry=geometry),
                on_meters=self.show_meters, on_pcm=self.show_pcm, on_clipping=self.show_clipping,
                on_spectrogram=self.show_spectrogram,
                on_scope=self.show_scope, on_compare=self.show_compare, on_abx=self.show_abx,
                on_testsignals=self.show_testsignals,
                prefs=self.prefs)

    def show_meters(self):
        """VU, PPM, RMS and true-peak meters (one window at a time)."""
        if self._meters is not None and self._meters.win.winfo_exists():
            self._meters.lift()
        else:
            self._meters = MeterWindow(self.root, self.player, self.prefs)

    def show_pcm(self):
        """The sample values being played, in decimal, hex or binary (one window at a time)."""
        if self._pcm is not None and self._pcm.win.winfo_exists():
            self._pcm.lift()
        else:
            self._pcm = PcmWindow(self.root, self.player, self.prefs,
                                  analysis_for=NowPlayingWindow.cached_analysis)

    def show_clipping(self):
        """(0.24.0) Inter-sample overs: live, the whole song, and (Experimental) hear them."""
        if self._clip is not None and self._clip.win.winfo_exists():
            self._clip.lift()
        else:
            self._clip = ClipWindow(self.root, self.player, self.prefs)

    def show_spectrogram(self):
        """A picture of the sound, live or the whole track (one window at a time)."""
        if self._spectrogram is not None and self._spectrogram.win.winfo_exists():
            self._spectrogram.lift()
        else:
            self._spectrogram = SpectrogramWindow(self.root, self.player, self.prefs,
                                                  analysis_for=NowPlayingWindow.cached_analysis)

    def show_scope(self):
        """The stereo scope: vectorscope, correlation, balance, DC offset (one window at a time)."""
        if self._scope is not None and self._scope.win.winfo_exists():
            self._scope.lift()
        else:
            self._scope = ScopeWindow(self.root, self.player, self.prefs)

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
            self._compare = CompareWindow(self.root, self.prefs, path_a=self._playing_file()[0])

    def show_abx(self):
        """The ABX blind test (one window at a time); A starts as the song playing."""
        if self._abx is not None and self._abx.win.winfo_exists():
            self._abx.lift()
        else:
            path, position = self._playing_file()
            self._abx = AbxWindow(self.root, self.player, self.prefs, path_a=path, start_s=position)

    def show_testsignals(self):
        """Hardware test: test signals, THD+N, J-test, frequency response (one window at a time)."""
        if self._testsignals is not None and self._testsignals.win.winfo_exists():
            self._testsignals.lift()
        else:
            self._testsignals = TestSignalsWindow(self.root, self.player, self.prefs)

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
        status = status or self.player.status()
        if self.playing_tab is None or status.track is None:
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
                extras += [str(t.path) for t in songs]
        return tracks, extras

    def _finish_drop(self, tab: Tab | None, result: dict):
        if "plan" not in result or (tab is not None and tab.loading):
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

    def _open_dropped(self, plan: dragdrop.DropPlan) -> str:
        """A drop on the window: new tabs. Only the first may start playing, and only when
        nothing is playing or paused (as for any new tab)."""
        actions = dragdrop.actions_for(plan, on_tab=False)
        play = self.player.status().state is State.STOPPED
        opened = []
        for action in actions:
            if action.kind == "open_folder":
                self.open_tab("folder", action.path, play=play)
                opened.append(action.path.name)
            elif action.kind == "open_playlist":
                self.open_tab("playlist", action.path, play=play)
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
        """One new tab holding just these music files, in this order (saved with the tab)."""
        tab = Tab("files", str(files[0].parent))
        tab.extras = [str(f) for f in files]
        self.tabs.append(tab)
        self.select_tab(len(self.tabs) - 1, then_play=play)

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
        have = {str(t.path) for t in tab.tracks} if tab.tracks else set()
        new = [t for t in tracks if str(t.path) not in have]
        if not new:
            return f"Already in “{tab.title}”"
        for extra in extras:
            if extra not in tab.extras and Path(extra) != Path(tab.path):
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
            tracks = [tab.tracks[r] for r in rows if not tab.tracks[r].missing]
            self._set_notice(self._add_tracks_to_tab(self.tabs[target[1]], tracks, [str(t.path) for t in tracks]))

    def _reorder_playlist(self, source: int, target: int):
        """Move one song within the playlist on show (dragged up or down); saved straight away."""
        tab = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        if tab is None or tab.kind != "playlist" or not tab.tracks:
            return
        tracks = dragdrop.move_item(tab.tracks, source, target)
        if [t.path for t in tracks] == [t.path for t in tab.tracks]:
            return
        self._save_playlist(Path(tab.path), tracks)
        self.playlist.select(target - 1 if target > source else target)

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

    # -- cover art -------------------------------------------------------------
    def _fit_art(self):
        """Size the cover square to the window: the full width, but never so tall that the
        track list drops below a few rows."""
        width = self.art_box.winfo_width()
        if width < 10 or not self.show_art.get() or self.mini_mode:
            return
        # What everything except the cover and the list needs (their requested heights,
        # plus the gaps between them), so the sum is right even mid-resize.
        parts = (self.bar, self.head_text, self.seek_row, self.transport, self.status_label)
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
        self._art_job = self.root.after(120, lambda: self._request_art(self._art_track, force=True))

    def _toggle_art(self):
        """Show or hide the big cover. Hidden, the track list takes the room."""
        if self.show_art.get():
            self.art_box.pack(fill="x", before=self.head_text)
            self.head_text.pack_configure(pady=theme.pad(theme.GAP_LARGE, 0))
            self.art_size = 0  # size it afresh; _fit_art then fetches the cover
            self.root.after_idle(self._fit_art)
        else:
            self.art_box.pack_forget()
            self.head_text.pack_configure(pady=0)

    def _request_art(self, path: Path | None, force: bool = False):
        """Find and shrink the cover off the UI thread; _poll picks the result up."""
        self._art_track = path
        if not self.show_art.get():
            return  # remembered, so switching the cover back on shows the right one
        key = (path, self.art_size)
        if (key == self._art_key and not force) or not self.art_size:
            return
        self._art_key = key
        if path is None:
            self._show_art(None)
            return
        size = self.art_size

        def work():
            self._art_result = (key, artwork.cover_for(path, size))

        threading.Thread(target=work, name="cover-art", daemon=True).start()

    def _show_art(self, image):
        """Put a cover up with softly rounded corners (0.20.0); no cover: a quiet ♫ placeholder
        the same size, so nothing shifts."""
        size = image.width if image is not None else self.art_size
        if size < 2:
            return
        if image is None:
            image = controls.placeholder(size)
        image = controls.round_corners(image, controls.corner_radius(size), "BG")
        self._art_photo = ImageTk.PhotoImage(image, master=self.root)  # keep a reference or Tk drops it
        self.art_label.configure(image=self._art_photo, text="")

    def _paint_empty_mark(self):
        """The empty window's ♫ tile (a picture: drawn again on a palette change)."""
        size = theme.px(72)
        tile = controls.round_corners(controls.placeholder(size), controls.corner_radius(size), "BG")
        self._empty_photo = ImageTk.PhotoImage(tile, master=self.root)
        self.empty_mark.configure(image=self._empty_photo)

    def _show_empty(self, empty: bool):
        """No tabs open: the empty view in place of the cover, transport and track list (the status
        line stays). A tab opening puts them back exactly as they were packed."""
        if empty == self._empty_shown:
            return
        self._empty_shown = empty
        parts = (self.head, self.seek_row, self.transport, self.playlist.frame)
        if empty:
            self._packed = {w: {k: v for k, v in w.pack_info().items() if k != "in"} for w in parts}
            for w in parts:
                w.pack_forget()
            self.empty_view.pack(fill="both", expand=True, after=self.status_label)
            return
        self.empty_view.pack_forget()
        for w in parts[:3]:   # above the status line, in their old order
            w.pack(**self._packed[w], before=self.status_label)
        after = self.up_next_label if self.up_next_label.winfo_manager() else self.status_label
        self.playlist.frame.pack(**self._packed[self.playlist.frame], after=after)
        self.art_size = 0   # size the cover afresh
        self.root.after_idle(self._fit_art)

    def _palette_changed(self, _palette):
        """After a palette change (theme.use): redraw what keeps its colours — the cover's
        placeholder (a picture), the tab strip, the status line and square."""
        self.bar.set_tabs([], -1, None)   # forget the drawn strip, so the next refresh rebuilds it
        self._refresh_tabs()
        if self._art_track is not None or self.art_size:
            self.art_size = 0
            self._fit_art()
        self._paint_order_buttons()
        self._paint_lock()
        self._paint_empty_mark()

    # -- refresh -------------------------------------------------------------
    def _album_summary(self, tab: Tab) -> str:
        """e.g. "FLAC · 24-bit / 96 kHz · 10 tracks · 41:07" for an album you've flicked to."""
        track = tab.view_track()
        if track is None:
            return "Loading…" if tab.loading else ("Empty playlist" if tab.kind == "playlist" else "")
        if tab.kind == "playlist":
            count = len(tab.tracks)
            return f"Playlist · {count} song{'' if count == 1 else 's'} · {fmt_time(sum(t.duration for t in tab.tracks))}"
        if track.lossy or not track.sample_rate:
            quality = track.codec
        else:
            quality = f"{track.codec} · {track.bits}-bit / {track.sample_rate / 1000:g} kHz"
        album = [t for t in tab.tracks if album_folder(t.path) == album_folder(track.path)]
        total = sum(t.duration for t in album)
        return f"{quality} · {len(album)} tracks · {fmt_time(total)}"

    def _poll(self):
        status = self.player.status()
        track = status.track if self.playing_tab is not None else None
        if self.playing_tab is not None and status.index >= 0:
            playing = self.tabs[self.playing_tab]
            # Noted continuously, so switching tabs leaves it paused right here (and an album
            # that played to the end goes back to the start: the engine reports 0 there).
            playing.resume_at = status.position
            if playing.last_index != status.index:
                playing.last_index = status.index
                self._refresh_tabs()  # a tab of many albums takes the name of the one it's on
        self._apply_guard(track, status.index)
        shown = self.tabs[self.current] if 0 <= self.current < len(self.tabs) else None
        viewing_playing = shown is not None and self.current == self.playing_tab

        # The top of the window follows the tab you're looking at. On the playing tab that's
        # the song; flick to another album and you see that album (press play to start it).
        if viewing_playing and track:
            self._request_art(track.path)
            self.title_label.configure(text=track.title)
            self.sub_label.configure(text=track.display_artist_album or track.path.parent.name)
            self.format_label.configure(text=track.format_line)
            text, tone = output_line(status.output, status.gain, track.float_samples, track.lossy, status.trim)
            # The proof (see proof.py) backs up — or contradicts — what the line above claims.
            proof = status.proof
            if proof is not None and proof.mismatch_at is not None:
                text, tone = f"{text} · ✗ the audio sent differed from the file at {fmt_time(proof.mismatch_at)}", "warn"
            elif tone == "good" and proof is not None and proof.complete and proof.untouched:
                text += f" · proven sample for sample ({fmt_time(proof.checked)} checked)"
            if status.dropouts:
                text, tone = f"{text} · {status.dropouts} dropout(s) this track: see ⓘ", "warn"
            showing_playing = True
            elsewhere = ""
        elif shown is not None:
            view = shown.view_track()
            self._request_art(view.path if view else shown.first_file)
            if view is not None and shown.last_index >= 0 and shown.resume_at >= 1:
                # Left paused part-way through a song: show where, ready to carry on.
                self.title_label.configure(text=view.title)
                self.sub_label.configure(text=view.display_artist_album or view.path.parent.name)
                self.format_label.configure(
                    text=f"Paused at {fmt_time(shown.resume_at)} of {fmt_time(view.duration)} · press play to carry on")
            else:
                self.title_label.configure(text=shown.title)
                artist = (view.artist if view else shown.artist) or ""
                self.sub_label.configure(text=artist or "Press play to start this album")
                self.format_label.configure(text=self._album_summary(shown))
            text, tone = "", "plain"
            showing_playing = False
            elsewhere = (f"♪ Playing in another tab: {track.title}" + (f" — {track.album}" if track.album else "")
                         if track else "")
        else:
            self._request_art(None)
            self.title_label.configure(text="Nothing playing")
            self.sub_label.configure(text="Open your library: Ctrl+L")
            self.format_label.configure(text="")
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

        # The status square: the colour says it all; hovering gives the detail.
        if text:
            colour = {"good": theme.GOOD, "warn": theme.WARN}.get(tone, theme.MUTED)
            verdict = {"good": "All good", "warn": "Heads up"}.get(tone, "Fine")
            self._status_detail = f"{verdict}: {text.removeprefix('→ ')}"
        else:
            colour, self._status_detail = theme.BG, ""
        self.status_square.configure(bg=colour)
        playing_now = showing_playing and status.state is State.PLAYING
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
        if not self.seek.dragging:   # while dragged, the knob and the times follow the pointer
            self.seek.set(position, duration)
            self._show_times(position, duration)
        self.status_label.configure(text=self._notice or status.message or elsewhere)
        # The mini strip shows the same as the top of the full window.
        self._mini_state = dict(title=self.title_label.cget("text"), artist=self.sub_label.cget("text"),
                                position=position, duration=duration, playing=playing_now,
                                status_colour=colour, status_detail=self._status_detail, notice=self._notice)
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

        key = (self.current, status.index if self.current == self.playing_tab else None)
        if key != self._shown_key:
            if key[1] is not None and key[1] >= 0:
                self.playlist.show_current(key[1])
            else:
                self.playlist.clear_current()
            self._shown_key = key

        self._paint_up_next()
        self._polls += 1
        if self._polls % AUTOSAVE_EVERY == 0:
            self._save_if_changed()
            if self._tag_waiting:
                self._retry_tag_saves()
        if self._index_updated:
            self._index_updated = False
            if self._library is not None and self._library.win.winfo_exists():
                self._library.reload()
        self._watch_reports()
        self._poll_job = self.root.after(POLL_MS, self._poll)

    def _save_if_changed(self):
        """Settings are written as they change, not only on close — so a crash, Windows
        restarting or Task Manager can't lose your window position or choices."""
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
        )
        self.prefs["mini_on_top" if self.mini_mode else "always_on_top"] = self.on_top.get()
        # (0.25.0) While Settings has grown the mini strip's window, the strip's own place is kept.
        geometry = self._mini_settings_from or placement.current(self.root)
        if geometry:  # not while minimised or maximised; the mini strip keeps its own
            self.prefs["mini_geometry" if self.mini_mode else "geometry"] = geometry
        snapshot = json.dumps(self.prefs, sort_keys=True)
        if snapshot != self._saved_snapshot:
            settings.save(self.prefs)
            self._saved_snapshot = snapshot

    def quit(self):
        self._save_if_changed()
        self.root.after_cancel(self._poll_job)
        self.root.after_cancel(self._media_job)
        self.root.after_cancel(self._theme_job)
        self._stop_media()
        self.watcher.stop()
        self.player.close()
        for save in self._tag_waiting:  # the player has let go of its files: a last quick try
            save.retry()
            save.abandon()
        self.root.destroy()

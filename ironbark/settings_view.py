"""Settings as a view (0.39.0): the full width of the player's window, in place of the column —
cover, seek bar, transport, track list, status line — under the tab strip. Two pages: **Simple**
(what shows first: the sound output, bit-perfect, volume, theme, album art, keep on top, the library
folders and Local network access) and **Advanced** (the clipping guard and its headroom, volume
levelling, media keys, the home position, the whole-library check, Network playback, Experimental,
Help). At the bottom, outside the scrolling part: Advanced settings… (or Back) on the left and Done
on the right. Done, Esc, the gear again, Ctrl+, / Ctrl+P and clicking a tab all return to the player.
The music isn't touched by any of it.

The window builds the controls (ui.PlayerWindow._build_settings, into `simple` and `advanced`) and
packs / unpacks the view (`_show_settings_view`); this class is the frame, the pages, the scrolling,
the bottom bar, and the keyboard: while the view is open, Tab and Shift+Tab go round its controls
(keyfocus.cycle), and the keyboard goes back where it was when it closes.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont
from tkinter import ttk

from . import controls, devices, experiments, keyfocus, keys, levelling, platforms, settings, theme
from .analysis import GUARD_HEADROOM_CHOICES, headroom_index, headroom_label
from .tabbar import IconButton, Popover, SettingRow, Tooltip

PAGES = ("simple", "advanced")


class SettingsView:
    def __init__(self, parent: tk.Misc, root: tk.Misc, on_done: Callable[[], None]):
        self.root = root
        self.on_done = on_done
        self.on_hide: Callable[[], None] | None = None   # told when the view closes
        self.page = "simple"
        self.is_open = False
        self._focus_before: tk.Misc | None = None
        self.frame = tk.Frame(parent, bg=theme.SURFACE, takefocus=1, highlightthickness=0)
        # The head: "Settings" and the Mini player button (the window puts it in `head`).
        self.head = ttk.Frame(self.frame, style="Pop.TFrame",
                              padding=theme.pad(theme.GAP_WIDE, theme.GAP_LARGE, theme.GAP_WIDE, 0))
        self.head.pack(fill="x")
        # The pages, in one scrolling area; one of them packed at a time.
        self.area = controls.ScrollArea(self.frame, bg="SURFACE")
        self.area.pack(fill="both", expand=True)
        self.simple = tk.Frame(self.area.inner, bg=theme.SURFACE)
        self.advanced = tk.Frame(self.area.inner, bg=theme.SURFACE)
        self.frame.bind("<Escape>", lambda e: self.close() or "break")
        self.bar = self.done_button = self.page_button = None   # (made by finish, after the window's controls)
        # The X at the top right of the head: back to the player, as Done, Esc and the gear are.
        self.close_button = IconButton(self.head, root, "close", self.close, size=theme.ICON_SMALL, width=28,
                                       bg="SURFACE", hover="SURFACE_HI", fg="MUTED", hover_fg="TEXT",
                                       label="Back to the player (also Esc, Done, or the gear)")
        self.close_button.configure(cursor="hand2")

    def finish(self):
        """The bottom bar, made last so the keyboard's Tab order starts at the top of the page:
        Advanced settings… / Back on the left, Done on the right, outside the scrolling part."""
        self.bar = tk.Frame(self.frame, bg=theme.SURFACE)
        # (before the area in the pack order, or the area's expand would leave the bar no room)
        self.bar.pack(side="bottom", fill="x", padx=theme.px(theme.GAP_WIDE), pady=theme.px(theme.GAP_LARGE),
                      before=self.area)
        tk.Frame(self.frame, bg=theme.BORDER, height=1).pack(side="bottom", fill="x", before=self.area)
        self.done_button = controls.PillButton(self.bar, "Done", self.close, kind="plain", small=True, bg="SURFACE",
                                               label="Back to the player (also Esc, or the gear)")
        self.done_button.pack(side="right")
        self.page_button = controls.PillButton(self.bar, "Advanced settings…", self.toggle_page, kind="plain",
                                               small=True, bg="SURFACE",
                                               label="The rest: clipping guard, levelling, media keys, home, the "
                                                     "library check, network playback, Experimental, Help")
        self.page_button.pack(side="left")
        self._pack_page()

    # -- pages ----------------------------------------------------------------------------------
    def _pack_page(self):
        pad = theme.pad(theme.GAP_WIDE, theme.GAP, theme.GAP_WIDE, theme.GAP_LARGE)
        for name, page in (("simple", self.simple), ("advanced", self.advanced)):
            if name == self.page:
                if not page.winfo_manager():
                    page.pack(fill="both", expand=True, padx=pad[0], pady=(pad[1], pad[3]))
            elif page.winfo_manager():
                page.pack_forget()
        if self.page_button is not None:
            self.page_button.set_text("Back" if self.page == "advanced" else "Advanced settings…")

    def show_page(self, page: str):
        if page not in PAGES or page == self.page:
            return
        self.page = page
        self._pack_page()
        try:
            self.area.canvas.yview_moveto(0.0)
        except tk.TclError:
            pass
        if self.is_open:
            keyfocus.cycle(self.frame, self.area)
            self.frame.focus_set()

    def toggle_page(self):
        self.show_page("advanced" if self.page == "simple" else "simple")

    # -- open and close -----------------------------------------------------------------------------
    def opened(self):
        """The window has packed the view: take the keyboard (Tab goes round the controls)."""
        self.is_open = True
        try:
            self._focus_before = self.root.focus_get()
        except (tk.TclError, KeyError):
            self._focus_before = None
        keyfocus.cycle(self.frame, self.area)
        self.area.bind_wheel()
        self.frame.focus_set()

    def close(self):
        """Done / Esc / the gear: the window puts the player back (`on_done`), then the keyboard goes
        back where it was."""
        if not self.is_open:
            return
        self.is_open = False
        self.on_done()
        before, self._focus_before = self._focus_before, None
        if before is not None:
            try:
                if before.winfo_exists():
                    before.focus_set()
            except (tk.TclError, KeyError):
                pass
        if self.on_hide is not None:
            self.on_hide()

    def natural_height(self) -> int:
        """How tall the view wants to be with the page on show (the mini strip grows the window by it)."""
        self.frame.update_idletasks()
        bar = self.bar.winfo_reqheight() if self.bar is not None else 0
        return (self.head.winfo_reqheight() + self.area.inner.winfo_reqheight() + bar
                + 2 * theme.px(theme.GAP_LARGE) + 1)

    def visible(self, widget: tk.Misc) -> bool:
        return self.area.visible(widget)


# -- the controls: built into the view by the main window's module-level builders below ---------
SETTINGS_WIDTH = 310       # px at 100%: the Settings panel (less in a narrow window)
SETTINGS_CHOICE_W = 120    # px at 100%: Settings' drop-downs beside their names (their left edges line up)

VOLUME_STEP = 0.05   # per mouse-wheel notch over the transport, and per Ctrl+↑ / ↓

# Settings -> Network playback (the network playback cache): (saved value, what the choice says)
NET_MODES = (("intelligent", "Intelligent"), ("fast", "Fast start"), ("complete", "Complete track"))

NET_BUFFERS = ((10, "10 seconds"), (30, "30 seconds"), (60, "1 minute"), (300, "5 minutes"))

NET_PREDOWNLOADS = (("off", "Off"), ("next", "Next track"), ("next2", "Next 2 tracks"), ("queue", "Up to 200 songs ahead"))

NET_KEEPS = (("none", "Don't keep"), ("session", "Until playback finishes"), ("day", "For 1 day"),
             ("limit", "Until the cache is full"))

NET_SIZES = ((1, "1 GB"), (5, "5 GB"), (10, "10 GB"), (25, "25 GB"), (50, "50 GB"), (0, "Unlimited"))

LOCAL_NETWORK_TEXT = ("Lets Ironbark read music on your home network (a NAS, a shared folder, a mapped drive) "
                      "and keep copies here, so songs keep playing if the network drops (Network playback, below). "
                      "Off, nothing on the network is read. Never the internet.")


def caption(parent, text: str, first: bool = False):
    """A section heading; every section after the first gets a faint line above it."""
    if not first:
        tk.Frame(parent, bg=theme.BORDER, height=1).pack(fill="x", pady=theme.pad(theme.GAP_LARGE, 0))
    ttk.Label(parent, text=text.upper(), style="PopCaption.TLabel").pack(
        anchor="w", pady=theme.pad(theme.GAP, theme.GAP_HAIR))


def setting(parent, root: tk.Misc, title: str, description: str, variable: tk.BooleanVar, command=None):
    """An on/off setting: a slider switch on the right, a line underneath saying what it does (wrapped
    at the text column's real edge, which SettingRow follows)."""
    column = SETTINGS_WIDTH - 2 * theme.GAP_WIDE - SettingRow.SWITCH_ROOM
    row = SettingRow(parent, root, title, description, variable, command, guess=theme.px(column))
    row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
    return row


def pop_button(parent, text: str, command, label: str = "") -> controls.PillButton:
    """(0.21.0) A button in a pop-over (Settings): a small plain pill, like the rest of the app's
    buttons instead of a stock ttk button."""
    return controls.PillButton(parent, text, command, kind="plain", small=True, bg="SURFACE", label=label)


def choice_box(parent, label: str, values, *, state: str = "readonly", full: bool = False,
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


def choice(parent, prefs: dict, label: str, choices, key: str, tip: str, on_change=None,
            width: int | None = None) -> ttk.Combobox:
    """A drop-down setting in a row (Settings' style, _choice_box): `choices` are (saved value,
    words); `on_change()` runs after a new choice is saved. `width`: the box's, in real px."""
    box = choice_box(parent, label, [words for _, words in choices], width=width)
    values = [value for value, _ in choices]
    box.current(values.index(prefs[key]) if prefs.get(key) in values else
                values.index(settings.DEFAULTS[key]))

    def chosen(_event=None):
        prefs[key] = values[box.current()]   # (the cache reads it live)
        box.selection_clear()
        if on_change is not None:
            on_change()
    box.bind("<<ComboboxSelected>>", chosen)
    Tooltip(box, tip)
    return box


def from_settings(win, show):
    """A Settings button that opens a window: Settings closes first (the mini strip's grown
    window goes back to the strip)."""
    Popover.close_open()
    win._close_settings()
    show()


def refresh_outputs(win):
    """Re-read the connected devices (runs each time the dropdown opens)."""
    names = devices.output_names()
    chosen = win.player.output_device
    default = devices.default_output_name()
    win._output_choices = [None] + names
    labels = [f"System default ({default})" if default else "System default"] + names
    if chosen is not None and chosen not in names:
        win._output_choices.append(chosen)
        labels.append(f"{chosen} (not connected)")
    win.output_box.configure(values=labels)
    win.output_box.current(win._output_choices.index(chosen))
    show_amp_volume(win)


AMP_VOLUME_TEXT = "The amplifier or receiver it feeds sets the level, so no volume warning in Bit-perfect."


def show_amp_volume(win):
    """Settings → Volume on my amplifier shows the setting for the output in use (it's kept per output)."""
    if getattr(win, "amp_volume", None) is None:
        return   # (not built yet, or no Bit-perfect on this system)
    name = win.player.output_name()
    win.amp_volume.set(bool(name) and name in win.prefs["amp_volume_outputs"])
    if win.amp_volume_row.about is not None:
        where = f" For {name} only." if name else ""
        win.amp_volume_row.about.configure(text=AMP_VOLUME_TEXT + where)


def amp_volume_toggled(win):
    """The switch, for the output in use: saved by name, and the player told. Turned off in
    Bit-perfect on an output with no volume of its own, the usual ear check follows."""
    name = win.player.output_name()
    if not name:
        win.amp_volume.set(False)
        return
    names = [n for n in win.prefs["amp_volume_outputs"] if n != name]
    if win.amp_volume.get():
        names.append(name)
    win.prefs["amp_volume_outputs"] = names
    win.player.set_amp_outputs(names)
    if not win.amp_volume.get() and win.exclusive.get():
        win._protect_ears(win.player.hardware_volume)


def add_help_buttons(win, box: ttk.Frame):
    """The rest of Settings → Help (the Advanced page): what to reach for when something's wrong
    (Save diagnostics…, Open log folder, Detailed logging)."""
    row = ttk.Frame(box, style="Pop.TFrame")
    row.pack(fill="x")
    win.diagnostics_button = pop_button(row, "Save diagnostics…", win.save_diagnostics,
                                               "Save a .zip to send with a problem report: versions, sound "
                                               "outputs, logs and settings. Your folders, tabs, playlists and "
                                               "outputs are given stand-in names, but song and album names in "
                                               "the logs can still show. Nothing is sent anywhere.")
    win.diagnostics_button.pack(side="left")
    pop_button(row, "Open log folder", win._open_log_folder,
                     "Where crash.log and app.log are kept").pack(side="right")
    setting(box, win.root, "Detailed logging", "Notes more of what the player does in its log, to help track "
                  "down a problem. Off normally.", win.detailed_logging, win._detailed_logging_changed)


def build_netplay(win, box: ttk.Frame):
    """Settings -> Network playback: buffering, pre-download, checking, and the cache itself."""
    caption(box, "Network playback")
    # One width for the section's drop-downs (their left edges line up), wide enough for the
    # longest choice: the words, their inset, and the chevron's room.
    font = tkfont.nametofont("TkTextFont", root=win.root)
    longest = max(font.measure(words) for choices in (NET_MODES, NET_BUFFERS, NET_PREDOWNLOADS, NET_KEEPS, NET_SIZES)
                  for _, words in choices)
    width = max(theme.px(SETTINGS_CHOICE_W),
                longest + theme.px(theme.FIELD_INSET + theme.SEARCH_CLEAR_ROOM + theme.GAP_SMALL))
    win.net_mode_box = choice(
        box, win.prefs, "Playback buffering", NET_MODES, "net_mode",
        "Intelligent: measures your network and waits only as long as it needs to. Fast start: starts as "
        "soon as the initial buffer is downloaded. Complete track: downloads and checks the whole song "
        "before it plays, so the network isn't needed once it starts.", width=width)
    win.net_buffer_box = choice(
        box, win.prefs, "Initial buffer", NET_BUFFERS, "net_buffer",
        "Intelligent uses this as its starting point and waits less on a fast, steady network.", width=width)
    win.net_predownload_box = choice(
        box, win.prefs, "Pre-download", NET_PREDOWNLOADS, "net_predownload",
        "How far ahead songs download while one plays, so they follow on without a wait. Songs not "
        "played yet use at most half the cache; on a slow network only the next one is fetched.",
        win._net_settings_changed, width=width)
    win.net_keep_box = choice(
        box, win.prefs, "Keep cached tracks", NET_KEEPS, "net_keep",
        "Don't keep: a copy goes about a minute after it has played. Until playback finishes: kept while "
        "the song is in the list playing or Up next. For 1 day: a day after it last played. Until the "
        "cache is full: the copies played longest ago make room for new ones.", win._net_settings_changed,
        width=width)
    win.net_size_box = choice(
        box, win.prefs, "Maximum cache size", NET_SIZES, "net_cache_gb",
        "The most the copies may take on this computer. Whatever you choose, at least 2 GB (or 5% of the "
        "drive) is always left free.", win._net_settings_changed, width=width)
    win.net_verify = tk.BooleanVar(value=bool(win.prefs.get("net_verify", True)))
    setting(box, win.root, "Check finished downloads", "Makes sure each downloaded copy is whole and plays "
                  "correctly (a FLAC file's own fingerprint is checked too).", win.net_verify,
                  win._net_verify_changed)
    win.net_now_text = ttk.Label(box, text="", style="PopMuted.TLabel", wraplength=theme.px(250),
                                  justify="left")
    win.net_now_text.pack(fill="x", pady=theme.pad(theme.GAP, 0))
    Tooltip(win.net_now_text, "Excellent, Good, Poor or Unstable: how well the network keeps up with the "
            "music. On a poorer network songs wait a little longer before they start, so they don't "
            "pause later")
    row = ttk.Frame(box, style="Pop.TFrame")
    row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
    win.cache_text = ttk.Label(row, text="", style="PopMuted.TLabel")
    win.cache_text.pack(side="left", fill="x", expand=True)
    row = ttk.Frame(box, style="Pop.TFrame")
    row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
    pop_button(row, "Empty the cache", win._empty_cache,
                     "Delete the copies of network songs kept on this computer (not the songs themselves). "
                     "Songs playing now are kept.").pack(side="right")
    pop_button(row, "Open folder", win._open_cache_folder,
                     "Where the copies of network songs are kept").pack(side="right", padx=theme.pad(0, theme.GAP))
    win._paint_cache()


def build_controls(win, view: SettingsView):
    """The controls, on the view's two pages. **Simple** (what shows first): Output, Bit-perfect,
    Volume · Theme, Album art, Keep on top · the library folders, Local network access · Keyboard
    shortcuts, About, the version. **Advanced**: Clipping guard and its headroom, Volume levelling
    and its pre-amp · Media keys, Home · Network playback (while Local network access is on) ·
    Experimental · Help (Save diagnostics…, Open log folder, Detailed logging). Every on/off choice
    is a slider switch with one plain line saying what it does."""
    win.settings_area = view.area
    b, a = view.simple, view.advanced
    row = view.head
    ttk.Label(row, text="Settings", style="PopTitle.TLabel").pack(side="left")
    view.close_button.pack(side="right", padx=theme.pad(theme.GAP, 0))
    win.mini_button = pop_button(row, "Mini player", win.toggle_mini,   # (it closes Settings first)
                                        "Shrink the player to a small strip. Also Z, or double-click the cover")
    win.mini_button.pack(side="right")

    # -- Simple --
    caption(b, "Sound", first=True)
    win._output_choices: list[str | None] = []
    win.output_box = choice_box(b, "", (), full=True)
    win.output_box.configure(postcommand=lambda: refresh_outputs(win))
    win.output_box.bind("<<ComboboxSelected>>", win._change_output)
    Tooltip(win.output_box, "Output: where the sound goes")
    refresh_outputs(win)
    if win.can_exclusive:
        setting(b, win.root, "Bit-perfect", "Sound exactly as recorded. Other apps go quiet.",
                      win.exclusive, win._toggle_exclusive)
        win.amp_volume = tk.BooleanVar(value=False)
        win.amp_volume_row = setting(b, win.root, "Volume on my amplifier", AMP_VOLUME_TEXT,
                                     win.amp_volume, lambda: amp_volume_toggled(win))
        show_amp_volume(win)
    row = ttk.Frame(b, style="Pop.TFrame")
    row.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
    ttk.Label(row, text="Volume", style="Pop.TLabel").pack(side="left", padx=theme.pad(0, theme.GAP_MEDIUM))
    win.volume_text = ttk.Label(row, text="", style="PopMuted.TLabel", width=15, anchor="e")
    win.volume_text.pack(side="right", padx=theme.pad(theme.GAP_MEDIUM, 0))
    # (0.21.0) The drawn slider (controls.Slider), following volume_var however it's changed.
    win.volume_slider = controls.Slider(row, on_release=win._volume_slid, on_drag=win._volume_slid,
                                         bg="SURFACE", step=VOLUME_STEP * 100, allow=win._volume_jump)
    win.volume_slider.pack(side="left", fill="x", expand=True)
    win.volume_slider.set(win.volume_var.get(), 100.0)
    win.volume_var.trace_add("write", lambda *_: win.volume_slider.set(win.volume_var.get(), 100.0))

    caption(b, "Window")
    win.theme_box = choice_box(b, "Theme", [label for _, label in theme.THEME_CHOICES], gap=0)
    win.theme_box.current([key for key, _ in theme.THEME_CHOICES].index(win.prefs["theme"]))
    win.theme_box.bind("<<ComboboxSelected>>", win._theme_chosen)
    setting(b, win.root, "Album art", "Off gives the track list more room.", win.show_art, win._toggle_art)
    setting(b, win.root, "Keep on top", "Stay in front of other windows. The mini player has its own (on at first).",
                  win.on_top, win._toggle_on_top)

    caption(b, "Library")
    win.library_rows = ttk.Frame(b, style="Pop.TFrame")   # one row per library folder
    win.library_rows.pack(fill="x")
    win._paint_library_folders()
    row = ttk.Frame(b, style="Pop.TFrame")
    row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
    pop_button(row, "Add folder…", win._add_library_folder,
                     "Add another folder of music to the Library").pack(side="right")
    setting(b, win.root, "Local network access", LOCAL_NETWORK_TEXT, win.local_network, win._local_network_changed)

    caption(b, "Help")
    win.help_box = ttk.Frame(b, style="Pop.TFrame")
    win.help_box.pack(fill="x")
    row = ttk.Frame(win.help_box, style="Pop.TFrame")
    row.pack(fill="x")
    pop_button(row, f"Keyboard shortcuts ({keys.hint('help')})", lambda: from_settings(win, win.show_help),
                     "Every key, with a filter box. Also F1 or ? in any Ironbark window").pack(side="left")
    pop_button(row, "About Ironbark", lambda: from_settings(win, win.show_about),
                     "Version, licences, privacy and the website").pack(side="right")
    win.version_label = ttk.Label(b, text="", style="PopMuted.TLabel")
    win.version_label.pack(anchor="e", pady=theme.pad(theme.GAP_LARGE, 0))
    win._paint_version()

    # -- Advanced --
    caption(a, "Sound", first=True)
    setting(a, win.root, "Clipping guard", "Lowers only tracks that would crackle, by just enough.",
                  win.clip_guard, win._guard_changed)
    # (0.24.0) "My DAC has headroom": room some converters have above full scale. Off by default.
    win.headroom_box = choice_box(a, "My DAC has headroom",
                                         [headroom_label(v) for v in GUARD_HEADROOM_CHOICES])
    win.headroom_box.current(headroom_index(win.prefs.get("clip_headroom", 0.0)))
    win.headroom_box.bind("<<ComboboxSelected>>", win._headroom_chosen)
    Tooltip(win.headroom_box, "Only if your converter is known to take overs: the clipping guard then lets "
                               "the wave go this far past full scale before it turns a track down. "
                               "Measured converters range from none to about 3.5 dB. Off = none.")
    # Volume levelling: off by default (any change of level means the device no longer gets the
    # file's own samples); the pre-amp goes with it.
    win.levelling_box = choice_box(a, "Volume levelling", [label for _, label in levelling.MODES])
    win.levelling_box.current([key for key, _ in levelling.MODES].index(win._levelling_setting()))
    win.levelling_box.bind("<<ComboboxSelected>>", win._levelling_chosen)
    Tooltip(win.levelling_box, "Plays songs at about the same loudness, from their ReplayGain tags (or "
                                "Ironbark measures them). Track: each song on its own. Album: an album's "
                                "songs keep their own balance. Smart: Album while an album plays in order, "
                                "else Track. Not bit-perfect while it's on.")
    win.preamp_box = choice_box(a, "Levelling pre-amp",
                                       [levelling.preamp_label(v) for v in levelling.PREAMP_CHOICES],
                                       state="disabled" if win._levelling_setting() == "off" else "readonly")
    win.preamp_box.current(levelling.PREAMP_CHOICES.index(win._preamp_db()))
    win.preamp_box.bind("<<ComboboxSelected>>", win._preamp_chosen)
    Tooltip(win.preamp_box, "Added to every levelling gain. A song is only turned up as far as its "
                             "peak allows (it stays under −0.1 dB true peak).")

    caption(a, "Window")
    if platforms.MEDIA_CONTROLS:   # (media keys reach the player on Windows only)
        setting(a, win.root, "Media keys", "Play/pause, next, previous keys and Windows' media panel.",
                      win.media_controls, win._toggle_media)
    row = ttk.Frame(a, style="Pop.TFrame")
    row.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
    # The buttons take their room first, so a narrow window shortens the text instead of
    # drawing it over them.
    win.snap_button = pop_button(row, "Snap to home", lambda: (win._close_settings(), win.snap_home()),
                                        "Also Ctrl+Shift+H, or double-click the empty strip at the top")
    win.snap_button.pack(side="right")
    pop_button(row, "Use current", win.make_home, "Make the window's current spot and size its home").pack(
        side="right", padx=theme.pad(0, theme.GAP))
    win.home_text = ttk.Label(row, text="", style="Pop.TLabel")
    win.home_text.pack(side="left", fill="x", expand=True)
    setting(a, win.root, "Open at home", "Start up at the home position.", win.open_at_home)
    win._update_home_text()

    # Network playback (only while Local network access is on): how songs on network drives are
    # fetched into the copy on this computer they play from.
    win.netplay_box = ttk.Frame(a, style="Pop.TFrame")
    win._netplay_after = None
    build_netplay(win, win.netplay_box)
    if win.local_network.get():
        win.netplay_box.pack(fill="x")

    if experiments.EXPERIMENTS:
        caption(a, "Experimental")
    else:  # one line, to keep the page short: the switches appear when there's something to try
        tk.Frame(a, bg=theme.BORDER, height=1).pack(fill="x", pady=theme.pad(theme.GAP_LARGE, 0))
        row = ttk.Frame(a, style="Pop.TFrame")
        row.pack(fill="x", pady=theme.pad(theme.GAP, 0))
        ttk.Label(row, text="EXPERIMENTAL", style="PopCaption.TLabel").pack(side="left")
        note = ttk.Label(row, text="none right now", style="PopMuted.TLabel")
        note.pack(side="left", padx=theme.pad(theme.GAP_MEDIUM, 0))
        Tooltip(note, "New features appear here first, off until you switch them on")
    for experiment in experiments.EXPERIMENTS:
        var = tk.BooleanVar(value=experiments.enabled(win.prefs, experiment.key))
        win.experiment_vars[experiment.key] = var
        setting(a, win.root, experiment.title, experiment.description, var,
                      lambda key=experiment.key: win._toggle_experiment(key))
    caption(a, "Help")
    win.help_more = ttk.Frame(a, style="Pop.TFrame")
    win.help_more.pack(fill="x")
    add_help_buttons(win, win.help_more)

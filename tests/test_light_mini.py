"""0.19.0: the Light theme, WCAG AA contrast for both palettes,
and mini mode — switching keeps the window's places and never touches the music."""

from __future__ import annotations

import gc
import sys
import time
import types

import numpy as np
import pytest
import soundfile as sf

from ironbark import settings, theme
from ironbark.engine import Player, State, Status
from ironbark.library import Track
from ironbark.mini_view import click_or_double


# -- contrast ---------------------------------------------------------------------------------
def test_contrast_formula_matches_wcag():
    """The W3C's definition: black on white is 21:1, a colour on itself 1:1, order doesn't matter.
    #777777 on white works out at 4.478:1 (fails AA — the threshold is never rounded) and #767676
    at 4.54:1 (passes)."""
    assert theme.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert theme.contrast_ratio("#3f9a6b", "#3f9a6b") == pytest.approx(1.0)
    assert theme.contrast_ratio("#ffffff", "#000000") == theme.contrast_ratio("#000000", "#ffffff")
    assert theme.relative_luminance("#ffffff") == pytest.approx(1.0)
    assert theme.relative_luminance("#000000") == 0.0
    assert theme.contrast_ratio("#777777", "#ffffff") == pytest.approx(4.478, abs=0.001)
    assert theme.contrast_ratio("#777777", "#ffffff") < theme.AA_TEXT <= theme.contrast_ratio("#767676", "#ffffff")
    # The linear part below 0.04045 (channel value 10 / 255 = 0.0392): c / 12.92.
    assert theme.relative_luminance("#0a0a0a") == pytest.approx((10 / 255) / 12.92)


@pytest.mark.parametrize("palette", [theme.DARK, theme.LIGHT], ids=["dark", "light"])
def test_every_text_and_icon_pair_passes_wcag_aa(palette):
    """Text 4.5:1 (SC 1.4.3), icons and meaningful marks 3:1 (SC 1.4.11), in both themes."""
    failures = []
    for pairs, need in ((theme.TEXT_PAIRS, theme.AA_TEXT), (theme.GRAPHIC_PAIRS, theme.AA_GRAPHICS)):
        for fg, bg in pairs:
            ratio = theme.contrast_ratio(getattr(palette, fg), getattr(palette, bg))
            if ratio < need:
                failures.append(f"{fg} on {bg}: {ratio:.2f} (needs {need})")
    assert failures == []


def test_pairs_name_real_roles_and_cover_every_text_colour():
    for fg, bg in theme.TEXT_PAIRS + theme.GRAPHIC_PAIRS:
        assert fg in theme.ROLES and bg in theme.ROLES
    text_colours = {fg for fg, _ in theme.TEXT_PAIRS}
    assert {"TEXT", "MUTED", "ACCENT_TEXT", "ON_ACCENT", "GOOD", "WARN", "DANGER"} <= text_colours


def test_the_rounded_looks_pairs_are_checked():
    for pair in (("TEXT", "STRIP_HI"), ("MUTED", "STRIP_HI"), ("TEXT", "HOVER"), ("MUTED", "HOVER")):
        assert pair in theme.TEXT_PAIRS
    for pair in (("ACCENT_TEXT", "STRIP"), ("ACCENT_TEXT", "STRIP_HI"), ("ACCENT_TEXT", "HOVER"),
                 ("THUMB", "BG"), ("THUMB", "SURFACE")):
        assert pair in theme.GRAPHIC_PAIRS
    assert ("ACCENT_TEXT", "SURFACE_HI") not in theme.GRAPHIC_PAIRS     # the menu tick moved to HOVER


def test_dg_green_is_never_drawn_on_the_hover_greys():
    """In Light, DG green on STRIP_HI or HOVER is under 3:1, so what sits on them in green — the
    playing tab's mark, the lit gear, a menu tick, an "on" glyph — is ACCENT_TEXT instead."""
    for background in ("STRIP_HI", "HOVER"):
        assert theme.contrast_ratio(theme.LIGHT.ACCENT, getattr(theme.LIGHT, background)) < theme.AA_GRAPHICS
        assert ("ACCENT", background) not in theme.GRAPHIC_PAIRS
    import inspect

    from ironbark import tabbar

    assert '"ACCENT_TEXT" if self.active' in inspect.getsource(tabbar.IconButton._paint)        # lit gear
    assert 'level_mark_photo(self, "ACCENT_TEXT")' in inspect.getsource(tabbar.TabBar._paint_slot)
    assert '"ACCENT_TEXT" if on' in inspect.getsource(tabbar.RoundIconButton.set_on)


@pytest.mark.parametrize("palette", [theme.DARK, theme.LIGHT], ids=["dark", "light"])
def test_each_palette_has_every_role_once(palette):
    values = [v.lower() for v in palette.roles().values()]
    assert set(palette.roles()) == set(theme.ROLES) and len(values) == len(set(values))


def test_light_palette_keeps_dg_green():
    assert theme.LIGHT.ACCENT == theme.DARK.ACCENT == "#3f9a6b"
    assert not theme.LIGHT.dark and theme.DARK.dark
    assert theme.relative_luminance(theme.LIGHT.BG) > 0.8 > 0.05 > theme.relative_luminance(theme.DARK.BG)
    assert theme.LIGHT.METER_FACE == theme.DARK.METER_FACE   # the VU face looks the same in both


# -- which palette ----------------------------------------------------------------------------
def test_dark_is_the_default():
    """Dark is the default theme."""
    assert theme.DEFAULT_THEME == "dark" and settings.DEFAULTS["theme"] == "dark"
    assert [key for key, _ in theme.THEME_CHOICES] == ["dark", "light"]


def test_palette_for_each_choice():
    assert theme.palette_for("dark") is theme.DARK
    assert theme.palette_for("light") is theme.LIGHT
    assert theme.palette_for("system") is theme.DARK                 # (no longer a choice: Dark, like anything unknown)
    assert theme.palette_for("nonsense") is theme.DARK


def test_a_saved_follow_windows_is_settled_once(monkeypatch):
    """0.38.0: nothing polls Windows; a "system" choice from before becomes Light or Dark once."""
    monkeypatch.setattr(theme, "windows_uses_light", lambda: True)
    assert theme.settled_choice("system") == "light"
    monkeypatch.setattr(theme, "windows_uses_light", lambda: None)
    assert theme.settled_choice("system") == "dark" and theme.settled_choice("light") == "light"


def _fake_winreg(value=None, missing=False):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    seen = {}

    def open_key(hive, path):
        seen["path"] = path
        if missing:
            raise FileNotFoundError(path)
        return Key()

    def query(_key, name):
        seen["name"] = name
        return value, 4

    return types.SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=open_key, QueryValueEx=query), seen


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_windows_setting_is_read_from_the_personalize_key(monkeypatch):
    fake, seen = _fake_winreg(1)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert theme.windows_uses_light() is True
    assert seen == {"path": r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                    "name": "AppsUseLightTheme"}
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(0)[0])
    assert theme.windows_uses_light() is False
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(missing=True)[0])
    assert theme.windows_uses_light() is None


# -- click vs double-click on the cover -------------------------------------------------------
class _FakeWidget:
    def __init__(self):
        self.bindings, self.jobs, self.next_id = {}, {}, 0

    def bind(self, event, handler):
        self.bindings[event] = handler

    def after(self, ms, fn):
        self.next_id += 1
        self.jobs[self.next_id] = fn
        return self.next_id

    def after_cancel(self, job):
        self.jobs.pop(job, None)

    def run_jobs(self):
        for job in list(self.jobs):
            self.jobs.pop(job)()


def test_a_double_click_on_the_cover_never_also_counts_as_a_click():
    widget, done = _FakeWidget(), []
    click_or_double(widget, lambda: done.append("details"), lambda: done.append("mini"))
    widget.bindings["<Button-1>"](None)        # one click: waits to see if a second follows
    assert done == [] and widget.jobs
    widget.run_jobs()
    assert done == ["details"]
    widget.bindings["<Button-1>"](None)        # a double click: Tk sends Button-1, then Double-Button-1
    widget.bindings["<Double-Button-1>"](None)
    widget.run_jobs()
    assert done == ["details", "mini"]


# -- the real window (Tk smoke tests: skipped without a display) ------------------------------
class FakePlayer(Player):
    """A real Player that never opens a device: status() reports a song playing at 0:42, and
    every call that would change playback is only noted."""

    def __init__(self, track: Track):
        super().__init__()
        self.track = track
        self.calls: list[str] = []
        self.set_tracks([track])

    def status(self) -> Status:
        return Status(State.PLAYING, 0, self.track, 42.0, self.track.duration)


def _noting(name):
    def method(self, *args, **kwargs):
        self.calls.append(name)
    return method


for _name in ("play", "pause", "toggle_pause", "stop", "seek", "suspend", "next", "previous"):
    setattr(FakePlayer, _name, _noting(_name))


@pytest.fixture
def make_window(tmp_path, monkeypatch):
    """Build the real PlayerWindow on temp settings (nothing of the user's is read or written),
    with a song 'playing' from a fake player. Media keys, the new-music watcher, the clipping
    guard and bit-perfect are off; the library folder is an empty temp folder."""
    tk = pytest.importorskip("tkinter")
    from ironbark.ui import PlayerWindow, Tab

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "settings" / "settings.json")
    music = tmp_path / "Music" / "Album"
    music.mkdir(parents=True)
    song = music / "01 Song.flac"
    sf.write(song, np.zeros((44100, 2), dtype=np.int16), 44100, subtype="PCM_16")
    made = []
    scale_before = theme._scale   # (theme.px's scaling: put back at the end, like Tk's own)

    def make(*, scaling: float | None = None, **prefs):
        """scaling: lay the window out as at that display scaling (1.0 = 100%, 1.5 = 150%)."""
        base = dict(media_controls=False, clip_guard=False, exclusive=False,
                    library_folder=str(tmp_path / "Music"), geometry="420x640+120+120", always_on_top=False)
        settings.save({**settings.DEFAULTS, **base, **prefs})
        try:
            root = tk.Tk()
        except tk.TclError as error:
            pytest.skip(f"no display: {error}")
        native = root.tk.call("tk", "scaling")
        if scaling:
            root.tk.call("tk", "scaling", scaling * 96 / 72)
        theme._scale = theme.scale(root)   # this root's (an earlier test's root may have had another)
        errors = []
        root.report_callback_exception = lambda *info: errors.append(info)
        track = Track(path=song, title="Song One", artist="Artist", album="Album", duration=200.0,
                      sample_rate=44100, bits=16, channels=2)
        player = FakePlayer(track)
        window = PlayerWindow(root, player)
        window.tabs = [Tab("album", str(music), tracks=[track])]
        window.current = window.playing_tab = 0
        window._refresh_tabs()
        made.append((window, root, native))
        _pump(root, 0.6)
        return window, root, player, errors

    yield make
    for window, root, native in made:
        try:
            # Tk's next root starts from the last scaling set in the process (its default fonts too):
            # put it back, or every later test would be laid out as at this one's.
            root.tk.call("tk", "scaling", native)
            window.quit()
        except tk.TclError:
            pass
    theme.use(theme.DARK)
    # theme.px goes by the scaling of the last window styled: a window laid out as at 150% would
    # otherwise leave every later test's sizes half as big again (a tab strip on a plain root, say).
    theme._scale = scale_before
    del made[:]
    gc.collect()   # on this thread (Tk objects freed on another thread abort Python)


def _pump(root, seconds: float):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def _colours_in_use(root) -> set[str]:
    """Every colour a plain Tk widget option or canvas item is showing, under `root`."""
    import tkinter as tk

    found, stack = set(), [root]
    while stack:
        w = stack.pop()
        try:
            stack.extend(w.winfo_children())
            options = w.configure() or {}
        except tk.TclError:
            continue
        for option in theme.COLOUR_OPTIONS:
            if option in options:
                found.add(str(w.cget(option)).lower())
        if isinstance(w, tk.Canvas):
            for item in w.find_all():
                item_options = w.itemconfigure(item)
                for option in theme.CANVAS_COLOUR_OPTIONS:
                    if option in item_options:
                        found.add(str(w.itemcget(item, option)).lower())
    return found


def _only_in(palette, other) -> set[str]:
    return {v.lower() for v in palette.roles().values()} - {v.lower() for v in other.roles().values()}


def test_theme_switch_recolours_every_window(make_window):
    """Open every window, switch Dark → Light → Dark from Settings: no errors, and nothing left
    showing the other palette's colours."""
    window, root, player, errors = make_window()
    assert theme.current is theme.DARK
    window._open_settings()
    for show in (window.show_details, window.show_meters, window.show_pcm, window.show_spectrogram,
                 window.show_scope, window.show_compare, window.show_abx, window.show_testsignals,
                 window.open_library):
        show()
    window.toggle_mini()   # the mini strip too (it's hidden, but must follow)
    window.toggle_mini()
    _pump(root, 0.8)

    window.theme_box.current(1)                     # Light
    window._theme_chosen()
    _pump(root, 0.8)
    assert theme.current is theme.LIGHT and window.prefs["theme"] == "light"
    left = _colours_in_use(root) & _only_in(theme.DARK, theme.LIGHT)
    assert left == set(), f"still dark: {left}"

    window.theme_box.current(0)                     # back to Dark
    window._theme_chosen()
    _pump(root, 0.8)
    assert theme.current is theme.DARK
    left = _colours_in_use(root) & _only_in(theme.LIGHT, theme.DARK)
    assert left == set(), f"still light: {left}"
    assert errors == []
    assert player.calls == []          # switching themes never touches the music


def test_light_from_settings_on_start_and_a_saved_follow_windows_settles(make_window, monkeypatch):
    window, root, _player, errors = make_window(theme="light")
    assert theme.current is theme.LIGHT and str(root.cget("bg")).lower() == theme.LIGHT.BG
    window.quit()
    monkeypatch.setattr(theme, "windows_uses_light", lambda: True)
    window, root, _player, errors = make_window(theme="system")      # (a settings file from before 0.38.0)
    assert window.prefs["theme"] == "light" and theme.current is theme.LIGHT
    assert [*window.theme_box.cget("values")] == ["Dark", "Light"]
    assert errors == []


def _where(root) -> tuple[int, int, int, int]:
    from ironbark import placement

    root.update_idletasks()
    return placement.parse(root.geometry())


# A full window whose place is checked: one that fits a 1024 x 768 screen's usable area at its place
# (macOS moves a window that would run off it: 420x640 at +120+120 opened at +120+40 there).
FITS_A_SMALL_SCREEN = "420x520+120+120"


def test_mini_and_full_keep_their_own_places_and_the_music_plays_on(make_window):
    window, root, player, errors = make_window(geometry=FITS_A_SMALL_SCREEN, mini_geometry="640x140+300+60")
    full_before = _where(root)
    assert full_before[2:] == (120, 120)
    found = window.keys.resolve(root, "z", 0)        # key Z is bound (through the keyboard table, keys.py)
    assert found is not None and found[0].id == "mini"

    window.toggle_mini()
    _pump(root, 0.5)
    assert window.mini_mode and window.mini.winfo_ismapped() and not window.bar.winfo_ismapped()
    w, h, x, y = _where(root)
    assert (x, y) == (300, 60) and abs(w - 640) <= 4 and abs(h - 140) <= 4
    assert window.mini.title_label.cget("text") == "Song One"
    assert window.mini.sub_label.cget("text").startswith("Artist")
    assert window.mini.seek.value == 42.0 and window.mini.seek.maximum == 200.0

    window.toggle_mini()
    _pump(root, 0.5)
    assert not window.mini_mode and window.bar.winfo_ismapped() and not window.mini.winfo_ismapped()
    w, h, x, y = _where(root)
    assert (x, y) == full_before[2:] and abs(w - full_before[0]) <= 4 and abs(h - full_before[1]) <= 4
    assert window.prefs["mini_geometry"].endswith("+300+60")

    window._save_if_changed()                        # the two places are saved separately
    saved = settings.load()
    assert saved["geometry"].endswith("+120+120") and saved["mini_geometry"].endswith("+300+60")
    assert player.calls == [] and player.status().state is State.PLAYING   # the music never noticed
    assert errors == []


def test_keep_on_top_survives_the_switch_and_the_mini_strip_keeps_its_place(make_window):
    window, root, player, errors = make_window(geometry=FITS_A_SMALL_SCREEN, always_on_top=True, mini_mode=True,
                                               mini_geometry="600x130+200+90")
    assert not window.mini_mode and window.bar.winfo_ismapped()      # always the full window at the start
    assert _where(root)[2:] == (120, 120)
    assert bool(root.attributes("-topmost"))
    window.toggle_mini()
    assert bool(root.attributes("-topmost")) and _where(root)[2:] == (200, 90)   # the strip's own last place
    window.toggle_mini()
    assert bool(root.attributes("-topmost")) and _where(root)[2:] == (120, 120)
    window.mini.show(title="t", artist="a", position=1.0, duration=2.0, playing=False,
                     status_colour=theme.BG, status_detail="")
    assert errors == [] and player.calls == []


def test_first_mini_strip_opens_at_the_full_windows_corner(make_window):
    window, root, _player, errors = make_window(geometry=FITS_A_SMALL_SCREEN)
    window.toggle_mini()
    w, h, x, y = _where(root)
    assert (x, y) == (120, 120) and abs(w - theme.px(380)) <= 4
    assert errors == []


# -- 0.25.0: mini-player tweaks ----------------------------------------------------------
def test_mini_strip_has_no_cover_and_buttons_only_as_tall_as_their_icons(make_window):
    window, root, _player, errors = make_window(mini_geometry="420x110+300+60")
    window.toggle_mini()
    _pump(root, 0.3)
    assert not hasattr(window.mini, "cover")                       # no artwork in the strip
    strip = window.mini.winfo_height()
    for button in (window.mini.previous_button, window.mini.play_button, window.mini.next_button,
                   window.mini.settings_button, window.mini.full_button, window.mini.close_button):
        assert button.winfo_ismapped()
        assert button.winfo_height() < strip * 0.7                 # not the strip's full height
    assert errors == []


def test_mini_strip_is_on_top_by_default_and_the_full_window_keeps_its_own(make_window):
    window, root, _player, errors = make_window()                  # full window: Keep on top off
    assert not bool(root.attributes("-topmost"))
    window.toggle_mini()
    _pump(root, 0.3)   # (X11 says so once the window manager has applied it, a moment later)
    assert bool(root.attributes("-topmost")) and window.on_top.get()   # the strip: on at first
    window.on_top.set(False)
    window._toggle_on_top()
    assert window.prefs["mini_on_top"] is False and window.prefs["always_on_top"] is False
    window.on_top.set(True)
    window._toggle_on_top()
    window.toggle_mini()                                           # back to full: off again
    _pump(root, 0.3)
    assert not bool(root.attributes("-topmost")) and not window.on_top.get()
    assert window.prefs["mini_on_top"] is True and window.prefs["always_on_top"] is False
    window._save_if_changed()
    assert window.prefs["mini_on_top"] is True and window.prefs["always_on_top"] is False
    assert errors == []


def test_settings_open_from_the_mini_strip_and_the_strip_comes_back(make_window):
    window, root, player, errors = make_window(mini_geometry="380x76+300+60")
    window.toggle_mini()
    _pump(root, 0.3)
    before = _where(root)
    window.mini.settings_button.command()                          # the strip's gear
    _pump(root, 0.3)
    assert window.mini_mode and window.settings.is_open        # still the strip, Settings open
    grown = _where(root)
    assert grown[1] > before[1] + theme.px(150)                    # the window grew for the panel
    assert window.mini.winfo_height() == before[1]                 # the strip itself didn't change
    assert window.mini_button.text_value == "Full player"
    window._save_if_changed()
    assert window.prefs["mini_geometry"].endswith("+300+60")       # the strip's own place is kept
    assert window.prefs["mini_geometry"].startswith("380x76")
    window.mini.settings_button.command()                          # gear again: closes
    _pump(root, 0.3)
    assert not window.settings.is_open and _where(root) == before
    # From Settings opened in the strip, "Full player" goes to the full window.
    window.mini.settings_button.command()
    _pump(root, 0.2)
    window.mini_button.command()
    _pump(root, 0.3)
    assert not window.mini_mode and not window.settings.is_open
    assert window.prefs["mini_geometry"].startswith("380x76") and window.mini_button.text_value == "Mini player"
    assert errors == [] and player.calls == []

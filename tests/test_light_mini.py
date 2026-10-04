"""0.19.0: the Light theme (and Follow Windows), WCAG AA contrast for both palettes,
and mini mode — switching keeps the window's places and never touches the music."""

from __future__ import annotations

import gc
import sys
import time
import types
from pathlib import Path

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
    assert [key for key, _ in theme.THEME_CHOICES] == ["dark", "light", "system"]


def test_palette_for_each_choice():
    assert theme.palette_for("dark") is theme.DARK
    assert theme.palette_for("light") is theme.LIGHT
    assert theme.palette_for("system", windows_light=True) is theme.LIGHT
    assert theme.palette_for("system", windows_light=False) is theme.DARK
    assert theme.palette_for("nonsense") is theme.DARK


def test_follow_windows_with_windows_unknown_stays_dark(monkeypatch):
    monkeypatch.setattr(theme, "windows_uses_light", lambda: None)
    assert theme.palette_for("system") is theme.DARK


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

    def make(**prefs):
        base = dict(media_controls=False, watch_music=False, clip_guard=False, exclusive=False,
                    library_folder=str(tmp_path / "Music"), geometry="420x640+120+120", always_on_top=False)
        settings.save({**settings.DEFAULTS, **base, **prefs})
        try:
            root = tk.Tk()
        except tk.TclError as error:
            pytest.skip(f"no display: {error}")
        errors = []
        root.report_callback_exception = lambda *info: errors.append(info)
        track = Track(path=song, title="Song One", artist="Artist", album="Album", duration=200.0,
                      sample_rate=44100, bits=16, channels=2)
        player = FakePlayer(track)
        window = PlayerWindow(root, player)
        window.tabs = [Tab("album", str(music), tracks=[track])]
        window.current = window.playing_tab = 0
        window._refresh_tabs()
        made.append((window, root))
        _pump(root, 0.6)
        return window, root, player, errors

    yield make
    for window, root in made:
        try:
            window.quit()
        except tk.TclError:
            pass
    theme.use(theme.DARK)
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


def test_light_from_settings_on_start_and_follow_windows(make_window, monkeypatch):
    window, root, _player, errors = make_window(theme="light")
    assert theme.current is theme.LIGHT and str(root.cget("bg")).lower() == theme.LIGHT.BG
    windows = {"light": False}
    monkeypatch.setattr(theme, "windows_uses_light", lambda: windows["light"])
    window.theme_box.current(2)                     # Follow Windows (dark right now)
    window._theme_chosen()
    assert theme.current is theme.DARK
    windows["light"] = True                         # Windows switches to light while we run…
    window._follow_windows()                        # …and the next look notices
    assert theme.current is theme.LIGHT
    windows["light"] = False
    window._follow_windows()
    assert theme.current is theme.DARK
    window.theme_box.current(1)                     # a fixed choice ignores Windows
    window._theme_chosen()
    windows["light"] = False
    window._follow_windows()
    assert theme.current is theme.LIGHT
    assert errors == []


def _where(root) -> tuple[int, int, int, int]:
    from ironbark import placement

    root.update_idletasks()
    return placement.parse(root.geometry())


def test_mini_and_full_keep_their_own_places_and_the_music_plays_on(make_window):
    window, root, player, errors = make_window(mini_geometry="640x140+300+60")
    full_before = _where(root)
    assert full_before[2:] == (120, 120)
    assert root.bind("z")                            # key Z is bound

    window.toggle_mini()
    _pump(root, 0.5)
    assert window.mini_mode and window.mini.winfo_ismapped() and not window.bar.winfo_ismapped()
    w, h, x, y = _where(root)
    assert (x, y) == (300, 60) and abs(w - 640) <= 4 and abs(h - 140) <= 4
    assert window.mini.title_label.cget("text") == "Song One"
    assert window.mini.sub_label.cget("text").startswith("Artist")
    assert window.mini.seek.value == 42.0 and window.mini.seek.maximum == 200.0
    assert window.prefs["mini_mode"] is True

    window.toggle_mini()
    _pump(root, 0.5)
    assert not window.mini_mode and window.bar.winfo_ismapped() and not window.mini.winfo_ismapped()
    w, h, x, y = _where(root)
    assert (x, y) == full_before[2:] and abs(w - full_before[0]) <= 4 and abs(h - full_before[1]) <= 4
    assert window.prefs["mini_geometry"].endswith("+300+60") and window.prefs["mini_mode"] is False

    window._save_if_changed()                        # the two places are saved separately
    saved = settings.load()
    assert saved["geometry"].endswith("+120+120") and saved["mini_geometry"].endswith("+300+60")
    assert player.calls == [] and player.status().state is State.PLAYING   # the music never noticed
    assert errors == []


def test_keep_on_top_survives_the_switch_and_the_mini_strip_reopens(make_window):
    window, root, player, errors = make_window(always_on_top=True, mini_mode=True, mini_geometry="600x130+200+90")
    assert window.mini_mode and window.mini.winfo_ismapped()         # left as the mini strip: opens as it
    assert _where(root)[2:] == (200, 90)
    assert bool(root.attributes("-topmost"))
    window.toggle_mini()
    assert bool(root.attributes("-topmost")) and _where(root)[2:] == (120, 120)
    window.mini.show(title="t", artist="a", position=1.0, duration=2.0, playing=False,
                     status_colour=theme.BG, status_detail="")
    assert errors == [] and player.calls == []


def test_first_mini_strip_opens_at_the_full_windows_corner(make_window):
    window, root, _player, errors = make_window()
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
    assert bool(root.attributes("-topmost")) and window.on_top.get()   # the strip: on at first
    window.on_top.set(False)
    window._toggle_on_top()
    assert window.prefs["mini_on_top"] is False and window.prefs["always_on_top"] is False
    window.on_top.set(True)
    window._toggle_on_top()
    window.toggle_mini()                                           # back to full: off again
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
    assert window.mini_mode and window.settings_pop.is_open        # still the strip, Settings open
    grown = _where(root)
    assert grown[1] > before[1] + theme.px(150)                    # the window grew for the panel
    assert window.mini.winfo_height() == before[1]                 # the strip itself didn't change
    assert window.mini_button.text_value == "Full player"
    window._save_if_changed()
    assert window.prefs["mini_geometry"].endswith("+300+60")       # the strip's own place is kept
    assert window.prefs["mini_geometry"].startswith("380x76")
    window.mini.settings_button.command()                          # gear again: closes
    _pump(root, 0.3)
    assert not window.settings_pop.is_open and _where(root) == before
    # From Settings opened in the strip, "Full player" goes to the full window.
    window.mini.settings_button.command()
    _pump(root, 0.2)
    window.mini_button.command()
    _pump(root, 0.3)
    assert not window.mini_mode and not window.settings_pop.is_open
    assert window.prefs["mini_geometry"].startswith("380x76") and window.mini_button.text_value == "Mini player"
    assert errors == [] and player.calls == []

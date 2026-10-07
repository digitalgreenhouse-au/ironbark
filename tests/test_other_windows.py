"""0.22.0: the other windows get the main window's finish — Settings' section headings,
flat pill buttons (controls.FlatButton), choices as a row of words (ChoiceRow) instead of radio
buttons, slider switches (Switch) instead of tick boxes, the shared search box, slim scrollbars, and
live numbers in fixed-width digits with a true minus sign. Now Playing: one label column for every
section, ✓ green / ✗ ⚠ amber, no blank lines, never taller than the screen. Pure functions are
tested without a display; the Tk parts are smoke tests (skipped without one)."""

from __future__ import annotations

import ast
import gc
import queue
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import controls, theme

PACKAGE = Path(theme.__file__).parent
WINDOW_MODULES = ("details.py", "library_window.py", "meter_window.py", "pcm_window.py", "spectrogram_window.py",
                  "scope_window.py", "compare_window.py", "abx_window.py", "testsignals_window.py", "tag_window.py")


# -- numbers that hold still (no display needed) ------------------------------------------------------
def test_figures_turns_a_number_s_hyphen_into_the_minus_sign():
    assert theme.figures("-1.2 dB") == "−1.2 dB"
    assert theme.figures("PSR -.5 · L -65 dBFS · (-3)") == "PSR −.5 · L −65 dBFS · (−3)"
    assert theme.figures("+0.4") == "+0.4"
    # Hyphens that aren't a number's sign stay as they are.
    for text in ("ITU-R BS.1770-5", "J-test", "Tape-1", "16-bit", "a - b", "—", ""):
        assert theme.figures(text) == text


def test_pad_figures_keeps_a_number_s_width():
    assert theme.pad_figures("99%", 4) == theme.FIGURE_SPACE + "99%"
    assert theme.pad_figures("99%", 4, after=True) == "99%" + theme.FIGURE_SPACE
    assert theme.pad_figures("100%", 4) == "100%" and theme.pad_figures("12345", 4) == "12345"


def test_tone_of_colours_lines_as_the_main_window_does():
    from ironbark.details import TONE_STYLE, tone_of
    assert tone_of("✓ Verified") == "good" and TONE_STYLE["good"] == "GoodValue.TLabel"
    assert tone_of("✗ MD5 mismatch") == "warn" and tone_of("⚠ Only 16 of 24 bits") == "warn"
    assert tone_of("DR5") == "plain" and tone_of("") == "plain"


def test_section_headings_follow_settings_and_pass_contrast():
    """Section headings are MUTED (as Settings' captions), on BG: a pair the contrast test covers."""
    assert ("MUTED", "BG") in theme.TEXT_PAIRS
    for palette in (theme.DARK, theme.LIGHT):
        assert theme.contrast_ratio(palette.MUTED, palette.BG) >= theme.AA_TEXT
        # the pill's text on its fills, plain and primary, at rest, under the pointer and pressed
        for fg, bg in (("TEXT", "SURFACE"), ("TEXT", "SURFACE_HI"), ("TEXT", "SURFACE_DOWN"), ("ON_ACCENT", "SELECT")):
            assert theme.contrast_ratio(getattr(palette, fg), getattr(palette, bg)) >= theme.AA_TEXT


def test_window_modules_use_the_shared_controls():
    """No stock ttk buttons, radio buttons, tick boxes, separators or full-size scrollbars are left
    in the secondary windows: each uses controls' flat versions."""
    stock = {"Button", "Radiobutton", "Checkbutton", "Scrollbar", "Separator"}
    found = []
    for name in WINDOW_MODULES:
        tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in stock
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in ("ttk", "tk")):
                found.append(f"{name}:{node.lineno} {node.func.value.id}.{node.func.attr}")
    assert found == []


def test_mac_build_gets_menlo():
    """Menlo ships with macOS (Apple's font list); the code picks it there (checked by reading the
    rule, since this test runs on Windows)."""
    source = (PACKAGE / "theme.py").read_text(encoding="utf-8")
    assert '"Menlo" if sys.platform == "darwin"' in source
    if sys.platform == "win32":
        assert theme.MONO == "Consolas"


# -- Tk ------------------------------------------------------------------------------------------
@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.geometry("520x520+60+60")
    r.title("DG test — other windows")
    theme.apply(r)
    errors = []
    r.report_callback_exception = lambda *info: errors.append(info)
    r.errors = errors
    r.update()
    yield r
    theme.use(theme.DARK)
    r.destroy()
    del r
    gc.collect()   # on this thread (Tk objects freed on another thread abort Python)


def _pump(root, seconds: float = 0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


def test_numbers_font_is_measured_and_segoe_ui_qualifies(root):
    """Tk can't ask for tabular figures, so apply() measures: on Windows, Segoe UI's digits are all
    one width and "+" is as wide as "−" (measured 05/10/2026), so live numbers stay in Segoe UI."""
    assert theme.NUMBERS in (theme.FONT, theme.MONO)
    assert theme.tabular_digits(theme.MONO, root)          # a fixed-width font always qualifies
    if sys.platform == "win32":
        assert theme.tabular_digits("Segoe UI", root) and theme.NUMBERS == "Segoe UI"
        from tkinter import font as tkfont
        if "Segoe UI Variable Text" in tkfont.families(root):   # Windows 11's: proportional digits
            assert not theme.tabular_digits("Segoe UI Variable Text", root)


def test_flat_button_speaks_ttk(root):
    pressed = []
    button = controls.FlatButton(root, "Freeze", lambda: pressed.append(1), fit="Resume")
    button.pack()
    root.update()
    width = button.winfo_reqwidth()
    button.configure(text="Resume")
    root.update()
    assert button.cget("text") == "Resume" and button.winfo_reqwidth() == width   # fit: no resize
    assert button.cget("state") == "normal" and button.instate(["!disabled"])
    button.invoke()
    assert pressed == [1]
    button.state(["disabled"])
    assert button.instate(["disabled"]) and str(button.cget("state")) == "disabled"
    button.invoke()
    assert pressed == [1]                                   # greyed out: does nothing
    button.configure(state="normal")
    assert button.enabled
    assert isinstance(button.configure(), dict)             # theme.recolour can still list its options
    calls = []
    button.configure(command=lambda: calls.append("new"))
    button.invoke()
    assert calls == ["new"]


def test_primary_button_greys_out_like_a_plain_one(root):
    plain = controls.FlatButton(root, "A", state="disabled")
    primary = controls.FlatButton(root, "B", kind="primary", state="disabled")
    assert primary.fill_colour() == plain.fill_colour() == theme.SURFACE
    primary.set_enabled(True)
    assert primary.fill_colour() == theme.SELECT


def test_choice_row_follows_its_variable(root):
    import tkinter as tk
    var = tk.StringVar(value="live")
    changes = []
    row = controls.ChoiceRow(root, (("live", "Live"), ("track", "Whole track")), var, command=lambda: changes.append(1))
    row.pack()
    root.update()
    assert row.labels["live"].cget("fg") == theme.ACCENT_TEXT and row.bars["live"].cget("bg") == theme.ACCENT
    assert row.labels["track"].cget("fg") == theme.MUTED
    font = row.labels["live"].cget("font")
    row.choose("track")
    assert var.get() == "track" and changes == [1]
    row.choose("track")                                      # already chosen: no second run
    assert changes == [1]
    assert row.labels["track"].cget("fg") == theme.ACCENT_TEXT and row.labels["live"].cget("font") == font
    var.set("live")                                          # set from code: the row follows
    assert row.bars["live"].cget("bg") == theme.ACCENT and row.bars["track"].cget("bg") == theme.BG
    row.marks = {"track"}
    row.paint()
    assert row.labels["track"].cget("fg") == theme.ACCENT_TEXT
    row.destroy()
    assert row.variable is None and row.command is None      # let go (no Tk variable freed off-thread)


def test_switch_flips_and_greys_out(root):
    import tkinter as tk
    var = tk.BooleanVar(value=True)
    heard = []
    switch = controls.Switch(root, "Match levels", var, lambda: heard.append(var.get()))
    switch.pack()
    root.update()
    switch.toggle.toggle()
    assert var.get() is False and heard == [False]
    switch.state(["disabled"])
    assert switch.instate(["disabled"])
    switch.toggle.toggle()
    assert var.get() is False and heard == [False]          # greyed out: clicks do nothing
    switch.state(["!disabled"])
    switch.label.event_generate("<ButtonRelease-1>", x=2, y=2)   # let go over the words
    assert var.get() is True


def test_search_box_hint_and_clear(root):
    cleared = []
    box = controls.SearchBox(root, "Search albums", on_clear=lambda: cleared.append(1))
    box.pack(fill="x")
    root.update()
    assert box.hint.winfo_manager() == "place" and not box.clear_button.winfo_manager()
    box.query.set("adele")
    assert box.clear_button.winfo_manager() == "place" and not box.hint.winfo_manager()
    root.update()
    box.clear_button.event_generate("<ButtonRelease-1>")
    assert cleared == [1]


def test_slim_horizontal_scrollbar(root):
    bar = controls.AutoScrollbar(root, orient="horizontal")
    assert str(bar.cget("style")) == theme.SLIM_HSCROLLBAR and str(bar.cget("orient")) == "horizontal"
    bar.auto_pack(side="bottom", fill="x")
    bar.set(0.0, 1.0)
    assert not bar.winfo_manager()
    bar.set(0.0, 0.5)
    assert bar.winfo_manager() == "pack"


def test_section_heading_has_a_rule_above_all_but_the_first(root):
    import tkinter as tk
    frame = tk.Frame(root)
    frame.pack()
    first = controls.section(frame, "Live", first=True)
    second = controls.section(frame, "Proof")
    assert first.cget("text") == "LIVE" and str(first.cget("style")) == "Section.TLabel"
    rules = [w for w in frame.winfo_children() if isinstance(w, tk.Frame)]
    assert len(rules) == 1 and rules[0].cget("bg") == theme.BORDER and second.cget("text") == "PROOF"


# -- Now Playing -------------------------------------------------------------------------------------
def _flac(tmp_path) -> Path:
    path = tmp_path / "01 Song.flac"
    t = np.arange(44100 * 2) / 44100
    tone = (np.sin(2 * np.pi * 440 * t) * 0.3 * 32767).astype(np.int16)
    sf.write(path, np.column_stack([tone, tone]), 44100, subtype="PCM_16")
    return path


class ShownPlayer:
    """Says a song is playing, with fixed figures; plays nothing."""
    volume = 1.0

    def __init__(self, track, levels=(0.42, 0.37)):
        self.track, self.levels = track, levels

    def status(self):
        from ironbark.engine import Health, OutputInfo, State, Status
        return Status(State.PLAYING, 0, self.track, 1.0, self.track.duration,
                      output=OutputInfo("Test output", 44100, 48000, False), levels=self.levels,
                      track_peak=0.5, buffer=0.97, health=Health(buffer_low=1.8), transition="gapless")


def test_now_playing_lines_up_colours_and_fits(root, tmp_path):
    from ironbark.analysis import analyse
    from ironbark.details import NowPlayingWindow
    from ironbark.library import read_track
    from ironbark import placement

    path = _flac(tmp_path)
    track = read_track(path)
    NowPlayingWindow._analysis_cache[path] = analyse(path, track.bits, track.md5)
    try:
        win = NowPlayingWindow(root, ShownPlayer(track), prefs={}, on_meters=lambda: None, on_pcm=lambda: None,
                               on_compare=lambda: None)
        _pump(root, 0.4)
        # One label column for every section: the same width everywhere.
        frames = [win.values["position"].master, win.values["sent"].master, win.file_frame, win.check_frame]
        widths = {int(frame.columnconfigure(0)["minsize"]) for frame in frames}
        assert len(widths) == 1 and widths.pop() >= theme.px(96)
        # ✓ lines green, ✗ / ⚠ amber (as the main window), live numbers in the numbers styles.
        styles = {w.cget("text"): str(w.cget("style")) for w in win.check_frame.grid_slaves(column=1)
                  if w.winfo_class() == "TLabel"}
        verified = [style for text, style in styles.items() if text.startswith("✓")]
        assert verified and set(verified) == {"GoodValue.TLabel"}
        assert str(win.values["position"].cget("style")) == "Number.TLabel"
        assert str(win.values["start"].cget("style")) == "GoodValue.TLabel"
        assert theme.FIGURE_SPACE in win.values["buffer"].cget("text")      # " 97%" holds its width
        texts = [win.meter.itemcget(i, "text") for i in win.meter.find_all() if win.meter.type(i) == "text"]
        assert any(t.startswith(theme.MINUS) for t in texts)                 # −7.5 dB, a true minus
        # Flat pill buttons, and no blank line under the Windows check while it has nothing to say.
        assert set(win.tool_buttons) == {"Meters (M)", "PCM stream (B)", "Compare two files (D)"}
        assert isinstance(win.windows_button, controls.FlatButton)
        assert not win.windows_note.winfo_manager()
        # Never taller than the screen; the panel scrolls instead.
        assert win.win.winfo_height() <= placement.work_height(win.win)
        assert isinstance(win.area, controls.ScrollArea)
        win.close()
        assert not root.errors
    finally:
        NowPlayingWindow._analysis_cache.pop(path, None)


def test_now_playing_check_stays_greyed_out_while_it_runs(root, tmp_path, monkeypatch):
    import threading
    from ironbark import details
    from ironbark.details import NowPlayingWindow
    from ironbark.library import read_track

    track = read_track(_flac(tmp_path))
    release, calls = threading.Event(), []

    def slow(path, bits, md5, progress=None, cancel=None):
        calls.append(path)
        progress(0.37)
        release.wait(10)
        return None                                   # (as when the check is cancelled)

    monkeypatch.setattr(details, "analyse", slow)
    win = NowPlayingWindow(root, ShownPlayer(track), prefs={})
    _pump(root, 0.2)
    win.check_button.invoke()
    _pump(root, 0.3)
    assert win.check_button.instate(["disabled"])
    win.network_changed()                             # the Check section is rebuilt while it runs
    _pump(root, 0.3)
    assert win.check_button.instate(["disabled"]) and "37%" in win.check_progress.cget("text")
    win.check_button.invoke()
    assert len(calls) == 1                            # no second check of the same file
    release.set()
    _pump(root, 0.6)
    assert win.check_button.instate(["!disabled"])    # it ended without a result: the button is back…
    assert not win.check_progress.winfo_manager()     # …and no progress line is left behind
    win.close()
    assert not root.errors


def test_now_playing_work_that_fails_gives_its_button_back(root, tmp_path, monkeypatch):
    from ironbark import details
    from ironbark.details import NowPlayingWindow
    from ironbark.library import read_track

    track = read_track(_flac(tmp_path))

    def out_of_memory(*args, **kwargs):
        raise MemoryError("too big")

    monkeypatch.setattr(details, "album_measure", out_of_memory)
    monkeypatch.setattr(details, "analyse", out_of_memory)
    win = NowPlayingWindow(root, ShownPlayer(track), prefs={})
    _pump(root, 0.2)
    win.album_button.invoke()
    _pump(root, 0.6)
    assert win.album_button.instate(["!disabled"]) and win._album_state is None
    win.check_button.invoke()
    _pump(root, 0.6)
    assert win.check_button.instate(["!disabled"])
    assert "The check failed" in win.check_progress.cget("text")
    assert NowPlayingWindow.cached_analysis(track.path) is None   # not mistaken for a damaged file
    win.close()
    assert not root.errors


def test_now_playing_follows_light(root, tmp_path):
    from ironbark.details import NowPlayingWindow
    from ironbark.library import read_track

    track = read_track(_flac(tmp_path))
    win = NowPlayingWindow(root, ShownPlayer(track), prefs={}, on_meters=lambda: None)
    _pump(root, 0.3)
    theme.use(theme.LIGHT, root)
    _pump(root, 0.3)
    button = win.tool_buttons["Meters (M)"]
    assert button.cget("bg") == theme.LIGHT.BG and button.fill_colour() == theme.LIGHT.SURFACE
    win.close()
    assert not root.errors


# -- the live windows: readings in fixed-width digits --------------------------------------------------
class FeedPlayer:
    volume, output_device, exclusive = 0.0, "Test output", False

    def monitor(self):
        return queue.Queue()

    def unmonitor(self, feed):
        pass

    def status(self):
        from ironbark.engine import State
        return SimpleNamespace(track=None, position=0, state=State.STOPPED, index=0, duration=0)


def test_meter_readings_use_the_numbers_font_and_minus(root):
    from ironbark.meter_window import MeterWindow
    from ironbark.meters import Reading

    win = MeterWindow(root, FeedPlayer(), {"vu_align": "-18 dBFS (EBU R68)"})
    win.win.geometry("640x700")
    _pump(root, 0.3)
    reading = Reading(vu=(-3.0, 1.0), ppm=(-4.1, -3.8), rms=(-8.1, -8.1), true_peak=(0.4, -0.2),
                      sample_peak=(0.0, 0.0), max_true_peak=0.4, momentary=-8.3, short_term=-7.6, integrated=-6.7,
                      loudness_range=1.6, max_momentary=-4.3, max_short_term=-5.6, measured=30.0, psr=8.7)
    win._draw_live(reading)
    texts = {win.canvas.itemcget(i, "text"): win.canvas.itemcget(i, "font") for i in win.canvas.find_withtag("live")
             if win.canvas.type(i) == "text"}
    assert "−4.1" in texts and "+0.4" in texts and "−3.0 VU" in texts
    assert theme.NUMBERS in texts["−4.1"]
    assert theme.MINUS in win.loud_label.cget("text") and "-" not in win.loud_label.cget("text").split("·")[0]
    assert isinstance(win.max_label.cget("font"), str) and theme.NUMBERS.split()[0] in win.max_label.cget("font")
    win.close()
    assert not root.errors


def test_analysis_windows_use_choice_rows(root):
    from ironbark.pcm_window import PcmWindow
    from ironbark.scope_window import ScopeWindow
    from ironbark.spectrogram_window import SpectrogramWindow

    prefs: dict = {}
    pcm = PcmWindow(root, FeedPlayer(), prefs)
    _pump(root, 0.2)
    pcm.base_choice.choose("Hex")
    assert pcm.base.get() == "Hex" and prefs["pcm_base"] == "Hex"
    assert isinstance(pcm.freeze_button, controls.FlatButton)
    pcm.close()
    spec = SpectrogramWindow(root, FeedPlayer(), prefs)
    assert set(spec.view_choice.labels) == {"live", "track"}
    spec.close()
    scope = ScopeWindow(root, FeedPlayer(), prefs)
    _pump(root, 0.2)
    scope.view_choice.choose("polar")
    assert prefs["scope_view"] == "polar"
    scope.close()
    _pump(root, 0.2)
    assert not root.errors


def test_library_window_has_the_main_window_finish(root, tmp_path, monkeypatch):
    from ironbark import library_index, settings
    from ironbark.library_window import LibraryWindow

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "settings" / "settings.json")
    monkeypatch.setattr(library_index, "_shared", None)
    music = tmp_path / "Music"
    (music / "Album").mkdir(parents=True)
    _flac(music / "Album")
    win = LibraryWindow(root, music, None, on_choose=lambda p: None, on_root_changed=lambda p: None,
                        on_load_all=lambda p, s: None)
    _pump(root, 0.3)
    assert isinstance(win.search, controls.SearchBox) and win.search_var is win.search.query
    assert isinstance(win.views, controls.ChoiceRow) and win.views.bars["albums"].cget("bg") == theme.ACCENT
    win.views.choose("artists")
    assert win.view == "artists" and win.views.bars["artists"].cget("bg") == theme.ACCENT
    assert isinstance(win.scrollbar, controls.AutoScrollbar)
    # Esc in the search box empties it; Esc in an empty box is left to the window (it closes).
    win.search_var.set("x")
    assert win._clear_search() == "break" and win.search_var.get() == ""
    assert win._clear_search() is None
    # The row under the pointer lights up (the hover tag comes first, so it wins).
    rows = win.tree.get_children()
    if rows:
        win._set_hover(rows[0])
        assert "hover" in win.tree.item(rows[0], "tags")
        win._set_hover(None)
        assert "hover" not in win.tree.item(rows[0], "tags")
    win.close()
    assert not root.errors

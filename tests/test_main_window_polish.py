"""0.20.0: the main window's polish — the drawn seek bar (shared with the mini strip),
the round play button and hover discs, the volume pill, rounded covers and the ♫ placeholder, the
track list's hover row / playing mark / tidy album rows / slim scrollbar / search × , and the
empty window's "Open your library". Pillow drawing is tested without a display; the Tk parts are
smoke tests (skipped without one)."""

from __future__ import annotations

import gc
from pathlib import Path

import pytest
from PIL import Image
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

from ironbark import controls, theme
from ironbark.library import Track
from ironbark.playlist_view import PLAYING_MARK, album_heading
from ironbark.ui import INFO_INK_INSET, STATUS_DOT


# -- drawing (no display needed) ----------------------------------------------------------------
def test_mix_goes_between_two_colours():
    assert theme.mix("#000000", "#ffffff", 0) == "#000000"
    assert theme.mix("#000000", "#ffffff", 1) == "#ffffff"
    assert theme.mix("#000000", "#ffffff", 0.5) == "#808080"
    assert theme.mix("ACCENT", "ACCENT", 0.3) == theme.ACCENT     # roles work too


def test_round_corners_only_touches_the_corners():
    cover = Image.new("RGB", (200, 200), (200, 30, 30))
    out = controls.round_corners(cover, 16, "BG")
    assert out.size == cover.size and cover.getpixel((0, 0)) == (200, 30, 30)   # the original untouched
    assert out.getpixel((0, 0)) == theme.rgb("BG") and out.getpixel((199, 199)) == theme.rgb("BG")
    assert out.getpixel((100, 100)) == (200, 30, 30) and out.getpixel((100, 0)) == (200, 30, 30)
    assert out.getpixel((0, 100)) == (200, 30, 30)
    assert controls.round_corners(cover, 0, "BG").getpixel((0, 0)) == (200, 30, 30)


def test_corner_radius_is_soft_and_never_big():
    assert 1 <= controls.corner_radius(40) <= 4          # a tenth at most
    assert controls.corner_radius(400) == theme.px(controls.COVER_RADIUS)
    assert controls.corner_radius(400, small=True) == theme.px(controls.THUMB_RADIUS)


def test_placeholder_is_a_quiet_square_the_cover_size():
    for size in (32, 150, 401):
        image = controls.placeholder(size)
        assert image.size == (size, size)
        assert image.getpixel((1, 1)) == theme.rgb("SURFACE")
        colours = {c for _, c in image.getcolors(size * size)}
        # Quiet: nothing brighter than the muted grey (the note is between SURFACE and MUTED).
        brightest = max(sum(c) for c in colours)
        assert brightest <= max(sum(theme.rgb("SURFACE")), sum(theme.rgb("MUTED")))
        assert len(colours) > 1                           # …but there is a note on it


def test_drawn_button_shades_keep_wcag_contrast():
    """The play glyph stays ≥ 3:1 on its circle at rest, under the pointer and pressed; the white
    text of "Open your library" stays ≥ 4.5:1 when its pill darkens under the pointer."""
    for palette in (theme.DARK, theme.LIGHT):
        theme.use(palette)
        try:
            for fill in (theme.ACCENT, theme.mix("ACCENT", "ON_ACCENT", controls.HOVER_LIGHTEN),
                         theme.mix("ACCENT", "ACCENT_INK", controls.PRESS_DARKEN)):
                assert theme.contrast_ratio(theme.ACCENT_INK, fill) >= theme.AA_GRAPHICS, (palette.name, fill)
            for amount in (controls.PRESS_DARKEN, controls.PRESS_DARKEN * 1.6):
                pill = theme.mix("SELECT", "ACCENT_INK", amount)
                assert theme.contrast_ratio(theme.ON_ACCENT, pill) >= theme.AA_TEXT, (palette.name, pill)
        finally:
            theme.use(theme.DARK)


def test_album_heading_reads_album_then_artist():
    folder = Path("Music") / "Madvillain-Madvillainy (2004) FLAC"
    t = Track(folder / "01.flac", "Accordion", artist="Madvillain", album="Madvillainy")
    assert album_heading([t], folder) == "Madvillainy  ·  Madvillain"
    assert album_heading([Track(folder / "01.flac", "x")], folder) == folder.name     # untagged: the folder
    self_titled = Track(folder / "01.flac", "x", artist="Amyl and the Sniffers", album="Amyl And The Sniffers")
    assert album_heading([self_titled], folder) == "Amyl And The Sniffers"
    various = Track(folder / "01.flac", "x", artist="Someone", album="Hits", compilation=True)
    assert album_heading([various], folder) == "Hits"
    by_album_artist = Track(folder / "01.flac", "x", artist="Guest", album="Duets", album_artist="Host")
    assert album_heading([by_album_artist], folder) == "Duets  ·  Host"


# -- Tk widgets (smoke tests) ---------------------------------------------------------------
@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.geometry("400x300+40+40")
    theme.apply(r)
    errors = []
    r.report_callback_exception = lambda *info: errors.append(info)
    r.errors = errors
    yield r
    theme.use(theme.DARK)
    r.destroy()
    del r
    gc.collect()   # on this thread (Tk objects freed on another thread abort Python)


class _Event:
    def __init__(self, x=0, y=0):
        self.x, self.y = x, y


def test_slider_click_drag_and_release(root):
    released, dragged = [], []
    slider = controls.Slider(root, on_release=released.append, on_drag=dragged.append)
    slider.pack(fill="x")
    _pump(root, 0.2)
    width = slider.winfo_width()
    assert width > 50
    slider.set(30.0, 120.0)
    assert (slider.value, slider.maximum) == (30.0, 120.0) and slider.fraction() == 0.25
    inset = slider._radii[1]
    slider._press(_Event(inset + (width - 2 * inset) / 2))      # press half-way
    assert slider.dragging and dragged[-1] == pytest.approx(60.0)
    slider.set(99.0, 120.0)                                     # the player's poll: ignored mid-drag
    assert slider.value == pytest.approx(60.0)
    slider._move(_Event(width + 50))                            # past the end: clamped
    assert dragged[-1] == pytest.approx(120.0)
    slider._release(_Event(inset))                              # let go at the start
    assert released == [pytest.approx(0.0)] and not slider.dragging
    slider.set(0.0, 0.0)                                        # nothing loaded: a press does nothing
    slider._press(_Event(width // 2))
    assert not slider.dragging and len(released) == 1
    assert root.errors == []


def test_slider_knob_grows_under_the_pointer_and_follows_the_palette(root):
    slider = controls.Slider(root, on_release=lambda v: None)
    slider.pack(fill="x")
    _pump(root, 0.2)
    slider.set(10.0, 20.0)
    resting = slider._drawn
    slider._hover(True)
    assert slider._drawn != resting and slider._drawn[3] == pytest.approx(slider._radii[1])
    slider._hover(False)
    assert slider._drawn[3] == pytest.approx(slider._radii[0])
    theme.use(theme.LIGHT, root)
    assert slider.cget("bg") == theme.LIGHT.BG and theme.LIGHT.ACCENT in slider._drawn[4]
    assert root.errors == []


def test_play_button_shows_play_or_pause_and_clicks(root):
    pressed = []
    button = controls.PlayButton(root, lambda: pressed.append(1), label="Play")
    button.pack()
    _pump(root, 0.2)
    # The circle, and room round it for the keyboard's focus ring.
    assert button.winfo_reqwidth() == max(16, theme.px(controls.PLAY_DIAMETER)) + 2 * button.margin
    play_image = str(button.cget("image"))
    button.set_playing(True)
    assert str(button.cget("image")) != play_image
    assert button.fill() == theme.ACCENT
    button._state(hover=True)
    assert button.fill() == theme.mix("ACCENT", "ON_ACCENT", controls.HOVER_LIGHTEN)
    button._state(pressed=True)
    button._release(_Event(5, 5))                  # let go over it: runs
    assert pressed == [1]
    button._state(pressed=True)
    button._release(_Event(-50, 5))                # dragged off first: doesn't
    assert pressed == [1]
    assert root.errors == []


def _pixel(widget, x, y) -> str:
    """The colour at (x, y) of the picture a label shows."""
    r, g, b = (int(v) for v in widget.tk.splitlist(widget.tk.call(str(widget.cget("image")), "get", x, y)))
    return f"#{r:02x}{g:02x}{b:02x}"


def _near(colour: str, role: str, tolerance: int = 24) -> bool:
    """`colour` is `role`'s colour, give or take what scaling a 4x drawing down leaves near an edge
    (LANCZOS rings a little: a few levels lighter or darker within a pixel or two of a shape's edge)."""
    return all(abs(a - b) <= tolerance for a, b in zip(theme.rgb(colour), theme.rgb(role)))


def test_round_icon_button_lights_a_disc_on_hover(root):
    button = controls.RoundIconButton(root, root, "shuffle", lambda: None, size=theme.ICON_MEDIUM, diameter=32,
                                      width=36, bg="BG")
    button.pack()
    _pump(root, 0.1)
    resting = str(button.cget("image"))
    middle = theme.px(32) // 2
    assert _pixel(button, middle, middle) == theme.BG
    button._paint(True)
    assert str(button.cget("image")) != resting and button.cget("bg") == theme.BG   # the disc, not a square
    assert _pixel(button, middle, middle) == theme.HOVER and _pixel(button, 0, 0) == theme.BG
    assert button.cget("fg") == theme.TEXT
    button._paint(False)
    button.set_on(True)                            # shuffle on: green glyph and a dot under it
    assert button.cget("fg") == theme.ACCENT_TEXT
    dot_y = middle + theme.px(controls.RoundIconButton.ON_DOT_BELOW)
    assert _near(_pixel(button, middle, dot_y), "ACCENT_TEXT")
    theme.use(theme.LIGHT, root)
    assert button.cget("bg") == theme.LIGHT.BG and button.cget("fg") == theme.LIGHT.ACCENT_TEXT
    assert _near(_pixel(button, middle, dot_y), "ACCENT_TEXT")
    button.set_on(False)
    assert button.cget("fg") == theme.LIGHT.MUTED and _pixel(button, middle, dot_y) == theme.LIGHT.BG   # no dot
    assert root.errors == []


def test_previous_and_next_are_drawn_filled(root):
    previous = controls.RoundIconButton(root, root, "prev", lambda: None, diameter=36, width=40, bg="BG", drawn="prev")
    after = controls.RoundIconButton(root, root, "next", lambda: None, diameter=36, width=40, bg="BG", drawn="next")
    for button in (previous, after):
        button.pack()
    _pump(root, 0.1)
    middle = theme.px(36) // 2
    for button in (previous, after):
        assert button.cget("text") == ""                                    # no icon font needed
        assert _near(_pixel(button, middle, middle), "TEXT")                # filled, in the middle
    bar = theme.px(controls.RoundIconButton.SKIP_SIZE) // 2 - 1
    assert _near(_pixel(previous, middle - bar, middle), "TEXT") and _near(_pixel(after, middle + bar, middle), "TEXT")
    assert root.errors == []


def test_pill_button_fits_its_text(root):
    pill = controls.PillButton(root, "5%", lambda: None, fill=None, fg="WARN", outline="WARN",
                               hover_fill="SURFACE_HI")
    pill.pack()
    _pump(root, 0.1)
    narrow = pill.winfo_reqwidth()
    pill.set_text("100%")
    _pump(root, 0.1)
    assert pill.winfo_reqwidth() > narrow and pill.cget("fg") == theme.WARN
    assert pill.fill_colour() is None
    pill._state(hover=True)
    assert pill.fill_colour() == theme.SURFACE_HI
    assert root.errors == []


def test_auto_scrollbar_only_shows_when_needed(root):
    from tkinter import ttk

    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    bar = controls.AutoScrollbar(frame)
    bar.auto_pack(side="right", fill="y")
    assert not bar.winfo_manager()
    bar.set("0.0", "1.0")
    assert not bar.winfo_manager()
    bar.set("0.0", "0.4")
    assert bar.winfo_manager() == "pack" and bar.needed
    bar.set("0.0", "1.0")
    assert not bar.winfo_manager()
    style = ttk.Style(root)
    assert "arrow" not in str(style.layout(theme.SLIM_SCROLLBAR))   # no arrow buttons
    assert "focus" not in str(style.layout("Treeview.Item"))         # no dotted focus box on a row


# -- the track list ---------------------------------------------------------------------------
def _album(folder: str, album: str, n: int) -> list[Track]:
    return [Track(Path(folder) / f"{i:02d}.flac", f"Song {i}", artist="Band", album=album, track_no=i,
                  duration=60.0 * i, sample_rate=44100, bits=16) for i in range(1, n + 1)]


def test_track_list_hover_playing_mark_and_album_rows(root):
    from ironbark.playlist_view import PlaylistView

    view = PlaylistView(root, on_play=lambda i: None)
    tracks = _album("A", "First", 3) + _album("B", "Second", 2)
    view.set_tracks(tracks)
    tree = view.tree
    headers = [r for r in tree.get_children() if not r.isdigit()]
    assert tree.set(headers[0], "title") == "First  ·  Band" and tree.set(headers[0], "time") == "16/44.1"

    view.show_current(1)                                   # the 2nd song plays
    assert tree.set("1", "no") == PLAYING_MARK and "playing" in tree.item("1", "tags")
    view.select(3)                                         # another row selected: still marked
    assert tree.set("1", "no") == PLAYING_MARK
    view.query.set("song")                                 # a search re-draws the list: still marked
    view._render()
    assert tree.set("1", "no") == PLAYING_MARK and "playing" in tree.item("1", "tags")
    view.query.set("")
    view._render()
    view.show_current(3)                                   # the next album's first song
    assert str(tree.set("1", "no")) == "2" and "playing" not in tree.item("1", "tags")
    assert tree.set("3", "no") == PLAYING_MARK
    view.clear_current()
    assert str(tree.set("3", "no")) == "1" and view.playing is None

    view._set_hover("2")
    assert tree.item("2", "tags")[0] == "hover"
    view._set_hover(headers[1])                            # album rows light up too
    assert "hover" not in tree.item("2", "tags") and tree.item(headers[1], "tags")[0] == "hover"
    view._set_hover(None)
    assert all("hover" not in tree.item(r, "tags") for r in tree.get_children())
    view.set_tracks(tracks[:2])                            # another tab's list: no stale mark
    assert view.playing is None and all(PLAYING_MARK != tree.set(r, "no") for r in tree.get_children())
    assert root.errors == []


def test_search_box_magnifier_and_clear(root):
    from ironbark.playlist_view import PlaylistView

    view = PlaylistView(root, on_play=lambda i: None)
    view.set_tracks(_album("A", "First", 3))
    _pump(root, 0.2)
    assert view.magnifier.winfo_manager() == "place" and not view.clear_button.winfo_manager()
    view.query.set("song 2")
    _pump(root, 0.3)
    assert view.clear_button.winfo_manager() == "place"
    assert [r for r in view.tree.get_children() if r.isdigit()] == ["1"]
    view.clear_button.event_generate("<ButtonRelease-1>")
    _pump(root, 0.3)
    assert view.query.get() == "" and not view.clear_button.winfo_manager()
    assert len([r for r in view.tree.get_children() if r.isdigit()]) == 3
    assert root.errors == []


# -- the main window --------------------------------------------------------------------------
def test_seek_bar_times_play_button_and_volume_pill(make_window):
    window, root, player, errors = make_window(volume=0.5)
    assert isinstance(window.seek, controls.Slider)
    assert (window.seek.value, window.seek.maximum) == (42.0, 200.0)       # FakePlayer: 0:42 of 3:20
    assert window.pos_label.cget("text") == "0:42" and window.remain_label.cget("text") == "−2:38"
    assert window.play_button.playing                                       # playing → shows pause
    assert window.volume_badge.winfo_manager() == "place" and window.volume_badge.cget("text") == "50%"

    window._seek_dragged(100.0)                                             # dragging: the times follow
    assert window.pos_label.cget("text") == "1:40" and window.remain_label.cget("text") == "−1:40"
    window._seek_released(100.0)                                            # let go on the playing tab
    assert player.calls == ["seek"]
    assert errors == []


def test_header_seek_bar_and_transport_line_up(make_window):
    """The seek track runs exactly from content edge to content edge (the search box's edges); the
    times start and end there too; the ⓘ's slot reaches into the margin; the volume badge is a
    24 px pill; previous / next are drawn pictures."""
    window, root, player, errors = make_window(volume=0.5)
    _pump(root, 0.3)
    seek = window.seek
    inset = round(seek.track_inset())
    search = window.playlist.search_row   # (0.40.0: the box and the Whole library switch share the row)
    left = seek.winfo_rootx() + inset
    right = seek.winfo_rootx() + seek.winfo_width() - inset
    assert abs(left - search.winfo_rootx()) <= 1
    assert abs(right - (search.winfo_rootx() + search.winfo_width())) <= 1
    assert abs(window.pos_label.winfo_rootx() - left) <= 1
    assert abs(window.remain_label.winfo_rootx() + window.remain_label.winfo_width() - right) <= 1
    info = window.info_button
    assert info.winfo_rootx() + info.winfo_width() == right + theme.px(INFO_INK_INSET)
    assert window.volume_badge.winfo_reqheight() == theme.px(theme.CONTROL_H_SMALL)
    assert window.title_label.cget("style") == "Display.TLabel"
    assert window.sub_label.cget("style") == "Subtitle.TLabel"
    for button in (window.previous_button, window.next_button):
        assert button.cget("text") == "" and str(button.cget("image"))
    assert errors == []


def test_the_status_dot_is_drawn_and_keeps_its_tooltip_and_click(make_window):
    window, root, player, errors = make_window()
    shown = []
    window.show_details = lambda: shown.append(1)   # (the dot's click looks show_details up when clicked)
    window._paint_status_dot("WARN")
    middle = theme.px(STATUS_DOT) // 2
    assert _near(_pixel(window.status_dot, middle, middle), "WARN")
    assert _near(_pixel(window.status_dot, 0, 0), "BG")                    # round, on the window
    window._status_detail = "Heads up: resampled"
    tips = [t for t in _tooltips(window.status_dot)]
    assert tips and tips[0]() == "Heads up: resampled"
    window._paint_status_dot("BG")                                          # nothing to say: no dot
    assert _pixel(window.status_dot, middle, middle) == theme.BG
    window.status_dot.event_generate("<Button-1>")
    assert shown == [1] and errors == []


def _tooltips(widget):
    """The texts of the Tooltips on `widget` (found among Python's live objects)."""
    import gc as collector

    from ironbark.tabbar import Tooltip

    return [t.text for t in collector.get_objects() if isinstance(t, Tooltip) and t.widget is widget]


def test_the_status_line_keeps_one_height(make_window):
    window, root, player, errors = make_window()
    _pump(root, 0.2)
    before = window.status_row.winfo_height()
    window.status_label.configure(text="新しい音楽 · 새 음악 · ♪ New music")   # fallback fonts are taller
    _pump(root, 0.2)
    assert window.status_row.winfo_height() == before > 0
    window.status_label.configure(text="")
    _pump(root, 0.2)
    assert window.status_row.winfo_height() == before and errors == []


def test_seek_bar_on_another_tab_sets_its_resume_point(make_window):
    from ironbark.ui import Tab

    window, root, player, errors = make_window()
    other = Track(window.tabs[0].tracks[0].path, "Other Song", duration=300.0)
    window.tabs.append(Tab("album", str(Path(window.tabs[0].path)), tracks=[other]))
    window.select_tab(1)
    _pump(root, 0.5)
    assert window.seek.value == 0.0 and window.seek.maximum == 300.0     # the viewed tab's song
    window._seek_released(120.0)
    assert player.calls == [] and window.tabs[1].resume_at == 120.0 and window.tabs[1].last_index == 0
    assert errors == []


def test_empty_window_offers_the_library_and_comes_back(make_window):
    window, root, player, errors = make_window()
    before = [str(w) for w in window.body.pack_slaves()]
    opened = []
    window.open_library = lambda: opened.append(1)
    window.empty_button.command = window.open_library
    tab = window.tabs[0]
    window.close_tab(0)
    _pump(root, 0.4)
    assert window.empty_view.winfo_manager() == "pack" and window.empty_view.winfo_ismapped()
    assert not window.head.winfo_manager() and not window.playlist.frame.winfo_manager()
    assert window.status_label.winfo_manager() == "pack"                 # "Closed … reopen" still shows
    assert "Ctrl+L" in window.empty_hint.cget("text")
    window.empty_button._state(pressed=True)
    window.empty_button._release(_Event(3, 3))
    assert opened == [1]

    window.reopen_tab()
    _pump(root, 0.4)
    assert window.tabs == [tab] and not window.empty_view.winfo_manager()
    assert [str(w) for w in window.body.pack_slaves()] == before         # everything back in its place
    assert errors == []


def test_empty_window_and_new_controls_follow_the_theme(make_window):
    from test_light_mini import _colours_in_use, _only_in

    window, root, player, errors = make_window()
    window.close_tab(0)
    _pump(root, 0.3)
    window.theme_box.current(1)
    window._theme_chosen()
    _pump(root, 0.5)
    assert theme.current is theme.LIGHT
    assert _colours_in_use(root) & _only_in(theme.DARK, theme.LIGHT) == set()
    assert window.empty_button.cget("bg") == theme.LIGHT.BG and window.seek.cget("bg") == theme.LIGHT.BG
    assert errors == []

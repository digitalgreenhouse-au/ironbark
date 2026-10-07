"""Drawn controls shared by the windows (0.20.0): the slim slider (the main window's seek bar,
the mini strip's, and later the Settings volume), the round play button, round-hover icon
buttons, pill buttons (the volume badge, "Open your library"), the slim scrollbar that only
shows when it's needed, and the cover helpers (softly rounded corners, the quiet music-note
placeholder).

Everything round is drawn with Pillow at SUPERSAMPLE× and scaled down, so edges are smooth (a
Tk canvas draws jagged circles on Windows — the same reason tabbar.Toggle is a picture). The
pictures take their colours from theme.py at the moment they're drawn and are drawn again on a
palette change (`theme.on_change`), so they follow Light / Dark like everything else. Pictures
are cached per Tk root (a Tk PhotoImage belongs to the root it was made in and can't be used in another).

(0.21.0) Also: the Settings volume slider (a Slider on SURFACE), pill buttons that can be greyed
out (`PillButton.set_enabled`), and `ScrollArea` — a panel that scrolls (wheel + slim scrollbar)
when its content is taller than it: Settings, and menus too long for their window.

(0.22.0) And the pieces that bring the other windows (Now Playing, Library, Meters, PCM stream, the
analysis and test windows, Edit tags) up to the main window's finish: `section` (Settings' heading
style), `FlatButton` (a flat pill that speaks enough of ttk.Button's language to replace it),
`ChoiceRow` (choices as a row of words, instead of radio buttons), `Switch` (Settings' slider switch
with its words, instead of a tick box), `SearchBox` (the track list's search box, now shared with the
Library) and a slim horizontal scrollbar (`AutoScrollbar(orient="horizontal")`).

The rounded look: one pill family for every button with words (`PillButton` kinds primary / plain /
danger / badge, CONTROL_H or CONTROL_H_SMALL tall), slim scrollbars whose thumb is a rounded pill and
that can sit in a list's margin (`AutoScrollbar.in_margin`), and keyboard access for every drawn
control (keyfocus.Focusable: Tab reaches it, Enter / Space press it, the arrow keys move a slider or a
row of choices, and a DG-green ring shows where the keyboard is).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from PIL import Image, ImageDraw, ImageTk

from . import keyfocus, shapes, theme
from .keyfocus import Focusable
from .tabbar import (DISABLED_FADE, IconButton, RoundIconButton, Toggle, Tooltip, glyph, icon_font,  # noqa: F401
                     released_over)

SUPERSAMPLE = shapes.SUPERSAMPLE

SLIDER_HEIGHT = 16      # px at 100%: the slider's strip (the bar itself is a quarter of that)
PLAY_DIAMETER = 44      # px at 100%: the main window's play button
COVER_RADIUS = theme.RADIUS_M   # px at 100%: the big cover's corners
THUMB_RADIUS = theme.RADIUS_S   # px at 100%: a small cover's corners (the mini strip)

# Shades worked out from the palette (theme.mix), so they follow Light / Dark without new roles.
HOVER_LIGHTEN = 0.14    # a green fill under the pointer: this much toward ON_ACCENT
PRESS_DARKEN = 0.18     # …pressed (or a dark button under the pointer): this much toward ACCENT_INK
PLACEHOLDER_INK = 0.45  # the placeholder's note: this far from SURFACE toward MUTED (quiet on purpose)


# The per-root picture cache lives in shapes.py (shared with the tab strip).
_cache = shapes.cache
_photo = shapes.photo


def _measuring_font(widget: tk.Misc, font) -> tkfont.Font:
    """A Font to measure text in (a pill's width): one per Tk root and font, shared by every
    button. A Font of each button's own is deleted, with a Tk call, whenever its button is
    freed, which can happen on any thread (Python's cycle collector) — and Tk calls belong on
    the main one."""
    root = widget._root()
    fonts = getattr(root, "_dg_measure_fonts", None)
    if fonts is None:
        fonts = root._dg_measure_fonts = {}
    key = tuple(font) if isinstance(font, list) else font
    if key not in fonts:
        fonts[key] = tkfont.Font(root=root, font=font)
    return fonts[key]


_down = shapes.down


# -- covers ------------------------------------------------------------------------------------
def corner_radius(size: int, small: bool = False) -> int:
    """Corner radius for a cover `size` px across: softly rounded, never more than a tenth of it."""
    return max(1, min(theme.px(THUMB_RADIUS if small else COVER_RADIUS), size // 10))


_corner_masks: dict[int, Image.Image] = {}


def round_corners(image: Image.Image, radius: int, bg: str) -> Image.Image:
    """A copy of `image` with its four corners rounded off into `bg` (antialiased). Only the
    corners are touched, so even a big cover costs next to nothing."""
    image = image.convert("RGB")
    w, h = image.size
    radius = min(radius, w // 2, h // 2)
    if radius < 1:
        return image.copy()
    if radius not in _corner_masks:   # the top-left quarter circle, opaque inside
        big = Image.new("L", (radius * 2 * SUPERSAMPLE,) * 2, 0)
        ImageDraw.Draw(big).ellipse((0, 0, big.width - 1, big.height - 1), fill=255)
        _corner_masks[radius] = _down(big, (radius * 2,) * 2).crop((0, 0, radius, radius))
    mask = _corner_masks[radius]
    out = image.copy()
    fill = Image.new("RGB", (radius, radius), theme.colour(bg))
    for flip, (x, y) in ((None, (0, 0)), (Image.Transpose.FLIP_LEFT_RIGHT, (w - radius, 0)),
                         (Image.Transpose.FLIP_TOP_BOTTOM, (0, h - radius)),
                         (Image.Transpose.ROTATE_180, (w - radius, h - radius))):
        corner_mask = mask if flip is None else mask.transpose(flip)
        piece = out.crop((x, y, x + radius, y + radius))
        out.paste(Image.composite(piece, fill, corner_mask), (x, y))
    return out


def note_mark(size: int, colour: str, bg: str) -> Image.Image:
    """A beamed pair of quavers (♫), `size` px square, drawn — not a font glyph, so it looks the
    same on every PC (and the Mac build). Used for the cover placeholder and the empty window."""
    size = max(8, size)
    k = SUPERSAMPLE
    s = size * k
    image = Image.new("RGB", (s, s), theme.colour(bg))
    d = ImageDraw.Draw(image)
    ink = theme.colour(colour)
    u = s / 100                                       # drawn on a 100-unit square
    stem = max(1, round(7 * u))
    heads = ((30, 76), (74, 66))                      # note-head centres
    rx, ry = 13 * u, 10 * u
    for cx, cy in heads:
        d.ellipse((cx * u - rx, cy * u - ry, cx * u + rx, cy * u + ry), fill=ink)
    tops = (24, 14)                                   # where each stem meets the beam
    for (cx, cy), top in zip(heads, tops):
        x = cx * u + rx - stem
        d.rectangle((x, top * u, x + stem, cy * u), fill=ink)
    x0, x1 = heads[0][0] * u + rx - stem, heads[1][0] * u + rx
    d.polygon([(x0, tops[0] * u), (x1, tops[1] * u), (x1, tops[1] * u + 13 * u), (x0, tops[0] * u + 13 * u)], fill=ink)
    return _down(image, (size, size))


def placeholder(size: int) -> Image.Image:
    """The quiet stand-in for a missing cover: a SURFACE square with a faint ♫, same size as a
    cover would be (so nothing shifts when one turns up)."""
    image = Image.new("RGB", (size, size), theme.SURFACE)
    mark = max(8, int(size * 0.3))
    note = note_mark(mark, theme.mix("SURFACE", "MUTED", PLACEHOLDER_INK), "SURFACE")
    image.paste(note, ((size - mark) // 2, (size - mark) // 2))
    return image


def matted(image: Image.Image, side: int) -> Image.Image:
    """A cover smaller than its slot, shown at its own size (never enlarged) in the middle of a mat
    `side` px square: SURFACE with a 1 px BORDER edge and rounded corners (on BG), so the slot keeps
    its size and the mat still shows on the Light theme. The cover's own corners are rounded too."""
    radius = corner_radius(side)
    edge = max(1, theme.px(1))
    b = radius + edge
    plain = shapes.rounded(2 * b + 4, 2 * b + 4, radius, "SURFACE", "BORDER", edge, bg="BG")
    mat = shapes.widened(plain.convert("RGB"), (b, b, b, b), side, side)
    cover = round_corners(image, corner_radius(min(image.size)), "SURFACE")
    mat.paste(cover, ((side - cover.width) // 2, (side - cover.height) // 2))
    return mat


def small_icon(widget: tk.Misc, kind: str, size: int, colour: str, bg: str) -> ImageTk.PhotoImage:
    """A small drawn icon (cached): "search" (a magnifier) or "clear" (a ×), `size` real px,
    `colour` / `bg` theme roles."""
    ink, back = theme.colour(colour), theme.colour(bg)

    def draw() -> Image.Image:
        k = SUPERSAMPLE
        s = size * k
        image = Image.new("RGB", (s, s), back)
        d = ImageDraw.Draw(image)
        line = max(k, round(s * 0.12))
        if kind == "search":
            r = s * 0.3
            cx = cy = s * 0.42
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ink, width=line)
            a = cx + r * 0.72
            d.line((a, a, s * 0.88, s * 0.88), fill=ink, width=round(line * 1.15))
        else:
            m = s * 0.24
            d.line((m, m, s - m, s - m), fill=ink, width=line)
            d.line((m, s - m, s - m, m), fill=ink, width=line)
        return _down(image, (size, size))

    return _photo(widget, ("icon", kind, size, ink, back), draw)


# -- slider --------------------------------------------------------------------------------------
class Slider(Focusable, tk.Canvas):
    """A slim slider: a thin rounded track, the part up to the value in DG green, a round knob
    that grows under the pointer. Click anywhere to jump there, or drag. `on_drag(value)` hears
    every movement (for showing the time being dragged to), `on_release(value)` the final one.
    From the keyboard: ← / → (or ↓ / ↑) move it by `step` (None: a twentieth of the way), Page Up /
    Page Down by four steps, Home / End to the ends.

    The seek bars (main window, mini strip) use it; `set(value, maximum)` is held back while it's
    being dragged, so the knob stays under the pointer, and shown when the drag ends. If the
    maximum changed meanwhile (the next song started), letting go doesn't seek: the spot was
    chosen in a song that has gone."""

    BUTTON1 = 0x100   # Tk's event state bit: the left button is held down

    def __init__(self, parent, *, on_release: Callable[[float], None],
                 on_drag: Callable[[float], None] | None = None, bg: str = "BG", height: int = SLIDER_HEIGHT,
                 step: float | None = None):
        self._height = max(8, theme.px(height))
        super().__init__(parent, height=self._height, width=1, bg=theme.colour(bg), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.on_release, self.on_drag = on_release, on_drag
        self.step = step
        self._bg = bg
        self.value = 0.0
        self.maximum = 0.0
        self.dragging = False
        self.hover = False
        self.band: tuple = ()     # ((from, to), …) as fractions: shown as a faint band (see set_band)
        self._held: tuple[float, float] | None = None   # the latest set() while dragged
        self._drawn = None        # what's on show: (width, knob x, hover, colours)
        self._photo = None
        self._item = self.create_image(0, 0, anchor="nw")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", self._left)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        # A drag whose release went elsewhere (a box took the mouse mid-drag): the pointer moving
        # with the button up, or the bar being hidden, ends it — or the bar would stop following.
        self.bind("<Motion>", lambda e: self._drag_lost())
        self.bind("<Unmap>", lambda e: self._drag_lost())
        theme.on_change(self._palette_changed)
        self._keyboard(keys=False)
        for key, steps in (("<Left>", -1), ("<Down>", -1), ("<Right>", 1), ("<Up>", 1), ("<Prior>", 4),
                           ("<Next>", -4), ("<Home>", "start"), ("<End>", "end")):
            self.bind(key, lambda e, n=steps: self.nudge(n))

    def activate(self):
        pass

    def focus_changed(self):
        self._drawn = None
        self._draw()

    def nudge(self, steps) -> str:
        """A key moved it: `steps` steps along (negative: back), or to "start" / "end"."""
        if self.maximum <= 0 or self.dragging:
            return "break"
        if steps in ("start", "end"):
            value = 0.0 if steps == "start" else self.maximum
        else:
            value = self.value + steps * (self.step or self.maximum / 20)
        self.value = max(0.0, min(self.maximum, value))
        self._draw()
        if self.on_drag:
            self.on_drag(self.value)
        self.on_release(self.value)
        return "break"   # (the window's own use of the key — seeking, say — doesn't act as well)

    # The knob's sizes and the track's thickness, as shares of the strip's height.
    @property
    def _radii(self) -> tuple[float, float]:
        return self._height * 0.3, self._height * 0.45   # at rest, under the pointer

    def track_inset(self) -> float:
        """How far in from each side the track's rounded ends are drawn (real px): room for the
        knob's centre to sit on the ends. A slider that should look edge to edge with its
        neighbours is made this much wider on each side."""
        return self._radii[1] - max(1.0, self._height * SUPERSAMPLE * 0.125) / SUPERSAMPLE

    def set(self, value: float, maximum: float):
        """Show `value` of `maximum` (maximum 0: nothing to show — the track alone, no knob)."""
        if self.dragging:
            self._held = (value, maximum)   # shown when the drag ends
            return
        if (value, maximum) != (self.value, self.maximum):
            self.value, self.maximum = value, maximum
            self._draw()

    def set_band(self, spans) -> None:
        """A faint band along the track over the parts that are on this computer (a network song's
        downloaded stretches, as fractions of it — like a video player's buffered bar); () for none."""
        band = tuple((max(0.0, min(1.0, a)), max(0.0, min(1.0, b))) for a, b in (spans or ()) if b > a)
        if band != self.band:
            self.band = band
            self._draw()

    def fraction(self) -> float:
        return max(0.0, min(1.0, self.value / self.maximum)) if self.maximum > 0 else 0.0

    def _value_at(self, x: float) -> float:
        inset = self._radii[1]
        width = max(1.0, self.winfo_width() - 2 * inset)
        return max(0.0, min(1.0, (x - inset) / width)) * self.maximum

    def _hover(self, on: bool):
        self.hover = on
        self._draw()

    def _left(self, event):
        if self.dragging and not event.state & self.BUTTON1:
            self._drag_lost()
        self._hover(False)

    def _press(self, event):
        if self.maximum <= 0:
            return
        self.dragging = True
        self._held = None
        self.value = self._value_at(event.x)
        self._draw()
        if self.on_drag:
            self.on_drag(self.value)

    def _move(self, event):
        if self.dragging:
            self.value = self._value_at(event.x)
            self._draw()
            if self.on_drag:
                self.on_drag(self.value)

    def _release(self, event):
        if self.dragging:
            held = self._held
            if held is not None and held[1] != self.maximum:   # another song now: no seek
                self._drag_lost()
                return
            self.dragging = False
            self._held = None
            self.value = self._value_at(event.x)
            self._draw()
            self.on_release(self.value)

    def _drag_lost(self):
        """End a drag without acting on it, and show what set() said meanwhile."""
        if not self.dragging:
            return
        self.dragging = False
        held, self._held = self._held, None
        if held is not None:
            self.value, self.maximum = held
        self._draw()

    def _palette_changed(self, _palette):
        self.configure(bg=theme.colour(self._bg))
        self._drawn = None
        self._draw()

    def _draw(self):
        width, height = self.winfo_width(), self._height
        if width < 8:
            return
        rest, big = self._radii
        knob = big if (self.hover or self.dragging) else rest
        has_knob = self.maximum > 0
        x = big + (width - 2 * big) * self.fraction()
        colours = (theme.colour(self._bg), theme.BORDER, theme.ACCENT, theme.MUTED)
        band = tuple((round(a * width), round(b * width)) for a, b in self.band)
        ring = keyfocus.ring_width() if self.focused else 0
        key = (width, height, round(x * SUPERSAMPLE) if has_knob else -1, knob if has_knob else 0, colours, band,
               ring)
        if key == self._drawn:
            return
        self._drawn = key

        def draw() -> Image.Image:
            k = SUPERSAMPLE
            image = Image.new("RGB", (width * k, height * k), colours[0])
            d = ImageDraw.Draw(image)
            middle = height * k / 2
            half = max(1.0, height * k * 0.125)          # the track: a quarter of the strip's height
            left, right = big * k - half, (width - big) * k + half
            d.rounded_rectangle((left, middle - half, right, middle + half), radius=half, fill=colours[1])
            for a, b in self.band:   # downloaded stretches, between the track and the part played
                xa = (big + (width - 2 * big) * a) * k
                xb = (big + (width - 2 * big) * b) * k
                d.rounded_rectangle((max(left, xa - half), middle - half, min(right, xb + half), middle + half),
                                    radius=half, fill=colours[3])
            if has_knob:
                xk = x * k
                if xk > left + half:
                    d.rounded_rectangle((left, middle - half, xk, middle + half), radius=half, fill=colours[2])
                r = knob * k
                d.ellipse((xk - r, middle - r, xk + r, middle + r), fill=colours[2])
            if ring:   # the keyboard is here: a DG-green ring round the whole bar
                d.rounded_rectangle((0, 0, width * k - 1, height * k - 1), radius=height * k // 2,
                                    outline=colours[2], width=ring * k)
            return _down(image, (width, height))

        # Not cached: a seek bar rarely shows the same spot twice.
        self._photo = ImageTk.PhotoImage(draw(), master=self)
        self.itemconfigure(self._item, image=self._photo)


# -- buttons -------------------------------------------------------------------------------------
class _Pressable(tk.Label):
    """A picture button's hover / pressed bookkeeping: `_paint()` draws the current state;
    the command runs when the button is let go over it."""

    def __init__(self, parent, command: Callable[[], None], bg: str, label: str = "", **options):
        super().__init__(parent, bg=theme.colour(bg), bd=0, highlightthickness=0, padx=0, pady=0, cursor="hand2",
                         **options)
        self.command = command
        self._bg = bg
        self.hover = False
        self.pressed = False
        self.bind("<Enter>", lambda e: self._state(hover=True))
        self.bind("<Leave>", lambda e: self._state(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._state(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.tip = Tooltip(self, label) if label else None
        theme.on_change(self._palette_changed)
        self.bind("<Destroy>", self._forget_command, add="+")

    def _forget_command(self, event):
        """(0.22.0) Once destroyed, let go of the command: it's often a method of the window that made
        the button, and holding it would keep that window alive until Python's cycle collector runs —
        possibly on another thread, where a Tk variable's clean-up fails ("main thread is not in main
        loop"). Its picture goes too, here on Tk's thread: deleting a picture is a Tk call."""
        if event.widget is self:
            self.command = lambda: None
            self._photo = None

    def _state(self, **changes):
        for name, value in changes.items():
            setattr(self, name, value)
        self._paint()

    def _release(self, event):
        was = self.pressed
        self._state(pressed=False)
        if was and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.command()

    def _palette_changed(self, _palette):
        self.configure(bg=theme.colour(self._bg))
        self._paint()

    def _paint(self):
        raise NotImplementedError


class PlayButton(Focusable, _Pressable):
    """The play / pause button: a smooth filled DG-green circle with a dark glyph. Lighter under
    the pointer, darker while pressed. `set_playing(True)` shows pause. Its picture keeps `margin`
    px round the circle for the focus ring (the circle itself is DG green), so pack it with that
    much less padding."""

    def __init__(self, parent, command: Callable[[], None], diameter: int = PLAY_DIAMETER, bg: str = "BG",
                 label: str = ""):
        self.diameter = max(16, theme.px(diameter))
        self.margin = keyfocus.ring_room()
        self.playing = False
        super().__init__(parent, command, bg, label)
        self._keyboard()
        self._paint()

    def activate(self):
        self.command()

    def focus_changed(self):
        self._paint()

    def set_playing(self, playing: bool):
        if playing != self.playing:
            self.playing = playing
            self._paint()

    def fill(self) -> str:
        """The circle's colour in the current state."""
        if self.pressed:
            return theme.mix("ACCENT", "ACCENT_INK", PRESS_DARKEN)
        if self.hover:
            return theme.mix("ACCENT", "ON_ACCENT", HOVER_LIGHTEN)
        return theme.ACCENT

    def _paint(self):
        d, playing, m = self.diameter, self.playing, self.margin
        colours = (theme.colour(self._bg), self.fill(), theme.ACCENT_INK)
        ring = keyfocus.ring_width() if self.focused else 0

        def draw() -> Image.Image:
            k = SUPERSAMPLE
            s = d * k
            image = Image.new("RGB", (s, s), colours[0])
            g = ImageDraw.Draw(image)
            g.ellipse((0, 0, s - 1, s - 1), fill=colours[1])
            c = s / 2
            if playing:   # two rounded bars
                bar_w, bar_h, gap = s * 0.1, s * 0.34, s * 0.09
                for x in (c - gap / 2 - bar_w, c + gap / 2):
                    g.rounded_rectangle((x, c - bar_h / 2, x + bar_w, c + bar_h / 2), radius=bar_w * 0.3,
                                        fill=colours[2])
            else:         # a triangle, nudged right so it looks centred (its weight is on the left)
                h, w = s * 0.36, s * 0.31
                left = c - w / 2 + s * 0.035
                g.polygon([(left, c - h / 2), (left + w, c), (left, c + h / 2)], fill=colours[2])
            whole = Image.new("RGB", ((d + 2 * m) * k,) * 2, colours[0])
            whole.paste(image, (m * k, m * k))
            if ring:
                ImageDraw.Draw(whole).ellipse((0, 0, whole.width - 1, whole.height - 1), outline=theme.ACCENT,
                                              width=ring * k)
            return _down(whole, (d + 2 * m, d + 2 * m))

        self._photo = _photo(self, ("play", d, m, playing, ring) + colours, draw)
        self.configure(image=self._photo)


_UNSET = object()   # a PillButton colour not given: the kind's own


class PillButton(Focusable, _Pressable):
    """A rounded pill with words: one family for every button with words in the app. `kind`:

    - "primary": SELECT green with ON_ACCENT words, a shade darker under the pointer — the one thing a
      window or box is for ("Open your library", Save, Compare, a prompt's default);
    - "plain": SURFACE_HI with an EDGE ring, a touch lighter under the pointer, SURFACE_DOWN pressed;
    - "danger": a DANGER ring with ordinary words (red words on the hover grey would fail WCAG AA);
    - "badge": a WARN ring and WARN words (the volume badge).

    CONTROL_H tall, or CONTROL_H_SMALL with `small=True` (badges, Settings' buttons); the words
    PILL_PAD (PILL_PAD_SMALL) in from the ends. `fill` / `fg` / `outline` / `hover_fill` /
    `pressed_fill` override the kind's colours: theme roles, None (no fill: the background shows; no
    ring), or theme.tone mixes. `set_text()` re-sizes it to fit; `fit` = never narrower than that text.
    The keyboard reaches it (keyfocus): Enter / Space press it, a DG-green ring shows it has the
    keyboard; greyed out (`set_enabled(False)`), Tab passes it by."""

    KINDS = {   # fill, words, under the pointer, pressed, ring (None: a shade of the fill / none)
        "primary": ("SELECT", "ON_ACCENT", None, None, None),
        "plain": ("SURFACE_HI", "TEXT", ("SURFACE_HI", "TEXT", 0.06), "SURFACE_DOWN", "EDGE"),
        "danger": (None, "TEXT", "SURFACE_HI", "SURFACE_DOWN", "DANGER"),
        "badge": (None, "WARN", "SURFACE_HI", "SURFACE_DOWN", "WARN"),
    }

    def __init__(self, parent, text: str, command: Callable[[], None], *, kind: str = "primary",
                 small: bool = False, fill=_UNSET, fg=_UNSET, outline=_UNSET, hover_fill=_UNSET,
                 pressed_fill=_UNSET, font: tuple | None = None, bg: str = "BG", pad_x: int | None = None,
                 label: str = "", fit: str = "", height: int | None = None):
        look = dict(zip(("fill", "fg", "hover_fill", "pressed_fill", "outline"), self.KINDS[kind]))
        for name, value in (("fill", fill), ("fg", fg), ("outline", outline), ("hover_fill", hover_fill),
                            ("pressed_fill", pressed_fill)):
            if value is not _UNSET:
                look[name] = value
        self.kind = kind
        self._fill, self._fg, self._outline = look["fill"], look["fg"], look["outline"]
        self._hover_fill, self._pressed_fill = look["hover_fill"], look["pressed_fill"]
        self._height = height or (theme.CONTROL_H_SMALL if small else theme.CONTROL_H)
        self._fit = fit   # (0.22.0) never narrower than this text: a label that changes doesn't resize it
        self._font = font or theme.font(theme.SIZE_BODY)
        self._pad = pad_x if pad_x is not None else (theme.PILL_PAD_SMALL if small else theme.PILL_PAD)
        self.text_value = text
        self.enabled = True
        super().__init__(parent, command, bg, label, text=text, compound="center", font=self._font,
                         fg=theme.colour(look["fg"]))
        self._measure = _measuring_font(self, self._font)
        self._keyboard()
        self._paint()

    def activate(self):
        if self.enabled:
            self.command()

    def can_focus(self) -> bool:
        return self.enabled

    def focus_changed(self):
        self._paint()

    def set_text(self, text: str):
        if text != self.text_value:
            self.text_value = text
            self.configure(text=text)
            self._paint()

    def set_enabled(self, enabled: bool):
        """(0.21.0) Greyed out (muted text, no hover, does nothing, Tab passes it by) or back to normal."""
        if enabled != self.enabled:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
            self.set_focusable(enabled)
            self._state(hover=False, pressed=False)

    def _release(self, event):
        if self.enabled:
            super()._release(event)
        else:
            self._state(pressed=False)

    def _state(self, **changes):
        if not self.enabled:
            changes = {k: False for k in changes}   # a greyed-out pill never lights up
        super()._state(**changes)

    def fill_colour(self) -> str | None:
        """The pill's colour in the current state (None: unfilled)."""
        base = self._fill
        if self.pressed and self._pressed_fill:
            return theme.tone(self._pressed_fill)
        if self.hover or self.pressed:
            if self._hover_fill:
                return theme.tone(self._hover_fill)
            if base:
                return theme.mix(base, "ACCENT_INK", PRESS_DARKEN * (1.6 if self.pressed else 1.0))
        return theme.tone(base) if base else None

    def _paint(self):
        measure = self._measure
        w = max(measure.measure(self.text_value or " "), measure.measure(self._fit)) + 2 * theme.px(self._pad)
        h = theme.px(self._height)
        line = max(1, theme.px(1))
        outline = theme.colour(self._outline) if self._outline else None
        if self.focused:
            outline, line = theme.ACCENT, keyfocus.ring_width()
        colours = (theme.colour(self._bg), self.fill_colour(), outline)

        def draw() -> Image.Image:
            k = SUPERSAMPLE
            image = Image.new("RGB", (w * k, h * k), colours[0])
            ImageDraw.Draw(image).rounded_rectangle(
                (0, 0, w * k - 1, h * k - 1), radius=h * k // 2, fill=colours[1] or colours[0],
                outline=colours[2], width=line * k if colours[2] else 0)
            return _down(image, (w, h))

        self._photo = _photo(self, ("pill", w, h, line) + colours, draw)
        fg = theme.colour(self._fg) if self.enabled else theme.mix("MUTED", self._fill or self._bg, DISABLED_FADE)
        self.configure(image=self._photo, fg=fg, bg=colours[0])


# -- scrollbar -----------------------------------------------------------------------------------
class AutoScrollbar(ttk.Scrollbar):
    """The slim scrollbar (theme.SLIM_SCROLLBAR: no arrow buttons, a rounded thumb), shown only while
    the list is longer than its window. Give it its packing with `auto_pack(...)` (or its place with
    `auto_place(...)` / `in_margin(...)`) instead of `pack(...)`; packed the ordinary way it's always
    there. (0.22.0) `orient="horizontal"`: the same, across. `bg="SURFACE"`: on a panel or card."""

    def __init__(self, parent, orient: str = "vertical", bg: str = "BG", **options):
        style = (theme.SLIM_HSCROLLBAR if orient == "horizontal"
                 else theme.CARD_SCROLLBAR if bg == "SURFACE" else theme.SLIM_SCROLLBAR)
        super().__init__(parent, orient=orient, style=style, **options)
        self._layout: dict | None = None
        self._placed = False
        self.needed = False

    def auto_pack(self, **options):
        self._layout, self._placed = options, False
        if self.needed:
            self.pack(**options)

    def auto_place(self, **options):
        self._layout, self._placed = options, True
        if self.needed:
            self.place(**options)

    def in_margin(self, target: tk.Misc, gap: int = theme.GAP_HAIR):
        """Sit in the margin right of `target` (a list), its own width wide and as tall as the list, so
        the list keeps its full width and its words line up with what's above it. The scrollbar must
        be a child of the frame whose padding is that margin (target inside it)."""
        self.auto_place(in_=target, relx=1.0, x=theme.px(gap), y=0, relheight=1.0, anchor="nw")

    def set(self, first, last):
        needed = float(first) > 0.0 or float(last) < 1.0
        if needed != self.needed:
            self.needed = needed
            if self._layout is not None:
                if needed:
                    (self.place if self._placed else self.pack)(**self._layout)
                    if self._placed:
                        self.lift()
                else:
                    (self.place_forget if self._placed else self.pack_forget)()
        super().set(first, last)


class ScrollArea(tk.Frame):
    """(0.21.0) A panel whose content scrolls up and down when it's taller than the panel: the
    mouse wheel anywhere over it, or the slim scrollbar (shown only while it's needed). Put the
    content in `.inner`. Used by Settings (about 1300 px tall at 150% display scaling) and by
    right-click menus too long for their window (tabbar.Popover(scroll=True))."""

    WHEEL_UNITS = 3     # scroll steps per notch of the wheel
    STEP = 20           # px at 100%: one scroll step

    def __init__(self, parent, bg: str = "SURFACE"):
        super().__init__(parent, bg=theme.colour(bg), bd=0, highlightthickness=0)
        self.canvas = tk.Canvas(self, bg=theme.colour(bg), bd=0, highlightthickness=0, width=1, height=1,
                                yscrollincrement=max(1, theme.px(self.STEP)))
        self.scrollbar = AutoScrollbar(self, command=self.canvas.yview, bg=bg)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.auto_pack(side="right", fill="y", before=self.canvas)
        self.inner = tk.Frame(self.canvas, bg=theme.colour(bg), bd=0, highlightthickness=0)
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self._content_changed(), add="+")
        self.canvas.bind("<Configure>", self._resized, add="+")
        # The wheel works over every widget inside: each gets this tag in its bindtags (bind_wheel).
        # One tag for every scroll panel, bound once: a class binding lasts as long as Tk does, so
        # one per panel (a menu is made per right-click) would keep each panel alive for good.
        self.wheel_tag = "DGScroll"
        root = self._root()
        if not getattr(root, "_dg_scroll_bound", False):
            root._dg_scroll_bound = True
            self.bind_class(self.wheel_tag, "<MouseWheel>", ScrollArea._wheel_anywhere)

    @staticmethod
    def _wheel_anywhere(event):
        """The wheel over a widget inside some scroll panel: that panel scrolls."""
        widget = event.widget
        while isinstance(widget, tk.Misc):
            if isinstance(widget, ScrollArea):
                return widget._wheel(event)
            widget = widget.master
        return None

    def _resized(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)   # the content is as wide as the panel
        self._content_changed()

    def _content_changed(self):
        height = max(self.inner.winfo_reqheight(), 1)
        self.canvas.configure(scrollregion=(0, 0, max(self.canvas.winfo_width(), 1), height))

    def bind_wheel(self):
        """Give every widget inside (and the panel itself) the wheel. Call after adding content.
        The tag goes straight after each widget's own, and the handler stops there — so the wheel
        never also turns a drop-down list's choice (the Output box) while scrolling past it."""
        stack = [self, self.canvas, self.inner]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            tags = w.bindtags()
            if self.wheel_tag not in tags:
                w.bindtags((tags[0], self.wheel_tag) + tags[1:])

    def needed(self) -> bool:
        """Is the content taller than the panel (so it scrolls)?"""
        self.update_idletasks()
        return self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1

    def _wheel(self, event):
        if self.needed():
            units = int(-event.delta / 120 * self.WHEEL_UNITS) or (-1 if event.delta > 0 else 1)
            self.canvas.yview_scroll(units, "units")
        return "break"

    def scroll_to(self, fraction: float):
        """Scroll so `fraction` (0 = top, 1 = bottom) of the content is above the view."""
        self.canvas.yview_moveto(fraction)

    def see(self, widget: tk.Misc):
        """Scroll just enough to bring `widget` (inside) fully into view."""
        self.update_idletasks()
        total = max(1, self.inner.winfo_reqheight())
        top = widget.winfo_rooty() - self.inner.winfo_rooty()
        bottom = top + widget.winfo_height()
        view_top = self.canvas.canvasy(0)
        view_h = self.canvas.winfo_height()
        if top < view_top:
            self.canvas.yview_moveto(top / total)
        elif bottom > view_top + view_h:
            # (0.24.0) Tk rounds a scroll down to a whole step (yscrollincrement), which could leave
            # the widget's last pixels just out of view: round up to the next step instead.
            step = max(1, int(float(self.canvas.cget("yscrollincrement")) or 1))
            target = -(-(bottom - view_h) // step) * step
            self.canvas.yview_moveto(min(target, total) / total)
        # Tk turns the fraction into whole scroll steps, and that can still land one step short
        # (seen at 100% display scaling): nudge a step at a time until the widget is wholly in view.
        for _ in range(4):
            self.update_idletasks()
            top = widget.winfo_rooty() - self.canvas.winfo_rooty()
            if top < 0:
                self.canvas.yview_scroll(-1, "units")
            elif top + widget.winfo_height() > self.canvas.winfo_height():
                self.canvas.yview_scroll(1, "units")
            else:
                break

    def visible(self, widget: tk.Misc) -> bool:
        """Is `widget` (inside) wholly in view right now?"""
        self.update_idletasks()
        top = widget.winfo_rooty() - self.canvas.winfo_rooty()
        return top >= 0 and top + widget.winfo_height() <= self.canvas.winfo_height()


# -- the other windows (0.22.0) --------------------------------------------------------------------
def section(parent: tk.Misc, text: str, first: bool = False) -> ttk.Label:
    """A section heading, as Settings has them: small bold capitals in the secondary text colour,
    with a faint line above every section but the first. Packed into `parent`; returns the label."""
    if not first:
        tk.Frame(parent, bg=theme.BORDER, height=1).pack(fill="x", pady=theme.pad(theme.GAP_LARGE, 0))
    label = ttk.Label(parent, text=text.upper(), style="Section.TLabel")
    label.pack(anchor="w", pady=theme.pad(theme.GAP, theme.GAP_SMALL))
    return label


class FlatButton(PillButton):
    """(0.22.0) A window's button, drawn like the rest of the app's: a plain pill (`kind="plain"`),
    or DG green for the one thing the window is for (`"primary"`: Compare, Start the test, Save, Play
    and measure). Replaces ttk.Button in the secondary windows, and speaks enough of its language
    that they didn't otherwise change: `configure(text=…, state=…, command=…)`, `cget`,
    `state([...])`, `instate([...])`, `invoke()`. `small=True`: a compact pill (Now Playing's tools).

    `fit`: a text it's always at least as wide as, for a button whose words change (Freeze /
    Resume, A / ▶ A), so its neighbours don't move."""

    def __init__(self, parent, text: str = "", command: Callable[[], None] | None = None, *, kind: str = "plain",
                 bg: str = "BG", state: str = "normal", label: str = "", fit: str = "", small: bool = False):
        super().__init__(parent, text, command or (lambda: None), kind=kind, small=small, bg=bg, label=label, fit=fit)
        if state == "disabled":
            self.set_enabled(False)

    def set_enabled(self, enabled: bool):
        """Greyed out, any kind looks the same: a plain pill with faded words (a pale-green "Start the
        test" read as half-on)."""
        look = dict(zip(("fill", "fg", "hover_fill", "pressed_fill", "outline"),
                        self.KINDS[self.kind if enabled else "plain"]))
        self._fill, self._outline = look["fill"], look["outline"]
        self._hover_fill, self._pressed_fill = look["hover_fill"], look["pressed_fill"]
        super().set_enabled(enabled)

    def set_text(self, text: str):
        if text != self.text_value:
            self.text_value = text
            tk.Label.configure(self, text=text)
            self._paint()

    # -- ttk.Button's language -------------------------------------------------------------
    def configure(self, cnf=None, **options):
        if cnf is None and not options:     # every option and its value (theme.recolour asks)
            return super().configure()
        if isinstance(cnf, str):            # a question ("which colour?"), not a change
            return self.cget(cnf) if cnf in ("state", "text", "command") else super().configure(cnf)
        if cnf:
            options = {**cnf, **options}
        if "state" in options:
            self.set_enabled(str(options.pop("state")) != "disabled")
        if "text" in options:
            self.set_text(str(options.pop("text")))
        if "command" in options:
            self.command = options.pop("command")
        if options:
            return super().configure(**options)
        return None

    config = configure

    def cget(self, key: str):
        if key == "state":
            return "normal" if self.enabled else "disabled"
        if key == "text":
            return self.text_value
        if key == "command":
            return self.command
        return super().cget(key)

    __getitem__ = cget

    def state(self, spec=None) -> tuple:
        """ttk-style: state(["disabled"]) / state(["!disabled"]); returns the flags now set."""
        for flag in spec or ():
            if flag == "disabled":
                self.set_enabled(False)
            elif flag == "!disabled":
                self.set_enabled(True)
        return () if self.enabled else ("disabled",)

    def instate(self, spec) -> bool:
        flags = {"disabled": not self.enabled, "!disabled": self.enabled}
        return all(flags.get(flag, False) for flag in spec)

    def invoke(self):
        """Press it from code (does nothing while it's greyed out, like a ttk button)."""
        if self.enabled:
            return self.command()
        return None


class ChoiceRow(Focusable, tk.Frame):
    """(0.22.0) A few choices side by side, one of them chosen: words, the chosen one in DG green
    (ACCENT_TEXT) with a short rounded DG-green pill under it, the others muted and lighter under
    the pointer. Replaces stock radio buttons (PCM stream, Spectrogram, Stereo scope, Test signals)
    and is the Library's view switcher. The words keep one font whatever is chosen, so nothing
    shifts when the choice changes.

    `variable` (a StringVar) holds the chosen value — set it from code and the row follows;
    `command()` runs after a click that changes it. `marks`: values shown green although not chosen
    (the Library's "Recently added ●" when there's new music). The keyboard reaches the row
    (keyfocus): ← / → choose the one before or after, and a DG-green ring round the chosen word
    shows the row has the keyboard (each word keeps room round it for that ring)."""

    BAR = (16, 3)   # px at 100%: the pill under the chosen word

    def __init__(self, parent, choices, variable: tk.StringVar, command: Callable[[], None] | None = None,
                 bg: str = "BG"):
        super().__init__(parent, bg=theme.colour(bg))
        self.variable, self.command, self._bg = variable, command, bg
        self.marks: set[str] = set()
        self.labels: dict[str, tk.Label] = {}
        self.bars: dict[str, tk.Label] = {}
        self.values = [value for value, _text in choices]
        self._hover: str | None = None
        self._font = theme.font(theme.SIZE_BODY)
        self._room = keyfocus.ring_room() + theme.px(theme.GAP_HAIR)
        for index, (value, text) in enumerate(choices):
            cell = tk.Frame(self, bg=theme.colour(bg), cursor="hand2")
            # The words sit GAP_WIDE apart (their focus-ring room counted in).
            cell.pack(side="left", padx=(0 if index == 0 else max(0, theme.px(theme.GAP_WIDE) - 2 * self._room), 0))
            label = tk.Label(cell, text=text, bg=theme.colour(bg), font=self._font, cursor="hand2", compound="center",
                             padx=0, pady=0, bd=0)
            label.pack()
            bar = tk.Label(cell, bg=theme.colour(bg), bd=0, padx=0, pady=0)
            bar.pack()
            bar.lit = False
            for widget in (cell, label, bar):
                widget.bind("<ButtonRelease-1>", lambda e, v=value, c=cell: released_over(e, c) and self.choose(v))
                widget.bind("<Enter>", lambda e, v=value: self._hovered(v))
                widget.bind("<Leave>", lambda e: self._hovered(None))
            self.labels[value], self.bars[value] = label, bar
        self._trace = variable.trace_add("write", lambda *_: self.paint())
        self.bind("<Destroy>", self._untrace, add="+")
        theme.on_change(self._palette_changed)
        self._keyboard()
        for key, step in (("<Left>", -1), ("<Up>", -1), ("<Right>", 1), ("<Down>", 1)):
            self.bind(key, lambda e, s=step: self.step(s))
        self.paint()

    def activate(self):
        pass   # (the choice under the keyboard is already the chosen one)

    def focus_changed(self):
        self.paint()

    def step(self, step: int) -> str:
        """The arrow keys: choose the one before (-1) or after (1) the chosen one."""
        values = self.values
        here = self.variable.get()
        index = values.index(here) if here in values else 0
        self.choose(values[max(0, min(len(values) - 1, index + step))])
        return "break"

    def choose(self, value: str):
        """As a click on `value`: choose it, and run the command if that changed anything."""
        if value != self.variable.get():
            self.variable.set(value)
            if self.command:
                self.command()

    def set_text(self, value: str, text: str):
        if self.labels[value].cget("text") != text:
            self.labels[value].configure(text=text)
            self.paint()

    def _hovered(self, value: str | None):
        self._hover = value
        self.paint()

    def _untrace(self, event):
        """Destroyed: stop following the variable and let go of it and of the command. The widgets
        of a closed window are freed by Python's cycle collector, which can run on any thread; a
        Tk variable freed there fails ("main thread is not in main loop"), so none may be left
        hanging off a dead widget (see _Pressable._forget_command)."""
        if event.widget is self and self.variable is not None:
            try:
                self.variable.trace_remove("write", self._trace)
            except tk.TclError:
                pass
            self.variable, self.command = None, None
            self._pictures = {}   # (pictures: let go here, on Tk's thread)

    def _palette_changed(self, _palette):
        if self.winfo_exists():
            self.paint()

    def paint(self):
        if self.variable is None:
            return
        chosen = self.variable.get()
        bg = theme.colour(self._bg)
        self.configure(bg=bg)
        measure = _measuring_font(self, self._font)
        room, line = self._room, measure.metrics("linespace")
        bar_w, bar_h = max(4, theme.px(self.BAR[0])), max(2, theme.px(self.BAR[1]))
        pill = shapes.photo(self, ("choice bar", bar_w, bar_h, theme.ACCENT, bg),
                            lambda: shapes.rounded(bar_w, bar_h, bar_h // 2, "ACCENT", bg=bg))
        blank = shapes.spacer_photo(self, bar_w, bar_h)
        self._pictures = {"pill": pill, "blank": blank}
        for value, label in self.labels.items():
            on = value == chosen
            fg = (theme.ACCENT_TEXT if on or value in self.marks
                  else theme.TEXT if value == self._hover else theme.MUTED)
            w, h = measure.measure(label.cget("text")) + 2 * room, line + 2 * room
            if on and self.focused:
                ring = shapes.photo(self, ("choice ring", w, h, theme.ACCENT),
                                    lambda: shapes.ring(w, h, h // 2, "ACCENT", keyfocus.ring_width()))
            else:
                ring = shapes.spacer_photo(self, w, h)
            self._pictures[value] = ring
            label.configure(fg=fg, bg=bg, image=ring)
            label.master.configure(bg=bg)
            bar = self.bars[value]
            bar.lit = on
            bar.configure(image=pill if on else blank, bg=bg)


class _FadingToggle(Toggle):
    """tabbar.Toggle (Settings' slider switch) that can be greyed out: drawn faded, clicks ignored."""

    enabled = True

    def _untrace(self):
        if self.variable is not None:
            super()._untrace()
            self.variable = self.command = None   # (destroyed: let go — see ChoiceRow._untrace)

    def toggle(self):
        if self.enabled:
            super().toggle()

    def can_focus(self) -> bool:
        return self.enabled

    def _image(self, on: bool):
        if self.enabled:
            return super()._image(on)
        return self._picture(on, theme.mix("ACCENT" if on else "SWITCH_OFF", self._bg, DISABLED_FADE),
                             theme.mix("ON_ACCENT" if on else "MUTED", self._bg, DISABLED_FADE))


class Switch(tk.Frame):
    """(0.22.0) An on/off choice inside a window: Settings' slider switch with its words beside it
    (click either), instead of a stock tick box — ABX's "Match levels", Test signals' "Bit-perfect".
    `state(["disabled"])` / `instate(...)` grey it out like the ttk widget it replaces."""

    def __init__(self, parent, text: str, variable: tk.BooleanVar, command: Callable[[], None] | None = None,
                 bg: str = "BG"):
        super().__init__(parent, bg=theme.colour(bg), cursor="hand2")
        self._bg = bg
        self.variable = variable
        self.toggle = _FadingToggle(self, self._root(), variable, command, bg=bg)
        self.toggle.pack(side="left")
        self.label = tk.Label(self, text=text, bg=theme.colour(bg), fg=theme.TEXT, font=theme.font(theme.SIZE_BODY),
                              anchor="w", padx=max(0, theme.px(theme.GAP_MEDIUM) - self.toggle.margin), bd=0)
        self.label.pack(side="left")
        for widget in (self, self.label):
            widget.bind("<ButtonRelease-1>", lambda e: released_over(e, self) and self.toggle.toggle())
        self.bind("<Destroy>", self._forget_command, add="+")
        theme.on_change(self._palette_changed)

    def _forget_command(self, event):
        if event.widget is self:   # (destroyed: let go of the window's method and variable — ChoiceRow._untrace)
            self.toggle.command = None
            self.variable = None

    @property
    def enabled(self) -> bool:
        return self.toggle.enabled

    def set_enabled(self, enabled: bool):
        if enabled != self.toggle.enabled:
            self.toggle.enabled = enabled
            self.toggle.set_focusable(enabled)
            self.toggle._draw()
            self._paint()

    def _paint(self):
        fg = theme.TEXT if self.toggle.enabled else theme.mix("MUTED", self._bg, DISABLED_FADE)
        cursor = "hand2" if self.toggle.enabled else "arrow"
        self.label.configure(fg=fg, cursor=cursor)
        self.toggle.configure(cursor=cursor)
        self.configure(cursor=cursor)

    def _palette_changed(self, _palette):
        if self.winfo_exists():
            self._paint()

    def state(self, spec=None) -> tuple:
        for flag in spec or ():
            if flag == "disabled":
                self.set_enabled(False)
            elif flag == "!disabled":
                self.set_enabled(True)
        return () if self.enabled else ("disabled",)

    def instate(self, spec) -> bool:
        flags = {"disabled": not self.enabled, "!disabled": self.enabled}
        return all(flags.get(flag, False) for flag in spec)


SEARCH_ICON = 14     # px at 100%: the drawn magnifier, where there's no icon font
CLEAR_DISC = 16      # px at 100%: the search box's × and the disc under it


class SearchBox(ttk.Frame):
    """The search box (0.20.0 for the track list; 0.22.0 shared with the Library): a rounded field
    with a magnifier inside on the left (centred SEARCH_ICON_ROOM / 2 in), a hint while it's empty
    and not being typed in (Tk entries have no placeholder of their own), the shortcut that reaches
    it (`keys`, small, at the right-hand end) while it's empty and not being typed in, and a round ×
    on the right while there's something to clear.

    `query` is its StringVar. The × (and Esc in the box) run `on_clear()` — by default, just
    emptying the box."""

    def __init__(self, parent, hint: str, on_clear: Callable[[], None] | None = None, keys: str = ""):
        super().__init__(parent)
        self.on_clear = on_clear or (lambda: self.query.set(""))
        self.query = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.query, style=theme.SEARCH_ENTRY)
        self.entry.pack(fill="x")
        self._icon_size = theme.px(SEARCH_ICON)
        root = self._root()
        font = icon_font(root, theme.ICON_SMALL)
        self.magnifier = tk.Label(self, bg=theme.SURFACE, fg=theme.MUTED, bd=0, padx=0, pady=0, cursor="xterm",
                                  text=glyph(root, "search") if font else "", font=font or theme.font(theme.SIZE_BODY))
        self.magnifier.place(x=theme.px(theme.SEARCH_ICON_ROOM) // 2, rely=0.5, anchor="center")
        self.magnifier.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.clear_button = RoundIconButton(self, root, "x", lambda: self.on_clear(), size=theme.TAB_CLOSE,
                                            diameter=CLEAR_DISC, width=CLEAR_DISC, bg="SURFACE",
                                            hover=("SURFACE", "TEXT", 0.16), pressed=("SURFACE", "TEXT", 0.26),
                                            label="Clear the search (Esc)")
        self.clear_button.set_focusable(False)   # (Esc in the box clears it)
        self.hint = tk.Label(self, text=hint, bg=theme.SURFACE, fg=theme.MUTED, font=theme.font(theme.SIZE_BODY),
                             anchor="w")
        self._hint_x = theme.px(theme.SEARCH_ICON_ROOM)
        self.hint.place(x=self._hint_x, rely=0.5, anchor="w")
        self.hint.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.keys_hint = None
        if keys:
            self.keys_hint = tk.Label(self, text=keys, bg=theme.SURFACE, fg=theme.MUTED,
                                      font=theme.font(theme.SIZE_SMALL), anchor="e", cursor="xterm")
            self.keys_hint.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.paint_icons()
        self._trace = self.query.trace_add("write", lambda *_: self.update_hint())
        self.entry.bind("<FocusIn>", lambda e: self.update_hint(), add="+")
        self.entry.bind("<FocusOut>", lambda e: self.update_hint(), add="+")
        self.entry.bind("<Escape>", lambda e: self.on_clear(), add="+")
        self.bind("<Destroy>", self._forget_command, add="+")
        theme.on_change(self._palette_changed)
        self.update_hint()

    def _forget_command(self, event):
        if event.widget is self and self.query is not None:   # (destroyed: let go — see ChoiceRow._untrace)
            try:
                self.query.trace_remove("write", self._trace)
            except tk.TclError:
                pass
            self.on_clear, self.query = (lambda: None), None
            self._magnifier_photo = None   # (pictures: let go on Tk's thread)

    def _palette_changed(self, _palette):
        if self.winfo_exists():
            for label in (self.hint, self.keys_hint):
                if label is not None:
                    label.configure(bg=theme.SURFACE, fg=theme.MUTED)
            self.paint_icons()

    def paint_icons(self):
        """The magnifier in the palette's colours (drawn, where there's no icon font)."""
        if self.magnifier.cget("text"):
            self.magnifier.configure(bg=theme.SURFACE, fg=theme.MUTED)
            return
        self._magnifier_photo = small_icon(self.entry, "search", self._icon_size, "MUTED", "SURFACE")
        self.magnifier.configure(image=self._magnifier_photo, bg=theme.SURFACE)

    def update_hint(self):
        # Tk's focus path, not focus_get(): that fails on a part of Tk's own Python has no object
        # for (an open drop-down list takes the focus), and the hint would stay hidden.
        typing = str(self.entry.tk.call("focus")) == str(self.entry)
        quiet = not self.query.get() and not typing
        if quiet:
            self.hint.place(x=self._hint_x, rely=0.5, anchor="w")
        else:
            self.hint.place_forget()
        if self.keys_hint is not None:
            if quiet:
                self.keys_hint.place(relx=1.0, x=-theme.px(theme.FIELD_INSET), rely=0.5, anchor="e")
            else:
                self.keys_hint.place_forget()
        if self.query.get():   # the × only while there's something to clear
            room = theme.px(theme.SEARCH_CLEAR_ROOM)
            self.clear_button.place(relx=1.0, x=-(room - theme.px(CLEAR_DISC)) // 2, rely=0.5, anchor="e")
        else:
            self.clear_button.place_forget()

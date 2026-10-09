"""The strip along the top of the window: tabs (one per album or library view), the
+ / gear buttons and, on Windows, our own minimise / close.

The tabs always share the one row with the buttons: as more open, or the
window narrows, they get slimmer and their names shorten, but they never drop to a row
of their own.

The selected tab is shaped like a browser's: rounded at the top, flowing into the page below
it with small inward curves at its foot. The others are quiet names, with a thin line between
two quiet neighbours and a rounded plate under the pointer; the tab playing music carries a
small level mark. Everything on the strip sits on one centre line (theme.ROW_Y).

Pressing on any empty part of the strip drags the window (natively, via chrome.py);
double-clicking it calls `on_double` (snap home).
"""

from __future__ import annotations

import sys
import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk

from . import chrome, keyfocus, shapes, theme
from .keyfocus import Focusable

# Colours are theme.py roles, read when drawn (so they follow a palette change): the strip is
# STRIP with a TAB_EDGE line along its foot, the selected tab BG, a hovered tab's plate STRIP_HI,
# a tab something dragged would land on gets a DG-green ring.

# Windows ships these icon fonts; elsewhere we fall back to plain characters.
GLYPHS = {
    "add": ("", "+"), "settings": ("", "⚙"), "home": ("", "⌂"),
    "min": ("", "–"), "max": ("", "□"), "restore": ("", "❐"), "close": ("", "✕"),
    "info": ("", "i"), "prev": ("", "⏮"), "next": ("", "⏭"),
    "play": ("", "▶"), "pause": ("", "⏸"), "playing": ("", "♪"), "x": ("", "×"),
    "note": ("", "♪"),
    "full": ("\ue740", "⤢"), "mini": ("\ue73f", "▭"),   # (0.19.0) full player / mini strip
    "lock": ("\ue72e", "L"), "unlock": ("\ue785", "U"),
    "shuffle": ("\ue8b1", "⤮"), "repeat": ("\ue8ee", "↻"), "repeat_one": ("\ue8ed", "↺"),
    "warning": ("\ue7ba", "⚠"),   # (0.21.0) a warning box's sign
    "check": ("\ue73e", "✓"), "search": ("\ue721", "⌕"),   # a menu's tick; the search box's magnifier
}


def icon_font(root: tk.Misc, size: int = theme.ICON):
    # (0.23.0) The installed fonts are listed once per Tk root: asking Tk for the list took a few
    # ms every time, and the tab strip asked for it twice per tab on every tab switch.
    app = root._root()
    families = getattr(app, "_dg_font_families", None)
    if families is None:
        families = app._dg_font_families = set(tkfont.families(root))
    for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if name in families:
            return (name, size)
    return None


def glyph(root: tk.Misc, name: str) -> str:
    fancy, plain = GLYPHS[name]
    return fancy if icon_font(root) else plain


TINT = "tint"          # a tinted tab's plate: between the strip and the hover plate
TINT_MIX = 0.45
TOOLTIP_DELAY_MS = 450   # (0.21.0) every tooltip waits this long under a still pointer — one delay app-wide
TOOLTIP_WRAP = 320       # px at 100%: a tooltip's longest line (less in a narrow window); longer text wraps
TOOLTIP_MIN_WRAP = 160   # px at 100%: …but never narrower than this


def released_over(event, widget: tk.Misc) -> bool:
    """Was the mouse button let go over `widget`? Tk sends the release to whatever took the press,
    wherever the pointer has gone since, so a press that slides off a control and is let go
    elsewhere must not count as a click on it (as with any Windows button). Screen coordinates,
    because the release may be bound on a part of the control (a row's words), not on the control."""
    x, y = event.x_root - widget.winfo_rootx(), event.y_root - widget.winfo_rooty()
    return 0 <= x < widget.winfo_width() and 0 <= y < widget.winfo_height()


DWMWA_WINDOW_CORNER_PREFERENCE = 33   # Windows 11's DWM (Desktop Window Manager) window attributes
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUNDSMALL = 3                  # small rounded corners, as Windows' own tooltips have


def round_corners(window: tk.Misc, rim: str) -> bool:
    """Ask Windows 11 to round a small pop-up window's corners and draw its 1 px rim in `rim` (a
    colour). True when it did; Windows 10 (and anything else) says no, and the window keeps the rim
    Tk draws."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        # Private handles: the argtypes set here are this function's alone.
        user32, dwm = ctypes.WinDLL("user32"), ctypes.WinDLL("dwmapi")
        user32.GetParent.argtypes, user32.GetParent.restype = [wintypes.HWND], wintypes.HWND
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
        corner = ctypes.c_int(DWMWCP_ROUNDSMALL)
        if dwm.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner),
                                     ctypes.sizeof(corner)) != 0:
            return False
        r, g, b = theme.rgb(rim)
        colour = wintypes.DWORD(r | g << 8 | b << 16)   # a COLORREF: 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(colour), ctypes.sizeof(colour))
        return True
    except (OSError, AttributeError, tk.TclError):
        return False


class Tooltip:
    """A small label that appears after hovering a moment. `text` may be a function, so it
    can describe the current state (e.g. what the status square means right now).

    (0.21.0) One delay for every tooltip (TOOLTIP_DELAY_MS); long text wraps to fit the window it
    belongs to (at most TOOLTIP_WRAP wide), and the tip goes above the widget when there's no room
    below it in the window.

    `at`: where the tip's left edge goes (screen x), for a widget that holds several things (the
    tab strip: under the tab the pointer is on); by default, the widget's left edge.

    On Windows 11 the tip has small rounded corners and an EDGE rim drawn by Windows (round_corners);
    elsewhere Tk's square 1 px rim."""

    def __init__(self, widget: tk.Misc, text, at: Callable[[], int] | None = None):
        self.widget, self.text, self.at = widget, text, at
        self.label: tk.Label | None = None   # the tip's words, while it's shown
        self._job = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._forget, add="+")

    def _forget(self, event):
        """The widget has gone: no tip for it, and let go of it. Buttons keep their tooltip
        (`self.tip`), so holding on to the button too would make a cycle that only Python's cycle
        collector frees, on whatever thread it happens to run, and a Tk object freed on another
        thread fails."""
        if event.widget is self.widget:
            self._hide()
            self.widget = None

    def _schedule(self, _event=None):
        self._cancel()
        if self.widget is not None:
            self._job = self.widget.after(TOOLTIP_DELAY_MS, self._show)

    def _cancel(self):
        if self._job:
            if self.widget is not None:
                self.widget.after_cancel(self._job)
            self._job = None

    def wrap(self) -> int:
        """How wide (real px) the tip's text may run: TOOLTIP_WRAP, less in a narrow window."""
        top = self.widget.winfo_toplevel()
        room = top.winfo_width() - theme.px(2 * theme.GAP_MEDIUM)
        return max(theme.px(TOOLTIP_MIN_WRAP), min(theme.px(TOOLTIP_WRAP), room))

    def _show(self):
        self._job = None
        if self.widget is None:
            return
        text = self.text() if callable(self.text) else self.text
        if not text or not self.widget.winfo_exists():
            return
        tip = tk.Toplevel(self.widget)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        self.label = tk.Label(tip, text=text, bg=theme.SURFACE_HI, fg=theme.TEXT, font=theme.font(theme.SIZE_BODY),
                              padx=theme.px(theme.GAP_MEDIUM), pady=theme.px(theme.GAP_SMALL), justify="left",
                              wraplength=self.wrap(), highlightthickness=1, highlightbackground=theme.EDGE)
        self.label.pack()
        tip.update_idletasks()
        if round_corners(tip, theme.EDGE):   # Windows 11 draws the rounded rim itself
            self.label.configure(highlightthickness=0)
        top = self.widget.winfo_toplevel()
        x = self.at() if self.at is not None else self.widget.winfo_rootx()
        right = top.winfo_rootx() + top.winfo_width()
        if x + tip.winfo_width() > right:  # keep it over the player, not off its edge
            x = max(top.winfo_rootx(), right - tip.winfo_width())
        gap = theme.px(theme.GAP_SMALL)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + gap
        # Past the window's bottom edge (often the screen's, for a tall player): above the widget instead.
        if y + tip.winfo_height() > top.winfo_rooty() + top.winfo_height():
            y = max(top.winfo_rooty(), self.widget.winfo_rooty() - tip.winfo_height() - gap)
        tip.geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:   # (already gone with its widget)
                pass
            self._tip = None

    def reschedule(self):
        """Start the wait again (what the tip would say has changed under a still widget)."""
        self._hide()
        self._schedule()


class IconButton(tk.Label):
    """A flat icon button with a hover colour (tk.Label, so it can sit on the dark strip).
    Colours are theme roles ("STRIP", "ACCENT", …), looked up each time it's painted, so the
    button follows a palette change; `size` is an icon size (theme.ICON_*), `width` px at 100%.
    `set_active(True)` lights it as if hovered, with its glyph in ACCENT_TEXT (the gear while
    Settings is open)."""

    def __init__(self, parent, root, name: str, command: Callable[[], None], size: int = theme.ICON,
                 width: int = 34, bg: str = "STRIP", hover: str = "STRIP_HI", fg: str = "MUTED",
                 hover_fg: str = "TEXT", label: str = ""):
        font = icon_font(root, size) or theme.font(size + 1)
        super().__init__(parent, text=glyph(root, name), font=font, bg=theme.colour(bg), fg=theme.colour(fg),
                         cursor="arrow")
        s = theme.scale()
        self.configure(width=1, padx=int((width * s - size * 1.6 * s) / 2))
        self._bg, self._hover, self._fg, self._hover_fg = bg, hover, fg, hover_fg
        self.command = command
        self.tooltip = label
        self.active = False
        self.bind("<Enter>", lambda e: self._paint(True))
        self.bind("<Leave>", lambda e: self._paint(False))
        self.bind("<ButtonRelease-1>", self._click)
        self.tip = Tooltip(self, label) if label else None

    def _click(self, event):
        if 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.command()

    def set_colour(self, fg: str):
        """Change the resting colour, a theme role (e.g. "GOOD" when the bit-perfect lock is on)."""
        self._fg = fg
        self._paint(False)

    def set_active(self, active: bool):
        self.active = active
        self._paint(False)

    def _paint(self, hover: bool):
        lit = hover or self.active
        self.configure(bg=theme.colour(self._hover if lit else self._bg),
                       fg=theme.colour(self._hover_fg if hover else ("ACCENT_TEXT" if self.active else self._fg)))


class RoundIconButton(Focusable, IconButton):
    """An IconButton (glyph from the icon font, colours as theme roles) that lights up with a soft
    round disc under the pointer instead of a square: the transport's buttons, the header's lock and
    ⓘ, the strip's + and gear. `diameter` and `width` px at 100% (the disc is centred in the width).
    `hover` is the disc's role (HOVER on the window, STRIP_HI on the strip) or a theme.tone mix,
    `pressed` the disc while held down. The keyboard reaches it (keyfocus): Enter / Space press it,
    and a DG-green ring round its disc shows it has the keyboard. `set_backdrop(role)`: what it sits
    on changed (a menu row lighting up).

    `drawn="prev"` / `"next"`: the glyph is drawn into the picture (shapes.skip, filled, TEXT)
    instead of taken from the icon font, `glyph` px tall at 100% (SKIP_SIZE when not given). `set_on(True)`: the glyph in ACCENT_TEXT with a small dot
    under it (shuffle / repeat on), so "on" isn't told by colour alone."""

    ON_DOT = 3          # px at 100%: the "on" dot…
    ON_DOT_BELOW = 12   # …centred this far under the glyph's centre
    SKIP_SIZE = 12      # px at 100%: a drawn previous / next glyph's height

    def __init__(self, parent, root, name: str, command: Callable[[], None], diameter: int = 36,
                 width: int = 40, hover: str = "HOVER", pressed: str = "SURFACE_DOWN", drawn: str = "",
                 glyph: int | None = None, **options):
        self._diameter = max(12, theme.px(diameter))
        self._pressed = False
        self._hovering = False
        self._pressed_role = pressed
        self._drawn = drawn
        self._glyph = theme.px(glyph or self.SKIP_SIZE)   # a drawn glyph's height (px)
        self.on = False
        super().__init__(parent, root, name, command, width=width, hover=hover, **options)
        side = max(0, (theme.px(width) - self._diameter) // 2)
        self.configure(width=0, height=0, padx=side, pady=0, bd=0, compound="center", cursor="hand2")
        if drawn:
            self.configure(text="")
        self.bind("<ButtonPress-1>", lambda e: self._press(True), add="+")
        self.bind("<ButtonRelease-1>", lambda e: self._press(False), add="+")
        self.bind("<Destroy>", self._let_go, add="+")
        theme.on_change(self._palette_changed)
        self._keyboard()
        self._paint(False)

    def activate(self):
        self.command()

    def focus_changed(self):
        self._paint(self._hovering)

    def set_backdrop(self, bg: str):
        if bg != self._bg:
            self._bg = bg
            self._paint(self._hovering)

    def _let_go(self, event):
        if event.widget is self:   # (destroyed: its picture is let go here, on Tk's thread)
            self._photo = None

    def _press(self, down: bool):
        self._pressed = down
        self._paint(self._hovering)

    def set_on(self, on: bool):
        """Shuffle / repeat on: ACCENT_TEXT glyph and the dot under it; off: MUTED, no dot."""
        self.on = on
        self.set_colour("ACCENT_TEXT" if on else "MUTED")

    def _palette_changed(self, _palette):
        self._paint(self._hovering)

    def glyph_colour(self, hover: bool) -> str:
        if self._drawn:
            return theme.TEXT
        return theme.colour(self._hover_fg if hover else ("ACCENT_TEXT" if self.active else self._fg))

    def _paint(self, hover: bool):
        self._hovering = hover
        if not hover:
            self._pressed = False
        d = self._diameter
        bg = theme.colour(self._bg)
        lit = hover or self.active
        disc = theme.tone(self._pressed_role if self._pressed else self._hover) if lit else None
        ink = self.glyph_colour(hover)
        dot = theme.ACCENT_TEXT if self.on and not hover else None
        ring = (theme.ACCENT, keyfocus.ring_width()) if self.focused else None
        drawn, glyph = self._drawn, self._glyph
        s = theme.scale()

        def draw() -> Image.Image:
            k = shapes.SUPERSAMPLE
            image = Image.new("RGB", (d * k, d * k), bg)
            g = ImageDraw.Draw(image)
            if disc:
                g.ellipse((0, 0, d * k - 1, d * k - 1), fill=disc)
            if dot:
                r, cy = self.ON_DOT * s * k / 2, (d / 2 + self.ON_DOT_BELOW * s) * k
                g.ellipse((d * k / 2 - r, cy - r, d * k / 2 + r, cy + r), fill=dot)
            if drawn:
                shapes.skip(glyph, ink, drawn == "next", image, (d / 2, d / 2))
            if ring:
                g.ellipse((0, 0, d * k - 1, d * k - 1), outline=ring[0], width=ring[1] * k)
            return shapes.down(image, (d, d))

        self._photo = shapes.photo(self, ("round button", d, bg, disc, ink if drawn else None, dot, drawn, glyph, ring),
                                   draw)
        self.configure(image=self._photo, bg=bg, fg=ink)


class CaptionButton(IconButton):
    """Minimise or close: a full-height plate CAPTION_W wide that stops one pixel short of the
    strip's foot (so the strip's bottom line runs under it), its glyph on the strip's centre line.
    One label: a see-through spacer picture, anchored to the bottom, puts the glyph's centre on
    ROW_Y, so the tooltip and the clicks need nothing more."""

    def __init__(self, parent, root, name: str, command: Callable[[], None], **options):
        super().__init__(parent, root, name, command, size=theme.ICON_SMALL, **options)
        height = theme.px(theme.STRIP_H) - TabBar.line_width()
        below = height - theme.px(theme.ROW_Y)          # from the centre line down to the plate's foot
        self._spacer = shapes.spacer_photo(self, 1, 2 * below)
        self.configure(image=self._spacer, compound="center", anchor="s", width=theme.px(theme.CAPTION_W),
                       height=height, padx=0, pady=0, bd=0)


class TabBar(tk.Frame):
    """The strip: one tk.Canvas for the tabs, with the +, gear, minimise and close buttons as
    widgets over it.

    Tabs are repainted, never rebuilt: each tab has a slot of canvas items (the line after it, its
    level mark, its name) made once, and the one-of-a-kind pieces (the selected tab's two ends,
    middle and top rim; the hover plate; the drop ring; the × and its disc; the strip's bottom line)
    are shared. A change only re-sends the options and positions that changed, so a tab switch costs
    a few item changes, not a strip. Nothing is drawn per width: plates and rings are two cached end
    pictures with plain rectangles between them (shapes.ends).

    The strip's buttons are the same at every width."""

    DRAG_GAP = theme.DRAG_GAP   # (the strip's tokens live in theme; kept here for older callers)
    MIN_TAB = theme.MIN_TAB
    CLOSE_REVEAL_MS = 450      # hover a tab this long and its small close button shows
    DRAG_START = 6             # px at 100%: move this far with the button down and a tab press becomes a drag
    X_DISC_MIX = 0.16          # the × disc under the pointer: this far from its plate toward TEXT
    FIT_CACHE = 600            # shortened names remembered (by name and width) before starting afresh
    # Back to front: each kind of item is raised in this order whenever new slots are made.
    LAYERS = ("line", "plate", "divider", "selected", "ring", "mark", "name", "xdisc", "xglyph")

    def __init__(self, parent, root: tk.Tk, *, on_select, on_close, on_new, on_settings, on_double,
                 on_quit, on_tab_menu=lambda index, event: None, on_move=lambda source, target: None):
        self.row_height = theme.px(theme.STRIP_H)
        self.height = self.row_height
        super().__init__(parent, bg=theme.STRIP)
        self.root = root
        self.on_select, self.on_close = on_select, on_close
        self.on_double = on_double
        self.on_tab_menu = on_tab_menu
        self.on_move = on_move
        # A tab being dragged: {"index", "x0", "moving"}. Kept here, not per tab: the tabs change
        # under the pointer mid-drag (each swap moves the names along the slots).
        self._drag: dict | None = None
        self._slot = 1        # one tab's width plus its gap, set by _layout
        self._per = 1         # one tab's width
        self.tab_max = theme.TAB_MAX   # a tab's widest (px at 100%)
        self._locked: frozenset = frozenset()   # tabs with no × (set_tabs)
        self._tinted: frozenset = frozenset()   # tabs on a plate of their own (set_tabs)
        self.top_row = tk.Frame(self, bg=theme.STRIP, height=self.row_height)
        self.top_row.pack(fill="x")
        self.top_row.pack_propagate(False)
        self.canvas = tk.Canvas(self.top_row, bg=theme.STRIP, height=self.row_height, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._titles: list[str] = []
        self._current = -1
        self._playing: int | None = None
        self._state = None
        self._highlight: int | None = None  # the tab something being dragged would land on
        self._hover: int | None = None      # the tab under the pointer
        self._x_shown: int | None = None    # the tab whose × is showing (after CLOSE_REVEAL_MS)
        self._x_hot = False                 # …and the pointer is on the ×
        self._x_job = None
        self._x_pressed: int | None = None  # a press that began on a tab's ×
        self._shown: list[str] = []         # each slot's name as shown (shortened, or "")
        self._fits: dict[tuple[str, int], str] = {}
        self._add_at = None                 # where + was last placed
        self.text_font = tkfont.Font(root=root, family=theme.FONT, size=theme.SIZE_BODY)
        icon = icon_font(root, theme.TAB_CLOSE)
        self.x_font = icon if icon else theme.font(theme.TAB_CLOSE)
        # What each item last showed, so only changes are sent to Tk (and the pictures it shows are
        # kept here: Tk drops a picture once Python lets go of it).
        self._sent: dict[int, dict] = {}
        self._placed: dict[int, tuple] = {}
        self._pictures: dict[int, object] = {}

        c = self.canvas
        self._line = c.create_rectangle(0, 0, 0, 0, outline="", tags="line")
        self._plate = self._three_slice("plate", 1)
        self._tints: list = []   # the tinted tabs' plates (set_tabs: tinted)
        self._sel_ends = (c.create_image(0, 0, anchor="nw", state="hidden", tags="selected"),
                          c.create_image(0, 0, anchor="nw", state="hidden", tags="selected"))
        self._sel_mid = c.create_rectangle(0, 0, 0, 0, outline="", state="hidden", tags="selected")
        self._sel_rim = c.create_rectangle(0, 0, 0, 0, outline="", state="hidden", tags="selected")
        self._ring = self._three_slice("ring", 2)
        self._x_disc = c.create_image(0, 0, anchor="center", state="hidden", tags="xdisc")
        self._x_glyph = c.create_text(0, 0, anchor="center", state="hidden", font=self.x_font,
                                      text=glyph(root, "x"), tags="xglyph")
        self._slots: list[dict[str, int]] = []

        # The buttons: widgets over the canvas, each centred on the strip's line.
        self.close_btn = self.min_btn = None
        if chrome.CUSTOM:
            self.close_btn = CaptionButton(self, root, "close", on_quit, hover="CLOSE_HOVER", hover_fg="ON_ACCENT",
                                           label="Close")
            self.close_btn.place(in_=self.top_row, relx=1.0, x=0, y=0, anchor="ne")
            # No maximise button: the window is a side player and can't be maximised (its width is
            # capped, ui.FULL_MAX_WIDTH). Double-clicking the strip brings it home once a home is set.
            self.min_btn = CaptionButton(self, root, "min", root.iconify, label="Minimise")
            self.min_btn.place(in_=self.top_row, relx=1.0, x=-theme.px(theme.CAPTION_W), y=0, anchor="ne")
        disc = theme.DISC
        self.settings_btn = RoundIconButton(self, root, "settings", on_settings, diameter=disc, width=disc,
                                            bg="STRIP", hover="STRIP_HI", pressed="STRIP_HI", label="Settings")
        right = self._caption_room() + theme.px(theme.DISC_MARGIN)
        self.settings_btn.place(in_=self.top_row, relx=1.0, x=-right, y=self._disc_top(), anchor="ne")
        self.add_btn = RoundIconButton(self, root, "add", on_new, diameter=disc, width=disc, bg="STRIP",
                                       hover="STRIP_HI", pressed="STRIP_HI", label="New tab")
        self.add_btn.place(in_=self.top_row, x=self._add_x(0), y=self._disc_top())
        for button in (self.add_btn, self.settings_btn, self.min_btn, self.close_btn):
            if button is not None:
                button.lift()

        c.bind("<ButtonPress-1>", self._pressed)
        c.bind("<Double-Button-1>", self._double)
        c.bind("<B1-Motion>", self._drag_motion)
        c.bind("<ButtonRelease-1>", self._released)
        c.bind("<Button-2>", self._middle)            # middle-click closes, as in browsers
        c.bind("<Button-3>", self._right)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Motion>", lambda e: self._pointer(e.x, e.y))
        c.bind("<Leave>", lambda e: self._pointer(None, None))
        # One tooltip for the whole strip: the hovered tab's full name, under that tab. (Made after
        # the bindings above: its own add to them.)
        self.tooltip = Tooltip(c, self._tip_text, at=self._tip_x)
        self.bind("<Configure>", lambda e: self._layout())
        theme.on_change(self._palette_changed)

    @staticmethod
    def line_width() -> int:
        """The strip's 1 px lines in real pixels (never thinner than one)."""
        return max(1, theme.px(1))

    def _caption_room(self) -> int:
        if not chrome.CUSTOM:
            return 0
        return 2 * theme.px(theme.CAPTION_W)   # minimise and close

    def _disc_top(self) -> int:
        return theme.px(theme.ROW_Y) - theme.px(theme.DISC) // 2

    def _add_x(self, count: int) -> int:
        """Where + goes: just after the last tab (at the drag gap's end when there are none)."""
        return theme.px(self.DRAG_GAP) + count * self._slot - theme.px(theme.TAB_GAP) + theme.px(theme.ADD_GAP)

    def _three_slice(self, layer: str, pieces: int) -> tuple[int, ...]:
        """A plate (`pieces` 1: one middle rectangle) or ring (2: its top and bottom edges) at any
        width: two end pictures and plain rectangles between them."""
        c = self.canvas
        ends = tuple(c.create_image(0, 0, anchor="nw", state="hidden", tags=layer) for _ in range(2))
        middle = tuple(c.create_rectangle(0, 0, 0, 0, outline="", state="hidden", tags=layer) for _ in range(pieces))
        return ends + middle

    # -- sending only what changed ---------------------------------------------------------------
    def _set(self, item: int, **options):
        sent = self._sent.setdefault(item, {})
        changed = {k: v for k, v in options.items() if sent.get(k) != v}
        if changed:
            if "image" in changed:
                self._pictures[item] = changed["image"]
            sent.update(changed)
            self.canvas.itemconfigure(item, **changed)

    def _at(self, item: int, *xy):
        xy = tuple(int(v) for v in xy)
        if self._placed.get(item) != xy:
            self._placed[item] = xy
            self.canvas.coords(item, *xy)

    def _hide(self, *items: int):
        for item in items:
            self._set(item, state="hidden")

    # -- tabs ----------------------------------------------------------------
    def set_tabs(self, titles: list[str], current: int, playing: int | None, locked=(), tinted=()):
        """`locked`: tabs with no × (they can't be closed); `tinted`: tabs drawn on a plate of their
        own, a shade apart from the strip (the main Library tab)."""
        state = (list(titles), current, playing, frozenset(locked), frozenset(tinted))
        if state == self._state:
            return
        self._state = state
        self._titles, self._current, self._playing = list(titles), current, playing
        self._locked, self._tinted = frozenset(locked), frozenset(tinted)
        self._layout()

    def _make_slots(self, count: int):
        """Slots follow the number of tabs: items are only made or deleted when that changes."""
        c = self.canvas
        made = False
        while len(self._slots) < count:
            self._slots.append({
                "divider": c.create_rectangle(0, 0, 0, 0, outline="", state="hidden", tags="divider"),
                "mark": c.create_image(0, 0, anchor="w", state="hidden", tags="mark"),
                "name": c.create_text(0, 0, anchor="w", font=self.text_font, text="", tags="name"),
            })
            self._shown.append("")
            made = True
        while len(self._slots) > count:
            for item in self._slots.pop().values():
                c.delete(item)
                self._sent.pop(item, None)
                self._placed.pop(item, None)
                self._pictures.pop(item, None)
            self._shown.pop()
        if made:
            for layer in self.LAYERS:
                c.tag_raise(layer)

    def _layout(self):
        n = len(self._titles)
        for name in ("_hover", "_x_shown", "_highlight"):
            if getattr(self, name) is not None and getattr(self, name) >= n:
                setattr(self, name, None)
        self._make_slots(n)
        px = theme.px
        width = max(1, self.winfo_width())
        height = self.row_height
        line = self.line_width()
        self._set(self._line, fill=theme.TAB_EDGE)
        self._at(self._line, 0, height - line, width, height)
        if n:
            right = self._caption_room() + px(theme.DISC) + 2 * px(theme.DISC_MARGIN)
            room = width - right - (px(theme.DISC) + px(theme.ADD_GAP)) - px(self.DRAG_GAP + 10)
            # Equal widths, so the albums sit side by side and you can flick between them; with
            # many open they slim down (names shortened with …) rather than leave the row.
            self._per = max(px(self.MIN_TAB), min(px(self.tab_max), room // n - px(theme.TAB_GAP)))
            self._slot = self._per + px(theme.TAB_GAP)
        add_x = self._add_x(n)
        if add_x != self._add_at:
            self._add_at = add_x
            self.add_btn.place_configure(x=add_x)
        self._paint_selected()
        self._paint_plate()
        self._paint_ring()
        for i in range(n):
            self._paint_slot(i)
        self._paint_close()

    def slot_x(self, i: int) -> int:
        """Tab i's left edge on the strip (real px)."""
        return theme.px(self.DRAG_GAP) + i * self._slot

    def _slim(self) -> bool:
        return self._per < theme.px(theme.TAB_SLIM)

    def _busy(self) -> set:
        """Tabs that hide the lines beside them: the selected one, the hovered one, a drop target."""
        return {self._current, self._hover, self._highlight} - {None}

    def _paint_slot(self, i: int):
        """Slot i as tab i: its name (shortened to fit), level mark, and the line after it."""
        px = theme.px
        slot = self._slots[i]
        x, per, cy = self.slot_x(i), self._per, px(theme.ROW_Y)
        busy = self._busy()
        lit = i in busy
        # The line between two quiet tabs.
        gap = x + per + px(theme.TAB_GAP) // 2
        half = px(theme.DIVIDER_H) // 2
        if i < len(self._titles) - 1 and not lit and i + 1 not in busy:
            self._at(slot["divider"], gap, cy - half, gap + self.line_width(), cy + half)
            self._set(slot["divider"], fill=theme.TAB_EDGE, state="normal")
        else:
            self._hide(slot["divider"])
        playing = i == self._playing
        colour = theme.TEXT if lit else theme.MUTED
        title = self._titles[i]
        if self._slim():
            # A slim tab: its mark alone when it's playing, else a short name, centred.
            centre = x + per // 2
            if playing:
                self._at(slot["mark"], centre - px(theme.MARK_W) // 2, cy)
                self._set(slot["mark"], image=shapes.level_mark_photo(self, "ACCENT_TEXT"), state="normal")
                shown = ""
            else:
                self._hide(slot["mark"])
                room = per - 2 * px(theme.TAB_PAD_SLIM)
                shown = self._fit(title, room) if room >= px(10) else ""
            self._at(slot["name"], centre, cy)
            self._set(slot["name"], anchor="center", text=shown, fill=colour)
        else:
            text_x, end = x + px(theme.TAB_PAD_L), x + per - px(theme.TAB_PAD_R)
            if playing:
                self._at(slot["mark"], text_x, cy)
                self._set(slot["mark"], image=shapes.level_mark_photo(self, "ACCENT_TEXT"), state="normal")
                text_x += px(theme.MARK_W + theme.MARK_GAP)
            else:
                self._hide(slot["mark"])
            if i == self._x_shown:   # the name ends before the ×, never cut by it
                end = x + per - px(theme.CLOSE_SLOT) - px(theme.GAP_HAIR)
            shown = self._fit(title, end - text_x) if end - text_x >= px(10) else ""
            self._at(slot["name"], text_x, cy)
            self._set(slot["name"], anchor="w", text=shown, fill=colour)
        self._shown[i] = shown

    def _paint_selected(self):
        """The selected tab: two end pictures (corner, flare and rim), a middle and a top rim."""
        items = (*self._sel_ends, self._sel_mid, self._sel_rim)
        i = self._current
        if not 0 <= i < len(self._titles):
            self._hide(*items)
            return
        px = theme.px
        x0, x1 = self.slot_x(i), self.slot_x(i) + self._per
        top, height, line, flare = px(theme.TAB_TOP), self.row_height, self.line_width(), px(theme.TAB_FLARE)
        radius = max(0, min(px(theme.RADIUS_M), self._per // 2 - px(2)))   # slim tabs: smaller corners
        left, right = shapes.tab_end_photos(self, radius, flare, height - top, line, "BG", "TAB_EDGE")
        cap = flare + radius + line
        self._at(self._sel_ends[0], x0 - flare, top)
        self._at(self._sel_ends[1], x1 + flare - cap, top)
        self._at(self._sel_mid, x0 - flare + cap, top, x1 + flare - cap, height)
        self._at(self._sel_rim, x0 - flare + cap, top, x1 + flare - cap, top + line)
        self._set(self._sel_ends[0], image=left, state="normal")
        self._set(self._sel_ends[1], image=right, state="normal")
        self._set(self._sel_mid, fill=theme.BG, state="normal")
        self._set(self._sel_rim, fill=theme.TAB_EDGE, state="normal")

    def _plate_box(self, i: int) -> tuple[int, int, int, int, int]:
        """(left, top, right, bottom, radius) of tab i's hover plate / drop ring (real px)."""
        px = theme.px
        x0 = self.slot_x(i) + px(theme.PLATE_INSET)
        x1 = self.slot_x(i) + self._per - px(theme.PLATE_INSET)
        top = px(theme.ROW_Y) - px(theme.PLATE_H) // 2
        bottom = top + px(theme.PLATE_H)
        radius = max(1, min(px(theme.RADIUS_M), (x1 - x0) // 2, (bottom - top) // 2))
        return x0, top, x1, bottom, radius

    def _paint_plate(self):
        """A rounded STRIP_HI plate under a hovered tab (never the selected one), and the tinted
        tabs' own plates — a shade between the strip and that — under them the rest of the time."""
        self._paint_one_plate(self._plate, self._hover, "STRIP_HI")
        for i, plate in self._tint_plates():
            self._paint_one_plate(plate, i if (i != self._hover and i != self._current) else None, TINT)

    def _tint_plates(self):
        """A plate (three canvas items) per tinted tab, made as needed."""
        wanted = sorted(self._tinted)
        while len(self._tints) < len(wanted):
            self._tints.append(self._three_slice("plate", 1))
        for plate in self._tints[len(wanted):]:
            self._hide(*plate)
        return list(zip(wanted, self._tints))

    def _paint_one_plate(self, plate, i: int | None, fill: str):
        if i is None or i == self._current or i >= len(self._titles):
            self._hide(*plate)
            return
        x0, top, x1, bottom, radius = self._plate_box(i)
        if x1 - x0 < 2:
            self._hide(*plate)
            return
        colour = theme.mix("STRIP", "STRIP_HI", TINT_MIX) if fill == TINT else theme.STRIP_HI
        left, right = shapes.ends_photos(self, bottom - top, radius, fill=colour)
        self._at(plate[0], x0, top)
        self._at(plate[1], x1 - radius, top)
        self._at(plate[2], x0 + radius, top, x1 - radius, bottom)
        self._set(plate[0], image=left, state="normal")
        self._set(plate[1], image=right, state="normal")
        self._set(plate[2], fill=colour, state="normal")

    def _paint_ring(self):
        """A 2 px DG-green ring round the tab something dragged would land on (no fill: green
        on the hover grey would be too faint in Light)."""
        i = self._highlight
        if i is None or i >= len(self._titles):
            self._hide(*self._ring)
            return
        x0, top, x1, bottom, radius = self._plate_box(i)
        ring = max(2, theme.px(2))
        left, right = shapes.ends_photos(self, bottom - top, radius, outline="ACCENT", line=ring)
        self._at(self._ring[0], x0, top)
        self._at(self._ring[1], x1 - radius, top)
        self._at(self._ring[2], x0 + radius, top, x1 - radius, top + ring)
        self._at(self._ring[3], x0 + radius, bottom - ring, x1 - radius, bottom)
        self._set(self._ring[0], image=left, state="normal")
        self._set(self._ring[1], image=right, state="normal")
        for edge in self._ring[2:]:
            self._set(edge, fill=theme.ACCENT, state="normal")

    def _x_centre(self, i: int) -> tuple[int, int]:
        px = theme.px
        return (self.slot_x(i) + self._per - px(theme.PLATE_INSET) - px(theme.GAP_HAIR) - px(theme.CLOSE_DISC) // 2,
                px(theme.ROW_Y))

    def _on_x(self, i: int, x: int, y: int) -> bool:
        """Is (x, y) on tab i's × (its CLOSE_SLOT-wide square)?"""
        cx, cy = self._x_centre(i)
        half = theme.px(theme.CLOSE_SLOT) // 2
        return abs(x - cx) <= half and abs(y - cy) <= half

    def _paint_close(self):
        """The × on the tab it's showing for: MUTED, or TEXT on a disc under the pointer."""
        i = self._x_shown
        if i is None or i >= len(self._titles) or self._slim():
            self._hide(self._x_disc, self._x_glyph)
            return
        cx, cy = self._x_centre(i)
        self._at(self._x_glyph, cx, cy)
        self._set(self._x_glyph, fill=theme.TEXT if self._x_hot else theme.MUTED, state="normal")
        if self._x_hot:
            plate = theme.BG if i == self._current else theme.STRIP_HI
            disc = shapes.disc_photo(self, theme.px(theme.CLOSE_DISC), theme.mix(plate, "TEXT", self.X_DISC_MIX))
            self._at(self._x_disc, cx, cy)
            self._set(self._x_disc, image=disc, state="normal")
        else:
            self._hide(self._x_disc)

    def _full_name(self, i: int) -> str:
        """Tab i's whole name for its tooltip — only when the tab shows less than that."""
        if i is None or i >= len(self._titles) or i >= len(self._shown):
            return ""
        title = self._titles[i]
        return title if self._shown[i] != title else ""

    def _tip_text(self) -> str:
        return self._full_name(self._hover) if self._hover is not None else ""

    def _tip_x(self) -> int:
        i = self._hover
        return self.canvas.winfo_rootx() + (self.slot_x(i) if i is not None else 0)

    # -- the pointer -----------------------------------------------------------------------------
    def _slot_at(self, x: int, y: int) -> int | None:
        """The tab at (x, y) on the strip (None: an empty part)."""
        if not self._titles or not theme.px(theme.TAB_TOP) <= y < self.row_height:
            return None
        offset = x - theme.px(self.DRAG_GAP)
        if offset < 0:
            return None
        i = offset // max(1, self._slot)
        if i >= len(self._titles) or offset - i * self._slot >= self._per:
            return None
        return int(i)

    def _pointer(self, x: int | None, y: int | None):
        """The pointer moved over the strip (None: it left): the hover plate, the × and the tooltip
        follow the tab under it."""
        slot = None if x is None else self._slot_at(x, y)
        hot = slot is not None and slot == self._x_shown and self._on_x(slot, x, y)
        if slot != self._hover:
            self._hover = slot
            self._x_shown, hot = None, False
            if self._x_job is not None:
                self.after_cancel(self._x_job)
                self._x_job = None
            if slot is not None and not self._slim():
                self._x_job = self.after(self.CLOSE_REVEAL_MS, self._reveal_x)
            self._x_hot = hot
            self._layout()
            if slot is not None:
                self.tooltip.reschedule()
            else:
                self.tooltip._hide()
        elif hot != self._x_hot:
            self._x_hot = hot
            self._paint_close()

    def _reveal_x(self):
        """The pointer has rested on a tab: its × shows (and its name makes room for it) — not on a
        locked tab, which has none."""
        self._x_job = None
        if self._hover is not None and self._hover in self._locked:
            return
        if self._hover is not None and self.winfo_exists() and not self._slim():
            self._x_shown = self._hover
            self._layout()

    def _forget_hover(self):
        self._hover = self._x_shown = None
        self._x_hot = False
        if self._x_job is not None:
            self.after_cancel(self._x_job)
            self._x_job = None
        self.tooltip._hide()
        self._layout()

    def _pressed(self, event):
        """A press on a tab selects it (and may turn into a drag); on its × it waits for the
        release; on an empty part it drags the window."""
        self.tooltip._hide()
        i = self._slot_at(event.x, event.y)
        if i is None:
            chrome.start_move(self.root)
            return
        if i == self._x_shown and self._on_x(i, event.x, event.y):
            self._x_pressed = i
            return
        self._press(i, event)

    def _released(self, event):
        i, self._x_pressed = self._x_pressed, None
        if i is not None and i < len(self._titles) and self._on_x(i, event.x, event.y):
            self.on_close(i)   # let go on the × it was pressed on (slid off: nothing, like any button)
        self._drag_end(event)

    def _double(self, event):
        i = self._slot_at(event.x, event.y)
        if i is None:
            self.on_double()
        else:
            self._pressed(event)   # a quick second press on a tab is just a press

    def _middle(self, event):
        i = self._slot_at(event.x, event.y)
        if i is not None and i not in self._locked:
            self.on_close(i)

    def _right(self, event):
        i = self._slot_at(event.x, event.y)
        if i is not None:
            self.on_tab_menu(i, event)

    def _palette_changed(self, _palette):
        """A new palette: everything is sent again in its colours, pointing at its pictures."""
        if not self.winfo_exists():
            return
        self._sent.clear()
        self._pictures.clear()
        self.canvas.configure(bg=theme.STRIP)
        self._layout()

    # -- drag and drop onto a tab ---------------------------------------------
    def tab_at(self, x_root: int, y_root: int) -> int | None:
        """The tab under this screen point (None: not over a tab)."""
        try:
            return self._slot_at(x_root - self.canvas.winfo_rootx(), y_root - self.canvas.winfo_rooty())
        except tk.TclError:
            return None

    def highlight(self, index: int | None):
        """Light up the tab a drag would land on (None: none)."""
        if index != self._highlight:
            self._highlight = index
            self._layout()

    # -- dragging tabs into a new order -------------------------------------
    def _press(self, index: int, event):
        """A press selects the tab, and may turn into a drag that moves it."""
        self._drag = {"index": index, "x0": event.x_root, "moving": False}
        self.on_select(index)

    def _drag_motion(self, event):
        drag = self._drag
        if drag is None or not self._titles:
            return
        if not drag["moving"]:
            if abs(event.x_root - drag["x0"]) < theme.px(self.DRAG_START):
                return
            drag["moving"] = True
            self.canvas.configure(cursor="sb_h_double_arrow")
            self._forget_hover()   # the tabs move under the pointer: no plate or × left behind
        # Like a browser: the tab follows the pointer, swapping places as it passes the others.
        x = event.x_root - self.canvas.winfo_rootx() - theme.px(self.DRAG_GAP)
        target = min(max(0, x // max(1, self._slot)), len(self._titles) - 1)
        if target != drag["index"]:
            source, drag["index"] = drag["index"], target
            self.on_move(source, target)

    def _drag_end(self, _event=None):
        if self._drag is not None and self._drag["moving"]:
            self.canvas.configure(cursor="")
        self._drag = None

    def _wheel(self, event):
        """Mouse wheel over the tabs flicks between them."""
        if self._titles:
            step = -1 if event.delta > 0 else 1
            self.on_select((self._current + step) % len(self._titles))

    def _fit(self, text: str, width: int) -> str:
        """Trim `text` with an ellipsis so it fits `width` pixels (a space before the … goes too:
        "Amyl And…", not "Amyl And …"). Remembered by name and width: a tab switch re-fits every
        name, and measuring a long name down to a short tab takes dozens of measurements."""
        key = (text, width)
        shown = self._fits.get(key)
        if shown is None:
            if len(self._fits) > self.FIT_CACHE:
                self._fits.clear()
            shown = self._fits[key] = self._shorten(text, width)
        return shown

    def _shorten(self, text: str, width: int) -> str:
        if self.text_font.measure(text) <= width:
            return text
        original = text
        while text and self.text_font.measure(text + "…") > width:
            text = text[:-1]
        text = text.rstrip()
        return text + "…" if text else original[:1]  # a very slim tab still shows its first letter


class Toggle(Focusable, tk.Label):
    """A slider switch for an on/off setting: green with the knob on the right when on.
    Drawn with Pillow at 4x and scaled down, so the round ends are smooth (a Tk canvas
    draws jagged circles on Windows). Follows its BooleanVar, however that gets changed.

    The keyboard reaches it (keyfocus): Space or Enter flips it. Its picture keeps a little room
    round the switch (`margin`) for the DG-green ring that shows it has the keyboard."""

    # Pictures are cached per Tk root (root._dg_toggle_images: (on, width, height, colours) -> PhotoImage).
    WIDTH, HEIGHT = 34, 18    # px at 100%: the switch itself

    def __init__(self, parent, root, variable: tk.BooleanVar, command: Callable[[], None] | None = None,
                 bg: str = "SURFACE"):
        self._size = (theme.px(self.WIDTH), theme.px(self.HEIGHT))
        self.margin = keyfocus.ring_room()
        self._app_root, self._bg = root, bg  # (not _root: that name is a Tk method); bg: a theme role
        super().__init__(parent, bg=theme.colour(bg), bd=0, cursor="hand2")
        self.variable, self.command = variable, command
        self._photo = None
        self.bind("<ButtonRelease-1>", lambda e: released_over(e, self) and self.toggle())
        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Destroy>", lambda e: self._untrace(), add="+")
        theme.on_change(self._palette_changed)   # the picture is Pillow-drawn: redraw it in new colours
        self._keyboard()
        self._draw()

    def activate(self):
        self.toggle()

    def focus_changed(self):
        self._draw()

    def toggle(self):
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()

    def _untrace(self):
        try:
            self.variable.trace_remove("write", self._trace)
        except tk.TclError:
            pass
        self._photo = None   # (destroyed: its picture is let go here, on Tk's thread)

    def _palette_changed(self, _palette):
        self.configure(bg=theme.colour(self._bg))
        self._draw()

    def _draw(self):
        if self.winfo_exists():
            # Kept here as well as in the cache it came from: a shared cache may be cleared, and
            # Tk deletes a picture once Python lets go of it.
            self._photo = self._image(bool(self.variable.get()))
            self.configure(image=self._photo)

    def _image(self, on: bool):
        return self._picture(on, theme.ACCENT if on else theme.SWITCH_OFF, theme.ON_ACCENT if on else theme.MUTED)

    def _picture(self, on: bool, track: str, knob: str):
        """The switch in these colours, with the focus ring round it while it has the keyboard."""
        w, h = self._size
        m = self.margin
        bg = theme.colour(self._bg)
        ring = (theme.ACCENT, keyfocus.ring_width()) if self.focused else None
        key = (on, w, h, m, bg, track, knob, ring)
        # (0.19.0) One cache per Tk root: a picture made under an earlier root (tests build
        # several) went with it, and Tk would refuse it ("image doesn't exist").
        images = getattr(self._app_root, "_dg_toggle_images", None)
        if images is None:
            images = self._app_root._dg_toggle_images = {}
        if key not in images:
            k = 4
            W, H = w + 2 * m, h + 2 * m
            img = Image.new("RGB", (W * k, H * k), bg)
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle((m * k, m * k, (m + w) * k - 1, (m + h) * k - 1), radius=h * k // 2, fill=track)
            pad = 3 * k
            d = h * k - 2 * pad
            x = m * k + (w * k - pad - d if on else pad)
            draw.ellipse((x, m * k + pad, x + d, m * k + pad + d), fill=knob)
            if ring:
                draw.rounded_rectangle((0, 0, W * k - 1, H * k - 1), radius=H * k // 2, outline=ring[0],
                                       width=ring[1] * k)
            images[key] = ImageTk.PhotoImage(img.resize((W, H), Image.LANCZOS), master=self._app_root)
        return images[key]


class SettingRow(tk.Frame):
    """One on/off setting: its name, a line saying what it does, and a Toggle on the right.
    Clicking anywhere on the row flips it.

    `wrap`: where the description starts a new line (real px). None: the text column's real width,
    followed as the row is laid out (Settings is narrower while its scrollbar shows), so no line
    stops short and leaves one word on a line of its own; `wrap` is then only the first guess."""

    SWITCH_ROOM = Toggle.WIDTH + theme.GAP_LARGE   # px at 100%: the switch and its gap, right of the text

    def __init__(self, parent, root, title: str, description: str, variable: tk.BooleanVar,
                 command: Callable[[], None] | None = None, wrap: int | None = None, guess: int = 220):
        super().__init__(parent, bg=theme.SURFACE, cursor="hand2")
        self.toggle = Toggle(self, root, variable, command)
        room = self.toggle.margin   # (the switch's picture has room round it for the focus ring)
        self.toggle.pack(side="right", anchor="n", pady=(max(0, theme.px(theme.GAP_HAIR) - room), 0),
                         padx=(max(0, theme.px(theme.GAP_LARGE) - room), 0))
        text = tk.Frame(self, bg=theme.SURFACE)
        text.pack(side="left", fill="x", expand=True)
        name = tk.Label(text, text=title, bg=theme.SURFACE, fg=theme.TEXT, font=theme.font(theme.SIZE_BODY), anchor="w")
        name.pack(fill="x")
        parts = [self, text, name]
        self.about = None
        if description:
            about = self.about = tk.Label(text, text=description, bg=theme.SURFACE, fg=theme.MUTED,
                                          font=theme.font(theme.SIZE_SMALL), anchor="w", justify="left",
                                          wraplength=wrap or guess)
            about.pack(fill="x")
            parts.append(about)
            if wrap is None:
                text.bind("<Configure>", self._column_changed, add="+")
        for w in parts:
            w.bind("<ButtonRelease-1>", lambda e: released_over(e, self) and self.toggle.toggle())

    def _column_changed(self, event):
        """The text column changed width: the description wraps at its new edge. (The width comes from
        the panel, never from the text, so a new wrap can't change it again.)"""
        if event.width > 1 and int(self.about.cget("wraplength")) != event.width:
            self.about.configure(wraplength=event.width)


DISABLED_FADE = 0.45   # (0.21.0) a greyed-out menu item: its text this far from MUTED toward the background


class MenuRow(tk.Frame):
    """One clickable line in a pop-over menu, with an optional shortcut on the right.

    (0.21.0) Also: `enabled=False` greys it out (shown, but does nothing); `check` True / False
    gives it a tick column (a switchable item such as a track-list column); `closes=False` keeps
    the pop-over open when it's chosen (a submenu's way in); `on_hover(row)` tells the menu the
    pointer is on it, so the mouse and the arrow keys share one highlight (`set_active`).

    Rows are MENU_ROW_H tall, their words MENU_INSET in from the menu's sides. The highlight is a
    rounded HOVER plate (`plate`), MENU_PLATE_INSET in from the sides: a square frame whose four
    corners are rounded off by small pictures drawn on the menu's SURFACE. `ticks`: keep a tick
    column even with no tick (every row of a menu that has ticks, so the words line up);
    `chevron="right"`: a way into a submenu (a chevron where the shortcut would be), `"left"`: the
    way back out (a chevron before the words, which are semibold — the submenu's title). Anything
    else put on the plate that has `set_backdrop(role)` is told when the row lights up.
    `lead` (even "") gives it a narrow column before the words, `lead_width` characters wide, for a
    small mark (Up next: how much of a network song is downloaded) drawn in `lead_fg` (a colour)."""

    def __init__(self, parent, text: str, command: Callable[[], None], shortcut: str = "", *,
                 enabled: bool = True, check: bool | None = None, closes: bool = True,
                 on_hover: Callable[[MenuRow], None] | None = None, ticks: bool | None = None,
                 chevron: str = "", lead: str | None = None, lead_fg: str = "", lead_width: int = 5):
        super().__init__(parent, bg=theme.SURFACE, cursor="hand2")
        self.command = command
        self.enabled = enabled
        self.closes = closes
        self.active = False
        self.on_hover = on_hover
        self.text = text
        self.chevron = chevron
        inset = theme.px(theme.MENU_PLATE_INSET)
        gap = max(1, inset // 2)
        self.plate = tk.Frame(self, bg=theme.SURFACE, cursor="hand2")
        self.plate.pack(fill="both", expand=True, padx=inset, pady=gap)
        # A strut as tall as the plate should be: the row is MENU_ROW_H whatever the font.
        strut = tk.Frame(self.plate, bg=theme.SURFACE, width=1, height=max(1, theme.px(theme.MENU_ROW_H) - 2 * gap))
        strut.pack(side="left")
        words_in = max(0, theme.px(theme.MENU_INSET) - inset - 1)
        parts = [self, self.plate, strut]
        self.tick = None
        self.checked = check
        column = check is not None if ticks is None else ticks
        if column or chevron == "left":
            self._column = shapes.spacer_photo(self, theme.px(theme.MENU_INSET + theme.GAP_SMALL), 1)
            self.tick = tk.Label(self.plate, text=glyph(self, "check") if check else "", image=self._column,
                                 compound="center", bg=theme.SURFACE, fg=theme.ACCENT_TEXT, bd=0, padx=0, pady=0,
                                 font=icon_font(self, theme.ICON_SMALL) or theme.font(theme.SIZE_BODY, "bold"))
            self.tick.pack(side="left", padx=(words_in, 0))
            parts.append(self.tick)
            words_in = 0
        self.lead = None
        if lead is not None:
            self.lead = tk.Label(self.plate, text=lead, bg=theme.SURFACE, fg=lead_fg or theme.MUTED,
                                 font=theme.font(theme.SIZE_SMALL), anchor="w", padx=0, pady=0, bd=0,
                                 width=lead_width)
            self.lead.pack(side="left", padx=(words_in, theme.px(theme.GAP_MEDIUM)))
            parts.append(self.lead)
            words_in = 0
        weight = ("semibold",) if chevron == "left" else ()
        self.label = tk.Label(self.plate, text=text, bg=theme.SURFACE, fg=theme.TEXT, anchor="w", padx=0, pady=0,
                              bd=0, font=theme.font(theme.SIZE_BODY, *weight))
        self.label.pack(side="left", fill="x", expand=True, padx=(words_in, 0))
        self.keys = tk.Label(self.plate, text="" if chevron == "right" else shortcut, bg=theme.SURFACE, fg=theme.MUTED,
                             font=theme.font(theme.SIZE_SMALL), padx=0, pady=0, bd=0)
        self.keys.pack(side="right", padx=(theme.px(theme.GAP_MEDIUM), max(0, theme.px(theme.MENU_INSET) - inset)))
        parts += [self.label, self.keys]
        self._parts = parts
        self._corners: list[tk.Label] = []
        for w in parts:
            self._bind_part(w)
        theme.on_change(self._palette_changed)
        self._paint_chevron()
        if not enabled:
            self.set_enabled(False)

    def _bind_part(self, w: tk.Misc):
        w.bind("<Enter>", lambda e: self._entered())
        w.bind("<Leave>", lambda e: self.set_active(False))
        w.bind("<ButtonRelease-1>", lambda e: released_over(e, self) and self._run())

    def _paint_chevron(self, ink: str | None = None):
        """The drawn chevron (a submenu's way in or out), in the row's words' colour."""
        if not self.chevron:
            return
        height = theme.px(theme.MENU_INSET)
        width = height + (theme.px(theme.GAP_SMALL) if self.chevron == "left" else 0)   # (the tick column's)
        ink = ink or theme.MUTED
        direction = self.chevron
        self._chevron = shapes.photo(self, ("menu chevron", direction, width, height, ink),
                                     lambda: shapes.chevron(width, height, ink, direction))
        (self.keys if direction == "right" else self.tick).configure(image=self._chevron)

    def _entered(self):
        if self.on_hover is not None:
            self.on_hover(self)        # the menu moves its highlight here (and off the last row)
        else:
            self.set_active(True)

    def set_active(self, active: bool):
        """Highlight the row (under the pointer, or chosen with the arrow keys). A greyed-out row
        never lights up."""
        self.active = active and self.enabled
        self._paint(theme.HOVER if self.active else theme.SURFACE)

    def _paint(self, bg):
        for w in self._parts:
            if w is not self:
                w.configure(bg=bg)
        for w in self.plate.winfo_children():
            if hasattr(w, "set_backdrop"):
                w.set_backdrop("HOVER" if self.active else "SURFACE")
        if self.active:
            self._round_plate()
        else:
            for corner in self._corners:
                corner.place_forget()

    def _round_plate(self):
        """Round the lit plate's corners: four small pictures over its square ones."""
        radius = theme.px(theme.RADIUS_S)
        pictures = [shapes.photo(self, ("menu corner", i, radius, theme.HOVER, theme.SURFACE),
                                 lambda i=i: shapes.corners(radius, theme.HOVER, theme.SURFACE)[i]) for i in range(4)]
        if not self._corners:
            for _ in range(4):
                corner = tk.Label(self.plate, bd=0, padx=0, pady=0, highlightthickness=0, cursor="hand2")
                self._bind_part(corner)
                self._corners.append(corner)
        self._corner_pictures = pictures   # (kept: a shared cache may be cleared)
        for corner, picture, (relx, rely, anchor) in zip(self._corners, pictures, (
                (0, 0, "nw"), (1, 0, "ne"), (0, 1, "sw"), (1, 1, "se"))):
            corner.configure(image=picture)
            corner.place(relx=relx, rely=rely, anchor=anchor)
            corner.lift()

    def disabled_colour(self) -> str:
        return theme.mix("MUTED", "SURFACE", DISABLED_FADE)

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        faded = self.disabled_colour()
        self.label.configure(fg=theme.TEXT if enabled else faded)
        self.keys.configure(fg=theme.MUTED if enabled else faded)
        self._paint_chevron(None if enabled else faded)
        for w in self._parts:
            w.configure(cursor="hand2" if enabled else "arrow")
        self.set_active(False)

    def _palette_changed(self, _palette):
        if not self.enabled:            # the faded grey is worked out, not a role: work it out again
            self.set_enabled(False)
        else:
            self._paint_chevron()
        if self.active:
            self._round_plate()

    def _run(self):
        if self.enabled:
            if self.closes:
                Popover.close_open()
            self.command()


class Popover(tk.Frame):
    """A small panel that drops down from the strip (settings, home menu, new-tab menu), or
    (0.21.0) appears at the pointer (`show_at`: right-click menus, popups.PopupMenu).
    Closes on Escape or a click anywhere outside it.

    (0.21.0) `scroll=True`: the panel is never taller than its window — when the content doesn't
    fit, it scrolls (mouse wheel anywhere over it, and controls.ScrollArea's slim scrollbar that
    only shows when needed). Settings needs this: at 150% display scaling it is ~1300 px tall.
    `transient=True`: the panel destroys itself when it closes (menus built for one right-click).
    `keys=True`: the arrow keys move through its MenuRows, Enter runs one, Esc closes; the panel
    takes the keyboard while it's open and gives it back when it closes.
    `controls=True` (Settings): the panel takes the keyboard while it's open, Tab and Shift+Tab go
    round its controls top to bottom (keyfocus.cycle), Esc closes it and the keyboard goes back.
    `at_edge=True`: opened by `toggle`, it sits MARGIN in from the window's right edge (not under
    its button).

    The panel is SURFACE with a 1 px EDGE rim."""

    open_one: Popover | None = None
    MARGIN = 8   # px at 100%: kept clear between the panel and its window's edges

    def __init__(self, root: tk.Misc, width: int, *, scroll: bool = False, transient: bool = False,
                 keys: bool = False, padding: tuple[int, int] = (theme.GAP_WIDE, theme.GAP_LARGE),
                 controls: bool = False, at_edge: bool = False):
        super().__init__(root, bg=theme.SURFACE, highlightthickness=1, highlightbackground=theme.EDGE,
                         highlightcolor=theme.EDGE)
        self.root = root
        self.width = width
        self.scroll, self.transient, self.keys_on = scroll, transient, keys
        self.controls_on, self.at_edge = controls, at_edge
        self.area = None
        if scroll:
            from .controls import ScrollArea  # (controls imports this module: import here, not at the top)
            self.area = ScrollArea(self, bg="SURFACE")
            self.area.pack(fill="both", expand=True)
            holder = self.area.inner
        else:
            holder = self
        self.body = tk.Frame(holder, bg=theme.SURFACE)
        self.body.pack(fill="both", expand=True, padx=theme.px(padding[0]), pady=theme.px(padding[1]))
        self.anchor_widget: tk.Widget | None = None
        self._top = 0                  # where the panel's top edge sits in its window (px)
        self._at: tuple[int, int] | None = None   # show_at's point, in the window (px)
        self._focus_before: tk.Misc | None = None
        self.active_row: MenuRow | None = None
        self.on_hide: Callable[[], None] | None = None   # (0.25.0) told when the panel closes
        # One set of bindings per window, however many pop-overs it has (menus are made per right-click).
        if not getattr(root, "_dg_popover_bound", False):
            root._dg_popover_bound = True
            root.bind("<ButtonPress-1>", Popover._click_anywhere, add="+")
            root.bind("<Escape>", lambda e: Popover._escape(root, e), add="+")
            root.bind("<Configure>", lambda e: Popover._window_resized(e, root), add="+")
        if controls:
            self.configure(takefocus=1)
        if keys:
            self.configure(takefocus=1)
            for key, step in (("<Down>", 1), ("<Up>", -1), ("<Tab>", 1), ("<Shift-Tab>", -1)):
                self.bind(key, lambda e, s=step: self.move(s) or "break")
            self.bind("<Home>", lambda e: self.move_to(0) or "break")
            self.bind("<End>", lambda e: self.move_to(-1) or "break")
            for key in ("<Return>", "<KP_Enter>", "<space>"):
                self.bind(key, lambda e: self.run_active() or "break")
            self.bind("<Escape>", lambda e: self.escape() or "break")
            # Any other key is swallowed while the menu has the keyboard (so "n" doesn't skip a track).
            self.bind("<Key>", lambda e: "break")

    @property
    def is_open(self) -> bool:
        return Popover.open_one is self

    def toggle(self, under: tk.Widget, top: int):
        if self.is_open:
            self.hide()
            return
        Popover.close_open()
        self.anchor_widget = under
        width = min(self.width, self.root.winfo_width() - 2 * theme.px(self.MARGIN))
        right = under.winfo_rootx() + under.winfo_width() - self.root.winfo_rootx()
        margin = theme.px(self.MARGIN)
        x = max(margin, min(right - width, self.root.winfo_width() - width - margin))
        if self.at_edge:
            x = max(margin, self.root.winfo_width() - width - margin)
        self._top, self._at = top, None
        self.place(x=x, y=top, width=width)
        self._opened()
        if isinstance(under, IconButton):
            under.set_active(True)
        self.fit_height()

    def show_at(self, x_root: int, y_root: int):
        """Open at a screen point (a right-click), kept inside the window: moved left when it
        would run off the right edge, and opened upwards when there's no room below."""
        Popover.close_open()
        self.anchor_widget = None
        self._at = (x_root - self.root.winfo_rootx(), y_root - self.root.winfo_rooty())
        self.place(x=0, y=0, width=1)   # mapped first, so its natural size can be measured
        self._opened()
        self.reseat()

    def reseat(self):
        """Size and place a show_at panel again (its content changed, or the window did)."""
        if self._at is None or not self.is_open:
            self.fit_height()
            return
        margin = theme.px(self.MARGIN)
        self.update_idletasks()
        room_w, room_h = self.root.winfo_width(), self.root.winfo_height()
        width = min(max(self.width, self.natural_width()), room_w - 2 * margin)
        height = min(self.natural_height(), room_h - 2 * margin)
        x, y = self._at
        x = max(margin, min(x, room_w - width - margin))
        if y + height > room_h - margin:     # no room below the pointer: open upwards (or as low as fits)
            y = y - height if y - height >= margin else max(margin, room_h - height - margin)
        self._top = y
        self.place_configure(x=x, y=y, width=width, height=max(1, height))
        if self.area is not None:
            self.area.bind_wheel()

    def natural_width(self) -> int:
        border = 2 * int(self.cget("highlightthickness"))
        content = self.body.winfo_reqwidth() + 2 * int(self.body.pack_info().get("padx", 0) or 0)
        return content + border + (self.area.scrollbar.winfo_reqwidth() if self.area is not None else 0)

    def natural_height(self) -> int:
        border = 2 * int(self.cget("highlightthickness"))
        holder = self.area.inner if self.area is not None else self.body
        pady = 0 if self.area is not None else 2 * int(self.body.pack_info().get("pady", 0) or 0)
        return holder.winfo_reqheight() + pady + border

    def fit_height(self):
        """A scrolling panel opened by `toggle`: as tall as its content, but never past the bottom
        of the window (then it scrolls)."""
        if self.area is None or not self.is_open or self._at is not None:
            return
        self.update_idletasks()
        room = self.root.winfo_height() - self._top - theme.px(self.MARGIN)
        self.place_configure(height=max(theme.px(60), min(self.natural_height(), room)))
        self.area.bind_wheel()

    def _opened(self):
        self.lift()
        Popover.open_one = self
        if self.keys_on:
            self.wire_rows()
        if self.keys_on or self.controls_on:
            try:
                self._focus_before = self.focus_get()
            except (tk.TclError, KeyError):   # (focus on a widget Python has no object for)
                self._focus_before = None
            if self.controls_on:
                keyfocus.cycle(self, self.area)
            self.focus_set()

    def wire_rows(self):
        """Let the rows share the highlight with the arrow keys (call again after adding rows)."""
        self.active_row = None
        for row in self.menu_rows():
            row.on_hover = self.hover

    def hide(self):
        # The keyboard goes back where it was only if it's still in the panel: closed by a click on
        # something that takes the keyboard itself (the list, the search box), it stays there.
        keyboard_here = self._has_keyboard()
        self.place_forget()
        if isinstance(self.anchor_widget, IconButton):
            self.anchor_widget.set_active(False)
        if Popover.open_one is self:
            Popover.open_one = None
        before, self._focus_before = self._focus_before, None
        if (self.keys_on or self.controls_on) and before is not None and keyboard_here:
            try:
                if before.winfo_exists():
                    before.focus_set()     # the keyboard goes back where it was (e.g. the track list)
            except (tk.TclError, KeyError):
                pass
        if self.on_hide is not None:
            self.on_hide()
        if self.transient:
            self.after_idle(self._destroy_quietly)

    def _has_keyboard(self) -> bool:
        """Is the keyboard (in this window) on the panel or something inside it?"""
        try:
            here, me = str(self.tk.call("focus", "-lastfor", self)), str(self)
        except tk.TclError:
            return True
        return here == me or here.startswith(me + ".")

    def _destroy_quietly(self):
        try:
            if self.winfo_exists():
                self.destroy()
        except tk.TclError:
            pass

    # -- the keyboard (keys=True) ---------------------------------------------------------------
    def menu_rows(self) -> list[MenuRow]:
        """The panel's MenuRows, top to bottom."""
        found, stack = [], list(reversed(self.body.winfo_children()))
        while stack:
            w = stack.pop()
            if isinstance(w, MenuRow):
                found.append(w)
            else:
                stack.extend(reversed(w.winfo_children()))
        return found

    def hover(self, row: MenuRow):
        """The pointer went onto `row`: it takes the one highlight (shared with the arrow keys)."""
        if self.active_row is not None and self.active_row is not row:
            try:
                self.active_row.set_active(False)
            except tk.TclError:
                pass
        self.active_row = row if row.enabled else None
        row.set_active(True)

    def move(self, step: int):
        """Arrow keys: the next (step 1) or previous (-1) item that can be chosen, round the ends."""
        rows = [r for r in self.menu_rows() if r.enabled]
        if not rows:
            return
        if self.active_row in rows:
            index = (rows.index(self.active_row) + step) % len(rows)
        else:
            index = 0 if step > 0 else len(rows) - 1
        self.hover(rows[index])
        if self.area is not None:
            self.area.see(rows[index])

    def move_to(self, index: int):
        rows = [r for r in self.menu_rows() if r.enabled]
        if rows:
            self.hover(rows[index])
            if self.area is not None:
                self.area.see(rows[index])

    def run_active(self):
        if self.active_row is not None and self.active_row.enabled:
            self.active_row._run()

    def escape(self):
        Popover.close_open()

    @staticmethod
    def close_open() -> bool:
        """Close the open pop-over. Returns whether there was one."""
        if Popover.open_one is None:
            return False
        try:
            Popover.open_one.hide()
        except tk.TclError:   # its window has gone (closed with the panel open)
            Popover.open_one = None
        return True

    @staticmethod
    def _escape(root, event):
        """Esc in a window: it closes the open pop-over; with none open, the window's own Esc
        (`root._dg_escape(event)`, when the window sets one) has it."""
        if Popover.close_open():
            return
        fallback = getattr(root, "_dg_escape", None)
        if fallback is not None:
            fallback(event)

    @staticmethod
    def _click_anywhere(event):
        pop = Popover.open_one
        if pop is None:
            return
        # Compare Tk paths: also covers parts Python has no object for (a combobox's list).
        path = str(event.widget)
        inside = [str(pop)] + ([str(pop.anchor_widget)] if pop.anchor_widget is not None else [])
        if any(path == p or path.startswith(p + ".") for p in inside):
            return
        pop.hide()

    @staticmethod
    def _window_resized(event, window):
        """The window changed size: the open panel keeps fitting it (a scrolling one gets taller or
        shorter; a right-click menu stays inside)."""
        pop = Popover.open_one
        if event.widget is not window or pop is None or pop.root is not window:
            return
        if pop._at is not None:
            pop.reseat()
        else:
            pop.fit_height()

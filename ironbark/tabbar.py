"""The strip along the top of the window: tabs (one per album or library view), the
+ / home / gear buttons and, on Windows, our own minimise / close.

The tabs always share the one row with the buttons: as more open, or the
window narrows, they get slimmer and their names shorten, but they never drop to a row
of their own.

Pressing on any empty part of the strip drags the window (natively, via chrome.py);
double-clicking it calls `on_double` (snap home).
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable

from PIL import Image, ImageDraw, ImageTk

from . import chrome, theme

# Colours are theme.py roles, read when drawn (so they follow a palette change): the strip is
# STRIP, the tab on show BG, a tab something dragged would land on SURFACE_HI.

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


TOOLTIP_DELAY_MS = 450   # (0.21.0) every tooltip waits this long under a still pointer — one delay app-wide
TOOLTIP_WRAP = 320       # px at 100%: a tooltip's longest line (less in a narrow window); longer text wraps
TOOLTIP_MIN_WRAP = 160   # px at 100%: …but never narrower than this


class Tooltip:
    """A small label that appears after hovering a moment. `text` may be a function, so it
    can describe the current state (e.g. what the status square means right now).

    (0.21.0) One delay for every tooltip (TOOLTIP_DELAY_MS); long text wraps to fit the window it
    belongs to (at most TOOLTIP_WRAP wide), and the tip goes above the widget when there's no room
    below it in the window."""

    def __init__(self, widget: tk.Misc, text):
        self.widget, self.text = widget, text
        self._job = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._job = self.widget.after(TOOLTIP_DELAY_MS, self._show)

    def _cancel(self):
        if self._job:
            self.widget.after_cancel(self._job)
            self._job = None

    def wrap(self) -> int:
        """How wide (real px) the tip's text may run: TOOLTIP_WRAP, less in a narrow window."""
        top = self.widget.winfo_toplevel()
        room = top.winfo_width() - theme.px(2 * theme.GAP_MEDIUM)
        return max(theme.px(TOOLTIP_MIN_WRAP), min(theme.px(TOOLTIP_WRAP), room))

    def _show(self):
        self._job = None
        text = self.text() if callable(self.text) else self.text
        if not text or not self.widget.winfo_exists():
            return
        tip = tk.Toplevel(self.widget)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, bg=theme.SURFACE_HI, fg=theme.TEXT, font=theme.font(theme.SIZE_BODY),
                 padx=theme.px(theme.GAP_MEDIUM), pady=theme.px(theme.GAP_SMALL), justify="left",
                 wraplength=self.wrap(), highlightthickness=1, highlightbackground=theme.BORDER).pack()
        tip.update_idletasks()
        top = self.widget.winfo_toplevel()
        x = self.widget.winfo_rootx()
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
            self._tip.destroy()
            self._tip = None


class IconButton(tk.Label):
    """A flat icon button with a hover colour (tk.Label, so it can sit on the dark strip).
    Colours are theme roles ("STRIP", "ACCENT", …), looked up each time it's painted, so the
    button follows a palette change; `size` is an icon size (theme.ICON_*), `width` px at 100%."""

    def __init__(self, parent, root, name: str, command: Callable[[], None], size: int = theme.ICON,
                 width: int = 34, bg: str = "STRIP", hover: str = "SURFACE_HI", fg: str = "MUTED",
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
        """Change the resting colour, a theme role (e.g. "ACCENT" when a toggle like shuffle is on)."""
        self._fg = fg
        self._paint(False)

    def set_active(self, active: bool):
        self.active = active
        self._paint(False)

    def _paint(self, hover: bool):
        lit = hover or self.active
        self.configure(bg=theme.colour(self._hover if lit else self._bg),
                       fg=theme.colour(self._hover_fg if hover else ("ACCENT" if self.active else self._fg)))


class TabBar(tk.Frame):
    DRAG_GAP = 28              # px at 100%: empty space left of the first tab, for dragging the window
    MIN_TAB = 6                # px at 100%: just a sliver, so however many are open they all fit (and +)
    CLOSE_REVEAL_MS = 450      # hover a tab this long and its small close button fades in

    DRAG_START = 6             # px at 100%: move this far with the button down and a tab press becomes a drag

    def __init__(self, parent, root: tk.Tk, *, on_select, on_close, on_new, on_settings, on_double,
                 on_quit, on_tab_menu=lambda index, event: None, on_move=lambda source, target: None):
        self.row_height = theme.px(36)
        self.height = self.row_height
        super().__init__(parent, bg=theme.STRIP)
        self.root = root
        self.on_select, self.on_close = on_select, on_close
        self.on_double = on_double
        self.on_tab_menu = on_tab_menu
        self.on_move = on_move
        # A tab being dragged: {"index", "x0", "moving"}. Kept here, not on the tab widgets: the
        # tabs change under the pointer mid-drag (each swap moves the names along the slots).
        self._drag: dict | None = None
        self._slot = 1  # one tab's width plus its gap, set by _layout
        self.top_row = tk.Frame(self, bg=theme.STRIP, height=self.row_height)
        self.top_row.pack(fill="x")
        self.top_row.pack_propagate(False)
        self._titles: list[str] = []
        self._current = -1
        self._playing: int | None = None
        self._state = None
        self._highlight: int | None = None  # the tab something being dragged would land on
        # (0.23.0) One slot per tab, made once and repainted on every change (select, rename, drag,
        # resize) — rebuilding the whole strip on each tab switch took 16–52 ms (3–10 tabs, measured
        # 05/10/2026). Slot i always shows tab i; a slot is only made or destroyed when the number
        # of tabs changes. Each slot frame carries .line, .label, .close and .shown (the text on it).
        self._tab_widgets: list[tk.Frame] = []
        self.text_font = tkfont.Font(root=root, family=theme.FONT, size=theme.SIZE_BODY)

        if chrome.CUSTOM:
            self.close_btn = IconButton(self, root, "close", on_quit, size=theme.ICON_SMALL, width=40,
                                        hover="CLOSE_HOVER", hover_fg="ON_ACCENT", label="Close")
            self.close_btn.pack(in_=self.top_row, side="right", fill="y")
            # No maximise button: a corner player rarely wants it, and the strip needs the room.
            # Windows+Up still maximises; so does double-clicking the strip until a home is set.
            self.min_btn = IconButton(self, root, "min", root.iconify, size=theme.ICON_SMALL, width=40,
                                      label="Minimise")
            self.min_btn.pack(in_=self.top_row, side="right", fill="y")
        self.settings_btn = IconButton(self, root, "settings", on_settings, width=30, label="Settings")
        self.settings_btn.pack(in_=self.top_row, side="right", fill="y", padx=theme.pad(0, theme.GAP_SMALL))

        self.tabs_frame = tk.Frame(self, bg=theme.STRIP)
        self.add_btn = IconButton(self, root, "add", on_new, size=theme.ICON_SMALL, width=28, label="New tab")
        self.spacer = tk.Frame(self, bg=theme.STRIP)
        # A gap before the first tab, like Firefox: somewhere to grab the window by.
        self.tabs_frame.pack(in_=self.top_row, side="left", fill="y", padx=(theme.px(self.DRAG_GAP), 0))
        self.add_btn.pack(in_=self.top_row, side="left", fill="y")
        self.spacer.pack(in_=self.top_row, side="left", fill="both", expand=True)

        for widget in (self, self.top_row, self.spacer, self.tabs_frame):
            widget.bind("<ButtonPress-1>", lambda e: chrome.start_move(self.root))
            widget.bind("<Double-Button-1>", lambda e: self.on_double())
            widget.bind("<MouseWheel>", self._wheel)
            widget.bind("<B1-Motion>", self._drag_motion)  # a tab dragged past the others' ends
            widget.bind("<ButtonRelease-1>", self._drag_end)
        self.bind("<Configure>", lambda e: self._layout())

    # -- tabs ----------------------------------------------------------------
    def set_tabs(self, titles: list[str], current: int, playing: int | None):
        state = (list(titles), current, playing)
        if state == self._state:
            return
        self._state = state
        self._titles, self._current, self._playing = list(titles), current, playing
        self._layout()

    def _layout(self):
        n = len(self._titles)
        while len(self._tab_widgets) > n:          # fewer tabs: drop the spare slots
            self._tab_widgets.pop().destroy()
        if not n:
            return
        width = self.winfo_width()
        buttons = [self.settings_btn]
        if chrome.CUSTOM:
            buttons += [self.min_btn, self.close_btn]
        room = (width - sum(w.winfo_reqwidth() for w in buttons) - self.add_btn.winfo_reqwidth()
                - theme.px(self.DRAG_GAP + 10))
        # Equal widths, so the albums sit side by side and you can flick between them; with
        # many open they slim down (names shortened with …) rather than leave the row.
        per_tab = max(theme.px(self.MIN_TAB), min(theme.px(200), room // n - theme.px(2)))
        self._slot = per_tab + theme.px(2)
        while len(self._tab_widgets) < n:          # more tabs: one new slot each
            self._tab_widgets.append(self._make_tab(len(self._tab_widgets)))
        for i, title in enumerate(self._titles):
            self._paint_tab(i, title, per_tab)

    def _make_tab(self, i: int) -> tk.Frame:
        """Slot i: a frame with the playing line along its bottom, the name, and the close button
        that fades in on hover. Its bindings use the slot's number, which is always the tab's."""
        tab = tk.Frame(self.tabs_frame, height=theme.px(30))
        tab.pack_propagate(False)
        tab.pack(side="left", anchor="s", padx=(0, theme.px(2)))
        # The tab playing music gets a green line along its bottom edge.
        # (A drop target gets a green outline instead, so it's clear where a drag will land.)
        tab.line = tk.Frame(tab, height=max(2, theme.px(2)))
        tab.line.pack(side="bottom", fill="x")
        tab.label = tk.Label(tab, font=self.text_font, anchor="w")
        tab.shown = ""
        # The whole name on hover, when it's shortened (or the tab is too slim to show it): on the
        # name when it shows, else on the tab itself — never both, or two tips would open.
        Tooltip(tab.label, lambda: self._full_name(i))
        Tooltip(tab, lambda: "" if tab.label.winfo_manager() else self._full_name(i))
        for w in (tab, tab.label):
            w.bind("<ButtonPress-1>", lambda e: self._press(i, e))
            w.bind("<B1-Motion>", self._drag_motion)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Button-2>", lambda e: self.on_close(i))  # middle-click closes, as in browsers
            w.bind("<MouseWheel>", self._wheel)
            w.bind("<Button-3>", lambda e: self.on_tab_menu(i, e))
        self._add_close_button(tab, i)
        return tab

    def _full_name(self, i: int) -> str:
        """Tab i's whole name for its tooltip — only when the tab shows less than that."""
        if i >= len(self._titles) or i >= len(self._tab_widgets):
            return ""
        title = self._titles[i]
        return title if self._tab_widgets[i].shown != title else ""

    def _paint_tab(self, i: int, title: str, per_tab: int):
        """Show slot i as tab `title`: selected / playing / drop target, width, shortened name."""
        tab = self._tab_widgets[i]
        active, playing, target = i == self._current, i == self._playing, i == self._highlight
        bg = theme.SURFACE_HI if target else (theme.BG if active else theme.STRIP)
        tab.configure(bg=bg, width=per_tab)
        tab.line.configure(bg=theme.ACCENT if playing else bg)
        thick = max(2, theme.px(2)) if target else 0
        tab.configure(highlightthickness=thick, highlightbackground=theme.ACCENT, highlightcolor=theme.ACCENT)
        text_room = per_tab - theme.px(8) - theme.px(4)
        if text_room >= theme.px(10):
            tab.shown = self._fit(title, text_room)
            tab.label.configure(text=tab.shown, bg=bg, fg=theme.TEXT if active or target else theme.MUTED)
            if not tab.label.winfo_manager():
                tab.label.pack(side="left", fill="x", expand=True, padx=(theme.px(6), theme.px(2)))
        else:
            tab.shown = ""
            tab.label.pack_forget()
        tab.close.configure(bg=bg)

    def _add_close_button(self, tab: tk.Frame, index: int):
        """A small close button that takes no room: it only fades in after hovering a tab
        for a moment, so it's easy to reach on purpose but hard to hit by accident."""
        icon = icon_font(self.root, theme.ICON_SMALL)
        small = (icon[0], theme.TAB_CLOSE) if icon else theme.font(theme.TAB_CLOSE)
        close = tab.close = tk.Label(tab, text=glyph(self.root, "x"), font=small, fg=theme.MUTED, cursor="hand2",
                                     padx=theme.px(3))
        close.bind("<ButtonRelease-1>", lambda e: self.on_close(index))
        close.bind("<Enter>", lambda e: close.configure(fg=theme.TEXT))
        close.bind("<Leave>", lambda e: close.configure(fg=theme.MUTED))
        state = {"job": None}

        def reveal():
            state["job"] = None
            if tab.winfo_exists():
                close.place(relx=1.0, x=-theme.px(4), rely=0.45, anchor="e")
                close.lift()

        def enter(_event):
            if state["job"] is None and not close.winfo_ismapped():
                state["job"] = self.after(self.CLOSE_REVEAL_MS, reveal)

        def leave(_event):
            self.after(60, hide_if_gone)

        def hide_if_gone():
            if not tab.winfo_exists():
                return
            x, y = self.winfo_pointerxy()
            under = str(self.winfo_containing(x, y) or "")
            if under == str(tab) or under.startswith(str(tab) + "."):
                return  # still over the tab (or its close button)
            if state["job"] is not None:
                self.after_cancel(state["job"])
                state["job"] = None
            close.place_forget()

        for w in (tab, tab.label, close):
            w.bind("<Enter>", enter, add="+")
            w.bind("<Leave>", leave, add="+")

    # -- drag and drop onto a tab ---------------------------------------------
    def tab_at(self, x_root: int, y_root: int) -> int | None:
        """The tab under this screen point (None: not over a tab)."""
        for i, widget in enumerate(self._tab_widgets):
            try:
                x, y = widget.winfo_rootx(), widget.winfo_rooty()
                if x <= x_root < x + widget.winfo_width() and y <= y_root < y + widget.winfo_height():
                    return i
            except tk.TclError:
                return None
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
            self.configure(cursor="sb_h_double_arrow")
        # Like a browser: the tab follows the pointer, swapping places as it passes the others.
        x = event.x_root - self.tabs_frame.winfo_rootx()
        target = min(max(0, x // max(1, self._slot)), len(self._titles) - 1)
        if target != drag["index"]:
            source, drag["index"] = drag["index"], target
            self.on_move(source, target)

    def _drag_end(self, _event=None):
        if self._drag is not None and self._drag["moving"]:
            self.configure(cursor="")
        self._drag = None

    def _wheel(self, event):
        """Mouse wheel over the tabs flicks between them."""
        if self._titles:
            step = -1 if event.delta > 0 else 1
            self.on_select((self._current + step) % len(self._titles))

    def _fit(self, text: str, width: int) -> str:
        """Trim `text` with an ellipsis so it fits `width` pixels."""
        if self.text_font.measure(text) <= width:
            return text
        original = text
        while text and self.text_font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…" if text else original[:1]  # a very slim tab still shows its first letter


class Toggle(tk.Label):
    """A slider switch for an on/off setting: green with the knob on the right when on.
    Drawn with Pillow at 4x and scaled down, so the round ends are smooth (a Tk canvas
    draws jagged circles on Windows). Follows its BooleanVar, however that gets changed."""

    # Pictures are cached per Tk root (root._dg_toggle_images: (on, width, height, colours) -> PhotoImage).

    def __init__(self, parent, root, variable: tk.BooleanVar, command: Callable[[], None] | None = None,
                 bg: str = "SURFACE"):
        self._size = (theme.px(34), theme.px(18))
        self._app_root, self._bg = root, bg  # (not _root: that name is a Tk method); bg: a theme role
        super().__init__(parent, bg=theme.colour(bg), bd=0, cursor="hand2")
        self.variable, self.command = variable, command
        self.bind("<ButtonRelease-1>", lambda e: self.toggle())
        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Destroy>", lambda e: self._untrace(), add="+")
        theme.on_change(self._palette_changed)   # the picture is Pillow-drawn: redraw it in new colours
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

    def _palette_changed(self, _palette):
        self.configure(bg=theme.colour(self._bg))
        self._draw()

    def _draw(self):
        if self.winfo_exists():
            self.configure(image=self._image(bool(self.variable.get())))

    def _image(self, on: bool):
        w, h = self._size
        bg, track = theme.colour(self._bg), theme.ACCENT if on else theme.SWITCH_OFF
        knob = theme.ON_ACCENT if on else theme.MUTED
        key = (on, w, h, bg, track, knob)
        # (0.19.0) One cache per Tk root: a picture made under an earlier root (tests build
        # several) went with it, and Tk would refuse it ("image doesn't exist").
        images = getattr(self._app_root, "_dg_toggle_images", None)
        if images is None:
            images = self._app_root._dg_toggle_images = {}
        if key not in images:
            k = 4
            img = Image.new("RGB", (w * k, h * k), bg)
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=h * k // 2, fill=track)
            pad = 3 * k
            d = h * k - 2 * pad
            x = w * k - pad - d if on else pad
            draw.ellipse((x, pad, x + d, pad + d), fill=knob)
            images[key] = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS), master=self._app_root)
        return images[key]


class SettingRow(tk.Frame):
    """One on/off setting: its name, a line saying what it does, and a Toggle on the right.
    Clicking anywhere on the row flips it."""

    def __init__(self, parent, root, title: str, description: str, variable: tk.BooleanVar,
                 command: Callable[[], None] | None = None, wrap: int = 220):
        super().__init__(parent, bg=theme.SURFACE, cursor="hand2")
        self.toggle = Toggle(self, root, variable, command)
        self.toggle.pack(side="right", anchor="n", pady=theme.pad(theme.GAP_HAIR, 0), padx=theme.pad(theme.GAP_LARGE, 0))
        text = tk.Frame(self, bg=theme.SURFACE)
        text.pack(side="left", fill="x", expand=True)
        name = tk.Label(text, text=title, bg=theme.SURFACE, fg=theme.TEXT, font=theme.font(theme.SIZE_BODY), anchor="w")
        name.pack(fill="x")
        parts = [self, text, name]
        if description:
            about = tk.Label(text, text=description, bg=theme.SURFACE, fg=theme.MUTED,
                             font=theme.font(theme.SIZE_SMALL), anchor="w", justify="left", wraplength=wrap)
            about.pack(fill="x")
            parts.append(about)
        for w in parts:
            w.bind("<ButtonRelease-1>", lambda e: self.toggle.toggle())


DISABLED_FADE = 0.45   # (0.21.0) a greyed-out menu item: its text this far from MUTED toward the background


class MenuRow(tk.Frame):
    """One clickable line in a pop-over menu, with an optional shortcut on the right.

    (0.21.0) Also: `enabled=False` greys it out (shown, but does nothing); `check` True / False
    gives it a tick column (a switchable item such as a track-list column); `closes=False` keeps
    the pop-over open when it's chosen (a submenu's way in); `on_hover(row)` tells the menu the
    pointer is on it, so the mouse and the arrow keys share one highlight (`set_active`)."""

    def __init__(self, parent, text: str, command: Callable[[], None], shortcut: str = "", *,
                 enabled: bool = True, check: bool | None = None, closes: bool = True,
                 on_hover: Callable[["MenuRow"], None] | None = None):
        super().__init__(parent, bg=theme.SURFACE, cursor="hand2")
        self.command = command
        self.enabled = enabled
        self.closes = closes
        self.active = False
        self.on_hover = on_hover
        self.text = text
        parts = [self]
        self.tick = None
        if check is not None:
            self.tick = tk.Label(self, text="✓" if check else "", bg=theme.SURFACE, fg=theme.ACCENT_TEXT,
                                 font=theme.font(theme.SIZE_BODY, "bold"), width=2, anchor="e")
            self.tick.pack(side="left")
            parts.append(self.tick)
        self.checked = check
        self.label = tk.Label(self, text=text, bg=theme.SURFACE, fg=theme.TEXT, font=theme.font(theme.SIZE_BODY),
                              anchor="w", padx=theme.px(theme.GAP_MEDIUM), pady=theme.px(theme.GAP))
        self.label.pack(side="left", fill="x", expand=True)
        self.keys = tk.Label(self, text=shortcut, bg=theme.SURFACE, fg=theme.MUTED, font=theme.font(theme.SIZE_SMALL),
                             padx=theme.px(theme.GAP_MEDIUM))
        self.keys.pack(side="right")
        parts += [self.label, self.keys]
        self._parts = parts
        for w in parts:
            w.bind("<Enter>", lambda e: self._entered())
            w.bind("<Leave>", lambda e: self.set_active(False))
            w.bind("<ButtonRelease-1>", lambda e: self._run())
        theme.on_change(self._palette_changed)
        if not enabled:
            self.set_enabled(False)

    def _entered(self):
        if self.on_hover is not None:
            self.on_hover(self)        # the menu moves its highlight here (and off the last row)
        else:
            self.set_active(True)

    def set_active(self, active: bool):
        """Highlight the row (under the pointer, or chosen with the arrow keys). A greyed-out row
        never lights up."""
        self.active = active and self.enabled
        self._paint(theme.SURFACE_HI if self.active else theme.SURFACE)

    def _paint(self, bg):
        for w in self._parts:
            w.configure(bg=bg)

    def disabled_colour(self) -> str:
        return theme.mix("MUTED", "SURFACE", DISABLED_FADE)

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        faded = self.disabled_colour()
        self.label.configure(fg=theme.TEXT if enabled else faded)
        self.keys.configure(fg=theme.MUTED if enabled else faded)
        for w in self._parts:
            w.configure(cursor="hand2" if enabled else "arrow")
        self.set_active(False)

    def _palette_changed(self, _palette):
        if not self.enabled:            # the faded grey is worked out, not a role: work it out again
            self.set_enabled(False)

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
    takes the keyboard while it's open and gives it back when it closes."""

    open_one: "Popover | None" = None
    MARGIN = 8   # px at 100%: kept clear between the panel and its window's edges

    def __init__(self, root: tk.Misc, width: int, *, scroll: bool = False, transient: bool = False,
                 keys: bool = False, padding: tuple[int, int] = (theme.GAP_WIDE, theme.GAP_LARGE)):
        super().__init__(root, bg=theme.SURFACE, highlightthickness=1, highlightbackground=theme.BORDER)
        self.root = root
        self.width = width
        self.scroll, self.transient, self.keys_on = scroll, transient, keys
        self.area = None
        if scroll:
            from .controls import ScrollArea   # (controls imports this module: import here, not at the top)
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
            root.bind("<Escape>", lambda e: Popover.close_open(), add="+")
            root.bind("<Configure>", lambda e: Popover._window_resized(e, root), add="+")
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
            try:
                self._focus_before = self.focus_get()
            except (tk.TclError, KeyError):   # (focus on a widget Python has no object for)
                self._focus_before = None
            self.focus_set()

    def wire_rows(self):
        """Let the rows share the highlight with the arrow keys (call again after adding rows)."""
        self.active_row = None
        for row in self.menu_rows():
            row.on_hover = self.hover

    def hide(self):
        self.place_forget()
        if isinstance(self.anchor_widget, IconButton):
            self.anchor_widget.set_active(False)
        if Popover.open_one is self:
            Popover.open_one = None
        before, self._focus_before = self._focus_before, None
        if self.keys_on and before is not None:
            try:
                if before.winfo_exists():
                    before.focus_set()     # the keyboard goes back where it was (e.g. the track list)
            except (tk.TclError, KeyError):
                pass
        if self.on_hide is not None:
            self.on_hide()
        if self.transient:
            self.after_idle(self._destroy_quietly)

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
    def close_open():
        if Popover.open_one is not None:
            try:
                Popover.open_one.hide()
            except tk.TclError:   # its window has gone (closed with the panel open)
                Popover.open_one = None

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

"""Keyboard access for the drawn controls: the pill buttons, the round icon buttons, the slider
switches, the choice rows and the sliders are pictures and labels, not stock buttons, so the
keyboard reaches them through `Focusable`.

- **Tab** stops on them (Tk's own Tab order: `takefocus`), Shift+Tab goes back.
- **Enter** or **Space** presses a button or flips a switch; the **arrow keys** move a slider or
  the choice in a row of choices.
- While one has the keyboard it shows a **FOCUS_RING** DG-green ring (theme.ACCENT), drawn by the
  control into its own picture, rounded like the control. A click never moves the keyboard to a
  drawn control, so the ring only ever shows for the keyboard.

`cycle(panel, area)`: Tab and Shift+Tab go round the controls inside one panel, top to bottom, and
the panel scrolls to show the one with the keyboard (the Settings pop-over).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme

CYCLE_TAG = "DGFocusCycle"   # the bindtag every widget in a cycling panel carries (see cycle)


def ring_width() -> int:
    """The focus ring's thickness in real px."""
    return max(1, theme.px(theme.FOCUS_RING))


def ring_room() -> int:
    """Room kept round a control whose ring sits outside it (a switch, a choice): ring + 1 px gap."""
    return ring_width() + max(1, theme.px(1))


class Focusable:
    """Mix into a drawn control (a Tk widget). Call `_keyboard()` once it's made. `activate()` is
    what Enter / Space do; `focus_changed()` repaints it (read `self.focused`); `can_focus()` says
    whether it takes the keyboard just now (a greyed-out control doesn't)."""

    focused = False

    def _keyboard(self, keys: bool = True):
        self.configure(takefocus=1)
        self.bind("<FocusIn>", self._focus_in, add="+")
        self.bind("<FocusOut>", self._focus_out, add="+")
        if keys:
            for key in ("<Return>", "<KP_Enter>", "<space>"):
                self.bind(key, self._key_pressed)

    def _focus_in(self, event):
        # (Tk also tells a widget's ancestors when one inside them takes the keyboard: only the
        # widget that has it now counts.)
        if event.widget is self and not self.focused and _has_focus(self):
            self.focused = True
            self.focus_changed()

    def _focus_out(self, event):
        if event.widget is self and self.focused:
            self.focused = False
            self.focus_changed()

    def _key_pressed(self, _event):
        if self.can_focus():
            self.activate()
        return "break"   # (the key acted here: the window's shortcut for it doesn't)

    def set_focusable(self, on: bool):
        """Take the keyboard (Tab stops here) or not (greyed out)."""
        self.configure(takefocus=1 if on else 0)

    def can_focus(self) -> bool:
        return True

    def activate(self):
        raise NotImplementedError

    def focus_changed(self):
        raise NotImplementedError


def _has_focus(widget: tk.Misc) -> bool:
    """Is `widget` the one in its window that has (or will have) the keyboard? (Asked of the window,
    not the screen: true even while another app is the active one.)"""
    try:
        return str(widget.tk.call("focus", "-lastfor", widget)) == str(widget)
    except tk.TclError:
        return False


def focusable_in(panel: tk.Misc) -> list[tk.Misc]:
    """The widgets inside `panel` the keyboard can go to now, top to bottom, then left to right."""
    found = []
    stack = list(panel.winfo_children())
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        try:
            take = str(w.cget("takefocus"))
            if take == "ttk::takefocus":   # a ttk widget's own rule: while it isn't greyed out
                take = "0" if w.instate(["disabled"]) else "1"
        except (tk.TclError, AttributeError):
            continue
        if take in ("1", "True", "true") and w.winfo_viewable():
            if isinstance(w, Focusable) and not w.can_focus():
                continue
            if isinstance(w, ttk.Widget) and w.instate(["disabled"]):
                continue
            found.append(w)
    return sorted(found, key=lambda w: (w.winfo_rooty(), w.winfo_rootx()))


def cycle(panel: tk.Misc, area=None):
    """Tab / Shift+Tab go round the controls inside `panel` (the keyboard never leaves it for the
    window behind), and `area` (a controls.ScrollArea) scrolls to show the one that has it. Call
    again after adding widgets to the panel."""
    root = panel._root()
    if not getattr(root, "_dg_cycle_bound", False):
        root._dg_cycle_bound = True
        root.bind_class(CYCLE_TAG, "<Tab>", lambda e: _step(e, 1))
        root.bind_class(CYCLE_TAG, "<Shift-Tab>", lambda e: _step(e, -1))
        root.bind_class(CYCLE_TAG, "<FocusIn>", _shown)
    panel._dg_cycle_area = area
    stack = [panel]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        w._dg_cycle_panel = panel
        tags = w.bindtags()
        if CYCLE_TAG not in tags:
            w.bindtags((CYCLE_TAG,) + tags)


def _panel_of(widget) -> tk.Misc | None:
    return getattr(widget, "_dg_cycle_panel", None)


def _step(event, step: int):
    panel = _panel_of(event.widget)
    if panel is None:
        return None
    widgets = focusable_in(panel)
    if not widgets:
        return "break"
    widget = event.widget
    if widget in widgets:
        target = widgets[(widgets.index(widget) + step) % len(widgets)]
    else:
        target = widgets[0 if step > 0 else -1]
    target.focus_set()
    return "break"


def _shown(event):
    """A widget in a cycling panel took the keyboard: scroll the panel to show it."""
    widget = event.widget
    if not isinstance(widget, tk.Misc) or not _has_focus(widget):   # (an ancestor of the one that has it)
        return
    panel = _panel_of(widget)
    area = getattr(panel, "_dg_cycle_area", None) if panel is not None else None
    if area is not None and widget is not panel:
        try:
            area.see(widget)
        except tk.TclError:
            pass

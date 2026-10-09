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
from tkinter import ttk

from . import controls, keyfocus, theme

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

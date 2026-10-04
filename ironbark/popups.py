"""In-app prompts and right-click menus (0.21.0), in the app's own colours instead of Windows'
light boxes and menus — so they follow Dark / Light like everything else (theme.py).

**Prompts** (`Prompt`, and the plain helpers `ask_text`, `confirm`, `tell`): a box over the
middle of a window, built on the same surface / border as the pop-overs (tabbar.Popover). They are
**modal**, like the Windows boxes they replace: the rest of the app takes no clicks or keys until
it's answered (a click outside makes the box flash), Enter = the default button, Esc = cancel.
The helpers wait for the answer (Tk keeps running meanwhile, so the music and the window carry on)
and return it. A warning that must not be missed — the hearing-safety one when bit-perfect is
switched on, "Turn your amp down first" before a test signal — uses `cover=True`: the box sits
alone over a plain background, with an amber edge and a warning sign.

**Menus** (`PopupMenu`): a right-click menu built from tabbar.Popover + MenuRow, opened at the
pointer and kept inside its window. Shortcuts show on the right, greyed-out items stay visible,
arrows / Home / End move, Enter runs, Esc closes. Where the old Windows menus had a cascade (Add
to playlist, Columns) the same menu shows the submenu in its place — `›` to go in, a `‹` row (or
←) to come back — which also works in the player's narrow window.

Windows' own file and folder pickers are left as they are (expected, and not reliably restylable).
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from types import SimpleNamespace
from typing import Callable

from . import theme
from .controls import PillButton
from .tabbar import MenuRow, Popover, glyph, icon_font

PROMPT_WIDTH = 340       # px at 100%: the box's width (less in a narrow window)
PROMPT_MIN_WIDTH = 200   # px at 100%: never narrower than this
WARN_EDGE = 2            # px at 100%: a warning box's amber edge
FLASH_MS = 110           # a click outside the box: its edge flashes, twice, this long each
MENU_MIN_WIDTH = 190     # px at 100%: a right-click menu's narrowest
MENU_TEXT = 44           # characters: longer item names (a song, a playlist) are cut with …

Answer = object


class Prompt(tk.Frame):
    """A modal box over the middle of `window` (any Tk / Toplevel window).

    `buttons`: (label, value) or (label, value, kind) left to right; kind "primary" (filled green),
    "danger" (red ring — Delete) or "plain"; `default` = the index Enter presses (drawn filled
    unless it's "danger"); Esc / closing answers `cancel`. `entry`: show a text box with this text
    (selected), and answer the typed text (stripped) for the default button; `validate(text)` returns
    a problem to show under the box ("There's already a playlist called …") or None. `tone`
    "warn" + `cover=True` for warnings that must not be missed.

    Call `wait()` for the answer, or (tests) `press_default()` / `press_cancel()` / `answer(value)`."""

    stack: list["Prompt"] = []     # the prompts on show, newest last (one over another is allowed)

    def __init__(self, window: tk.Misc, title: str, message: str = "", *, detail: str = "",
                 buttons=(("OK", True),), default: int = 0, cancel: Answer = None, entry: str | None = None,
                 validate: Callable[[str], str | None] | None = None, tone: str = "plain",
                 cover: bool = False):
        self.window = window
        self.cover = None
        if cover:   # nothing else of the window shows while the warning is up
            self.cover = tk.Frame(window, bg=theme.BG)
            self.cover.place(x=0, y=0, relwidth=1, relheight=1)
            self.cover.lift()
        edge = theme.px(WARN_EDGE) if tone == "warn" else 1
        self.edge_colour = theme.WARN if tone == "warn" else theme.BORDER
        super().__init__(window, bg=theme.SURFACE, highlightthickness=edge, highlightbackground=self.edge_colour,
                         highlightcolor=self.edge_colour)
        self.heading, self.tone = title, tone
        self.cancel_value, self.validate = cancel, validate
        self.result: Answer = cancel
        self.done = False
        self._done_var = tk.IntVar(self, value=0)
        self.tag = f"DGPrompt{id(self)}"
        width = self._width()
        wrap = width - 2 * theme.px(theme.GAP_WIDE) - 2 * edge
        body = tk.Frame(self, bg=theme.SURFACE)
        body.pack(fill="both", expand=True, padx=theme.px(theme.GAP_WIDE), pady=theme.px(theme.GAP_LARGE))

        head = tk.Frame(body, bg=theme.SURFACE)
        head.pack(fill="x")
        if tone == "warn":
            font = icon_font(window, theme.ICON_MEDIUM) or theme.font(theme.SIZE_HEADING, "bold")
            self.sign = tk.Label(head, text=glyph(window, "warning"), font=font, bg=theme.SURFACE, fg=theme.WARN)
            self.sign.pack(side="left", anchor="n", padx=theme.pad(0, theme.GAP_MEDIUM), pady=theme.pad(theme.GAP_HAIR, 0))
        self.title_label = tk.Label(head, text=title, bg=theme.SURFACE, fg=theme.TEXT, anchor="w", justify="left",
                                    font=theme.font(theme.SIZE_HEADING, "bold"), wraplength=wrap)
        self.title_label.pack(side="left", fill="x", expand=True)
        self.message_label = self.detail_label = None
        if message:
            self.message_label = tk.Label(body, text=message, bg=theme.SURFACE, fg=theme.TEXT, anchor="w",
                                          justify="left", font=theme.font(theme.SIZE_BODY), wraplength=wrap)
            self.message_label.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
        if detail:
            self.detail_label = tk.Label(body, text=detail, bg=theme.SURFACE, fg=theme.MUTED, anchor="w",
                                         justify="left", font=theme.font(theme.SIZE_SMALL), wraplength=wrap)
            self.detail_label.pack(fill="x", pady=theme.pad(theme.GAP, 0))
        self.entry = self.problem_label = None
        self.text = tk.StringVar(self, value=entry or "")
        if entry is not None:
            self.entry = ttk.Entry(body, textvariable=self.text)
            self.entry.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
            self.entry.selection_range(0, "end")
            self.entry.icursor("end")
            self.problem_label = tk.Label(body, text="", bg=theme.SURFACE, fg=theme.DANGER, anchor="w",
                                          justify="left", font=theme.font(theme.SIZE_SMALL), wraplength=wrap)
            self.text.trace_add("write", lambda *_: self.show_problem(""))   # typing clears it

        row = tk.Frame(body, bg=theme.SURFACE)
        row.pack(fill="x", pady=theme.pad(theme.GAP_LARGE, 0))
        self.buttons: list[PillButton] = []
        self.values: list[Answer] = []
        self.default = default
        specs = [tuple(b) + (("plain",) if len(b) == 2 else ()) for b in buttons]
        for index, (label, value, kind) in reversed(list(enumerate(specs))):
            if kind == "plain" and index == default:
                kind = "primary"
            button = PillButton(row, label, lambda v=value, i=index: self._chosen(i), bg="SURFACE",
                                **self._button_style(kind))
            button.pack(side="right", padx=theme.pad(theme.GAP, 0) if index < len(specs) - 1 else 0)
            self.buttons.insert(0, button)
            self.values.insert(0, value)

        # Keys and clicks: the box's widgets get their own tag *instead of* the window's, so the
        # player's shortcuts (Ctrl+W, n, Space…) and the pop-overs' click-outside don't act while
        # it's up. Enter / Esc / Tab are handled here; typing still works (each widget keeps its class).
        for key in ("<Return>", "<KP_Enter>"):
            self.bind_class(self.tag, key, lambda e: self.press_default() or "break")
        self.bind_class(self.tag, "<Escape>", lambda e: self.press_cancel() or "break")
        for key in ("<Tab>", "<Shift-Tab>"):
            self.bind_class(self.tag, key, lambda e: "break")
        self.bind_class(self.tag, "<ButtonPress>", self._pressed)
        self._retag(self)
        if self.cover is not None:
            self._retag(self.cover)
        self.bind("<Destroy>", self._destroyed, add="+")

        self.place(relx=0.5, y=0, anchor="n", width=width)
        self.lift()
        self._seat()
        # Kept in the window when it's resized. One binding per window, made once and never removed (a
        # box per question would otherwise pile up bindings, and unbinding one safely depends on the
        # Python version).
        if not getattr(window, "_dg_prompt_bound", False):
            window._dg_prompt_bound = True
            window.bind("<Configure>", lambda e: Prompt._window_resized(e, window), add="+")
        Prompt.stack.append(self)
        self._take_over()

    # -- layout -------------------------------------------------------------------------------
    def _width(self) -> int:
        room = self.window.winfo_width() - 2 * theme.px(theme.GAP_WIDE)
        if room < theme.px(PROMPT_MIN_WIDTH):   # a window not yet laid out (or tiny): its requested size
            room = max(room, self.window.winfo_reqwidth() - 2 * theme.px(theme.GAP_WIDE))
        return max(theme.px(PROMPT_MIN_WIDTH), min(theme.px(PROMPT_WIDTH), room))

    def _seat(self):
        """Centred across, a little above the middle (where the eye goes), inside the window."""
        if not self.winfo_exists():
            return
        self.update_idletasks()
        height = self.winfo_reqheight()
        room = self.window.winfo_height()
        y = max(theme.px(theme.GAP_MEDIUM), int((room - height) * 0.4))
        self.place_configure(y=y, width=self._width())

    @staticmethod
    def _button_style(kind: str) -> dict:
        if kind == "primary":
            return dict(fill="SELECT", fg="ON_ACCENT")
        if kind == "danger":
            # A red ring, ordinary text: red text on the hover grey would be under WCAG AA's 4.5:1 in Dark.
            return dict(fill=None, fg="TEXT", outline="DANGER", hover_fill="SURFACE_HI")
        return dict(fill=None, fg="TEXT", outline="BORDER", hover_fill="SURFACE_HI")

    def _retag(self, widget: tk.Misc):
        """Swap each widget's window tag for the box's own (see __init__)."""
        top = str(widget.winfo_toplevel())
        stack = [widget]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            tags = [t for t in w.bindtags() if t != top]
            if self.tag not in tags:
                tags.insert(1 if len(tags) > 1 else len(tags), self.tag)
            w.bindtags(tuple(tags))

    def _take_over(self):
        """Modal: only the box takes the mouse; the keyboard goes to its text box or default button."""
        try:
            self.grab_set()
        except tk.TclError:   # another app holds a grab, or the window isn't viewable yet
            pass
        target = self.entry if self.entry is not None else self
        try:
            target.focus_set()
        except tk.TclError:
            pass

    def _pressed(self, event):
        """A click: outside the box (Tk hands those to the box while it holds the grab) → flash."""
        if event.widget is self or event.widget is self.cover:
            x, y = event.x_root - self.winfo_rootx(), event.y_root - self.winfo_rooty()
            if not (0 <= x < self.winfo_width() and 0 <= y < self.winfo_height()):
                self.flash()

    def flash(self, times: int = 2):
        """Draw the eye back to the box: its edge blinks (and Windows' default beep)."""
        self.bell()
        bright = theme.ACCENT if self.tone != "warn" else theme.TEXT

        def blink(n: int):
            if not self.winfo_exists():
                return
            colour = bright if n % 2 == 0 else self.edge_colour
            self.configure(highlightbackground=colour, highlightcolor=colour)
            if n + 1 < times * 2:
                self.after(FLASH_MS, blink, n + 1)

        blink(0)

    # -- answering ------------------------------------------------------------------------------
    def press_default(self):
        self._chosen(self.default)

    def press_cancel(self):
        self.answer(self.cancel_value)

    def _chosen(self, index: int):
        value = self.values[index]
        if self.entry is not None and index == self.default:
            text = self.text.get().strip()
            problem = self.validate(text) if self.validate is not None else None
            if problem:
                self.show_problem(problem)
                self.entry.focus_set()
                self.entry.selection_range(0, "end")
                self.flash(1)
                return
            value = text
        self.answer(value)

    def show_problem(self, text: str):
        """The note under the text box (red), shown only while there's something to say."""
        if self.problem_label is None or self.done:
            return
        self.problem_label.configure(text=text)
        if text and not self.problem_label.winfo_manager():
            self.problem_label.pack(fill="x", pady=theme.pad(theme.GAP_SMALL, 0), after=self.entry)
            self._retag(self.problem_label)
            self._seat()
        elif not text and self.problem_label.winfo_manager():
            self.problem_label.pack_forget()
            self._seat()

    def answer(self, value: Answer):
        """Close the box with this answer (what `wait` returns)."""
        if self.done:
            return
        self.done = True
        self.result = value
        if self in Prompt.stack:
            Prompt.stack.remove(self)
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self._done_var.set(1)
        if self.cover is not None and self.cover.winfo_exists():
            self.cover.destroy()
        if self.winfo_exists():
            self.destroy()
        if Prompt.stack:   # a box under this one gets the grab back
            Prompt.stack[-1]._take_over()

    def _destroyed(self, event):
        if event.widget is self and not self.done:   # its window closed under it: that's a cancel
            self.answer(self.cancel_value)

    @staticmethod
    def _window_resized(event, window):
        if event.widget is window:
            for prompt in Prompt.stack:
                if prompt.window is window:
                    prompt._seat()

    def wait(self) -> Answer:
        """Wait (Tk keeps running) until the box is answered; return the answer."""
        if not self.done:
            try:
                self.wait_variable(self._done_var)
            except tk.TclError:
                pass
        return self.result


# -- the plain helpers ------------------------------------------------------------------------------
def ask_text(window: tk.Misc, title: str, message: str = "", *, initial: str = "", ok: str = "OK",
             validate: Callable[[str], str | None] | None = None, detail: str = "") -> str | None:
    """Ask for a line of text (a playlist's name). Returns the text, stripped — or None for Cancel / Esc."""
    return Prompt(window, title, message, detail=detail, buttons=((ok, True), ("Cancel", None)), cancel=None,
                  entry=initial, validate=validate).wait()


def confirm(window: tk.Misc, title: str, message: str = "", *, detail: str = "", ok: str = "OK",
            cancel: str = "Cancel", danger: bool = False, tone: str = "plain", cover: bool = False) -> bool:
    """A yes / no question. Enter = `ok`, Esc = `cancel`. True for ok."""
    return bool(Prompt(window, title, message, detail=detail, tone=tone, cover=cover, cancel=False,
                       buttons=((ok, True, "danger" if danger else "primary"), (cancel, False))).wait())


def tell(window: tk.Misc, title: str, message: str = "", *, detail: str = "", tone: str = "plain",
         cover: bool = False, ok: str = "OK") -> None:
    """Something to read, with one button."""
    Prompt(window, title, message, detail=detail, tone=tone, cover=cover, buttons=((ok, True),)).wait()


# -- right-click menus ---------------------------------------------------------------------------
@dataclass
class MenuItem:
    label: str
    command: Callable[[], None] | None = None
    shortcut: str = ""
    enabled: bool = True
    checked: bool | None = None          # a tick item (True / False); None = an ordinary item
    submenu: "MenuItems | None" = None
    separator: bool = False


@dataclass
class MenuItems:
    """A menu's (or submenu's) list of items."""
    title: str = ""
    items: list[MenuItem] = field(default_factory=list)

    def add(self, label: str, command: Callable[[], None], shortcut: str = "", enabled: bool = True) -> MenuItem:
        self.items.append(MenuItem(_short(label), command, shortcut, enabled))
        return self.items[-1]

    def add_check(self, label: str, checked: bool, command: Callable[[], None], shortcut: str = "") -> MenuItem:
        self.items.append(MenuItem(_short(label), command, shortcut, True, bool(checked)))
        return self.items[-1]

    def add_separator(self):
        if self.items and not self.items[-1].separator:   # never two in a row, never at the top
            self.items.append(MenuItem("", separator=True))

    def add_submenu(self, label: str, enabled: bool = True) -> "MenuItems":
        sub = MenuItems(_short(label))
        self.items.append(MenuItem(sub.title, None, "›", enabled, submenu=sub))
        return sub

    def labels(self) -> list[str]:
        """Every item's label (not the separators), top to bottom."""
        return [item.label for item in self.items if not item.separator]

    def find(self, label: str) -> MenuItem:
        return next(item for item in self.items if item.label == label and not item.separator)


def _short(text: str) -> str:
    return text if len(text) <= MENU_TEXT else text[:MENU_TEXT - 1] + "…"


def row_event(tree: ttk.Treeview) -> SimpleNamespace:
    """A stand-in for a right-click on a list's focused row — for the keyboard's menu key (or
    Shift+F10), so the same menu opens there, just under the row."""
    iid = tree.focus() or next(iter(tree.selection()), "")
    x = y = height = 0
    if iid:
        tree.see(iid)
        tree.update_idletasks()
        box = tree.bbox(iid)
        if box:
            x, y, _width, height = box
    x += theme.px(theme.GAP_WIDE * 2)
    return SimpleNamespace(widget=tree, x=x, y=y + height // 2,
                           x_root=tree.winfo_rootx() + x, y_root=tree.winfo_rooty() + y + height)


class PopupMenu(Popover):
    """A right-click menu (see the module's notes). Build it with add / add_check / add_separator /
    add_submenu, then `popup(x_root, y_root)`. It destroys itself when it closes."""

    BACK = "‹"

    def __init__(self, window: tk.Misc):
        super().__init__(window, theme.px(MENU_MIN_WIDTH), scroll=True, transient=True, keys=True,
                         padding=(0, theme.GAP_SMALL))
        self.menu = MenuItems()
        self.view: MenuItems = self.menu     # what's on show: the menu, or a submenu
        self.trail: list[MenuItems] = []     # the menus "above" the one on show (for ‹ / ←)
        self.bind("<Right>", lambda e: self._enter_active() or "break")
        self.bind("<Left>", lambda e: self.back() or "break")

    # building (the same calls as MenuItems)
    def add(self, *args, **kwargs) -> MenuItem:
        return self.menu.add(*args, **kwargs)

    def add_check(self, *args, **kwargs) -> MenuItem:
        return self.menu.add_check(*args, **kwargs)

    def add_separator(self):
        self.menu.add_separator()

    def add_submenu(self, *args, **kwargs) -> MenuItems:
        return self.menu.add_submenu(*args, **kwargs)

    def labels(self) -> list[str]:
        return self.menu.labels()

    def popup(self, x_root: int, y_root: int):
        """Open at the pointer (a right-click's event.x_root / y_root)."""
        self._render(self.menu)
        self.show_at(x_root, y_root)

    # the view
    def _render(self, items: MenuItems):
        for child in self.body.winfo_children():
            child.destroy()
        self.view = items
        if self.trail:
            back = MenuRow(self.body, f"{self.BACK}  {items.title}", self.back, "←", closes=False)
            back.label.configure(font=theme.font(theme.SIZE_BODY, "bold"))
            back.pack(fill="x")
            tk.Frame(self.body, bg=theme.BORDER, height=1).pack(fill="x", pady=theme.pad(theme.GAP_SMALL))
        shown = list(items.items)
        while shown and shown[-1].separator:   # never a line at the bottom
            shown.pop()
        for item in shown:
            if item.separator:
                tk.Frame(self.body, bg=theme.BORDER, height=1).pack(
                    fill="x", padx=theme.px(theme.GAP_MEDIUM), pady=theme.pad(theme.GAP_SMALL))
                continue
            if item.submenu is not None:
                row = MenuRow(self.body, item.label, lambda s=item.submenu: self.open_submenu(s), item.shortcut,
                              enabled=item.enabled, closes=False)
            else:
                row = MenuRow(self.body, item.label, item.command or (lambda: None), item.shortcut,
                              enabled=item.enabled, check=item.checked)
            row.item = item
            row.pack(fill="x")
        self.wire_rows()

    def rows(self) -> list[MenuRow]:
        """The rows on show (the ‹ row included when in a submenu)."""
        return self.menu_rows()

    def open_submenu(self, items: MenuItems):
        """Show a submenu in the menu's place (‹ / ← comes back)."""
        self.trail.append(self.view)
        self._render(items)
        self.reseat()
        rows = [r for r in self.rows() if r.enabled]
        if len(rows) > 1:          # the highlight on its first item (not on the way back)
            self.hover(rows[1])
        self.focus_set()

    def back(self):
        if self.trail:
            came_from = self.view
            self._render(self.trail.pop())
            self.reseat()
            for row in self.rows():   # the highlight goes back to the submenu's own row
                if getattr(row, "item", None) is not None and row.item.submenu is came_from:
                    self.hover(row)
            self.focus_set()

    def escape(self):
        Popover.close_open()

    def _enter_active(self):
        row = self.active_row
        if row is not None and getattr(row, "item", None) is not None and row.item.submenu is not None:
            row._run()

    # for tests and the keyboard: choose an item on show by its label
    def invoke(self, label: str):
        for row in self.rows():
            if row.text == label:
                row._run()
                return
        raise KeyError(label)

    def press(self, key: str):
        """Act as if `key` were pressed: Down, Up, Home, End, Return, Escape, Left, Right."""
        actions = {"Down": lambda: self.move(1), "Up": lambda: self.move(-1), "Home": lambda: self.move_to(0),
                   "End": lambda: self.move_to(-1), "Return": self.run_active, "Escape": self.escape,
                   "Left": self.back, "Right": self._enter_active}
        actions[key]()

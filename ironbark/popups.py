"""In-app prompts and right-click menus (0.21.0), in the app's own colours instead of Windows'
light boxes and menus — so they follow Dark / Light like everything else (theme.py).

**Prompts** (`Prompt`, and the plain helpers `ask_text`, `confirm`, `tell`): a box over the
middle of a window, built on the same surface / border as the pop-overs (tabbar.Popover). They are
**modal**, like the Windows boxes they replace: the rest of the app takes no clicks or keys until
it's answered (a click outside makes the box flash), Enter = the default button, Esc = cancel.
Tab / Shift+Tab go round the box's text box and buttons (a DG-green ring shows which has the
keyboard), and Enter then presses that one.
The helpers wait for the answer (Tk keeps running meanwhile, so the music and the window carry on)
and return it. A warning that must not be missed — the hearing-safety one when bit-perfect is
switched on, "Turn your amp down first" before a test signal — uses `cover=True`: the box sits
alone over a plain background, with an amber edge and a warning sign.

**Menus** (`PopupMenu`): a right-click menu built from tabbar.Popover + MenuRow, opened at the
pointer and kept inside its window. Shortcuts show on the right, greyed-out items stay visible,
arrows / Home / End move, Enter runs, Esc closes. Where the old Windows menus had a cascade (Add
to playlist, Columns) the same menu shows the submenu in its place — a row with a chevron (or →)
to go in, a back row with its title (or ←) to come back — which also works in the player's narrow
window. The highlighted row is a rounded plate; a menu with ticks keeps a tick column on every row,
so the words line up; separators run MENU_INSET in from the sides, like the words.

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
PROMPT_TAG = "DGPrompt"  # the bindtag every prompt's widgets carry in place of their window's

Answer = object


class Prompt(tk.Frame):
    """A modal box over the middle of `window` (any Tk / Toplevel window).

    `buttons`: (label, value) or (label, value, kind) left to right; kind "primary" (filled green),
    "danger" (red ring — Delete) or "plain"; `default` = the index Enter presses (drawn filled
    unless it's "danger"); Esc / closing answers `cancel`. Tab / Shift+Tab move the keyboard round the
    text box and the buttons (left to right; the first Tab goes to the default button), and Enter or
    Space then press the button that has it. `entry`: show a text box with this text
    (selected), and answer the typed text (stripped) for the default button; `validate(text)` returns
    a problem to show under the box ("There's already a playlist called …") or None. `tone`
    "warn" + `cover=True` for warnings that must not be missed. Enter never presses a "danger"
    button: in such a box the default is the button that answers `cancel`, and it stays plain.

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
        self.edge_role = "WARN" if tone == "warn" else "EDGE"   # a role: the flash follows a palette change
        super().__init__(window, bg=theme.SURFACE, highlightthickness=edge, highlightbackground=self.edge_colour,
                         highlightcolor=self.edge_colour)
        self.heading, self.tone = title, tone
        self.cancel_value, self.validate = cancel, validate
        self.result: Answer = cancel
        self.done = False
        self._done_var = tk.IntVar(self, value=0)
        self.tag = PROMPT_TAG
        width = self._width()
        wrap = width - 2 * theme.px(theme.GAP_WIDE) - 2 * edge
        body = tk.Frame(self, bg=theme.SURFACE)
        body.pack(fill="both", expand=True, padx=theme.px(theme.GAP_WIDE), pady=theme.px(theme.GAP_LARGE))

        head = tk.Frame(body, bg=theme.SURFACE)
        head.pack(fill="x")
        self.title_wrap = wrap
        if tone == "warn":
            font = icon_font(window, theme.ICON_MEDIUM) or theme.font(theme.SIZE_HEADING, "bold")
            self.sign = tk.Label(head, text=glyph(window, "warning"), font=font, bg=theme.SURFACE, fg=theme.WARN)
            self.sign.pack(side="left", anchor="n", padx=theme.pad(0, theme.GAP_MEDIUM), pady=theme.pad(theme.GAP_HAIR, 0))
            # The sign shares the title's line: the title wraps in the room left beside it, or its
            # last word runs past the box's edge and is cut off.
            self.title_wrap = max(theme.px(theme.GAP_WIDE),
                                  wrap - self.sign.winfo_reqwidth() - theme.px(theme.GAP_MEDIUM))
        self.title_label = tk.Label(head, text=title, bg=theme.SURFACE, fg=theme.TEXT, anchor="w", justify="left",
                                    font=theme.font(theme.SIZE_HEADING, "semibold"), wraplength=self.title_wrap)
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
        self._text_trace = None
        if entry is not None:
            self.entry = ttk.Entry(body, textvariable=self.text, style=theme.CARD_ENTRY)
            self.entry.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
            self.entry.selection_range(0, "end")
            self.entry.icursor("end")
            self.problem_label = tk.Label(body, text="", bg=theme.SURFACE, fg=theme.DANGER, anchor="w",
                                          justify="left", font=theme.font(theme.SIZE_SMALL), wraplength=wrap)
            self._text_trace = self.text.trace_add("write", lambda *_: self.show_problem(""))   # typing clears it

        row = tk.Frame(body, bg=theme.SURFACE)
        row.pack(fill="x", pady=theme.pad(theme.GAP_LARGE, 0))
        self.buttons: list[PillButton] = []
        self.values: list[Answer] = []
        specs = [tuple(b) + (("plain",) if len(b) == 2 else ()) for b in buttons]
        danger = any(kind == "danger" for _label, _value, kind in specs)
        if specs[default][2] == "danger":   # Enter (also the key that chose the menu item) must be safe
            default = next((i for i, (_label, value, kind) in enumerate(specs)
                            if value == cancel and kind != "danger"), default)
        self.default = default
        for index, (label, value, kind) in reversed(list(enumerate(specs))):
            if kind == "plain" and index == default and not danger:   # (no green go-ahead beside Delete)
                kind = "primary"
            button = PillButton(row, label, lambda v=value, i=index: self._chosen(i), bg="SURFACE", kind=kind)
            # Packed from the right: every button but the rightmost keeps its gap on its right side,
            # between it and the button after it.
            button.pack(side="right", padx=(0, theme.px(theme.GAP_MEDIUM)) if index < len(specs) - 1 else 0)
            self.buttons.insert(0, button)
            self.values.insert(0, value)

        # Keys and clicks: the box's widgets get the prompts' tag *instead of* the window's, so the
        # player's shortcuts (Ctrl+W, n, Space…) and the pop-overs' click-outside don't act while
        # it's up. Enter / Esc / Tab are handled there (a button with the keyboard takes Enter itself,
        # first); typing still works (each widget keeps its class). The tag is bound once for all
        # prompts: a class binding lasts as long as Tk does,
        # so one made per box (with the box in its function) would keep every box ever shown alive.
        root = self._root()
        if not getattr(root, "_dg_prompt_keys_bound", False):
            root._dg_prompt_keys_bound = True
            for key in ("<Return>", "<KP_Enter>"):
                self.bind_class(PROMPT_TAG, key, lambda e: Prompt._dispatch(e, Prompt.press_default) or "break")
            self.bind_class(PROMPT_TAG, "<Escape>", lambda e: Prompt._dispatch(e, Prompt.press_cancel) or "break")
            for key, step in (("<Tab>", 1), ("<Shift-Tab>", -1)):
                self.bind_class(PROMPT_TAG, key, lambda e, s=step: Prompt._dispatch(e, Prompt.tab, e.widget, s)
                                or "break")
            self.bind_class(PROMPT_TAG, "<ButtonPress>", lambda e: Prompt._dispatch(e, Prompt._pressed, e))
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

    @property
    def edge_colour(self) -> str:
        """The box's edge colour in the palette now (amber for a warning)."""
        return theme.colour(self.edge_role)

    @staticmethod
    def _owner(widget) -> "Prompt | None":
        """The prompt on show that a widget with the prompts' tag belongs to (its cover counts)."""
        for prompt in reversed(Prompt.stack):
            if widget is prompt.cover:
                return prompt
        while isinstance(widget, tk.Misc):
            if isinstance(widget, Prompt):
                return widget
            widget = widget.master
        return None

    @staticmethod
    def _dispatch(event, action, *args):
        """A key or click on a prompt's widget: `action` on the prompt it belongs to."""
        prompt = Prompt._owner(event.widget)
        if prompt is not None and not prompt.done:
            action(prompt, *args)

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

    def focus_order(self) -> list[tk.Misc]:
        """Where Tab goes, in turn: the text box (if there is one), then the buttons left to right."""
        return ([self.entry] if self.entry is not None else []) + [b for b in self.buttons if b.can_focus()]

    def tab(self, widget, step: int = 1):
        """Tab (step 1) or Shift+Tab (-1) from `widget`: the keyboard to the next (or previous) one
        in `focus_order`, round and round. From the box itself (nothing in it has the keyboard yet),
        it goes to the default button — the one Enter presses — so the first Tab shows that."""
        order = self.focus_order()
        if not order:
            return
        if widget in order:
            target = order[(order.index(widget) + step) % len(order)]
        else:
            default = self.buttons[self.default]
            target = default if default in order else order[0]
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
            colour = bright if n % 2 == 0 else self.edge_colour   # (the palette's, if it has changed since)
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
        if event.widget is not self:
            return
        if not self.done:   # its window closed under it: that's a cancel
            self.answer(self.cancel_value)
        # Let go of the box's Tk variables here, on Tk's thread: an answered box is freed by Python's
        # cycle collector (its widgets and their masters), which may run on any thread, and a Tk
        # variable freed there fails ("main thread is not in main loop").
        if self._text_trace is not None:
            try:
                self.text.trace_remove("write", self._text_trace)
            except tk.TclError:
                pass
            self._text_trace = None
        self.text = self._done_var = None

    @staticmethod
    def _window_resized(event, window):
        if event.widget is window:
            for prompt in Prompt.stack:
                if prompt.window is window:
                    prompt._seat()

    def wait(self) -> Answer:
        """Wait (Tk keeps running) until the box is answered; return the answer."""
        if not self.done and self._done_var is not None:
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
    """A yes / no question. Enter = `ok`, Esc = `cancel`. True for ok. `danger` (Delete…): `ok` is
    ringed in red and Enter answers `cancel` — only choosing `ok` on purpose goes ahead (a click, or Tab
    past `cancel` to it and then Enter or Space)."""
    return bool(Prompt(window, title, message, detail=detail, tone=tone, cover=cover, cancel=False,
                       buttons=((ok, True, "danger" if danger else "primary"), (cancel, False)),
                       default=1 if danger else 0).wait())


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


def separator(parent: tk.Misc) -> tk.Frame:
    """A menu's separator: a 1 px BORDER line, MENU_INSET in from the sides like the words."""
    line = tk.Frame(parent, bg=theme.BORDER, height=1)
    line.pack(fill="x", padx=theme.px(theme.MENU_INSET), pady=theme.pad(theme.GAP_SMALL))
    return line


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

    def __init__(self, window: tk.Misc):
        super().__init__(window, theme.px(MENU_MIN_WIDTH), scroll=True, transient=True, keys=True,
                         padding=(0, theme.GAP_SMALL))
        self.menu = MenuItems()
        self.view: MenuItems = self.menu     # what's on show: the menu, or a submenu
        self.trail: list[MenuItems] = []     # the menus "above" the one on show (for the back row / ←)
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
            back = MenuRow(self.body, items.title, self.back, closes=False, chevron="left")
            back.back = True
            back.pack(fill="x")
            separator(self.body)
        shown = list(items.items)
        while shown and shown[-1].separator:   # never a line at the bottom
            shown.pop()
        ticks = any(item.checked is not None for item in shown)
        for item in shown:
            if item.separator:
                separator(self.body)
                continue
            if item.submenu is not None:
                row = MenuRow(self.body, item.label, lambda s=item.submenu: self.open_submenu(s), item.shortcut,
                              enabled=item.enabled, closes=False, ticks=ticks, chevron="right")
            else:
                row = MenuRow(self.body, item.label, item.command or (lambda: None), item.shortcut,
                              enabled=item.enabled, check=item.checked, ticks=ticks)
            row.item = item
            row.pack(fill="x")
        self.wire_rows()

    def rows(self) -> list[MenuRow]:
        """The rows on show (the back row included when in a submenu)."""
        return self.menu_rows()

    def open_submenu(self, items: MenuItems):
        """Show a submenu in the menu's place (its back row, or ←, comes back)."""
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

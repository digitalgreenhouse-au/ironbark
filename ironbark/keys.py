"""The keyboard: one table of every shortcut (`ACTIONS`), used to bind the keys, to fill the
Keyboard shortcuts sheet (F1, help_window.py), the README's Keyboard section and the hints menus
show beside their items — so the four can't drift apart.

How a key press finds its action (`Keys`): every Ironbark window carries one shared bindtag
(`TAG`), placed straight after the window's own tag, with a single <KeyPress> dispatcher on it. A
window's own bindings come first, so its local keys win (A / B / X / Space in the ABX window
return "break"). The dispatcher works the key out once (`normalise`: the keysym in lower case,
Caps Lock ignored, Shift only where the table says it doesn't matter), looks it up, and runs the
action if the window (or the widget with the keyboard) is in its scope:

- "app": every Ironbark window (play / pause, next, the measuring windows, …);
- "main": the player window and its mini strip (tabs, playlists, undo);
- "list": the main window's track list; "library": the Library's views (in the Library tab); "tags": Edit tags.

While a text box has the keyboard (a search box, a tag field), single keys, Enter, Delete and
the arrows are the box's: only Ctrl / Alt shortcuts and the function keys act — and not the
ones text boxes use themselves (Ctrl+←/→ jump by words, Ctrl+Z undoes, Ctrl+A/C/V/X). So Ctrl+Space
still plays or pauses from inside a search box. Something read-only that takes the keyboard (the PCM
stream's columns, a licence, a choice list) isn't a text box: every key acts there. Tk's entries
also delete to the end of the line on Ctrl+K (an Emacs habit); Ctrl+K is the table's (search), so
that's taken out — it would wipe the rest of what was typed before the search opened.

While an in-app question box is waiting for an answer (`held`), no shortcut acts, in any window:
the box is modal, and a key in another window mustn't act behind it.

Lists (ttk.Treeview) move their selection on the arrow keys with *any* modifier held, and fold on
Space / Enter; the keys the table gives those meanings (Ctrl+↑ = volume, Alt+↑ = move a song,
Space = play / pause, Enter = play) are bound on each list itself, ahead of its own bindings, so a
key never does both. Plain ↑ ↓ PgUp PgDn Home End stay the list's.
"""

from __future__ import annotations

import textwrap
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from tkinter import ttk

from . import platforms

TAG = "IronbarkKeys"

# Tk's modifier bits in event.state. Alt has no fixed bit: Windows' Tk reports it as 0x20000
# (checked on Tk 8.6.12), a Mac's as Mod2 (Option), X11's as Mod1. Caps Lock (0x2), Num Lock and
# the mouse buttons are ignored.
SHIFT, CONTROL = 0x1, 0x4
ALT = 0x20000 if platforms.IS_WINDOWS else 0x10 if platforms.IS_MAC else 0x8

GROUPS = ("Playback", "Tabs & lists", "Windows & tools", "Library & search", "Editing")
SCOPES = ("app", "main", "list", "library", "tags")
SCOPE_ORDER = {"list": 0, "library": 0, "tags": 0, "main": 1, "app": 2}   # the narrowest scope wins


@dataclass(frozen=True)
class Action:
    id: str
    keys: tuple[str, ...]   # as written for people: "Space", "Ctrl+Shift+T", "Alt+Up", "?"
    scope: str              # see SCOPES
    label: str
    group: str              # see GROUPS
    shift: bool = False     # a letter (or "?", "/") works with Shift / Caps Lock too: S and Shift+S both shuffle
    arg: bool = False       # the handler is told which key was pressed (as written in `keys`)
    shown: str = ""         # how the keys read in the sheet, when listing them all would be too long
    reserved: bool = False  # bound for a feature still to come: not listed in the sheet or the README


def _keys(*names: str) -> tuple[str, ...]:
    return names


ACTIONS: tuple[Action, ...] = (
    # -- Playback ----------------------------------------------------------------------------
    Action("play", _keys("Space", "Ctrl+Space"), "app", "Play / pause (Ctrl+Space works while you type)", "Playback"),
    Action("next", _keys("N", "Ctrl+Right"), "app", "Next song", "Playback", shift=True),
    Action("previous", _keys("P", "Ctrl+Left"), "app", "Previous song", "Playback", shift=True),
    Action("seek", _keys("Left", "Right"), "app", "Back / forward 5 seconds", "Playback", arg=True),
    Action("seek_long", _keys("Shift+Left", "Shift+Right"), "app", "Back / forward 30 seconds", "Playback", arg=True),
    Action("jump", tuple(str(n) for n in range(10)), "app", "Jump to 0% – 90% of the song (0: the start)",
           "Playback", arg=True, shown="0 – 9"),
    Action("shuffle", _keys("S"), "app", "Shuffle on / off", "Playback", shift=True),
    Action("repeat", _keys("R"), "app", "Repeat: off, the album, the song", "Playback", shift=True),
    Action("volume", _keys("Ctrl+Up", "Ctrl+Down"), "app", "Volume up / down 5%", "Playback", arg=True),
    Action("mute", _keys("Ctrl+Shift+Down",), "app", "Mute / unmute", "Playback"),
    Action("full_volume", _keys("Ctrl+Shift+Up",), "app", "Volume 100% (untouched)", "Playback"),
    Action("stop_after", _keys("Ctrl+.",), "app", "Stop after this song (again: carry on)", "Playback"),
    Action("sleep_timer", _keys("Ctrl+Shift+S",), "app", "Sleep timer and Stop after (a menu)", "Playback"),
    # -- Tabs & lists --------------------------------------------------------------------------
    Action("new_tab", _keys("Ctrl+T",), "main", "New tab menu (the + button)", "Tabs & lists"),
    Action("close_tab", _keys("Ctrl+W",), "main", "Close the tab", "Tabs & lists"),
    Action("reopen_tab", _keys("Ctrl+Shift+T",), "main", "Reopen the tab closed last", "Tabs & lists"),
    Action("next_tab", _keys("Ctrl+Tab", "Ctrl+PgDn"), "main", "Next tab", "Tabs & lists"),
    Action("previous_tab", _keys("Ctrl+Shift+Tab", "Ctrl+PgUp"), "main", "Previous tab", "Tabs & lists"),
    Action("move_tab", _keys("Ctrl+Shift+PgUp", "Ctrl+Shift+PgDn"), "main", "Move the tab left / right",
           "Tabs & lists", arg=True),
    Action("go_tab", tuple(f"Ctrl+{n}" for n in range(1, 9)), "main", "Go to tab 1 – 8", "Tabs & lists",
           arg=True, shown="Ctrl+1 – Ctrl+8"),
    Action("last_tab", _keys("Ctrl+9",), "main", "Go to the last tab", "Tabs & lists"),
    Action("open_folder", _keys("Ctrl+O",), "main", "Open a folder", "Tabs & lists"),
    Action("open_playlist", _keys("Ctrl+Alt+O",), "main", "Open a playlist file", "Tabs & lists"),
    Action("new_playlist", _keys("Ctrl+N",), "main", "New playlist", "Tabs & lists"),
    # (shift: '/' is Shift+7 on German, Nordic and Spanish keyboards; on a US one Shift+/ is "?")
    Action("search_tab", _keys("Ctrl+F", "/"), "main", "Search this tab", "Tabs & lists", shift=True),
    Action("list_play", _keys("Enter",), "list", "Track list: play the song (an album row: the album)",
           "Tabs & lists"),
    Action("list_queue", _keys("Ctrl+Enter",), "list", "Track list: add to Up next", "Tabs & lists"),
    Action("list_play_next", _keys("Ctrl+Shift+Enter",), "list", "Track list: play next", "Tabs & lists"),
    Action("select_all", _keys("Ctrl+A",), "list", "Track list: select every song",
           "Tabs & lists"),
    # -- Windows & tools ---------------------------------------------------------------------------
    Action("now_playing", _keys("I",), "app", "Now Playing", "Windows & tools", shift=True),
    Action("meters", _keys("M",), "app", "Meters", "Windows & tools", shift=True),
    Action("pcm", _keys("B",), "app", "PCM stream", "Windows & tools", shift=True),   # b for bits
    Action("clipping", _keys("C",), "app", "Clipping", "Windows & tools", shift=True),
    Action("spectrogram", _keys("F",), "app", "Spectrogram", "Windows & tools", shift=True),   # f: frequencies
    Action("scope", _keys("O",), "app", "Stereo scope", "Windows & tools", shift=True),   # o: its round picture
    Action("compare", _keys("D",), "app", "Compare two files", "Windows & tools", shift=True),   # d: difference
    Action("abx", _keys("X",), "app", "ABX blind test", "Windows & tools", shift=True),   # x: the mystery version
    Action("test_signals", _keys("T",), "app", "Test signals", "Windows & tools", shift=True),
    Action("lyrics", _keys("L",), "app", "Lyrics", "Windows & tools", shift=True),
    Action("reels", _keys("W",), "app", "Tape reels (Experimental)", "Windows & tools", shift=True),   # w: wind
    # z: zoom the window down to the mini strip, and back
    Action("mini", _keys("Z", "Ctrl+Shift+M"), "app", "Mini player and back", "Windows & tools", shift=True),
    Action("settings", _keys("Ctrl+,", "Ctrl+P"), "app", "Settings", "Windows & tools"),
    Action("snap_home", _keys("Ctrl+Shift+H",), "main", "Snap the window to its home spot", "Windows & tools"),
    Action("help", _keys("F1", "?"), "app", "Keyboard shortcuts (this list)", "Windows & tools", shift=True),
    Action("quit", _keys("Ctrl+Q",), "app", "Quit Ironbark", "Windows & tools"),
    # -- Library & search -------------------------------------------------------------------------
    Action("library", _keys("Ctrl+L",), "app", "The Library's views (Albums, Artists, …) in the Library tab; again: back",
           "Library & search"),
    Action("find", _keys("Ctrl+K", "Ctrl+Shift+F"), "app", "Search the whole library (the Library tab's search box)",
           "Library & search"),
    Action("library_search", _keys("Ctrl+F", "/"), "library", "Library views: search box", "Library & search",
           shift=True),
    Action("library_select_all", _keys("Ctrl+A",), "library", "Library views: select every song (the song views)",
           "Library & search"),
    Action("library_open", _keys("Enter",), "library", "Library views: open the album in a new tab (a song: play it)",
           "Library & search"),
    Action("library_queue", _keys("Ctrl+Enter",), "library", "Library views: add the album to Up next",
           "Library & search"),
    Action("library_play_next", _keys("Ctrl+Shift+Enter",), "library", "Library views: play the album next",
           "Library & search"),
    Action("library_view", _keys("Ctrl+Tab", "Ctrl+Shift+Tab"), "library",
           "Library views: next / previous view (Albums, Artists, …)", "Library & search", arg=True),
    Action("library_rescan", _keys("F5",), "library", "Library views: look for added or changed music",
           "Library & search"),
    # -- Editing --------------------------------------------------------------------------------
    Action("remove", _keys("Delete",), "list", "Remove the song from the playlist", "Editing"),
    Action("move_song", _keys("Alt+Up", "Alt+Down"), "list", "Move the song up / down the playlist", "Editing",
           arg=True),
    Action("undo", _keys("Ctrl+Z",), "main", "Undo: songs removed or moved, or taken out of Up next", "Editing"),
    Action("rename", _keys("F2",), "main", "Rename the tab (a playlist: its file)", "Editing"),
    Action("open_album_tab", _keys("Ctrl+Shift+O",), "list", "Track list: open the song's album in a new tab",
           "Tabs & lists"),
    Action("add_to_this_playlist", _keys("Ctrl+Shift+A",), "list",
           "Track list: add the library song found to this playlist", "Tabs & lists"),
    Action("edit_tags", _keys("E",), "main", "Edit tags of the selected song (Experimental)", "Editing", shift=True),
    Action("save_tags", _keys("Ctrl+S",), "tags", "Edit tags: save", "Editing"),
)

BY_ID: dict[str, Action] = {a.id: a for a in ACTIONS}

# Keys that belong to one window or one control, bound there (not through the table): listed in
# the sheet and the README so they can be found too. (keys as shown, what they do, group)
OTHER_KEYS: tuple[tuple[str, str, str], ...] = (
    ("↓ · Enter", "Search box: go to the list · play (open) the first result", "Library & search"),
    ("Enter · Shift+Enter · Ctrl+Enter · Alt+Enter",
     "Library search (Ctrl+K): play · play next · add to Up next · open the album", "Library & search"),
    ("A – Z · 0 – 9", "Library list: jump to the first name starting with what you type", "Library & search"),
    ("Menu key · Shift+F10", "The selected row's right-click menu", "Tabs & lists"),
    ("Shift+click · Ctrl+click · Shift+↑ / ↓",
     "Track list, the Library's songs, the library search's results: "
     "select several songs", "Tabs & lists"),
    ("Esc", "Close a menu or window (a search box: clear it first)", "Windows & tools"),
    ("Tab · Shift+Tab", "A question box: go from button to button (Enter presses the one ringed in green)",
     "Windows & tools"),
    ("A · B · X · Space", "ABX window: hear A, B or X · stop or replay", "Windows & tools"),
    ("A · B · D · Space", "Clipping (Hear it on): hear A, B or D · stop or replay", "Windows & tools"),
)

# How keys are written in the table → Tk's lower-cased keysym.
_NAMES = {"space": "space", "enter": "return", "esc": "escape", "delete": "delete", "tab": "tab", "left": "left",
          "right": "right", "up": "up", "down": "down", "pgup": "prior", "pgdn": "next", "home": "home",
          "end": "end", ",": "comma", ".": "period", "?": "question", "/": "slash"}
# …and back again, for people (arrows as arrows).
_SHOWN = {"Left": "←", "Right": "→", "Up": "↑", "Down": "↓"}
# Keysyms that name the same key another way.
_KEYSYMS = {"ISO_Left_Tab": "tab", "KP_Enter": "return", "KP_Prior": "prior", "KP_Next": "next",
            "KP_Up": "up", "KP_Down": "down", "KP_Left": "left", "KP_Right": "right", "KP_Home": "home",
            "KP_End": "end", "KP_Delete": "delete",
            # Tk 9 names punctuation by its character ("/"), where Tk 8.6 gave X11's name ("slash").
            "/": "slash", "?": "question", ",": "comma", ".": "period"}
_MODIFIERS = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt"}
_TK_MODIFIERS = {"Ctrl": "Control", "Shift": "Shift", "Alt": "Alt"}

# Keys a text box uses itself, even with Ctrl held: word jumps, undo, select all / copy / paste / cut.
_TEXT_KEYS = {"left", "right", "home", "end", "backspace", "delete", "a", "c", "v", "x", "z", "y"}
# Keys a list (ttk.Treeview) answers with any modifier held — the table's meanings for them are
# bound on the list itself. Plain navigation keys stay the list's.
_LIST_KEYS = {"up": "Up", "down": "Down", "left": "Left", "right": "Right", "prior": "Prior", "next": "Next",
              "home": "Home", "end": "End", "space": "space", "return": "Return"}
_NAVIGATION = {"up", "down", "left", "right", "prior", "next", "home", "end"}

Key = tuple[frozenset, str]   # (modifiers, base keysym)


def parse(text: str) -> Key:
    """"Ctrl+Shift+T" → ({"Ctrl", "Shift"}, "t"); "Ctrl+," → ({"Ctrl"}, "comma"); "?" → ({}, "question")."""
    *mods, name = text.split("+")
    base = _NAMES.get(name.lower(), name.lower())
    return frozenset(_MODIFIERS[m.lower()] for m in mods), base


def sequences(action_id: str) -> list[str]:
    """Tk event sequences for an action's keys, for a widget that binds them itself (a list of its
    own that isn't the track list: the docked Up next's Ctrl+A). A letter both ways round (Caps Lock)."""
    found = []
    for text in BY_ID[action_id].keys:
        mods, base = parse(text)
        names = [_TK_MODIFIERS[m] for m in ("Ctrl", "Alt", "Shift") if m in mods]
        if len(base) == 1 and base.isalpha():
            keysyms = [base, base.upper()]
        elif base in ("space", "comma", "period", "question", "slash") or base.isdigit():
            keysyms = [base]
        else:
            keysyms = [base.capitalize()]
        found += ["<" + "-".join([*names, "Key", keysym]) + ">" for keysym in keysyms]
    return found


def normalise(keysym: str, state: int) -> Key:
    """A key press as the table spells it: modifiers held (Ctrl, Alt, Shift — Caps Lock and Num
    Lock ignored) and the keysym in lower case (Caps Lock gives "T" for t)."""
    mods = set()
    if state & CONTROL:
        mods.add("Ctrl")
    if state & ALT:
        mods.add("Alt")
    if state & SHIFT or keysym == "ISO_Left_Tab":
        mods.add("Shift")
    base = _KEYSYMS.get(keysym)
    if base is None:
        base = keysym[3:] if keysym.startswith("KP_") and keysym[3:].isdigit() else keysym.lower()
    return frozenset(mods), base


def _single_character(base: str) -> bool:
    return len(base) == 1 or base in ("question", "slash", "comma", "period")


def works_while_typing(key: Key) -> bool:
    """Does this key act even while a text box has the keyboard? Function keys do; otherwise only
    with Ctrl or Alt held, and never the keys text boxes use themselves."""
    mods, base = key
    if base[:1] == "f" and base[1:].isdigit():
        return True
    if not mods & {"Ctrl", "Alt"}:
        return False
    return base not in _TEXT_KEYS


def is_text_box(widget) -> bool:
    """Something you type into: an entry (search boxes, tag fields), a text area. Not a read-only
    text area (state "disabled": the PCM stream's columns, a licence — clicked, it still takes the
    keyboard on Windows) nor a choice list (a read-only combobox): nothing can be typed there."""
    if isinstance(widget, tk.Text):
        return str(widget.cget("state")) != "disabled"
    if isinstance(widget, ttk.Combobox):
        return not widget.instate(("readonly",))
    return isinstance(widget, (tk.Entry, ttk.Entry, tk.Spinbox, ttk.Spinbox))


def build_map(actions=ACTIONS) -> dict[Key, list[Action]]:
    """Every key → the actions it can mean (one per scope), narrowest scope first."""
    found: dict[Key, list[Action]] = {}
    for action in actions:
        for text in action.keys:
            key = parse(text)
            variants = [key]
            if action.shift and not key[0] and _single_character(key[1]):
                variants.append((frozenset({"Shift"}), key[1]))
            for variant in variants:
                found.setdefault(variant, []).append(action)
    for listed in found.values():
        listed.sort(key=lambda a: SCOPE_ORDER[a.scope])
    return found


# -- words for people ----------------------------------------------------------------------------
def shown(text: str) -> str:
    """A key as written for people: "Alt+Up" → "Alt+↑", "Ctrl+PgDn" stays."""
    *mods, name = text.split("+")
    return "+".join([*mods, _SHOWN.get(name, name)])


def keys_text(action: Action) -> str:
    """All of an action's keys, for the sheet: "N · Ctrl+→" (either one), "← / →" (back / forward)."""
    return action.shown or (" / " if action.arg else " · ").join(shown(k) for k in action.keys)


def hint(action_id: str, which: int = 0) -> str:
    """The hint a menu shows beside an item: the action's first key (or `which` of a pair)."""
    return shown(BY_ID[action_id].keys[which])


def listed() -> list[Action]:
    """The actions the sheet and the README list (not the reserved ones)."""
    return [a for a in ACTIONS if not a.reserved]


def sheet_rows() -> list[tuple[str, str, str]]:
    """(group, keys, what it does) for every row of the sheet, group by group in GROUPS order:
    the table's actions, then the keys bound elsewhere (OTHER_KEYS)."""
    rows = []
    for group in GROUPS:
        rows += [(group, keys_text(a), a.label) for a in listed() if a.group == group]
        rows += [(group, keys, label) for keys, label, g in OTHER_KEYS if g == group]
    return rows


def readme_section() -> str:
    """The README's "## Keyboard" section, made from the table (a test checks the README matches)."""
    intro = ("Press **F1** (or **?**) in the player for this list, with a filter box. Most keys work in every "
             "Ironbark window; while you're typing in a box, only the Ctrl and function-key shortcuts act "
             "(Ctrl+Space still plays and pauses).")
    lines = ["## Keyboard", "", textwrap.fill(intro, 100), ""]
    for group in GROUPS:
        rows = [(keys, label) for g, keys, label in sheet_rows() if g == group]
        lines += [f"### {group}", "", "| Key | Action |", "|---|---|"]
        lines += [f"| {keys} | {label} |" for keys, label in rows]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# -- the dispatcher ------------------------------------------------------------------------------
class Keys:
    """The app's keyboard. `handlers` are the main window's: action id → callable (an `arg`
    action's handler takes the key as written in the table). A handler that returns False didn't
    act (nothing selected, say): the key then goes on to the widget, as if it weren't a shortcut.

    `attach(window, scope, handlers)` once per window when it's made: the window answers the "app"
    keys and its own scope's, with `handlers` for those; `mark(widget, scope)` gives one widget a
    scope of its own (the main track list is "list"). While `held()` is true (a question box is
    waiting), no key acts."""

    def __init__(self, root: tk.Misc, handlers: dict[str, Callable] | None = None,
                 held: Callable[[], bool] | None = None):
        self.root = root
        self.handlers: dict[str, Callable] = dict(handlers or {})
        self.held = held or (lambda: False)
        self.map = build_map()
        self._windows: dict[str, tuple[frozenset, dict[str, Callable]]] = {}
        self._widgets: dict[str, str] = {}
        self._passed: tuple | None = None   # a key a list's own binding looked at and let go on
        root.bind_class(TAG, "<KeyPress>", self._dispatch)
        # A ttk entry's Ctrl+K deletes to the end of the line before the table's Ctrl+K (search)
        # acts: an empty script removes that class binding (Ctrl+K then types nothing, as any Ctrl key).
        root.bind_class("TEntry", "<Control-Key-k>", "")

    # -- windows -------------------------------------------------------------------------------
    def attach(self, window: tk.Misc, scope: str = "app", handlers: dict[str, Callable] | None = None):
        """Make `window` answer the keys: its scope ("app", or "main" / "library" / "tags") and its
        own handlers. The shared tag goes on every widget in it now, and on any widget made later
        when it first takes the keyboard (<FocusIn>)."""
        path = str(window)
        first = path not in self._windows
        self._windows[path] = (frozenset({"app", scope}), dict(handlers or {}))
        if not first:
            return
        window.bind("<FocusIn>", self._focused, add="+")
        if window is not self.root:
            window.bind("<Destroy>", lambda e, p=path: self._forget(e, p), add="+")
        stack = [window]
        while stack:
            widget = stack.pop()
            self._tag(str(widget), path)
            stack.extend(child for child in widget.winfo_children() if not isinstance(child, tk.Toplevel))

    def mark(self, widget: tk.Misc, scope: str):
        """`widget` answers `scope`'s keys too, while it has the keyboard (the track list: "list")."""
        self._widgets[str(widget)] = scope
        top = str(widget.winfo_toplevel())
        self._tag(str(widget), top)
        if isinstance(widget, ttk.Treeview):
            self._claim_list_keys(widget)

    def add_handlers(self, window: tk.Misc, handlers: dict[str, Callable]):
        """More handlers for an attached window (the Library's views live inside the main window:
        their keys answer there, on the widgets `mark`ed "library")."""
        scopes, local = self._windows[str(window)]
        local.update(handlers)

    def attached(self, window: tk.Misc) -> bool:
        return str(window) in self._windows

    def scopes_of(self, window: tk.Misc) -> frozenset:
        found = self._windows.get(str(window))
        return found[0] if found else frozenset()

    def _forget(self, event, path: str):
        if str(event.widget) == path:
            self._windows.pop(path, None)
            for widget in [w for w in self._widgets if w == path or w.startswith(path + ".")]:
                del self._widgets[widget]

    def _focused(self, event):
        """A widget took the keyboard: make sure it carries the shared tag (it may be new)."""
        path = str(event.widget)
        try:
            top = str(self.root.tk.call("winfo", "toplevel", path))
        except tk.TclError:
            return
        if top in self._windows:
            self._tag(path, top)

    def _tag(self, path: str, top: str):
        """Put the shared tag straight after the window's own one in `path`'s bindtags (a widget
        whose bindtags don't hold the window's tag — an in-app prompt's — is left alone)."""
        call = self.root.tk.call
        try:
            tags = self.root.tk.splitlist(call("bindtags", path))
        except tk.TclError:
            return
        if TAG in tags or top not in tags:
            return
        at = tags.index(top) + 1
        call("bindtags", path, tags[:at] + (TAG,) + tags[at:])
        if path != top:
            try:
                widget = self.root.nametowidget(path)
            except KeyError:
                return
            if isinstance(widget, ttk.Treeview):
                self._claim_list_keys(widget)

    def _claim_list_keys(self, tree: ttk.Treeview):
        """Bind on the list itself the table's keys a Treeview would also act on (an arrow with a
        modifier, Space, Enter), so the list doesn't move or fold as well. A key the table doesn't
        take here goes on to the list's own bindings unchanged."""
        if getattr(tree, "_dg_keys_claimed", False):
            return
        tree._dg_keys_claimed = True
        sequences = set()
        for mods, base in self.map:
            if base in _LIST_KEYS and (mods or base not in _NAVIGATION):
                names = [_TK_MODIFIERS[m] for m in ("Ctrl", "Alt", "Shift") if m in mods]
                sequences.add("<" + "-".join([*names, "Key", _LIST_KEYS[base]]) + ">")
        for sequence in sorted(sequences):
            if not tree.bind(sequence):   # (a key the list binds itself stays its own)
                tree.bind(sequence, self._list_key)

    # -- a key press ---------------------------------------------------------------------------------
    def resolve(self, widget, keysym: str, state: int) -> tuple[Action, Callable, str] | None:
        """What a key press on `widget` would do: (action, handler, the key as written), or None."""
        path = str(widget)
        try:
            top = str(self.root.tk.call("winfo", "toplevel", path))
        except tk.TclError:
            return None
        window = self._windows.get(top)
        if window is None:
            return None
        scopes, local = window
        own = self._widgets.get(path)
        if own is not None:
            scopes = scopes | {own}
        key = normalise(keysym, state)
        mods, base = key
        if is_text_box(widget) and not works_while_typing(key):
            return None
        if (not mods and base in ("left", "right") and isinstance(widget, ttk.Treeview)
                and "tree" in str(widget.cget("show"))):
            return None   # a list of groups: ← → fold them
        for action in self.map.get(key, ()):
            if action.scope not in scopes:
                continue
            handler = local.get(action.id) or self.handlers.get(action.id)
            if handler is None:
                continue
            return action, handler, self._written(action, key)
        return None

    @staticmethod
    def _written(action: Action, key: Key) -> str:
        """Which of the action's keys this press was (Shift ignored where the action says so)."""
        for text in action.keys:
            mods, base = parse(text)
            if base == key[1] and (mods == key[0] or (action.shift and key[0] == frozenset({"Shift"}))):
                return text
        return action.keys[0]

    def run(self, widget, keysym: str, state: int) -> bool:
        """Carry out the key's action, if it has one here. True when it acted."""
        if self.held():
            return False
        found = self.resolve(widget, keysym, state)
        if found is None:
            return False
        action, handler, written = found
        result = handler(written) if action.arg else handler()
        return result is not False

    def _dispatch(self, event):
        seen = (str(event.widget), event.serial, event.keysym, event.state)
        if seen == self._passed:   # (a list's own binding has looked at this one already)
            self._passed = None
            return None
        return "break" if self.run(event.widget, event.keysym, event.state) else None

    def _list_key(self, event):
        if self.run(event.widget, event.keysym, event.state):
            return "break"
        self._passed = (str(event.widget), event.serial, event.keysym, event.state)
        return None

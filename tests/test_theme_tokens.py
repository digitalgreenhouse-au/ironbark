"""Design tokens (0.18.0): colours and text sizes live only in theme.py, and the palette can be
swapped while the app runs (what the Light theme will need)."""

from __future__ import annotations

import ast
import gc
import re
from pathlib import Path

import pytest

from ironbark import theme

PACKAGE = Path(theme.__file__).parent
HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8}|[0-9a-fA-F]{12})")
COLOUR_NAMES = {"white", "black", "red", "green", "blue", "yellow", "orange", "grey", "gray", "amber", "purple",
                "magenta", "cyan", "pink", "brown"}
COLOUR_KEYWORDS = {"fill", "outline", "bg", "fg", "background", "foreground", "activebackground", "activeforeground",
                   "highlightbackground", "highlightcolor", "insertbackground", "selectbackground",
                   "selectforeground", "selectcolor", "troughcolor", "disabledforeground", "colour", "color",
                   "hover", "hover_fg", "foreground_colour"}
FONT_CALLS = {"Font", "icon_font", "truetype", "IconButton"}

# Genuinely data-like exceptions, each with its reason. (Empty: nothing needed one so far.)
ALLOWED: dict[tuple[str, int], str] = {}


def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_number(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)


def problems_in(source: str, filename: str) -> list[str]:
    """Hex colours, colour names, RGB tuples and font sizes written as literals (one report per line)."""
    found: dict[int, str] = {}
    tree = ast.parse(source, filename)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))

    def report(node, what):
        if (filename, node.lineno) not in ALLOWED:
            found.setdefault(node.lineno, f"{filename}:{node.lineno}: {what}")

    for node in ast.walk(tree):
        # "#3f9a6b" anywhere in a string (not docstrings: they may describe a colour).
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if HEX.fullmatch(node.value.strip()):
                report(node, f"hex colour {node.value!r}")
        # A colour by name or as an RGB tuple: fill="white" if on else …, colour = (30, 30, 30)
        coloured = []
        if isinstance(node, ast.Call):
            coloured += [(kw.arg, kw.value) for kw in node.keywords if kw.arg in COLOUR_KEYWORDS]
        if isinstance(node, ast.Assign):
            coloured += [(_name(t), node.value) for t in node.targets
                         if _name(t).lower().endswith(("colour", "color"))]
        for key, branch in coloured:
            for value in ast.walk(branch):
                if isinstance(value, ast.Constant) and isinstance(value.value, str) \
                        and value.value.lower() in COLOUR_NAMES:
                    report(value, f"colour name {key}={value.value!r}")
                if isinstance(value, ast.Tuple) and len(value.elts) in (3, 4) and all(map(_is_number, value.elts)):
                    report(value, f"RGB tuple {key}=(…)")
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                value = kw.value
                if kw.arg == "size" and _is_number(value):
                    report(value, f"font size size={value.value}")
                if kw.arg == "font" and isinstance(value, ast.Tuple) and any(map(_is_number, value.elts)):
                    report(value, "font size in font=(…)")
            if _name(node.func) in FONT_CALLS and len(node.args) >= 2 and _is_number(node.args[1]):
                report(node, f"font size in {_name(node.func)}(…, {node.args[1].value})")
        # (theme.FONT, 9) / ("Consolas", 10): a font written as a tuple.
        if isinstance(node, ast.Tuple) and len(node.elts) >= 2 and _is_number(node.elts[1]):
            first = node.elts[0]
            if _name(first) in ("FONT", "MONO") or (isinstance(first, ast.Constant) and isinstance(first.value, str)
                                                  and first.value in ("Segoe UI", "Consolas", "TkDefaultFont")):
                report(node, "font size in a font tuple")
        # A default argument that is a colour: colour=(240, 240, 240)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.args[len(node.args.args) - len(node.args.defaults):] + node.args.kwonlyargs
            defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
            for arg, default in zip(args, defaults):
                if arg.arg in COLOUR_KEYWORDS and isinstance(default, ast.Tuple) and all(map(_is_number, default.elts)):
                    report(default, f"RGB tuple default {arg.arg}=(…)")
    return [found[line] for line in sorted(found)]


def test_checker_catches_each_kind():
    source = ('x = "#1e1f22"\nf(bg="white")\ng(font=("Segoe UI", 9))\nh(fill=(1, 2, 3))\n'
              'k(size=9)\nicon_font(root, 9)\nfont = (theme.FONT, 8, "bold")\n'
              'def b(colour=(240, 240, 240)):\n    """Uses #ffffff."""\n'
              'd.ellipse(box, fill="white" if on else theme.MUTED)\ncolour = (30, 30, 30) if a else (9, 9, 9)\n')
    assert len(problems_in(source, "x.py")) == 10
    fine = 'x = theme.BG\nf(font=theme.font(theme.SIZE_SMALL))\npadding = (10, 8, 10, 4)\n"""#abc in a docstring"""\n'
    assert problems_in(fine, "x.py") == []


def test_no_colour_or_font_size_literals_outside_theme():
    problems = []
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "theme.py":
            continue
        problems += problems_in(path.read_text(encoding="utf-8"), path.name)
    assert problems == [], "Use theme.py's tokens instead:\n" + "\n".join(problems)


def test_palette_roles_complete_and_distinct():
    roles = theme.DARK.roles()
    assert set(roles) == set(theme.ROLES)
    for role, value in roles.items():
        assert HEX.fullmatch(value) and len(value) == 7, role
        assert getattr(theme, role) == value, f"theme.{role} doesn't match the current palette"
    # recolour() finds a widget's role by its colour, so no two roles may share one.
    values = [v.lower() for v in roles.values()]
    assert len(values) == len(set(values))


def test_dark_palette_is_todays_look():
    """0.18.0 changed nothing on screen; 0.19.0 nudged four colours for WCAG AA contrast (MUTED
    #9aa0a6, DANGER #e5534b, METER_RED #c0392b before; ACCENT_TEXT is new) — the rest as before."""
    assert (theme.BG, theme.SURFACE, theme.ACCENT, theme.TEXT, theme.MUTED) == \
        ("#1e1f22", "#2b2d31", "#3f9a6b", "#e6e6e6", "#9fa5ab")
    assert theme.STRIP.lower() == "#0e0f12" and theme.CLOSE_HOVER.lower() == "#c42b1c"
    assert theme.DANGER == "#e96e67" and theme.SWITCH_OFF == "#4a4d55" and theme.ACCENT_TEXT == "#44a673"
    assert theme.METER_FACE == "#efe4c8" and theme.METER_INK == "#1d1d1b" and theme.METER_RED == "#ba372a"
    assert theme.font(theme.SIZE_SMALL, "bold") == (theme.FONT, 8, "bold")
    assert theme.rgb("ACCENT") == (0x3F, 0x9A, 0x6B) and theme.colour("BG") == "#1e1f22"
    assert theme.colour("#123456") == "#123456"


def _other_palette() -> theme.Palette:
    """A made-up palette: every colour different (inverted), for switching tests."""
    def invert(value):
        r, g, b = theme.rgb(value)
        return f"#{255 - r:02x}{255 - g:02x}{255 - b:02x}"
    return theme.Palette(name="test", dark=False, **{k: invert(v) for k, v in theme.DARK.roles().items()})


def test_use_switches_names_and_runs_hooks():
    seen = []

    class Window:
        def changed(self, palette):
            seen.append(palette.name)

    window = Window()
    theme.on_change(window.changed)
    other = _other_palette()
    try:
        theme.use(other)
        assert theme.BG == other.BG and theme.colour("ACCENT") == other.ACCENT and theme.current is other
    finally:
        theme.use(theme.DARK)
    assert seen == ["test", "dark"] and theme.BG == "#1e1f22"
    del window
    gc.collect()
    theme.use(theme.DARK)          # a hook whose window is gone is simply dropped
    assert seen == ["test", "dark"]


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.withdraw()
    theme.apply(r)
    yield r
    theme.use(theme.DARK)
    r.destroy()
    del r
    gc.collect()   # on this thread (Tk objects freed on another thread abort Python)


def test_px_follows_display_scaling(root):
    s = root.winfo_fpixels("1i") / 96
    assert theme.scale() == pytest.approx(s)
    assert theme.px(10) == int(10 * s) and theme.pad(10, 4) == (int(10 * s), int(4 * s))


def test_palette_switch_recolours_widgets(root):
    """The Light theme: switching re-colours what's already on screen, both ways."""
    import tkinter as tk

    from ironbark.tabbar import DISABLED_FADE, IconButton, MenuRow, SettingRow

    other = _other_palette()
    frame = tk.Frame(root, bg=theme.SURFACE)
    label = tk.Label(frame, text="x", bg=theme.SURFACE, fg=theme.MUTED)
    canvas = tk.Canvas(root, bg=theme.BG)
    item = canvas.create_rectangle(0, 0, 5, 5, fill=theme.ACCENT, outline=theme.BORDER)
    text = tk.Text(root)
    text.tag_configure("head", foreground=theme.ACCENT)
    button = IconButton(root, root, "settings", lambda: None)
    row = MenuRow(root, "Open", lambda: None, "Ctrl+O")
    switch = tk.BooleanVar(root, value=True)
    setting = SettingRow(root, root, "Bit-perfect", "Sound exactly as recorded.", switch)
    photo_before = str(setting.toggle.cget("image"))

    theme.use(other, root)
    assert frame.cget("bg") == other.SURFACE and label.cget("fg") == other.MUTED
    assert canvas.cget("bg") == other.BG
    assert canvas.itemcget(item, "fill") == other.ACCENT and canvas.itemcget(item, "outline") == other.BORDER
    assert text.tag_cget("head", "foreground") == other.ACCENT
    assert button.cget("bg") == other.STRIP and button.cget("fg") == other.MUTED
    button._paint(True)            # hover after the switch: the new palette's colours, not the old
    assert button.cget("bg") == other.SURFACE_HI and button.cget("fg") == other.TEXT
    button._paint(False)
    row.set_enabled(False)
    # (0.21.0) a greyed-out menu item: MUTED faded toward the background, worked out in the new palette
    assert row.label.cget("fg") == theme.mix(other.MUTED, other.SURFACE, DISABLED_FADE)
    assert row.cget("bg") == other.SURFACE
    assert str(setting.toggle.cget("image")) != photo_before   # the slider switch was redrawn
    assert ttk_background(root, "TFrame") == other.BG

    theme.use(theme.DARK, root)
    assert frame.cget("bg") == theme.DARK.SURFACE and canvas.itemcget(item, "fill") == theme.DARK.ACCENT
    assert button.cget("fg") == theme.DARK.MUTED and ttk_background(root, "TFrame") == theme.DARK.BG


def ttk_background(root, style: str) -> str:
    from tkinter import ttk
    return str(ttk.Style(root).lookup(style, "background"))

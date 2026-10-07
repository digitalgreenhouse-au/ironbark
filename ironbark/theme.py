"""The look of the whole app, in one place: design tokens (colours, text sizes, spacing), the
ttk "clam" theme restyled with them, and the dark Windows title bar.

Every screen takes its colours, text sizes and gaps from here instead of writing raw numbers
(tests/test_theme_tokens.py fails if a hex colour or a font size turns up anywhere else).

**Colours are a palette the app can swap while it runs** (0.18.0): `DARK` (the default) and,
since 0.19.0, `LIGHT`; Settings → Window → Theme picks Dark, Light or Follow Windows
(`palette_for`, `windows_uses_light`). Both palettes pass the WCAG AA contrast rules (the web's
accessibility standard) for every text / background pair the app draws — `TEXT_PAIRS` and
`GRAPHIC_PAIRS` below, checked by tests/test_theme_tokens.py. `use(palette, root)` makes another palette current:
the module-level names below (`theme.BG`, `theme.ACCENT`, …) change with it, so anything drawn
afterwards picks up the new colours; the ttk styles are re-applied; every plain Tk widget, canvas
item and Text tag still showing an old palette colour is switched to the same role's new colour;
and the `on_change` hooks run (for what can't be re-coloured in place — Pillow-drawn pictures such
as the VU faces and slider switches, Treeview tags, and colours a window keeps between frames).
"""

from __future__ import annotations

import dataclasses
import sys
import tkinter as tk
import weakref
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable


# -- colours -------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Palette:
    """Every colour the app draws with, by role (the UPPER-CASE fields)."""

    name: str
    dark: bool                # dark window chrome (the title bar)

    BG: str                   # window
    SURFACE: str              # lists, fields, buttons, pop-overs
    SURFACE_HI: str           # hover
    SURFACE_DOWN: str         # pressed
    BORDER: str               # thin lines, guides, an empty bar's track
    TEXT: str
    MUTED: str                # secondary text, scales, icons at rest
    ACCENT: str               # DG green: fills, lines, icons (the switch, the playing tab, play)
    ACCENT_TEXT: str          # (0.19.0) green *text* — headings, album rows, Up next, new albums: DG
                              # green itself is too pale for small text on a light background
    ACCENT_INK: str           # (0.20.0) a near-black mark drawn on a DG-green fill: the play button's
                              # glyph (dark in both themes, by design)
    SELECT: str              # selected row
    ON_ACCENT: str            # text / knob drawn on ACCENT, SELECT or CLOSE_HOVER
    GOOD: str                 # a good value ("all good" green, lighter than ACCENT)
    WARN: str                 # amber: something to look at
    DANGER: str               # red: over full scale, clipping
    STRIP: str                # the tab strip along the top
    CLOSE_HOVER: str          # the window's close button under the pointer
    SWITCH_OFF: str           # the track of a slider switch that's off: clearly "empty", not disabled
    # The rounded look. DG green (ACCENT) is never drawn on STRIP_HI or HOVER: in Light it falls
    # under 3:1 there, so marks, ticks and "on" glyphs on them are ACCENT_TEXT.
    STRIP_HI: str             # on the strip under the pointer: a tab's plate, the + and gear discs
    TAB_EDGE: str             # the selected tab's rim, the strip's bottom line, the lines between tabs
    EDGE: str                 # the 1 px rim of fields, plain buttons, menus, pop-overs and prompts
    HOVER: str                # a list or menu row under the pointer; round icon buttons' disc on BG
    THUMB: str                # a scrollbar's thumb at rest

    # The meters' own colours. The VU face is drawn like a real meter's (cream card, black ink,
    # red over 0 VU) whatever the theme; they're roles so they're named in one place.
    METER_FACE: str
    METER_INK: str
    METER_RED: str
    METER_RIM: str
    METER_LEVEL: str          # RMS bars (Meters), the balance bar (Stereo scope)
    METER_LOUDNESS: str       # momentary / short-term loudness bars

    def roles(self) -> dict[str, str]:
        """{role: colour} for every colour in the palette."""
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self) if f.name.isupper()}


ROLES = tuple(f.name for f in dataclasses.fields(Palette) if f.name.isupper())

DARK = Palette(
    name="dark", dark=True,
    BG="#1e1f22",
    SURFACE="#2b2d31",
    SURFACE_HI="#383a40",
    SURFACE_DOWN="#44464d",
    BORDER="#3a3c42",
    TEXT="#e6e6e6",
    MUTED="#9fa5ab",        # (0.19.0: was #9aa0a6 — a touch lighter, for AA on a hovered row)
    ACCENT="#3f9a6b",       # DG green
    ACCENT_TEXT="#44a673",  # (0.19.0) DG green a shade lighter: AA on SURFACE too
    ACCENT_INK="#121316",   # (0.20.0) 5.3:1 on DG green
    SELECT="#2f6f4e",
    ON_ACCENT="#ffffff",
    GOOD="#5cc28a",
    WARN="#f0a04b",
    DANGER="#e96e67",       # (0.19.0: was #e5534b — lighter, for AA on SURFACE)
    STRIP="#0e0f12",
    CLOSE_HOVER="#c42b1c",  # Windows' own close-button red
    SWITCH_OFF="#4a4d55",
    STRIP_HI="#24262b",
    TAB_EDGE="#2c2e33",
    EDGE="#45484f",
    HOVER="#36383e",
    THUMB="#787d85",
    METER_FACE="#efe4c8",   # the classic VU face
    METER_INK="#1d1d1b",
    METER_RED="#ba372a",    # (0.19.0: was #c0392b — a shade darker, for AA on the cream face)
    METER_RIM="#8c8370",
    METER_LEVEL="#4f8fd6",
    METER_LOUDNESS="#9b7fd4",
)

# (0.19.0) The Light theme: Windows-like pale greys, the same DG green for fills and icons, darker
# greens / amber / red for text so every pair passes WCAG AA. DG green (#3f9a6b) only reaches 3:1
# — enough for icons and lines, not for small text — against a background at least as light as
# #edeff2, which is why the strip and hover greys are no darker than that. The meters keep their
# cream VU face and bar colours: they're drawn the same in both themes.
LIGHT = Palette(
    name="light", dark=False,
    BG="#f6f7f9",
    SURFACE="#fdfdfe",
    SURFACE_HI="#f0f2f4",
    SURFACE_DOWN="#e3e6ea",
    BORDER="#d3d6db",
    TEXT="#1f2124",
    MUTED="#5c6168",
    ACCENT="#3f9a6b",       # DG green, unchanged
    ACCENT_TEXT="#2a7650",
    ACCENT_INK="#121316",   # (0.20.0) the play button's glyph: dark in both themes
    SELECT="#2f7a53",
    ON_ACCENT="#ffffff",
    GOOD="#1f7a49",
    WARN="#a85a00",
    DANGER="#c62f27",
    STRIP="#edeff2",
    CLOSE_HOVER="#c42b1c",  # Windows' own close-button red
    SWITCH_OFF="#c9ccd1",
    STRIP_HI="#dcdfe4",
    TAB_EDGE="#d0d4d9",
    EDGE="#c3c7cd",
    HOVER="#e8ebee",
    THUMB="#878c94",
    METER_FACE="#efe4c8",
    METER_INK="#1d1d1b",
    METER_RED="#ba372a",
    METER_RIM="#8c8370",
    METER_LEVEL="#4f8fd6",
    METER_LOUDNESS="#9b7fd4",
)

PALETTES = {"dark": DARK, "light": LIGHT}
# Settings → Window → Theme: (prefs value, what the menu says). Dark by default.
THEME_CHOICES = (("dark", "Dark"), ("light", "Light"), ("system", "Follow Windows"))
DEFAULT_THEME = "dark"

current: Palette = DARK

# The current palette's colours as plain names (theme.BG, theme.ACCENT, …). `use` rewrites them;
# always read them as `theme.X` when drawing (never copy one into a module constant or a default
# argument — it wouldn't follow a palette change).
BG = SURFACE = SURFACE_HI = SURFACE_DOWN = BORDER = TEXT = MUTED = ACCENT = ACCENT_TEXT = ACCENT_INK = ""
SELECT = ON_ACCENT = ""
GOOD = WARN = DANGER = STRIP = CLOSE_HOVER = SWITCH_OFF = ""
STRIP_HI = TAB_EDGE = EDGE = HOVER = THUMB = ""
METER_FACE = METER_INK = METER_RED = METER_RIM = METER_LEVEL = METER_LOUDNESS = ""
globals().update(DARK.roles())

# Not palette roles: these follow something other than the app's theme.
# The spectrogram's colour scale (data, not decoration): dark → blue → magenta → red → orange →
# pale yellow, like most audio spectrograms. (position 0–1, colour)
SPECTROGRAM_STOPS = ((0.00, "#000000"), (0.18, "#0e0a46"), (0.38, "#641487"), (0.58, "#cd2d46"),
                     (0.78, "#fa961e"), (1.00, "#fffcd2"))
# The taskbar's thumbnail buttons sit on Windows' taskbar, so they follow *its* light/dark setting.
TASKBAR_GLYPH_ON_DARK = "#f0f0f0"
TASKBAR_GLYPH_ON_LIGHT = "#1e1e1e"


def colour(role_or_colour: str) -> str:
    """A role's colour in the current palette ("ACCENT" → "#3f9a6b"); anything else unchanged.
    Widgets that repaint themselves later (hover, on/off) keep the *role*, so they follow a
    palette change."""
    return getattr(current, role_or_colour) if role_or_colour in ROLES else role_or_colour


def rgb(colour_value: str) -> tuple[int, int, int]:
    """"#rrggbb" (or a role) → (r, g, b), for Pillow and numpy drawing."""
    value = colour(colour_value).lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def mix(a: str, b: str, amount: float) -> str:
    """A colour part-way from `a` to `b` (roles or "#rrggbb"; amount 0 = a, 1 = b) — for the hover
    and pressed shades of drawn buttons, worked out from the palette rather than written down."""
    pa, pb = rgb(a), rgb(b)
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(pa, pb))


# -- contrast (WCAG 2.2) ------------------------------------------------------------------
# Checked 05/10/2026 against the W3C's "Understanding SC 1.4.3 Contrast (Minimum)" (WCAG 2.2):
# relative luminance L = 0.2126 R + 0.7152 G + 0.0722 B, each channel c = value / 255 made linear
# as c / 12.92 if c <= 0.04045, else ((c + 0.055) / 1.055) ** 2.4; contrast ratio = (L1 + 0.05) /
# (L2 + 0.05), lighter over darker; never rounded before comparing (4.499 fails 4.5). Level AA:
# text 4.5:1 (SC 1.4.3; 3:1 is allowed for large text, 18 pt / 14 pt bold — the app's text is
# smaller, so everything is held to 4.5), icons, lines and other graphics 3:1 (SC 1.4.11).
AA_TEXT = 4.5
AA_GRAPHICS = 3.0

# (foreground role, background role) for every kind of text the app draws…
TEXT_PAIRS = (
    ("TEXT", "BG"), ("TEXT", "SURFACE"), ("TEXT", "SURFACE_HI"), ("TEXT", "SURFACE_DOWN"),  # + tooltips, pressed
    ("MUTED", "BG"), ("MUTED", "SURFACE"), ("MUTED", "SURFACE_HI"), ("MUTED", "STRIP"),  # tab names, menu keys
    ("ACCENT_TEXT", "BG"), ("ACCENT_TEXT", "SURFACE"),
    ("ON_ACCENT", "SELECT"),                                   # a selected row
    ("GOOD", "BG"), ("GOOD", "SURFACE"), ("WARN", "BG"), ("WARN", "SURFACE"),
    ("WARN", "SURFACE_HI"),                                    # (0.20.0) the volume badge under the pointer
    ("DANGER", "BG"), ("DANGER", "SURFACE"),
    ("METER_INK", "METER_FACE"), ("METER_RED", "METER_FACE"),  # the VU face's numbers
    ("TEXT", "STRIP_HI"), ("MUTED", "STRIP_HI"),               # a tab's name on its hover plate
    ("TEXT", "HOVER"), ("MUTED", "HOVER"),                     # a hovered list or menu row
)
# …and for icons, lines and marks that carry meaning.
GRAPHIC_PAIRS = (
    ("ACCENT", "BG"), ("ACCENT", "SURFACE"), ("ACCENT", "SURFACE_HI"), ("ACCENT", "STRIP"),  # play, shuffle on, playing tab
    ("ON_ACCENT", "ACCENT"),                                   # a switch's knob when on
    ("ACCENT_INK", "ACCENT"),                                  # (0.20.0) the play button's glyph
    ("MUTED", "SWITCH_OFF"),                                   # …and when off
    ("ON_ACCENT", "CLOSE_HOVER"),                              # the × on the red close button
    ("METER_LEVEL", "BG"), ("METER_LOUDNESS", "BG"),           # meter bars
    ("ACCENT_TEXT", "HOVER"),                                  # a menu tick on the highlighted row; an "on"
                                                               # shuffle / repeat on its hover disc
    ("WARN", "SURFACE"), ("WARN", "BG"),                       # (0.21.0) a warning box's edge and sign
    ("DANGER", "SURFACE"),                                     # (0.21.0) the red ring round "Delete"
    ("ACCENT_TEXT", "STRIP"),                                  # the playing tab's level mark
    ("ACCENT_TEXT", "STRIP_HI"),                               # …on a hovered tab; the gear while Settings is open
    ("GOOD", "HOVER"),                                         # the bit-perfect lock (on) on its hover disc
    ("GOOD", "BG"), ("MUTED", "BG"),                           # the status dot (WARN on BG is above)
    ("THUMB", "BG"), ("THUMB", "SURFACE"),                     # a scrollbar's thumb at rest
)


def relative_luminance(colour_value: str) -> float:
    """WCAG relative luminance of a "#rrggbb" colour (or a role): 0 = black, 1 = white."""
    def linear(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(v) for v in rgb(colour_value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio of two colours, 1.0 (same) to 21.0 (black on white)."""
    lighter, darker = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


# -- which palette ------------------------------------------------------------------------
def windows_uses_light() -> bool | None:
    """Is Windows set to light mode for apps? (Settings → Personalisation → Colours → "Choose
    your app mode".) None when it can't be told (not Windows, or the value isn't there).

    Read-only registry look, cheap enough to poll: HKCU\\Software\\Microsoft\\Windows\\
    CurrentVersion\\Themes\\Personalize, DWORD AppsUseLightTheme (1 = light, 0 = dark) — the same
    key mediakeys.py reads SystemUsesLightTheme from for the taskbar. Microsoft Learn doesn't
    document this value as an API (it documents UISettings' foreground colour for Win32 apps);
    checked on Windows 11 on 05/10/2026 that the two agree (dark: AppsUseLightTheme 0, UISettings
    foreground white)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except OSError:
        return None


def palette_for(choice: str, windows_light: bool | None = None) -> Palette:
    """The palette for a Settings choice ("dark" / "light" / "system"). "system" follows Windows
    (`windows_light`, read now if not given); when Windows can't be asked it stays Dark."""
    if choice == "system":
        if windows_light is None:
            windows_light = windows_uses_light()
        return LIGHT if windows_light else DARK
    return PALETTES.get(choice, PALETTES[DEFAULT_THEME])


# -- text ----------------------------------------------------------------------------------
FONT = "Segoe UI" if sys.platform == "win32" else "TkDefaultFont"
# The fixed-width font (sample numbers; live numbers too where FONT's digits aren't all one width).
# Consolas ships with Windows since Vista (Microsoft Learn's Consolas page, checked 05/10/2026:
# "Fixed pitch: True", supplied by Windows 7 / 8 / 8.1 / 10 / 11). (0.22.0) For the Mac build: Menlo
# ships with macOS (Apple's "Fonts included with macOS Sequoia" lists Menlo Regular / Bold / Italic /
# Bold Italic, checked 05/10/2026); SF Mono isn't on that list. Not yet tried on a Mac.
MONO = "Consolas" if sys.platform == "win32" else "Menlo" if sys.platform == "darwin" else "TkFixedFont"
# (0.22.0) The family live numbers are drawn in, so they don't jiggle as their digits change: FONT
# when its digits are all one width, else MONO. Tk can't ask a font for tabular figures (Tk 8.6's
# `font` command has only -family -size -weight -slant -underline -overstrike: no OpenType
# features; its manual, checked 05/10/2026), so `apply` *measures* — see tabular_digits(). Measured
# 05/10/2026 on Windows 11 (Tk 8.6.12): Segoe UI's ten digits are one width at 9 / 10 / 11 pt, and
# "+" is as wide as the minus sign "−" (U+2212), but the hyphen "-" is narrower — so live numbers
# show "−" (figures()). Windows 11's "Segoe UI Variable Text" has narrow ones (a "1" 7 px against
# 10 px at 9 pt), which is why the family is measured rather than assumed. On a Mac (its system
# font's digits unverified) the same measurement decides.
NUMBERS = FONT
MINUS = "\u2212"          # the minus sign: as wide as "+" in Segoe UI (the hyphen isn't)
FIGURE_SPACE = "\u2007"   # a space as wide as a digit (measured: 10 px, as a digit, Segoe UI 9 pt)

# The semibold face, for titles: Windows ships Segoe UI in a true semibold, which Tk can use as a
# family of its own (Tk 8.6's -weight knows only normal and bold). `apply` checks Tk really has it
# (semibold_family); where it doesn't (a Mac, so far), "semibold" falls back to bold.
FONT_SEMIBOLD = "Segoe UI Semibold" if sys.platform == "win32" else ""
SEMIBOLD = ""         # set by apply: FONT_SEMIBOLD when Tk has it, else "" (bold stands in)

# The type scale, in points (Tk scales points with the display itself, so no px() here).
SIZE_TINY = 7         # scale numbers under bars and plots
SIZE_SMALL = 8        # captions, one-line descriptions, small notes
SIZE_BODY = 9         # ordinary text (Windows' own default size)
SIZE_LARGE = 10       # a slightly bigger mark (the × in Up next); the line under the now-playing title
SIZE_HEADING = 11     # a pop-over's title
SIZE_TITLE = 12       # a window's title line
SIZE_DISPLAY = 13     # the now-playing title (semibold)
SIZE_MONO = 10        # sample numbers (PCM stream)

# Icon glyphs (Segoe Fluent Icons / MDL2 Assets), in points — each size has one job.
TAB_CLOSE = 8         # a tab's ×, the search ×, Up next's ×, chevrons
ICON_SMALL = 9        # minimise / close, the magnifier
ICON = 10             # +, the gear, the lock, ⓘ; the mini strip's and menus' icons
ICON_MEDIUM = 11      # shuffle / repeat
ICON_LARGE = 13       # the mini strip's play glyph only (previous / next are drawn: shapes.skip)

# The VU face (Pillow): TrueType files, and text heights as a share of the face's height.
METER_FONT_FILE = "segoeui.ttf"
METER_FONT_BOLD_FILE = "segoeuib.ttf"
METER_FACE_TEXT = 0.085    # the scale numbers
METER_FACE_BIG = 0.16      # "VU", "−" and "+"


def tone(spec) -> str:
    """A colour from a role, a "#rrggbb", or a mix spelt (a, b, amount) — a worked-out shade that is
    worked out again whenever it's drawn, so it follows a palette change."""
    if isinstance(spec, tuple):
        return mix(*spec)
    return colour(spec)


def font(size: int = SIZE_BODY, *style: str) -> tuple:
    """A Segoe UI font at one of the SIZE_* steps, e.g. font(SIZE_SMALL, "bold"). "semibold" is the
    semibold face where Tk has it (see FONT_SEMIBOLD), else bold."""
    if "semibold" in style:
        rest = tuple(s for s in style if s != "semibold")
        return (SEMIBOLD, size, *rest) if SEMIBOLD else (FONT, size, "bold", *rest)
    return (FONT, size, *style)


def semibold_family(root: tk.Misc | None = None) -> str:
    """FONT_SEMIBOLD if Tk really draws it here, else "". Measured, like tabular_digits: Tk quietly
    swaps a family it can't find for another (Arial, on Windows), so the family Tk ends up with must
    be the one asked for, and its words must come out wider than the regular face's."""
    if not FONT_SEMIBOLD:
        return ""
    semi = tkfont.Font(root=root, family=FONT_SEMIBOLD, size=SIZE_DISPLAY)
    regular = tkfont.Font(root=root, family=FONT, size=SIZE_DISPLAY)
    sample = "Gacked On Anger"
    if semi.actual("family") != FONT_SEMIBOLD or semi.measure(sample) <= regular.measure(sample):
        return ""
    return FONT_SEMIBOLD


def mono(size: int = SIZE_MONO, *style: str) -> tuple:
    """The fixed-width font (sample numbers)."""
    return (MONO, size, *style)


def numbers(size: int = SIZE_BODY, *style: str) -> tuple:
    """(0.22.0) The font for live numbers: every digit one width (see NUMBERS), so a reading that
    changes ten times a second keeps its place instead of jiggling."""
    return (NUMBERS, size, *style)


def tabular_digits(family: str, root: tk.Misc | None = None) -> bool:
    """Are `family`'s digits all one width, with "+" as wide as "−", at the body and title sizes?
    Measured with Tk itself, so it's true of the font Tk really uses on this machine."""
    for size in (SIZE_BODY, SIZE_TITLE):
        measure = tkfont.Font(root=root, family=family, size=size).measure
        if len({measure(d) for d in "0123456789"}) != 1 or measure("+") != measure(MINUS):
            return False
    return True


def figures(text: str) -> str:
    """(0.22.0) A live reading ready to show: a hyphen in front of a number becomes the minus sign
    "−", as wide as "+" (Segoe UI's hyphen is narrower, so -0.2 → +0.4 would shift the text). Only a
    hyphen that starts a number changes ("BS.1770-5", "J-test", "Tape-1" don't)."""
    out = []
    for i, char in enumerate(text):
        if (char == "-" and i + 1 < len(text) and (text[i + 1].isdigit() or text[i + 1] == ".")
                and (i == 0 or not text[i - 1].isalnum())):
            char = MINUS
        out.append(char)
    return "".join(out)


def pad_figures(text: str, width: int, after: bool = False) -> str:
    """`text` padded with digit-wide spaces to `width` characters, so a number keeps its width when it
    gains or loses a digit: on the left (" 99%" over "100%"), or `after` it, so the number stays
    lined up on the left with the text above and what follows it still doesn't move ("99% " → "100%")."""
    room = FIGURE_SPACE * max(0, width - len(text))
    return text + room if after else room + text


# -- spacing -------------------------------------------------------------------------------
# Gaps are written at 100% display scaling and go through px(), so they grow with Windows'
# display scaling (125%, 150%, …) like the windows themselves.
GAP_HAIR = 2
GAP_SMALL = 4
GAP = 6
GAP_MEDIUM = 8
GAP_LARGE = 10
GAP_WIDE = 12

# Corners (px at 100%): one rule for the whole app — icon-only buttons are round, buttons with
# words are pills, and what you type into or what holds content has corners of 4, 8 or 12.
RADIUS_S = 4
RADIUS_M = 8
RADIUS_L = 12

# The tab strip (px at 100%). Every name and glyph on it is centred on one line, ROW_Y down.
STRIP_H = 36              # the strip's height
TAB_TOP = 6               # where the selected tab's body starts (it runs down into the page)
TAB_H = STRIP_H - TAB_TOP
ROW_Y = 21                # the centre line of every tab name and strip glyph
TAB_FLARE = 6             # the small inward curves where the selected tab flows into the page
TAB_GAP = 2               # between two tabs
TAB_MAX = 200             # a tab's widest, however wide the window
MIN_TAB = 6               # a tab's narrowest: however many are open, they all fit (and +)
TAB_SLIM = 48             # narrower than this, a tab shows a centred short name (or just its mark)
TAB_PAD_L = 8             # a tab's name starts this far in…
TAB_PAD_R = 6             # …and ends this far before the tab's right edge
TAB_PAD_SLIM = 4          # a slim tab's name, each side
PLATE_INSET = 4           # a hovered tab's plate, in from the tab's sides (= TAB_FLARE - TAB_GAP)
PLATE_H = 24              # …and its height
DIVIDER_H = 16            # the line between two quiet tabs
MARK_W = 9                # the playing mark: three 2 px level bars…
MARK_GAP = 5              # …and the gap after it, before the name
CLOSE_SLOT = 20           # a tab's × and the room it takes from the name
CLOSE_DISC = 16           # the disc under the × when the pointer is on it
DRAG_GAP = 28             # empty strip left of the first tab, for dragging the window
DISC = 28                 # the + and gear buttons' round hover disc
DISC_MARGIN = 2           # either side of the gear
ADD_GAP = 2               # between the last tab and +
CAPTION_W = 40            # minimise and close: full-height plates this wide

# Controls (px at 100%).
CONTROL_H = 32            # fields, and pills with words
CONTROL_H_SMALL = 24      # badges, compact pills
PILL_PAD = 12             # a pill's words, in from its ends…
PILL_PAD_SMALL = 8        # …on a compact pill
FIELD_INSET = 12          # a field's text, in from its left edge
FOCUS_RING = 2            # the DG-green ring round whatever has the keyboard
MENU_ROW_H = 32           # a menu row's height
MENU_INSET = 12           # a menu's words and separators, in from its sides
MENU_PLATE_INSET = 4      # the highlighted row's rounded plate, in from the menu's sides

# (0.20.0) The slim scrollbar (no arrow buttons; see SLIM_SCROLLBAR) and the search box's insets.
SCROLLBAR_WIDTH = 8       # px at 100%: the scrollbar's trough
THUMB_REST = 4            # its thumb: a pill this wide at rest…
THUMB_HOT = 6             # …and this wide under the pointer or while dragged
SEARCH_ICON_ROOM = 30     # px at 100%: room left of the search text for the magnifier
SEARCH_CLEAR_ROOM = 24    # …and right of it for the ×

# ttk style names made by apply() (0.20.0).
SLIM_SCROLLBAR = "Slim.Vertical.TScrollbar"
SLIM_HSCROLLBAR = "Slim.Horizontal.TScrollbar"   # (0.22.0) the same, across (PCM stream's long rows)
SEARCH_ENTRY = "Search.TEntry"
# Fields and slim scrollbars on a SURFACE panel or card (Settings, prompts) rather than the window:
# a field is filled one step "into" what holds it — SURFACE on the window, BG on a card.
CARD_ENTRY = "Card.TEntry"
CARD_COMBOBOX = "Pop.TCombobox"
CARD_SCROLLBAR = "Pop.Slim.Vertical.TScrollbar"

_scale: float | None = None


def scale(widget: tk.Misc | None = None) -> float:
    """Windows' display scaling as a factor: 1.0 at 100%, 1.5 at 150%. Measured once from the
    app's Tk window (every window of the app is on the same scaling)."""
    global _scale
    if widget is not None:
        return widget.winfo_fpixels("1i") / 96
    if _scale is None:
        root = tk._default_root  # the app's (or a test's) Tk root
        if root is None:
            return 1.0
        _scale = root.winfo_fpixels("1i") / 96
    return _scale


def px(n: float) -> int:
    """`n` pixels at 100% display scaling, in real pixels now (rounded down, like the
    `int(n * scale)` sums this replaces)."""
    return int(n * scale())


def pad(*values: float):
    """px() for a padding: pad(10, 8, 10, 4) → each side scaled; one value → one int."""
    scaled = tuple(px(v) for v in values)
    return scaled[0] if len(scaled) == 1 else scaled


# -- ttk styles ----------------------------------------------------------------------------
def _elements(root: tk.Tk, style: ttk.Style) -> None:
    """The ttk image elements the styles draw with: fields, the drop-downs' chevron, the list
    heading's line, the selected row's rounded plate and the scrollbar thumbs (shapes.element_pictures).
    Made once per Tk root — an element's name can't be made twice — and kept on
    `root._dg_style_images`; after a palette change the same pictures get new pixels."""
    from PIL import ImageTk

    from . import shapes   # (shapes draws with this module's colours: imported when it's needed)

    pictures = shapes.element_pictures()
    natural = {name: picture.size for name, (picture, _border) in pictures.items()}
    # Ttk repeats a picture's middle to fill its element, one draw call per copy: drawn with a
    # middle a few pixels across, a heading or row the width of a big window took seconds to paint.
    # So the middles are made wide (or long, for a scrollbar's thumb), while each element still asks
    # for its natural size (its width and height options), so nothing is laid out differently.
    pictures = {name: (shapes.widened(picture, border, *_drawn_size(name, picture.size)), border)
                for name, (picture, border) in pictures.items()}
    images = getattr(root, "_dg_style_images", None)
    if images is not None:
        for name, (picture, _border) in pictures.items():
            if (images[name].width(), images[name].height()) == picture.size:
                images[name].paste(picture)
        return
    images = root._dg_style_images = {name: ImageTk.PhotoImage(picture, master=root)
                                      for name, (picture, _border) in pictures.items()}
    border = {name: b for name, (_picture, b) in pictures.items()}

    def size(name: str) -> dict:
        return {"width": natural[name][0], "height": natural[name][1]}

    for prefix, where in (("Round", "BG"), ("Card", "SURFACE")):
        name = f"field {where}"
        style.element_create(f"{prefix}.field", "image", images[name], ("disabled", images[name + " off"]),
                             ("focus", images[name + " focus"]), border=border[name], padding=0, sticky="nswe",
                             **size(name))
    style.element_create("Round.chevron", "image", images["chevron"], ("disabled", images["chevron off"]),
                         ("pressed", images["chevron hot"]), ("active", images["chevron hot"]), sticky="")
    style.element_create("Round.heading", "image", images["heading"], border=border["heading"], sticky="nswe",
                         **size("heading"))
    style.element_create("Round.selection", "image", images["clear"], ("selected", images["selection"]),
                         border=border["selection"], sticky="nswe", **size("selection"))
    # (A scrollbar finds its thumb by the element's name: it must end in ".thumb".)
    for axis, element in (("v", "Slim.Vertical.Scrollbar.thumb"), ("h", "Slim.Horizontal.Scrollbar.thumb")):
        name = f"thumb {axis}"
        style.element_create(element, "image", images[name], ("pressed", images[name + " hot"]),
                             ("active", images[name + " hot"]), border=border[name], sticky="nswe", **size(name))


ELEMENT_MIDDLE = 1024   # px: how far a style picture's plain middle is stretched (see _elements)


def _drawn_size(name: str, size: tuple[int, int]) -> tuple[int, int]:
    """The size a style picture is drawn at: wide (rows, headings, fields), or long along a
    scrollbar thumb's own direction only (across, its rounded profile must stay as drawn)."""
    if name.startswith("chevron"):
        return size
    if name.startswith("thumb v"):
        return size[0], ELEMENT_MIDDLE
    if name.startswith("thumb h"):
        return ELEMENT_MIDDLE, size[1]
    return ELEMENT_MIDDLE, max(size[1], px(3 * CONTROL_H))


def apply(root: tk.Tk) -> None:
    """Style the app's ttk widgets with the current palette (run again after a palette change)."""
    global _scale, NUMBERS, SEMIBOLD
    _scale = root.winfo_fpixels("1i") / 96
    NUMBERS = FONT if tabular_digits(FONT, root) else MONO
    SEMIBOLD = semibold_family(root)
    root.configure(bg=BG)
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    style.configure(".", background=BG, foreground=TEXT, fieldbackground=SURFACE,
                    bordercolor=BORDER, lightcolor=BG, darkcolor=BG, troughcolor=BORDER,
                    focuscolor=ACCENT, selectbackground=SELECT, selectforeground=ON_ACCENT,
                    insertcolor=TEXT, arrowcolor=TEXT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)

    style.configure("TButton", background=SURFACE, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=SURFACE, darkcolor=SURFACE)
    style.map("TButton",
              background=[("pressed", SURFACE_DOWN), ("active", SURFACE_HI)],
              lightcolor=[("pressed", SURFACE_DOWN), ("active", SURFACE_HI)],
              darkcolor=[("pressed", SURFACE_DOWN), ("active", SURFACE_HI)])

    style.configure("TCheckbutton", background=BG, foreground=TEXT,
                    indicatorbackground=SURFACE, indicatorforeground=ON_ACCENT)
    style.map("TCheckbutton",
              background=[("active", BG)],
              indicatorbackground=[("selected", ACCENT), ("active", SURFACE_HI)])

    # Fields and drop-downs are pictures (ttk image elements, see _elements): a rounded field with a
    # 1 px EDGE rim, a 2 px DG-green ring while it has the keyboard, and a drawn chevron instead of
    # a separate arrow box. All of them are CONTROL_H tall, the words FIELD_INSET in.
    _elements(root, style)
    line = tkfont.nametofont("TkDefaultFont", root=root).metrics("linespace")
    pad_y = max(1, (px(CONTROL_H) - line) // 2)
    for name, field in (("TCombobox", "Round.field"), (CARD_COMBOBOX, "Card.field")):
        style.layout(name, [(field, {"sticky": "nswe", "children": [
            ("Round.chevron", {"side": "right", "sticky": "ns"}),
            ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]})])
        style.configure(name, foreground=TEXT, padding=(px(FIELD_INSET), pad_y, 0, pad_y), arrowcolor=MUTED)
        style.map(name, foreground=[("disabled", MUTED), ("readonly", TEXT)],
                  fieldbackground=[("readonly", BG if name == CARD_COMBOBOX else SURFACE)],
                  selectbackground=[("readonly", BG if name == CARD_COMBOBOX else SURFACE)],
                  selectforeground=[("readonly", TEXT)])
    # The combobox's drop-down list is a classic Tk listbox.
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECT)
    root.option_add("*TCombobox*Listbox.selectForeground", ON_ACCENT)

    style.configure("Horizontal.TScale", background=ACCENT, troughcolor=BORDER,
                    bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT)
    style.map("Horizontal.TScale", background=[("active", GOOD)])

    style.configure("Vertical.TScrollbar", background=SURFACE, troughcolor=BG,
                    bordercolor=BG, lightcolor=SURFACE, darkcolor=SURFACE, arrowcolor=MUTED)
    style.map("Vertical.TScrollbar", background=[("active", SURFACE_HI)])
    # (0.20.0) The slim scrollbar: just a thumb in a trough the colour of what holds it — no arrow
    # buttons; clam makes the bar `arrowsize` wide. The thumb is a picture: a THUMB_REST pill, wider
    # and lighter under the pointer or while dragged. (controls.AutoScrollbar shows it only when the
    # list is longer than its window.)
    for name, trough in ((SLIM_SCROLLBAR, BG), (CARD_SCROLLBAR, SURFACE)):
        style.layout(name, [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Slim.Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure(name, troughcolor=trough, bordercolor=trough, lightcolor=trough, darkcolor=trough,
                        gripcount=0, arrowsize=max(4, px(SCROLLBAR_WIDTH)))
    style.layout(SLIM_HSCROLLBAR, [("Horizontal.Scrollbar.trough", {"sticky": "we", "children": [
        ("Slim.Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure(SLIM_HSCROLLBAR, troughcolor=BG, bordercolor=BG, lightcolor=BG, darkcolor=BG, gripcount=0,
                    arrowsize=max(4, px(SCROLLBAR_WIDTH)))

    for name, field in (("TEntry", "Round.field"), (CARD_ENTRY, "Card.field")):
        style.layout(name, [(field, {"sticky": "nswe", "children": [
            ("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})])
        style.configure(name, foreground=TEXT, insertcolor=TEXT, padding=(px(FIELD_INSET), pad_y))
        style.map(name, foreground=[("disabled", MUTED)])
    style.configure("Various.TEntry", foreground=MUTED)  # (0.16.0) Edit tags: a "(various)" field
    # (0.20.0) The track list's search box: room inside for the magnifier (left) and the × (right).
    style.configure(SEARCH_ENTRY, padding=(px(SEARCH_ICON_ROOM), pad_y, px(SEARCH_CLEAR_ROOM), pad_y))

    # Lists are frameless, on the window's own colour: a heading line under small muted headings,
    # the row under the pointer HOVER (a tag, full width: Tk 8.6 rows have no hover state), and the
    # selected row a rounded SELECT plate (Round.selection, drawn over the row's own background).
    row_height = int(tkfont.nametofont("TkDefaultFont").metrics("linespace") * 1.5)
    style.configure("Treeview", background=BG, fieldbackground=BG, foreground=TEXT, bordercolor=BG,
                    lightcolor=BG, darkcolor=BG, rowheight=row_height)
    style.map("Treeview", background=[("selected", BG)], foreground=[("selected", ON_ACCENT)])
    style.layout("Treeview.Row", [("Treeitem.row", {"sticky": "nswe"}), ("Round.selection", {"sticky": "nswe"})])
    style.layout("Treeview.Heading", [("Round.heading", {"sticky": "nswe"}), ("Treeheading.padding", {
        "sticky": "nswe", "children": [("Treeheading.image", {"side": "right", "sticky": ""}),
                                       ("Treeheading.text", {"sticky": "we"})]})])
    style.configure("Treeview.Heading", background=BG, foreground=MUTED, font=font(SIZE_SMALL), relief="flat",
                    padding=(px(GAP_SMALL), px(GAP_SMALL), px(GAP_SMALL), px(GAP)))
    style.map("Treeview.Heading", foreground=[("active", TEXT)])
    # (0.20.0) No dotted focus box round a list row: a row is just its text (some Tk builds' themes
    # add a Treeitem.focus element here; the selected row's colour already says where you are).
    style.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
        ("Treeitem.indicator", {"side": "left", "sticky": ""}), ("Treeitem.image", {"side": "left", "sticky": ""}),
        ("Treeitem.text", {"sticky": "nswe"})]})])

    # Pop-over panels (settings, home menu) sit on the raised surface colour.
    style.configure("Pop.TFrame", background=SURFACE)
    style.configure("Pop.TLabel", background=SURFACE, foreground=TEXT)
    style.configure("PopMuted.TLabel", background=SURFACE, foreground=MUTED)
    style.configure("PopCaption.TLabel", background=SURFACE, foreground=MUTED, font=font(SIZE_SMALL, "semibold"))
    style.configure("PopTitle.TLabel", background=SURFACE, foreground=TEXT, font=font(SIZE_HEADING, "semibold"))
    style.configure("Pop.TCheckbutton", background=SURFACE, foreground=TEXT,
                    indicatorbackground=BG, indicatorforeground=ON_ACCENT)
    style.map("Pop.TCheckbutton", background=[("active", SURFACE)],
              indicatorbackground=[("selected", ACCENT), ("active", SURFACE_HI)])
    # (0.21.0) No ttk button or scale styles for pop-overs any more: Settings' buttons are drawn pills
    # (controls.PillButton) and its volume the drawn slider (controls.Slider).

    style.configure("Title.TLabel", font=font(SIZE_TITLE, "semibold"))
    # The main window's now-playing title, and the line under it (artist — album).
    style.configure("Display.TLabel", font=font(SIZE_DISPLAY, "semibold"))
    style.configure("Subtitle.TLabel", foreground=MUTED, font=font(SIZE_LARGE))
    # (0.22.0) Section headings in every window as in Settings (PopCaption): small bold capitals in
    # the secondary text colour, with a faint line above each one but the first (controls.section).
    # (They were DG green until 0.21.0; muted, the green and amber *values* stand out.)
    style.configure("Section.TLabel", foreground=MUTED, font=font(SIZE_SMALL, "semibold"))
    # (0.22.0) Live numbers: digits all one width (numbers()), plain / green / amber.
    style.configure("Number.TLabel", font=numbers(SIZE_BODY))
    style.configure("GoodNumber.TLabel", foreground=GOOD, font=numbers(SIZE_BODY))
    style.configure("WarnNumber.TLabel", foreground=WARN, font=numbers(SIZE_BODY))
    style.configure("GoodValue.TLabel", foreground=GOOD)
    style.configure("WarnValue.TLabel", foreground=WARN)
    style.configure("Sub.TLabel", foreground=MUTED)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("UpNext.TLabel", foreground=ACCENT_TEXT, font=font(SIZE_BODY))
    style.configure("Info.TLabel", foreground=MUTED, font=font(SIZE_SMALL))
    style.configure("Good.TLabel", foreground=GOOD, font=font(SIZE_SMALL))
    style.configure("Warn.TLabel", foreground=WARN, font=font(SIZE_SMALL))


def dark_title_bar(window: tk.Misc) -> None:
    """Ask Windows 10/11 for a title bar that matches the palette (dark for a dark palette).
    Harmless no-op elsewhere."""
    if sys.platform != "win32":
        return
    import ctypes

    window.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
    value = ctypes.c_int(1 if current.dark else 0)
    for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (newer builds, then older)
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                      ctypes.sizeof(value)) == 0:
            break


# -- switching palettes --------------------------------------------------------------------
_hooks: list = []   # callables returning the on_change callback (or None once it's gone)
_hooks_kept = 0     # how many hooks were alive at the last sweep (see on_change)


def on_change(callback: Callable[[Palette], None]) -> Callable[[Palette], None]:
    """Call `callback(new_palette)` after every palette change — for what `use` can't re-colour
    in place (Pillow pictures, Treeview tags, colours a window keeps between frames). A bound
    method is held weakly (it lives as long as its window); a callback that raises TclError
    (its window is gone) is dropped. Returns the callback."""
    global _hooks_kept
    # Widgets made and dropped all day (a menu per right-click) would otherwise leave their dead
    # hooks here until the next palette change: sweep them out whenever the list has doubled.
    if len(_hooks) >= 2 * _hooks_kept + 64:
        _hooks[:] = [ref for ref in _hooks if ref() is not None]
        _hooks_kept = len(_hooks)
    if hasattr(callback, "__self__"):
        _hooks.append(weakref.WeakMethod(callback))
    else:
        _hooks.append(lambda: callback)
    return callback


def _drop_hook(ref):
    try:
        _hooks.remove(ref)
    except ValueError:   # (already swept out: a hook added during a palette change sweeps too)
        pass


# Tk widget options that hold a colour.
COLOUR_OPTIONS = ("background", "foreground", "activebackground", "activeforeground", "highlightbackground",
                  "highlightcolor", "insertbackground", "selectbackground", "selectforeground",
                  "disabledforeground", "selectcolor", "troughcolor", "readonlybackground", "disabledbackground")
CANVAS_COLOUR_OPTIONS = ("fill", "outline", "activefill", "activeoutline", "disabledfill", "disabledoutline")
TEXT_TAG_OPTIONS = ("foreground", "background", "selectforeground", "selectbackground")


def recolour(widget: tk.Misc, old: Palette, new: Palette) -> int:
    """Give `widget` and everything inside it the new palette's colours, role for role: an
    option still showing an old role's colour gets that role's new colour. Covers plain Tk
    widgets (and menus), canvas items and Text tags; ttk widgets follow their styles (`apply`).
    Every role has its own colour in a palette (a test checks), so the role is never in doubt.
    Returns how many options changed."""
    swap = {value.lower(): getattr(new, role) for role, value in old.roles().items()}
    changed = 0

    def swapped(value) -> str | None:
        value = str(value).lower()
        return swap[value] if value in swap and swap[value].lower() != value else None

    stack = [widget]
    while stack:
        w = stack.pop()
        try:
            stack.extend(w.winfo_children())
            options = w.configure() or {}
        except tk.TclError:
            continue
        for option in COLOUR_OPTIONS:
            if option in options:
                new_value = swapped(w.cget(option))
                if new_value:
                    w.configure(**{option: new_value})
                    changed += 1
        if isinstance(w, tk.Canvas):
            for item in w.find_all():
                item_options = w.itemconfigure(item)
                for option in CANVAS_COLOUR_OPTIONS:
                    if option in item_options:
                        new_value = swapped(w.itemcget(item, option))
                        if new_value:
                            w.itemconfigure(item, **{option: new_value})
                            changed += 1
        elif isinstance(w, tk.Text):
            for tag in w.tag_names():
                for option in TEXT_TAG_OPTIONS:
                    new_value = swapped(w.tag_cget(tag, option))
                    if new_value:
                        w.tag_configure(tag, **{option: new_value})
                        changed += 1
    return changed


def _recolour_drop_down(box: ttk.Combobox) -> None:
    """A combobox's drop-down list is a Tk listbox Python has no object for (`apply` only sets
    the colours new ones get): re-colour this one's through Tcl."""
    try:
        popdown = box.tk.eval(f"ttk::combobox::PopdownWindow {box}")
        box.tk.call(f"{popdown}.f.l", "configure", "-background", SURFACE, "-foreground", TEXT,
                    "-selectbackground", SELECT, "-selectforeground", ON_ACCENT)
    except tk.TclError:
        pass


def use(palette: Palette, root: tk.Misc | None = None) -> None:
    """Make `palette` the current one: theme.BG etc. change; with `root`, the ttk styles are
    re-applied, every window under it re-coloured and the title bars matched; then the
    on_change hooks run."""
    global current
    old, current = current, palette
    globals().update(palette.roles())
    if root is not None:
        apply(root)
        recolour(root, old, palette)
        windows = [root]
        stack = list(root.winfo_children())
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if isinstance(w, tk.Toplevel):
                windows.append(w)
            elif isinstance(w, ttk.Combobox):
                _recolour_drop_down(w)
        for window in windows:
            try:
                dark_title_bar(window)
            except (tk.TclError, OSError, AttributeError):
                pass
    for ref in list(_hooks):
        callback = ref()
        if callback is None:
            _drop_hook(ref)
            continue
        try:
            callback(palette)
        except tk.TclError:
            _drop_hook(ref)

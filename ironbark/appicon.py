"""The app icon (0.23.0): drawn here in code, saved as picture files in `assets/`, and put on
every window.

The design: **a dark record on a DG-green rounded tile, with a green centre label** — "music" at a
glance, in the play button's own green and dark ink. It uses no letters, so it doesn't depend on
the product's name (still parked). At 16 px it is a green square with a dark ring and a green dot;
the spindle hole joins from 32 px and two faint grooves from 48 px. (A first sketch — a green disc
with three dark bars — was dropped: a green circle with dark marks is too close to another music
app's well-known icon.)

**Growth Rings (0.28.0):** from 128 px the grooves
become an ironbark log's growth rings — seven slightly uneven rings, unevenly spaced (good and dry
years), the pith a touch off-centre — and a timber drying crack runs in from the edge. The Ironbark
logo is drawn from the same `growth_rings` / `crack`, so the app icon and the logo always match.
16–64 px are unchanged: the detail can't be seen there.

Why drawn in code: the icon can be rebuilt exactly (`python -m ironbark.appicon` rewrites the
files in `ironbark/assets/`), and each size is drawn for itself (8x bigger, then shrunk), with the
fine details only where they can be seen, rather than one big picture shrunk to a blur.

Files (all in the package, so a wheel / the PyInstaller packages carry them):
- `icon_<n>.png` for every size in SIZES — Tk's `iconphoto` uses these (title bar, taskbar, Alt+Tab).
- `ironbark.ico` — one Windows icon file holding every size, kept ready for the PyInstaller
  packages (`--icon`), which need an .ico for the program file itself.

To swap the icon: change `draw()` (or replace the files by hand) and run `python -m ironbark.appicon`.
"""

from __future__ import annotations

import io
import logging
import math
import sys
import tkinter as tk
from importlib import resources
from pathlib import Path

from PIL import Image, ImageDraw, ImageTk

from . import theme

log = logging.getLogger(__name__)

# The sizes Windows asks for: 16 / 20 / 24 / 32 / 40 / 48 (small and large icons at 100–250%
# display scaling), 64, 128 and 256 (Explorer's bigger views). All go into the .ico.
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ICO_NAME = "ironbark.ico"
SUPERSAMPLE = 8            # drawn this many times bigger, then shrunk (smooth round edges)

# Shapes as shares of the icon's size.
TILE_RADIUS = 0.22         # the green tile's rounded corners
DISC = 0.40                # the record's radius
LABEL = 0.38               # the record's centre label, a share of the record's radius
HOLE = 0.16                # the spindle hole, a share of the label's radius (from HOLE_FROM px up)
HOLE_FROM = 32
GROOVES = (0.62, 0.84)     # groove rings, shares of the record's radius (from GROOVE_FROM px up)
GROOVE_FROM = 48
GROOVE_WIDTH = 1 / 96      # a share of the size
GROOVE_SHADE = 0.30        # a groove: this far from the record's dark ink toward the green

# Growth Rings (0.28.0), from RINGS_FROM px up, in place of the two plain grooves.
RINGS_FROM = 128
RINGS = (0.50, 0.565, 0.62, 0.735, 0.79, 0.85, 0.915)   # shares of the record's radius
RING_WIDTH = 0.018         # a share of the record's radius
RING_SHADE = 0.55          # brighter than a groove: these sizes are big enough to show them
RING_PHASES = (2.03, 0.95, 4.09)    # the rings' wobble (radians); every ring echoes it, as real ones do
CRACK_ANGLE = -38          # degrees (0 = right, clockwise): the drying crack, upper right

# Windows: the taskbar groups windows by an "AppUserModelID". Without one of its own the player is
# grouped (and pinned) as python.exe; with one, Windows uses the window's own icon on the taskbar.
APP_ID = "DigitalGreenhouse.Ironbark"


def _colours() -> tuple[str, str, str]:
    """(tile and label, record, groove). Always the Dark palette's: the icon is one fixed picture
    in both themes (DG green and the play button's dark ink are the same in Light anyway)."""
    green, ink = theme.DARK.ACCENT, theme.DARK.ACCENT_INK
    return green, ink, theme.mix(ink, green, GROOVE_SHADE)


def growth_rings(cx: float, cy: float, radius: float, shares=RINGS, points: int = 96):
    """The growth rings of a record of `radius` centred on (cx, cy): one closed list of (x, y) per
    ring (first point not repeated). Each ring wobbles a little (three slow waves sharing
    RING_PHASES, drifting a touch ring to ring) and the pith sits off-centre, more so further out,
    so the rings bunch on one side like a real log's."""
    rings = []
    for i, share in enumerate(shares):
        r0 = share * radius
        amp = (0.010 + 0.012 * share) * radius
        drift = [p + 0.2 * math.sin(1.7 * i + k) for k, p in enumerate(RING_PHASES)]
        ox, oy = cx - 0.03 * radius * share, cy + 0.022 * radius * share
        ring = []
        for j in range(points):
            t = 2 * math.pi * j / points
            r = r0 + amp * (0.65 * math.sin(2 * t + drift[0]) + 0.35 * math.sin(3 * t + drift[1])
                            + 0.18 * math.sin(7 * t + drift[2] + i))
            ring.append((ox + r * math.cos(t), oy + r * math.sin(t)))
        rings.append(ring)
    return rings


def crack(cx: float, cy: float, radius: float, angle: float = CRACK_ANGLE):
    """The drying crack: a thin, slightly crooked wedge from just inside the record's edge to a
    point a little past half way in, as a closed list of (x, y). Filled in the record's colour, it
    breaks the rings."""
    a = math.radians(angle)
    ux, uy, nx, ny = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
    spine = ((0.985, 0.030, 0.0), (0.86, 0.020, 0.010), (0.74, 0.013, -0.006), (0.64, 0.007, 0.004))
    left, right = [], []
    for along, half, kink in spine:     # shares of the radius
        px, py = cx + (ux * along + nx * kink) * radius, cy + (uy * along + ny * kink) * radius
        left.append((px + nx * half * radius, py + ny * half * radius))
        right.append((px - nx * half * radius, py - ny * half * radius))
    return left + [(cx + ux * 0.56 * radius, cy + uy * 0.56 * radius)] + right[::-1]


def draw(size: int) -> Image.Image:
    """The icon at `size` x `size` pixels (RGBA; transparent outside the tile's rounded corners)."""
    green, ink, groove = _colours()
    k = SUPERSAMPLE
    big = size * k
    img = Image.new("RGBA", (big, big))
    pen = ImageDraw.Draw(img)
    pen.rounded_rectangle((0, 0, big - 1, big - 1), radius=TILE_RADIUS * big, fill=green)
    c = big / 2

    def circle(radius, **style):
        pen.ellipse((c - radius, c - radius, c + radius, c + radius), **style)

    record = DISC * big
    circle(record, fill=ink)
    if size >= RINGS_FROM:
        ring_colour = theme.mix(ink, green, RING_SHADE)
        for ring in growth_rings(c, c, record, points=240):
            pen.line(ring + ring[:1], fill=ring_colour, width=round(RING_WIDTH * record), joint="curve")
        pen.polygon(crack(c, c, record), fill=ink)
    elif size >= GROOVE_FROM:
        for share in GROOVES:
            circle(record * share, outline=groove, width=max(k, round(GROOVE_WIDTH * big)))
    label = record * LABEL
    circle(label, fill=green)
    if size >= HOLE_FROM:
        circle(max(k / 2, label * HOLE), fill=ink)
    return img.resize((size, size), Image.LANCZOS)


# -- the files -------------------------------------------------------------------------------
def assets() -> Path:
    """Where the icon files live inside the package (a real folder for an ordinary install)."""
    return Path(str(resources.files(__package__) / "assets"))


def png_name(size: int) -> str:
    return f"icon_{size}.png"


def write_assets(folder: Path | None = None) -> list[Path]:
    """Draw every size and save the PNGs + the multi-size .ico. Returns the files written."""
    folder = Path(folder) if folder is not None else assets()
    folder.mkdir(parents=True, exist_ok=True)
    pictures = {size: draw(size) for size in SIZES}
    written = []
    for size, picture in pictures.items():
        path = folder / png_name(size)
        picture.save(path, optimize=True)
        written.append(path)
    # Pillow's ICO writer takes each size from the pictures given (not a shrink of the biggest)
    # when one of exactly that size is supplied.
    biggest = pictures[max(SIZES)]
    ico = folder / ICO_NAME
    biggest.save(ico, format="ICO", sizes=[(s, s) for s in SIZES],
                 append_images=[pictures[s] for s in SIZES if s != max(SIZES)])
    written.append(ico)
    return written


def load(size: int) -> Image.Image:
    """One size, read from the package's files."""
    data = (resources.files(__package__) / "assets" / png_name(size)).read_bytes()
    return Image.open(io.BytesIO(data)).convert("RGBA")


# -- on the windows ---------------------------------------------------------------------------
# Handed to Tk's `wm iconphoto`: Windows picks the small (title bar / taskbar at 100–150%) and large
# (Alt+Tab) icons from these. 256 is left out: Tk turns every picture into an icon in memory.
WINDOW_SIZES = (16, 20, 24, 32, 40, 48, 64, 128)
DOCK_SIZES = (max(SIZES),)   # a Mac shows the icon only in the Dock, and big


def apply(root: tk.Tk) -> bool:
    """Put the icon on `root` and, as the default, on every window made after it (Tk's
    `iconphoto -default`: every Toplevel — Now Playing, Meters, Library … — takes it).
    Never raises: a missing file or an old Tk just leaves Tk's own icon. True when set.

    On a Mac windows have no icon of their own; `iconphoto` replaces the Dock icon instead. The
    packaged app's .icns is sharper than any of these pictures, so it's left alone there; run from
    source (no app bundle) the Dock gets the largest picture."""
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        return True
    sizes = DOCK_SIZES if sys.platform == "darwin" else WINDOW_SIZES
    try:
        photos = [ImageTk.PhotoImage(load(size), master=root) for size in sizes]
        root.iconphoto(True, *photos)
    except (OSError, tk.TclError, ValueError) as error:
        log.warning("couldn't set the window icon: %s", error)
        return False
    root._dg_icon_images = photos     # Tk needs the pictures kept alive
    _sharp_on_windows(root)
    return True


# Measured 05/10/2026 (Tk 8.6.12, Windows 11, display at 150%): Tk's `iconphoto -default` sets the
# icon of its window *class* (so every Toplevel shares it), but always picks the 16 and 32 px pictures
# — the sizes for 100% scaling — whatever the display; at 150% Windows wants 24 and 48 and would
# stretch the 16 / 32 ones. So on Windows the class icons are then swapped for the .ico's own
# pictures at the sizes Windows asks for (GetSystemMetrics). Toplevels made later keep them (checked).
_class_icons: dict[int, int] = {}     # size → HICON, loaded once per process and kept


def _sharp_on_windows(root: tk.Tk) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ico = assets() / ICO_NAME
        if not ico.is_file():
            return
        user32 = ctypes.WinDLL("user32")       # private handle (argtypes never leak to windll)
        user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_uint]
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        user32.SetClassLongPtrW.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        root.update_idletasks()
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()   # Tk's wrapper window
        for which, metric in ((GCLP_HICON, SM_CXICON), (GCLP_HICONSM, SM_CXSMICON)):
            size = user32.GetSystemMetrics(metric)
            if size not in _class_icons:
                _class_icons[size] = user32.LoadImageW(None, str(ico), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if _class_icons[size]:
                user32.SetClassLongPtrW(hwnd, which, _class_icons[size])
    except (AttributeError, OSError, tk.TclError) as error:
        log.warning("couldn't set the sharp window icons: %s", error)


GCLP_HICON, GCLP_HICONSM = -14, -34       # Win32 constants (winuser.h)
SM_CXICON, SM_CXSMICON = 11, 49
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10


def set_app_id(app_id: str = APP_ID) -> bool:
    """Windows only: give the process its own AppUserModelID, before any window is shown, so the
    taskbar groups the player as itself (with its own icon) rather than as python.exe. Windows'
    media controls (SMTC) take the same ID, so they and the taskbar agree. True when set."""
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        shell32 = ctypes.WinDLL("shell32")      # a private handle: never touch windll's argtypes
        set_id = shell32.SetCurrentProcessExplicitAppUserModelID
        set_id.argtypes = [ctypes.c_wchar_p]
        set_id.restype = ctypes.c_long
        return set_id(app_id) == 0
    except (AttributeError, OSError) as error:
        log.warning("couldn't set the AppUserModelID: %s", error)
        return False


def app_id() -> str | None:
    """The process's AppUserModelID as Windows has it now (None: none set, or not Windows)."""
    if sys.platform != "win32":
        return None
    import ctypes

    try:
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        get_id = shell32.GetCurrentProcessExplicitAppUserModelID
        get_id.argtypes = [ctypes.POINTER(ctypes.c_wchar_p)]
        get_id.restype = ctypes.c_long
        out = ctypes.c_wchar_p()
        if get_id(ctypes.byref(out)) != 0:
            return None
        value = out.value
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))
        return value
    except (AttributeError, OSError):
        return None


if __name__ == "__main__":
    for written in write_assets():
        print("wrote", written)

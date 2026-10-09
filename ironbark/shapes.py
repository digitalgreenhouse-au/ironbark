"""Drawn shapes shared by the tab strip and the controls, with Pillow only: rounded plates and
rings cut into end pieces (the middle is a plain canvas rectangle, so nothing is drawn per width),
the selected tab's two ends, discs and dots, the playing tab's level mark, and the previous / next
glyphs (drawn rather than taken from an icon font, so they look the same on every PC and the Mac).

Everything is drawn at SUPERSAMPLE× and scaled down with LANCZOS, so curves are smooth (a Tk canvas
draws jagged circles on Windows). Colours are read from theme.py when a picture is drawn and are part
of its cache key, so a palette change simply asks for new pictures. Pictures with no background of
their own are see-through (RGBA): Tk blends them over whatever is under them on a canvas or label.

Pictures are cached per Tk root (`photo`): a Tk PhotoImage belongs to the root it was made in and
can't be used in another.
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import Callable

from PIL import Image, ImageDraw, ImageTk

from . import theme

SUPERSAMPLE = 4
CACHE_LIMIT = 200   # pictures kept per root; past this the cache starts afresh (widgets keep their own)
CLEAR = (0, 0, 0, 0)   # a see-through pixel


# -- the cache -------------------------------------------------------------------------------------
def cache(widget: tk.Misc) -> dict:
    """Pictures made for this widget's Tk root (one dict per root, never shared between roots)."""
    root = widget._root()   # Tk's own method: the interpreter's root window
    images = getattr(root, "_dg_control_images", None)
    if images is None:
        images = root._dg_control_images = {}
    return images


def photo(widget: tk.Misc, key: tuple, draw: Callable[[], Image.Image]) -> ImageTk.PhotoImage:
    """A cached PhotoImage for `key`, drawn by `draw()` the first time. The key must hold every size
    and colour the picture depends on."""
    images = cache(widget)
    if key not in images:
        if len(images) > CACHE_LIMIT:   # e.g. a badge re-sized many times: start afresh
            images.clear()
        images[key] = ImageTk.PhotoImage(draw(), master=widget)
    return images[key]


def down(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale a supersampled drawing down to its real size (Pillow blends see-through edges properly)."""
    return image.resize(size, Image.Resampling.LANCZOS)


def _canvas(width: int, height: int, bg: str | None) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """A supersampled drawing surface: see-through, or filled with `bg` (a role or colour)."""
    k = SUPERSAMPLE
    image = Image.new("RGBA", (width * k, height * k), CLEAR) if bg is None else \
        Image.new("RGB", (width * k, height * k), theme.colour(bg))
    return image, ImageDraw.Draw(image)


def _arc(cx: float, cy: float, r: float, start: float, end: float, steps: int = 28) -> list[tuple[float, float]]:
    return [(cx + r * math.cos(math.radians(start + (end - start) * i / steps)),
             cy + r * math.sin(math.radians(start + (end - start) * i / steps))) for i in range(steps + 1)]


# -- pictures (PIL) -----------------------------------------------------------------------------------
def rounded(width: int, height: int, radius: int, fill: str | None = None, outline: str | None = None,
            line: int = 0, bg: str | None = None) -> Image.Image:
    """A rounded rectangle `width` × `height` real px: filled, ringed (`line` px), or both."""
    image, d = _canvas(width, height, bg)
    k = SUPERSAMPLE
    d.rounded_rectangle((0, 0, width * k - 1, height * k - 1), radius=max(0, radius * k),
                        fill=theme.colour(fill) if fill else None,
                        outline=theme.colour(outline) if outline else None, width=line * k if outline else 0)
    return down(image, (width, height))


def ends(height: int, radius: int, fill: str | None = None, outline: str | None = None,
         line: int = 0) -> tuple[Image.Image, Image.Image]:
    """The left and right ends (each `radius` px wide) of a rounded plate or ring `height` px tall,
    for drawing it at any width: the ends as pictures, the straight middle as canvas rectangles."""
    radius = max(1, radius)
    whole = rounded(2 * radius + 2, height, radius, fill, outline, line)
    return whole.crop((0, 0, radius, height)), whole.crop((whole.width - radius, 0, whole.width, height))


def tab_ends(radius: int, flare: int, height: int, line: int, body: str, rim: str) -> tuple[Image.Image, Image.Image]:
    """The selected tab's two ends, `height` px tall (from the tab's top to the strip's bottom):
    a rounded top corner (`radius`), a small inward curve at the foot (`flare`) where the tab flows
    into the page, and a `line` px rim that runs down into the strip's bottom line. The tab's body
    fills to the bottom, hiding that line under the tab. Each end is flare + radius + line wide; the
    body's own edge is `flare` px in from the outside of each end."""
    cap = flare + radius + line
    width = 2 * cap + 2
    k = SUPERSAMPLE
    image, d = _canvas(width, height, None)
    half = line / 2                      # lines are drawn on their pixels' centres, so they stay crisp
    x0, x1 = flare + half, width - flare - half
    top, foot = half, height - half
    outline = _arc(x0 - flare, foot - flare, flare, 90, 0)          # the left flare, up into the side
    outline += _arc(x0 + radius, top + radius, radius, 180, 270)    # the top-left corner
    outline += _arc(x1 - radius, top + radius, radius, 270, 360)    # the top-right corner
    outline += _arc(x1 + flare, foot - flare, flare, 180, 90)       # the right flare, down to the line
    scaled = [(x * k, y * k) for x, y in outline]
    d.polygon(scaled + [((x1 + flare) * k, height * k), ((x0 - flare) * k, height * k)], fill=theme.colour(body))
    d.line(scaled, fill=theme.colour(rim), width=max(1, round(line * k)), joint="curve")
    whole = down(image, (width, height))
    return whole.crop((0, 0, cap, height)), whole.crop((width - cap, 0, width, height))


def disc(diameter: int, colour: str, bg: str | None = None) -> Image.Image:
    """A filled circle, see-through around it (or on `bg`)."""
    image, d = _canvas(diameter, diameter, bg)
    k = SUPERSAMPLE
    d.ellipse((0, 0, diameter * k - 1, diameter * k - 1), fill=theme.colour(colour))
    return down(image, (diameter, diameter))


def level_mark(colour: str) -> Image.Image:
    """The playing tab's mark: three level bars, 2 px wide and 1.5 apart, 6 / 10 / 7 px tall at
    100% scaling, standing on one line (the picture's bottom edge)."""
    s = theme.scale()
    width, height = theme.px(theme.MARK_W) + 1, theme.px(10) + 1
    image, d = _canvas(width, height, None)
    k = SUPERSAMPLE
    bar, gap = 2 * s * k, 1.5 * s * k
    for i, tall in enumerate((6, 10, 7)):
        x = i * (bar + gap)
        d.rounded_rectangle((x, (height - tall * s) * k, x + bar, height * k - 1), radius=bar / 2,
                            fill=theme.colour(colour))
    return down(image, (width, height))


def skip(size: int, colour: str, forward: bool, image: Image.Image | None = None,
         centre: tuple[float, float] | None = None) -> Image.Image:
    """Previous / next, drawn filled: a bar and a triangle `size` px tall, pointing forward (next)
    or back. Drawn into `image` (a supersampled drawing, around `centre` in real px) when given,
    else on its own see-through picture."""
    k = SUPERSAMPLE
    own = image is None
    if own:
        image, _ = _canvas(size, size, None)
        centre = (size / 2, size / 2)
    d = ImageDraw.Draw(image)
    cx, cy = centre[0] * k, centre[1] * k
    h, bar, ink = size * k, size * k * 0.17, theme.colour(colour)
    if forward:
        right = cx + h * 0.5
        d.rounded_rectangle((right - bar, cy - h / 2, right, cy + h / 2), radius=bar / 2, fill=ink)
        tip = right - bar - h * 0.05
        d.polygon([(tip, cy), (cx - h * 0.5, cy - h / 2), (cx - h * 0.5, cy + h / 2)], fill=ink)
    else:
        left = cx - h * 0.5
        d.rounded_rectangle((left, cy - h / 2, left + bar, cy + h / 2), radius=bar / 2, fill=ink)
        tip = left + bar + h * 0.05
        d.polygon([(tip, cy), (cx + h * 0.5, cy - h / 2), (cx + h * 0.5, cy + h / 2)], fill=ink)
    return down(image, (size, size)) if own else image


def spacer(width: int, height: int) -> Image.Image:
    """A see-through picture that only takes up room (a label's text is centred on it)."""
    return Image.new("RGBA", (max(1, width), max(1, height)), CLEAR)


def chevron(width: int, height: int, colour: str, direction: str = "down", line: float | None = None,
            bg: str | None = None) -> Image.Image:
    """A thin chevron ("down", "right" or "left") centred in `width` × `height`, drawn rather than taken
    from an icon font (Windows 10's icon font may lack these glyphs, and the Mac has neither)."""
    image, d = _canvas(width, height, bg)
    k = SUPERSAMPLE
    s = theme.scale()
    line = line if line is not None else max(1.0, 1.3 * s)
    cx, cy, arm = width / 2 * k, height / 2 * k, 3.6 * s * k
    if direction == "down":
        points = [(cx - arm, cy - arm / 2), (cx, cy + arm / 2), (cx + arm, cy - arm / 2)]
    elif direction == "right":
        points = [(cx - arm / 2, cy - arm), (cx + arm / 2, cy), (cx - arm / 2, cy + arm)]
    else:
        points = [(cx + arm / 2, cy - arm), (cx - arm / 2, cy), (cx + arm / 2, cy + arm)]
    d.line(points, fill=theme.colour(colour), width=max(1, round(line * k)), joint="curve")
    return down(image, (width, height))


def corners(radius: int, fill: str, bg: str) -> tuple[Image.Image, ...]:
    """The four corners (top-left, top-right, bottom-left, bottom-right) of a rounded `fill` plate on
    `bg`, each `radius` px square: laid over a square plate's corners, they round it."""
    radius = max(1, radius)
    whole = rounded(2 * radius + 2, 2 * radius + 2, radius, fill, bg=bg)
    w = whole.width
    return (whole.crop((0, 0, radius, radius)), whole.crop((w - radius, 0, w, radius)),
            whole.crop((0, w - radius, radius, w)), whole.crop((w - radius, w - radius, w, w)))


def ring(width: int, height: int, radius: int, colour: str, line: int) -> Image.Image:
    """A see-through rounded ring `line` px wide just inside `width` × `height`: the focus ring."""
    return rounded(width, height, min(radius, height // 2, width // 2), outline=colour, line=line)


# -- ttk image elements -------------------------------------------------------------------------------
def at_least(size: tuple[int, int], border: tuple[int, int, int, int]) -> tuple[int, int]:
    """`size` grown, if need be, to its 9-slice borders plus 2 px each way: Tk hangs drawing an image
    element whose picture is smaller than its borders."""
    left, top, right, bottom = border
    return max(size[0], left + right + 2), max(size[1], top + bottom + 2)


def widened(picture: Image.Image, border: tuple[int, int, int, int], width: int, height: int) -> Image.Image:
    """A 9-slice picture made at least `width` × `height` by stretching its plain middle (the corners
    stay as drawn). Ttk repeats an image element's middle to fill it, one copy per Tk draw call, so a
    middle a few pixels across costs thousands of calls on a wide list row or heading; a wide middle,
    a handful. (The element is still asked for at its natural size: see theme._elements.)"""
    left, top, right, bottom = border
    w, h = picture.size
    new_w, new_h = max(w, width), max(h, height)
    if (new_w, new_h) == (w, h):
        return picture
    xs = ((0, left, 0, left), (left, w - right, left, new_w - right), (w - right, w, new_w - right, new_w))
    ys = ((0, top, 0, top), (top, h - bottom, top, new_h - bottom), (h - bottom, h, new_h - bottom, new_h))
    out = Image.new(picture.mode, (new_w, new_h))
    for x0, x1, nx0, nx1 in xs:
        for y0, y1, ny0, ny1 in ys:
            if x1 > x0 and y1 > y0 and nx1 > nx0 and ny1 > ny0:
                piece = picture.crop((x0, y0, x1, y1))
                if piece.size != (nx1 - nx0, ny1 - ny0):
                    piece = piece.resize((nx1 - nx0, ny1 - ny0), Image.Resampling.NEAREST)
                out.paste(piece, (nx0, ny0))
    return out


def element_pictures() -> dict[str, tuple[Image.Image, tuple[int, int, int, int]]]:
    """Every picture the ttk styles draw with (theme._elements makes them image elements), by name:
    (picture, its 9-slice border — left, top, right, bottom). The border's corners stay as drawn and
    the middle is repeated to fill, so the middle of every picture is plain.

    - "field BG" / "field SURFACE" (+ " focus", " off"): a rounded field on the window or on a card,
      filled one step "into" it, with a 1 px EDGE rim (a FOCUS_RING DG-green ring when focused).
    - "chevron" (+ " hot", " off"): a drop-down's chevron, see-through.
    - "heading": a list heading's background, with a 1 px BORDER line along its foot.
    - "selection" / "clear": the selected row's rounded SELECT plate, and nothing (other rows).
    - "thumb v" / "thumb h" (+ " hot"): the slim scrollbar's thumb, a THUMB pill (MUTED and wider
      under the pointer), see-through on the trough."""
    px = theme.px
    line = max(1, px(1))
    out: dict[str, tuple[Image.Image, tuple[int, int, int, int]]] = {}

    radius = px(theme.RADIUS_M)
    focus = max(line, px(theme.FOCUS_RING))
    b = radius + 1
    border = (b, b, b, b)
    size = at_least((2 * b + 4, 2 * b + 4), border)
    for where, fill in (("BG", "SURFACE"), ("SURFACE", "BG")):
        out[f"field {where}"] = rounded(*size, radius, fill, "EDGE", line, bg=where), border
        out[f"field {where} focus"] = rounded(*size, radius, fill, "ACCENT", focus, bg=where), border
        out[f"field {where} off"] = rounded(*size, radius, theme.mix(fill, where, 0.5), "BORDER", line,
                                            bg=where), border

    room = (px(theme.SEARCH_CLEAR_ROOM), px(theme.CONTROL_H_SMALL))
    out["chevron"] = chevron(*room, "MUTED"), (0, 0, 0, 0)
    out["chevron hot"] = chevron(*room, "TEXT"), (0, 0, 0, 0)
    out["chevron off"] = chevron(*room, theme.mix("MUTED", "BG", 0.45)), (0, 0, 0, 0)

    border = (1, 1, 1, line + 1)
    heading = Image.new("RGB", at_least((4, line + 4), border), theme.BG)
    ImageDraw.Draw(heading).rectangle((0, heading.height - line, heading.width, heading.height), fill=theme.BORDER)
    out["heading"] = heading, border

    radius = px(theme.RADIUS_S)
    b = radius + 1
    border = (b, b, b, b)
    size = at_least((2 * b + 4, 2 * b + 4), border)
    out["selection"] = rounded(*size, radius, "SELECT"), border
    out["clear"] = Image.new("RGBA", size, CLEAR), border

    trough = max(4, px(theme.SCROLLBAR_WIDTH))
    end = (max(2, px(theme.THUMB_HOT)) + 1) // 2 + 1
    for name, wide, colour in (("thumb v", px(theme.THUMB_REST), "THUMB"),
                               ("thumb v hot", px(theme.THUMB_HOT), "MUTED")):
        wide = max(2, min(trough, wide))
        border = (0, end, 0, end)
        w, h = at_least((trough, 2 * end + 4), border)
        thumb = Image.new("RGBA", (w, h), CLEAR)
        thumb.paste(rounded(wide, h, wide // 2, colour), ((w - wide) // 2, 0))
        out[name] = thumb, border
        out[name.replace(" v", " h")] = thumb.transpose(Image.Transpose.ROTATE_90), (end, 0, end, 0)
    return out


# -- cached PhotoImages -------------------------------------------------------------------------------
def disc_photo(widget: tk.Misc, diameter: int, colour: str, bg: str | None = None) -> ImageTk.PhotoImage:
    ink, back = theme.colour(colour), (theme.colour(bg) if bg else None)
    return photo(widget, ("disc", diameter, ink, back), lambda: disc(diameter, ink, back))


def _pair(widget: tk.Misc, key: tuple, draw: Callable[[], tuple[Image.Image, Image.Image]]):
    """Two cached PhotoImages drawn together (a shape's left and right ends). The caller keeps
    them: the cache may start afresh between the two."""
    images = cache(widget)
    left, right = images.get(key + ("left",)), images.get(key + ("right",))
    if left is None or right is None:
        a, b = draw()
        left = photo(widget, key + ("left",), lambda: a)
        right = photo(widget, key + ("right",), lambda: b)
    return left, right


def ends_photos(widget: tk.Misc, height: int, radius: int, fill: str | None = None, outline: str | None = None,
                line: int = 0) -> tuple[ImageTk.PhotoImage, ImageTk.PhotoImage]:
    fill, outline = (theme.colour(fill) if fill else None), (theme.colour(outline) if outline else None)
    return _pair(widget, ("ends", height, radius, fill, outline, line),
                 lambda: ends(height, radius, fill, outline, line))


def tab_end_photos(widget: tk.Misc, radius: int, flare: int, height: int, line: int, body: str,
                   rim: str) -> tuple[ImageTk.PhotoImage, ImageTk.PhotoImage]:
    body, rim = theme.colour(body), theme.colour(rim)
    return _pair(widget, ("tab ends", radius, flare, height, line, body, rim),
                 lambda: tab_ends(radius, flare, height, line, body, rim))


def level_mark_photo(widget: tk.Misc, colour: str) -> ImageTk.PhotoImage:
    ink = theme.colour(colour)
    return photo(widget, ("level mark", theme.px(theme.MARK_W), theme.px(10), ink), lambda: level_mark(ink))


def spacer_photo(widget: tk.Misc, width: int, height: int) -> ImageTk.PhotoImage:
    return photo(widget, ("spacer", width, height), lambda: spacer(width, height))

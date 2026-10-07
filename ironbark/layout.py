"""The main window's layouts and the arithmetic behind them — no Tk here, so all of it is tested
without a window.

Four tiers, by the window's size in logical px (real px ÷ Windows' display scaling, so 1920 real px
at 150% is 1280):

- COLUMN: the narrow window — the cover above the track list, one column. Every window narrower
  than SPLIT_MIN, a 780–879 px window whose column cover would still fill its width, and the mini
  strip's full window always.
- SPLIT, SPLIT_M, SPLIT_L: two panes side by side — on the left the cover, the now-playing text, the
  seek bar and the transport, stacked and centred; on the right the track list, the full height of
  the window. The three differ only in type sizes, list rows and which columns may show.

Crossing back is held off by HYSTERESIS px, so a window dragged to and fro across a breakpoint
changes layout once, not on every pixel. Every size the window works out (cover slot, pane widths)
depends only on the window, never on the album on show, so changing album never moves anything.
"""

from __future__ import annotations

from dataclasses import dataclass

COLUMN, SPLIT, SPLIT_M, SPLIT_L = "column", "split", "split-m", "split-l"
ORDER = (COLUMN, SPLIT, SPLIT_M, SPLIT_L)
WIDE = ORDER[1:]

# -- where the tiers start (logical px) -------------------------------------------------------------
SPLIT_MIN = 780          # narrower than this: always COLUMN
SPLIT_AT = 880           # this wide or wider: always one of the split tiers
SPLIT_M_AT = 1280
SPLIT_L_AT = 1920
HYSTERESIS = 40          # a tier is left only this far below where it starts
RATIO_ENTER = 0.80       # 780–879 wide: split when the column's cover would fill under 80% of the width…
RATIO_LEAVE = 0.88       # …and back to the column only once it would fill 88% or more

# -- the column (the narrow window), as it lays itself out ------------------------------------------
COLUMN_CHROME = 253      # its height without the cover and the list: strip, now-playing text, seek bar,
                         # transport, status line and the gaps between them
MIN_LIST_HEIGHT = 190    # what the list always keeps there (as the window's own MIN_LIST_HEIGHT)
BODY_PAD_X = 24          # the window's padding, left and right together

# -- the split tiers --------------------------------------------------------------------------------
GUTTER = 24              # round the now-playing stack, and between the panes
STACK_MIN = 340          # the stack's narrowest: the transport (~220, centred) and the volume badge clear
LEFT_SHARE = 0.38        # the left pane's share of the content…
LEFT_MIN = STACK_MIN + 2 * GUTTER   # …never less than this…
LEFT_MAX = 720           # …nor more (so the cover tops out at COVER_MAX)
COVER_MAX = LEFT_MAX - 2 * GUTTER
CONTENT_MAX = 2200       # wider than this the two panes stay together, centred; the margins grow
COVER_HIDE_BELOW = 160   # a cover slot smaller than this hides the cover (a very short wide window)
SLOT_STEP = 8            # cover slots snap down to whole steps: fewer covers fetched while dragging
TEXT_GAP = 10            # between the cover and the caption
TAB_MAX = {COLUMN: 200, SPLIT: 200, SPLIT_M: 240, SPLIT_L: 240}   # a tab's widest on the strip

# Optional track-list columns, in the order they show, and the ones each tier may show. (Plays —
# how often each song has been played — is never on by itself: it shows only when ticked.)
OPTIONAL = ("artist", "album", "quality", "plays")
ALLOWED = {COLUMN: frozenset(), SPLIT: frozenset({"artist", "plays"}),
           SPLIT_M: frozenset(OPTIONAL), SPLIT_L: frozenset(OPTIONAL)}


@dataclass(frozen=True)
class Heights:
    """Heights the plan works from, in logical px: measured from the window once a tier's styles are
    on, or these defaults (Segoe UI at 100%)."""

    text: int        # the now-playing text: caption, title, artist, format line
    seek: int        # the seek bar and its times, with the gap above them
    transport: int   # the transport row, with its gaps
    chrome: int      # strip, the window's padding top and bottom, the status line and its gap
    list_head: int   # the list's header (search box, count) and the gap under it
    heading: int     # the list's column headings
    row: int         # one list row


DEFAULT_HEIGHTS = {
    SPLIT: Heights(text=86, seek=41, transport=62, chrome=79, list_head=38, heading=23, row=22),
    SPLIT_M: Heights(text=97, seek=41, transport=62, chrome=79, list_head=38, heading=23, row=26),
    SPLIT_L: Heights(text=105, seek=41, transport=62, chrome=79, list_head=38, heading=26, row=29),
}


def column_cover(width: float, height: float) -> float:
    """The cover the column would give a window this size (logical px): the full width, or what's
    left once everything else and the list's minimum have their room."""
    return min(width - BODY_PAD_X, height - COLUMN_CHROME - MIN_LIST_HEIGHT)


def choose(width: float, height: float, previous: str = COLUMN, show_art: bool = True) -> str:
    """The tier for a window `width` × `height` logical px. `previous` (the tier it's in now) gives
    the hysteresis; `show_art`: with the cover off there is no cover to compare, so a 780–879 px
    window stays a column."""
    inside = previous != COLUMN
    slack = HYSTERESIS if inside else 0
    if width >= SPLIT_AT - slack:
        split = True
    elif width >= SPLIT_MIN - slack and show_art:
        ratio = column_cover(width, height) / max(1.0, width - BODY_PAD_X)
        split = ratio < (RATIO_LEAVE if inside else RATIO_ENTER)
    else:
        split = False
    if not split:
        return COLUMN
    tier = SPLIT
    for name, start in ((SPLIT_M, SPLIT_M_AT), (SPLIT_L, SPLIT_L_AT)):
        if width >= start - (HYSTERESIS if ORDER.index(previous) >= ORDER.index(name) else 0):
            tier = name
    return tier


@dataclass(frozen=True)
class Plan:
    """A split tier's sizes, in logical px."""

    tier: str
    margin: int = 0      # extra room each side of the two panes, once the content is CONTENT_MAX
    content: int = 0     # the two panes together
    left_w: int = 0      # the now-playing pane
    stack_w: int = 0     # the stack in it (cover, text, seek bar, transport)
    slot: int = 0        # the cover's square; 0 = no cover
    right_w: int = 0     # the track list's pane
    pane_h: int = 0
    rows: int = 0        # list rows that fit


def plan(width: float, height: float, tier: str, show_art: bool = True, heights: Heights | None = None) -> Plan:
    """Pane sizes for a window `width` × `height` logical px in a split `tier`."""
    if tier == COLUMN:
        return Plan(COLUMN)
    h = heights or DEFAULT_HEIGHTS[tier]
    inner = int(width) - BODY_PAD_X
    content = min(inner, CONTENT_MAX)
    margin = max(0, (inner - content) // 2)
    pane_h = int(height) - h.chrome
    left_max = max(LEFT_MIN, min(LEFT_MAX, round(LEFT_SHARE * content)))
    stack_max = left_max - 2 * GUTTER
    room = pane_h - (h.text + TEXT_GAP + h.seek + h.transport) - 2 * GUTTER
    slot = min(stack_max, room) if show_art else 0
    slot -= slot % SLOT_STEP
    if slot < COVER_HIDE_BELOW:
        slot = 0
    stack_w = max(STACK_MIN, min(stack_max, slot or STACK_MIN))
    left_w = stack_w + 2 * GUTTER
    rows = max(0, (pane_h - h.list_head - h.heading) // max(1, h.row))
    return Plan(tier, margin, content, left_w, stack_w, slot, content - left_w, pane_h, rows)


def columns_for(tier: str, wanted, varies) -> tuple[str, ...]:
    """The optional columns a wide tier shows: those wanted (the wide-window column setting), that
    the tier has room for, and whose value isn't the same on every row of the tab."""
    allowed = ALLOWED.get(tier, frozenset())
    return tuple(c for c in OPTIONAL if c in wanted and c in allowed and c in varies)


def shown_cover(slot_real: int, source_side: int) -> tuple[int, bool]:
    """(the picture's side, matted?) in real px: a cover is never enlarged past its own pixels, so a
    smaller one is shown at its own size on a mat the size of the slot."""
    side = min(slot_real, source_side)
    return side, side < slot_real

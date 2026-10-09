"""The main window's layouts (layout.py): which tier a window size gets, with hysteresis at every
breakpoint, and the sizes worked out for the split tiers. Pure arithmetic: no display needed."""

from __future__ import annotations

import pytest

from ironbark import layout
from ironbark.layout import COLUMN, SPLIT, SPLIT_L, SPLIT_M, choose, columns_for, plan, shown_cover


@pytest.mark.parametrize("size", [(420, 1000), (520, 1000), (300, 260), (779, 300), (779, 2000)])
def test_narrow_windows_are_always_a_column(size):
    assert choose(*size) == COLUMN
    assert choose(*size, previous=COLUMN) == COLUMN


def test_780_to_879_splits_only_when_the_column_cover_would_be_small():
    assert choose(800, 900) == SPLIT                 # the column's cover would fill 0.59 of the width
    assert choose(800, 1400) == COLUMN               # tall: the column's cover fills the width
    assert choose(853, 533) == SPLIT                 # 1280x800 at 150%
    assert choose(800, 900, show_art=False) == COLUMN   # no cover to compare: a column until 880
    assert choose(880, 2000) == SPLIT and choose(880, 2000, show_art=False) == SPLIT


def test_the_ratio_rule_enters_at_80_and_leaves_at_88_percent():
    width = 830
    inner = width - layout.BODY_PAD_X
    # The column cover is the height less COLUMN_CHROME and the list's minimum: pick heights for ratios.
    def height(ratio):
        return round(ratio * inner) + layout.COLUMN_CHROME + layout.MIN_LIST_HEIGHT
    assert choose(width, height(0.79)) == SPLIT
    assert choose(width, height(0.84)) == COLUMN                      # from a column: 84% is enough
    assert choose(width, height(0.84), previous=SPLIT) == SPLIT       # from split: not yet
    assert choose(width, height(0.89), previous=SPLIT) == COLUMN


def test_wide_tiers_by_width():
    assert choose(880, 2000) == SPLIT
    assert choose(1279, 800) == SPLIT and choose(1280, 800) == SPLIT_M
    assert choose(1919, 1080) == SPLIT_M and choose(1920, 1080) == SPLIT_L
    assert choose(3840, 2088) == SPLIT_L


def test_hysteresis_dragging_across_880():
    tier, seen = COLUMN, []
    for width in (900, 860, 839, 879, 880):
        tier = choose(width, 2000, tier)
        seen.append(tier)
    assert seen == [SPLIT, SPLIT, COLUMN, COLUMN, SPLIT]


def test_hysteresis_at_the_wider_breakpoints():
    assert choose(1250, 800, SPLIT_M) == SPLIT_M and choose(1239, 800, SPLIT_M) == SPLIT
    assert choose(1890, 1000, SPLIT_L) == SPLIT_L and choose(1879, 1000, SPLIT_L) == SPLIT_M
    assert choose(1250, 800, SPLIT) == SPLIT              # going up, a tier starts at its breakpoint
    assert choose(1890, 1000, SPLIT_M) == SPLIT_M


@pytest.mark.parametrize("edge", [880, 1280, 1920])
def test_jitter_round_a_breakpoint_changes_the_tier_at_most_once(edge):
    tier = choose(edge + 10, 2000)
    changes = 0
    for width in [edge - 10, edge + 10] * 10:
        new = choose(width, 2000, tier)
        changes += new != tier
        tier = new
    assert changes <= 1


def test_the_rules_hold_at_every_size():
    for width in range(780, 4001, 7):
        for height in (500, 800, 1080, 1440, 2088):
            tier = choose(width, height)
            if tier == COLUMN:
                continue
            p = plan(width, height, tier)
            assert p.slot % layout.SLOT_STEP == 0, (width, height)
            assert p.slot == 0 or p.slot >= layout.COVER_HIDE_BELOW
            assert layout.STACK_MIN <= p.stack_w <= layout.COVER_MAX
            assert p.slot <= p.stack_w
            assert p.content <= layout.CONTENT_MAX and p.right_w >= 300
            assert p.left_w + p.right_w == p.content
            assert p.margin * 2 + p.content <= width - layout.BODY_PAD_X


def test_planned_sizes_for_common_windows():
    p = plan(1920, 1080, SPLIT_L)
    assert (p.left_w, p.slot, p.right_w, p.rows) == (720, 672, 1176, 32)
    p = plan(1280, 720, SPLIT_M)
    assert (p.slot, p.right_w) == (376, 832)
    p = plan(2560, 1440, SPLIT_L)
    assert p.slot == 672 and p.margin == (2560 - 24 - 2200) // 2 and p.rows >= 40
    assert plan(800, 900, SPLIT).slot > 0
    assert plan(420, 1000, COLUMN) == layout.Plan(COLUMN)


def test_the_cover_slot_depends_only_on_the_window():
    assert plan(1600, 900, SPLIT_M) == plan(1600, 900, SPLIT_M)
    assert plan(1600, 900, SPLIT_M, show_art=False).slot == 0
    assert plan(1600, 900, SPLIT_M, show_art=False).stack_w == layout.STACK_MIN
    assert plan(1600, 330, SPLIT_M).slot == 0          # too short: the cover is hidden, never tiny


def test_measured_heights_change_the_plan():
    tall_text = layout.Heights(text=200, seek=41, transport=62, chrome=79, list_head=38, heading=23, row=26)
    assert plan(1600, 900, SPLIT_M, heights=tall_text).slot < plan(1600, 900, SPLIT_M).slot
    big_rows = layout.Heights(text=97, seek=41, transport=62, chrome=79, list_head=38, heading=23, row=52)
    assert plan(1600, 900, SPLIT_M, heights=big_rows).rows == plan(1600, 900, SPLIT_M).rows // 2


def test_a_cover_is_never_enlarged():
    assert shown_cover(1152, 700) == (700, True)
    assert shown_cover(600, 800) == (600, False)
    assert shown_cover(672, 672) == (672, False)


def test_columns_for_a_tab():
    wanted = ("artist", "album", "quality")
    assert columns_for(SPLIT_L, wanted, set()) == ()                                  # one album: # · Title · Time
    assert columns_for(SPLIT_L, wanted, {"artist"}) == ("artist",)                     # a compilation
    assert columns_for(SPLIT_M, wanted, {"artist", "album", "quality"}) == wanted       # a playlist
    assert columns_for(SPLIT, wanted, {"artist", "album", "quality"}) == ("artist",)   # no room for more
    assert columns_for(SPLIT_L, ("album",), {"artist", "album"}) == ("album",)         # unticked stays off
    assert columns_for(COLUMN, wanted, {"artist"}) == ()


def test_tabs_may_be_wider_in_the_wider_tiers():
    assert layout.TAB_MAX[COLUMN] == layout.TAB_MAX[SPLIT] == 200
    assert layout.TAB_MAX[SPLIT_M] == layout.TAB_MAX[SPLIT_L] == 240

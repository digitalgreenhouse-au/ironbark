import sys

import pytest

from ironbark import placement


def test_parse_handles_negative_multi_monitor_positions():
    assert placement.parse("480x1681+6227+353") == (480, 1681, 6227, 353)
    assert placement.parse("300x200+-1920+0") == (300, 200, -1920, 0)
    assert placement.parse("+-825+40") == (None, None, -825, 40)
    assert placement.parse("garbage") is None


@pytest.mark.skipif(sys.platform != "win32", reason="monitor check is Windows-only")
def test_off_screen_position_is_detected():
    assert placement.on_screen(10, 10, 300)                 # top-left of the main monitor
    assert not placement.on_screen(-100_000, -100_000, 300)  # a monitor that isn't there

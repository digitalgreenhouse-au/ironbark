"""Release wording: what the player says matches what it does, each thing has one name, and messages
aren't cut off in the narrow window."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ironbark import diagnostics, help_window, keys, library_view, quick_search, statustext, ui
from ironbark.tools import details
from ironbark.library import Track
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)


def _narrow_fonts_ok(root) -> bool:
    from tkinter import font as tkfont

    from ironbark import theme
    return tkfont.nametofont("TkDefaultFont", root=root).actual()["size"] == theme.SIZE_BODY


# -- what Save diagnostics says it leaves out ---------------------------------------------------------
def test_the_diagnostics_wording_says_names_in_the_logs_can_still_show():
    for text in (help_window.PRIVACY, diagnostics.README):
        assert "left out" not in text.split("Save diagnostics")[-1]
        assert "can still show" in text and "look through" in text
    assert "stand-ins" in help_window.PRIVACY and "<folder 1>" in help_window.PRIVACY


# -- the names of things ------------------------------------------------------------------------------
def test_each_thing_is_called_one_name():
    assert dict(ui.NET_PREDOWNLOADS)["queue"] == "Up to 200 songs ahead"   # "queue" is Up next elsewhere
    assert library_view.HEADINGS["quality"] == quick_search.HEADINGS["quality"] == "Format"
    assert "Undo" in keys.BY_ID["undo"].label and "Up next" in keys.BY_ID["undo"].label


def test_the_proof_line_names_the_button_under_it():
    from ironbark.engine import OutputInfo
    from ironbark.proof import Proof
    text, _tone = details.describe_proof(Proof(sent=60, checked=60), OutputInfo("Speakers", 48000, 48000, False),
                                         1.0, 1.0, bits=16, codec="FLAC")
    # (The button, and so the line's hint, is only there where the mixer can be recorded: Windows.)
    assert (f"“{details.CHECK_MIXER}”" in text) == details.loopback.AVAILABLE
    assert details.CHECK_MIXER == f"Check what {statustext.MIXER} does to the sound"


def test_the_about_box_doesnt_call_a_paid_build_free():
    import inspect
    source = inspect.getsource(help_window.AboutWindow)
    assert "Free and open source" not in source and "Open source under the MIT licence" in source


def test_a_one_song_folder_says_one_song_and_big_counts_have_separators():
    from ironbark.ui import PlayerWindow, Tab
    song = Track(path=__import__("pathlib").Path("C:/M/Single/01.flac"), title="One", duration=225.0,
                 sample_rate=44100, bits=16, channels=2)
    tab = Tab("album", "C:/M/Single", tracks=[song])
    tab.last_index = 0
    assert PlayerWindow._album_summary(SimpleNamespace(), tab) == "FLAC · 16-bit / 44.1 kHz · 1 song · 3:45"
    assert ui.plural(200_000, "song") == "200,000 songs" and ui.plural(1, "album") == "1 album"


# -- the full-volume question ---------------------------------------------------------------------------
def test_the_lock_names_what_a_click_switches(make_window):
    window, root, player, errors = make_window(levelling="track")
    window.exclusive.set(True)
    tip = window._lock_tip()
    assert tip.endswith("Click to turn Bit-perfect off.")
    assert "exact samples" not in tip and "Volume levelling is on" in tip   # (levelling changes them)
    window.exclusive.set(False)
    assert window._lock_tip().endswith("Click to lock Bit-perfect on.")
    assert not errors


# -- nothing cut off in the narrow window ---------------------------------------------------------------
def test_the_new_status_messages_fit_the_narrow_window(make_window):
    window, root, player, errors = make_window(geometry="420x640+20+20")
    _pump(root, 0.4)
    if not _narrow_fonts_ok(root):
        pytest.skip("Tk's default font isn't the system's own size here (an earlier test changed the process)")
    messages = [
        f"Go back to the full window ({keys.hint('mini')}) first, then choose Use current (Settings → Window)",
        f"Home set: {keys.hint('snap_home')} or Snap to home (Settings) brings the player back here",
        "No home yet: choose Use current (Settings → Window) to make this spot home",
        "The network playback cache is full: songs further ahead aren't being downloaded",
        "The network playback cache's list was damaged, so the cache was emptied (songs will download again).",
        "Not enough free space on C: for the network playback cache (needs 123 MB): free some space or make "
        "the cache smaller",
        "Loaded 200,000 songs from 17,944 albums",
    ]
    for message in messages:
        window._set_notice(message, 0)
        _pump(root, 0.3)
        shown = " ".join(part for part in (window.status_more.cget("text") if window.status_more.winfo_ismapped()
                                           else "", window.status_label.cget("text")) if part)
        assert shown == message, message                 # whole, on one line or two
    assert not errors


def test_the_library_views_status_line_wraps_in_the_narrow_window(make_window):
    window, root, player, errors = make_window(geometry="420x640+20+20")
    _pump(root, 0.4)
    window.open_library()
    _pump(root, 0.4)
    status = window._library.status
    long = "The library database is damaged: it will be repaired when the player next starts"
    window._library._say(long)
    _pump(root, 0.3)
    assert status.cget("text") == long
    wrap = int(str(status.cget("wraplength")) or 0)
    assert 0 < wrap <= status.winfo_width()             # wrapped to the view, not cut at its edge
    assert status.winfo_reqwidth() <= status.winfo_width() + 2 and not errors
    window._library.close()

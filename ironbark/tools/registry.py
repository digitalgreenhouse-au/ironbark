"""The Check tools in one table: which window each key action opens, what it is called, what the
window needs to be made, whether an Experimental switch gates it, and whether the library quality
check should wait while it is open. The main window opens every tool through this table
(`PlayerWindow.show_tool`), so adding a tool is one line here and one `Action` in keys.py.

Now Playing is not in the table: it is the hub the others are reached from, with a prologue of its
own, and it is opened by name.
"""
from __future__ import annotations

from dataclasses import dataclass

from .abx_window import AbxWindow
from .clip_window import ClipWindow
from .compare_window import CompareWindow
from .meter_window import MeterWindow
from .pcm_window import PcmWindow
from .reels_window import EXPERIMENT as TAPE_REELS
from .reels_window import ReelsWindow
from .scope_window import ScopeWindow
from .spectrogram_window import SpectrogramWindow
from .testsignals_window import TestSignalsWindow

__all__ = ["BY_ACTION", "TAPE_REELS", "TOOLS", "Tool"]


@dataclass(frozen=True)
class Tool:
    action: str          # the key action in keys.py; also the window's slot in the main window
    label: str           # as the user sees it
    window: type         # the ToolWindow subclass
    blurb: str           # one line on what it shows
    needs: str = "player"   # what the window is made with: "player" (root, player, prefs);
    #                         "analysis" adds the cached analysis; "file" is (root, prefs, path_a);
    #                         "file_at" adds the song playing and how far in
    experiment: str | None = None   # the Experimental switch that gates it, if any
    holds_check: bool = False       # the library quality check waits while this window is open


TOOLS: tuple[Tool, ...] = (
    Tool("meters", "Meters", MeterWindow, "VU, PPM, RMS and true-peak meters"),
    Tool("pcm", "PCM stream", PcmWindow, "the sample values being played, in decimal, hex or binary",
         needs="analysis"),
    Tool("clipping", "Clipping", ClipWindow, "inter-sample overs: live, the whole song, and (Experimental) hear them"),
    Tool("spectrogram", "Spectrogram", SpectrogramWindow, "a picture of the sound, live or the whole track",
         needs="analysis"),
    Tool("scope", "Stereo scope", ScopeWindow, "vectorscope, correlation, balance, DC offset"),
    Tool("compare", "Compare two files", CompareWindow, "are two files the same music, sample for sample?",
         needs="file"),
    Tool("abx", "ABX blind test", AbxWindow, "the blind test; A starts as the song playing",
         needs="file_at", holds_check=True),
    Tool("test_signals", "Test signals", TestSignalsWindow,
         "hardware test: test signals, THD+N, J-test, frequency response", holds_check=True),
    Tool("reels", "Tape reels", ReelsWindow, "a reel-to-reel deck playing along", experiment=TAPE_REELS),
)

BY_ACTION: dict[str, Tool] = {tool.action: tool for tool in TOOLS}

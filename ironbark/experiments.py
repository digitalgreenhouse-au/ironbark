"""Experimental features: new ideas tried out in the real player before they're settled. No UI code.

Each experiment is listed once, in EXPERIMENTS. Settings → Experimental shows a switch for each,
off by default, and the code that behaves differently asks `enabled(prefs, key)`.

An experiment's life:
1. Built on its own branch (`experiment/<name>`) while it's rough.
2. Once it works, merged into `dev` and listed here, so it can be lived with in daily listening
   (and kept switched off, or left out, in a public release).
3. Rolled in for good: delete its entry here and the `enabled` checks, so it's simply part of
   the player. Dropped: delete its entry and its code.
A removed experiment's saved switch is ignored (and tidied away), so it can never switch
itself back on, and its key is never reused for something different.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Experiment:
    key: str          # as saved in settings.json; never reused for something different
    title: str        # the switch's name in Settings
    description: str  # one plain line saying what it does, like the other settings
    since: str        # the version it arrived in


# Example (not listed):
#   Experiment("room_eq", "Room correction", "Evens out your room's boomy notes. Not bit-perfect.", "0.6.0")
EXPERIMENTS: tuple[Experiment, ...] = (
    Experiment("tag_editor", "Tag editing",
               "Edit songs' titles, artists, albums and covers. Writes to your music files: each one is "
               "checked sample for sample first, and every edit can be undone.", "0.16.0"),
    Experiment("clip_audition", "Hear clipping",
               "Clipping window: loop a few seconds around an over and switch between an ordinary "
               "converter (chopped at full scale) and clean. Plays sound of its own; not bit-perfect.",
               "0.24.0"),
)


def known() -> dict[str, Experiment]:
    return {e.key: e for e in EXPERIMENTS}


def _switches(prefs: dict) -> dict:
    switches = prefs.get("experiments")
    return switches if isinstance(switches, dict) else {}


def enabled(prefs: dict, key: str) -> bool:
    """Is this experiment switched on? Never, for one that isn't listed (any more)."""
    return key in known() and bool(_switches(prefs).get(key, False))


def set_enabled(prefs: dict, key: str, on: bool) -> None:
    if key not in known():
        raise KeyError(f"no experiment called {key!r}")
    prefs["experiments"] = {**_switches(prefs), key: bool(on)}


def switched_on(prefs: dict) -> list[Experiment]:
    return [e for e in EXPERIMENTS if enabled(prefs, e.key)]


def tidy(prefs: dict) -> None:
    """Forget the switches of experiments that have since been rolled in or dropped."""
    switches = _switches(prefs)
    kept = {k: v for k, v in switches.items() if k in known()}
    if kept != switches or "experiments" in prefs and not isinstance(prefs["experiments"], dict):
        prefs["experiments"] = kept

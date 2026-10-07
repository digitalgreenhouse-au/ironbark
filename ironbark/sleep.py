"""The sleep timer (no UI): stop the music after a while, or at the end of the album playing.

When the time is up:
- shared mode: a gentle FADE_SECONDS fade-out, then the player pauses with the device let go
  (play carries on from that spot). The fade goes through the same gain path as the volume, so
  the proof records it as a change of level.
- bit-perfect mode: no fade, since any change of level would break it. Playback stops at the
  next song boundary instead (the end of the song playing when the time is up), and the status
  line says so.
"End of this album" stops at the end of the album playing (Player.set_stop_after("album")).

Kept for the session only. The window ticks it from its poll; the clock is passed in so tests
can move time on by hand.
"""

from __future__ import annotations

import math
import time

CHOICES = (15, 30, 45, 60, 90)   # minutes offered
ALBUM = "album"
FADE_SECONDS = 10.0

COUNTING, FADING, AT_BOUNDARY, AT_ALBUM_END, DONE = "counting", "fading", "boundary", "album", "done"


def choice_label(target) -> str:
    return "End of this album" if target == ALBUM else f"{target} minutes"


class SleepTimer:
    def __init__(self, player, target, clock=time.monotonic):
        """`target`: minutes (one of CHOICES, or any positive number) or ALBUM."""
        self.player, self.target, self.clock = player, target, clock
        self.started = clock()
        if target == ALBUM:
            player.set_stop_after("album")
            self.state = AT_ALBUM_END
        else:
            self.seconds = float(target) * 60
            self.state = COUNTING

    @property
    def active(self) -> bool:
        return self.state != DONE

    def remaining(self) -> float | None:
        """Seconds left on the clock (None for "end of this album")."""
        if self.target == ALBUM:
            return None
        return max(0.0, self.seconds - (self.clock() - self.started))

    def tick(self, status) -> None:
        """Called with the player's status a few times a second: acts when the time is up, and
        notices when the player has done what was asked (or was stopped some other way)."""
        if self.state == COUNTING:
            if self.remaining() > 0:
                return
            playing = getattr(status.state, "value", status.state) == "playing"
            network = getattr(status, "network", None)
            if network is not None and network.auto:
                playing = True   # (waiting for the network, about to play by itself: it still has to stop)
            if not playing:
                self.state = DONE   # nothing playing when the time is up: nothing to do
            elif status.output is not None and status.output.exclusive:
                self.player.set_stop_after("song")   # bit-perfect: stop at the next song boundary
                self.state = AT_BOUNDARY
            elif self.player.fade_out(FADE_SECONDS):
                self.state = FADING
            else:
                self.state = DONE
        elif self.state == FADING:
            if not status.fading:
                self.state = DONE
        elif self.state in (AT_BOUNDARY, AT_ALBUM_END):
            if not status.stop_after:
                self.state = DONE   # stopped there (or the stop was taken away)

    def cancel(self) -> None:
        if self.state in (AT_BOUNDARY, AT_ALBUM_END):
            self.player.set_stop_after(None)
        elif self.state == FADING:
            self.player.cancel_fade()
        self.state = DONE

    def describe(self) -> str:
        """The status line's words while it's set ("" once it has done its job)."""
        if self.state == COUNTING:
            left = self.remaining()
            if left < 60:
                return "Sleep timer: under a minute left"
            return f"Sleep timer: {math.ceil(left / 60)} min left"
        if self.state == FADING:
            return "Sleep timer: fading out…"
        if self.state == AT_BOUNDARY:
            return "Sleep timer: time's up · stops after this song (bit-perfect: no fade)"
        if self.state == AT_ALBUM_END:
            return "Sleep timer: stopping at the end of this album"
        return ""

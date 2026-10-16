"""Playback engine: plays a list of tracks in order. No UI code in here.

Each stream is a session.py _Session: a decoder thread reads blocks from the file into a queue,
and the sound-card callback fills each request from that queue. Reading the file off the
audio thread keeps playback smooth even when the UI is busy. The decoder thread owns the
files it reads: it alone closes them, so no file is ever closed under a read in progress.

Gapless: near the end of a track the decoder carries straight on into the next one when it
has the same sample rate and channel count, on the same stream, so there's no silence
between them (albums recorded as one continuous performance play as one). A track that
can't join on (a different sample rate, say) gets a new stream, with the short gap that
reopening the device takes; the player says why. When the play list, Up next or repeat
changes after the next track was lined up (but before it's heard), it's lined up again.

Output goes to the device the user picked (or the system default) — see devices.py.
Two output modes:
- shared (default): float32 through the OS mixer, which resamples to its own
  rate and mixes with other apps. On Windows this is WASAPI shared mode.
- exclusive: the player takes the device at the file's own rate and sends
  left-justified int32 samples with dithering off, so at 100% volume the device
  receives the file's samples untouched ("bit-perfect"). Other apps go silent
  while it plays. On Windows that's WASAPI exclusive mode; on a Mac the device is
  hogged and switched to the file's rate and its deepest format first (see
  devices.claim). Falls back to shared if the device
  refuses — but never to another device: if the chosen one isn't connected, it
  doesn't play (full scale on a device with no volume control of its own could
  be far too loud).

When things go wrong the player says so and leaves "playing": a stretch of a file that
won't decode is skipped and reported; a device that stops asking for audio (unplugged,
switched off) leaves the player paused at the same spot.

Songs on network drives (only with the network playback cache installed, see netcache) play
from a copy on this computer. The player asks the cache's preparer first: a song not ready
yet shows "Preparing…" — paused at its spot, no device opened — and starts by itself when
enough of it is here. A copy still downloading is read through readers that wait for their
bytes (audiofile.Waiting when they don't come in time). Mid-song, a short hiccup is ridden out
from the buffer unheard; if the buffer runs nearly dry, playback pauses at the quietest sample
of the last block (a stall marker in the queue) and the stream plays exact zeros — so a lost
device is still noticed — until the music is back, from exactly where it stopped. After 30 s of
that silence the device is let go; it carries on by itself within 5 minutes, on the same device
at the same volume, else waits for play. Nothing is ever faded, scaled or skipped for it. If the
copy's bytes go missing from this computer mid-song (deleted behind the cache's back), the song
is started again from that spot on a fresh copy ("Preparing…") rather than waiting for them.

All public Player methods are safe to call from any thread.
"""
from __future__ import annotations

import collections
import logging
import queue
import random
import threading
import time
from array import array
from dataclasses import dataclass, field
from enum import Enum

import sounddevice as sd

from . import audiofile, devices, platforms
from .library import Track, album_folder
from .proof import Proof
from .session import (  # noqa: F401 — re-exported: the rest of the program gets these from the engine
    _AWAIT,
    _NOT_YET,
    BITS,
    BLOCK_FRAMES,
    BUFFER_SECONDS,
    CLOSE_WAIT,
    DAMAGE_SKIPS,
    JOIN_GIVE_UP_S,
    PATIENCE_FRACTION,
    PATIENCE_MAX,
    PATIENCE_MIN,
    QUEUE_BLOCKS,
    QUIET_WINDOW_S,
    STALL_COMMIT_S,
    STALL_SECONDS,
    STALL_STEP_WINDOW_S,
    Health,
    PlaybackEvent,
    WaitInfo,
    _Blocks,
    _clock,
    _init_com_for_this_thread,
    _Session,
    _Source,
    _Stall,
    quiet_point,
)

log = logging.getLogger(__name__)

DEFAULT_OWNER = "Windows'" if platforms.IS_WINDOWS else "The system's"   # (whose default output it is)


RESTART_THRESHOLD = 3.0  # "previous" within this many seconds goes back a track


REPEAT_MODES = ("off", "all", "one")


TRIMS_KEPT = 8        # clipping-guard settings remembered (the track playing and the ones lined up)


WATCH_SECONDS = 1.0   # how often the player's own thread checks for that


FADE_FLOOR_DB = -60.0  # a fade-out falls evenly (in dB) to this, then to silence


STOP_AFTER_MODES = ("song", "album")   # stop at the end of this song / of this album


HOLD_RELEASE_S = 30.0     # silent this long, waiting for the network: the sound device is let go


AUTO_RESUME_S = 300.0     # a song starts (or carries on) by itself only within this long


_UNCHECKED = object()  # Player._checked_default before the system default's volume control was checked


class State(Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass
class OutputInfo:
    device: str
    rate: int                # rate we send at
    mixer_rate: int | None   # rate the OS mixer runs at (shared mode)
    exclusive: bool
    hardware_volume: bool | None = None  # exclusive only: does the OS volume slider still work?
    device_bits: int | None = None       # exclusive only: bits per sample the device takes (None = unknown)
    amp_volume: bool = False             # exclusive only: its volume is set on an amplifier (Player.amp_outputs)

    @property
    def resampled(self) -> bool:
        return not self.exclusive and self.mixer_rate is not None and self.mixer_rate != self.rate


@dataclass
class Status:
    state: State
    index: int
    track: Track | None
    position: float  # seconds
    duration: float  # seconds
    message: str = ""
    output: OutputInfo | None = None
    gain: float = 1.0                 # the volume setting (1.0 = untouched)
    trim: float = 1.0                 # this track's own gain: the clipping guard and volume levelling together
    guard: float = 1.0                # the clipping guard's part of it (1.0 = none)
    levelling: object = None          # volume levelling on this track (levelling.Levelling), or None
    encoded: str = ""                 # "DTS" / "HDCD": an encoded stream, played at its own level (passthrough.py)
    fading: bool = False              # the sleep timer is fading the music out
    stop_after: str = ""              # "song" / "album": playback stops at the end of this song / album
    levels: tuple[float, ...] = ()    # peak of the latest block per channel, 0..1 (before volume)
    track_peak: float = 0.0           # highest peak so far this track, 0..1
    buffer: float = 0.0               # decoded audio waiting, as a fraction of the buffer
    dropouts: int = 0                 # times the sound card ran dry this track
    proof: Proof | None = None        # what was sent, checked against the file (see proof.py)
    health: Health | None = None    # how close playback came to running dry, this track
    events: list = field(default_factory=list)  # recent dropouts / near misses / damage (PlaybackEvent), newest last
    transition: str = ""              # how this track began: "gapless", "started", or why there was a gap
    network: NetworkView | None = None  # a song from the network playback cache (None for a local one)


@dataclass
class NetworkView:
    """Status.network: how a song on a network drive is getting on (the network playback cache).
    While it's "preparing", "downloading", "checking", "waiting", "fetching" or "released" the
    player reports PAUSED (nothing is sounding); `auto` says it starts or carries on by itself, so
    the transport shows the user's intent (pause) all the same."""
    phase: str                 # "preparing" | "downloading" | "checking" | "waiting" | "fetching" | "released" | "playing"
    auto: bool                 # starts / carries on by itself
    percent: int | None = None
    detail: str = ""           # the cache's own words for where it has got to
    copy: str = ""             # the copy playing: "downloading", "checking", "verified", "failed", "rejected", "stale"
    checks: str = ""           # the checks its complete copy passed (e.g. "size+identity+decode+flac-md5")
    cached: float = 0.0        # fraction of this song's file on this computer
    next_cached: float | None = None
    quality: str = ""          # its server's: "excellent" | "good" | "poor" | "unstable" ("" not known yet)
    speed: float | None = None  # bytes per second, from its server
    at: float = 0.0            # where it is paused or starts (seconds into the track)
    spans: tuple = ()          # what of the track is on this computer: ((from, to), …) as fractions of it


@dataclass
class Preparing:
    """A song waiting to start until enough of it has downloaded (no session, no device open)."""
    at: object                 # the list position or queued song (as _start takes it)
    start_at: float
    transition: str
    token: int                 # this wait's own number (the "ready" event names it)
    since: float               # Player._clock() when it began
    deadline: float            # it starts by itself only until then
    auto: bool = True          # starts by itself (paused / suspended / lapsed: waits for play)
    resume: bool = False       # carrying on after the device was let go during a network pause
    wait_token: object = None  # the preparer's notify_when_startable token


class Queued:
    """One song in the Up next list. Its own object, so the same song queued twice is two entries;
    a session's `_Source.index` is one of these (instead of a list position) while it's playing."""

    __slots__ = ("track",)

    def __init__(self, track: Track):
        self.track = track

    def __repr__(self):
        return f"Queued({self.track.title!r})"


class Player:
    def __init__(self, preparer=None):
        # The network playback cache's preparer (netcache.Manager), asked about every song before
        # it's opened; None = every song is read directly. See set_preparer.
        self._preparer = preparer
        self._preparing: Preparing | None = None   # a song waiting to start until it has downloaded enough
        self._prepare_serial = 0
        # The list playing. Never copied (a tab of a whole library can hold 200,000 songs, read a page
        # at a time: library_tracks.LibraryTrackList) and never changed in place by the window: a
        # change hands over a new list. Songs are found in it by path (`_find`).
        self.tracks: list[Track] = []
        self._paths: tuple | None = None   # (the list, {path: its first place}) — made once per list
        self.index = -1
        # Up next ("Play next" / "Add to Up next"): songs that play before the list carries on.
        # Replaced whole on every change, so the decoder can read it without the lock. While a
        # queued song plays, `index` stays on the list's song it interrupted (where the list
        # carries on from) and `queued_now` is the song.
        self.queue: list[Queued] = []
        self.queued_now: Queued | None = None
        self._volume = 0.8
        self._exclusive = False
        self._device: str | None = None  # output device name; None = system default
        self.hardware_volume: bool | None = None  # see refresh_hardware_volume()
        # Outputs whose volume is set on an amplifier or receiver they feed (Settings → Volume on my
        # amplifier), by name: with no volume of their own, that's as it should be, not a risk.
        self.amp_outputs: frozenset[str] = frozenset()
        self.amp_volume = False   # the output last checked is one of them (see refresh_hardware_volume)
        # Bit-perfect on the system default plays only on the device whose volume control was checked
        # (its name, from refresh_hardware_volume). When Windows makes another device the default,
        # nothing plays in bit-perfect until that one has been checked too: `volume_check_due` asks
        # the window to (see check_default).
        self._checked_default = _UNCHECKED
        self.volume_check_due = False
        self._session: _Session | None = None
        self._lock = threading.RLock()
        self._message = ""
        # Play order: the list's own order, or a shuffled one. Repeat "all" wraps round it;
        # "one" plays the same track again when it ends.
        self.shuffle = False
        self.repeat = "off"
        # Clipping guard: a small turn-down for loud tracks (see set_trim), by file.
        self._trims: dict = {}
        # Volume levelling (off unless chosen): a gain per file (see set_levelling).
        self._levels: dict = {}
        # Files that are encoded streams (DTS, HDCD): never given a gain of their own (see set_encoded).
        self._encoded: dict = {}
        # "Stop after this song" / "… this album" (see set_stop_after), and the sleep timer's
        # fade-out: (when it began, how long it lasts), read by the audio thread without a lock.
        self.stop_after: str | None = None
        self._fade: tuple[float, float] | None = None
        self._clock = time.monotonic
        self._resume_with: Queued | None = None  # stopped after a song with this one from Up next to come
        self._order: range | array = range(0)   # play order: list positions (a range when not shuffled)
        self.transition = ""  # how the track playing began (Status.transition)
        self.events: collections.deque = collections.deque(maxlen=50)  # dropouts / near misses, all tracks
        self._monitors: list[queue.Queue] = []  # see monitor()
        self._suspended_at: float | None = None  # see suspend(): paused with the device let go
        self._silent_run = 0  # tracks in a row that ended with nothing heard (see _ended)
        # (path, stall times) of a song whose session closed while it waited for the network: its
        # next session carries on counting them (each resume waits twice as long after each).
        self._carried_stalls: tuple | None = None
        self._starting = False   # _start is under way (see active)
        # ("end", session) / ("advance", session, previous, new) / ("lost", session) / ("refetch", session)
        # / ("ready", None, token)
        self._ended_events: queue.Queue = queue.Queue()
        threading.Thread(target=self._advance_loop, name="player-control", daemon=True).start()

    # -- loading -------------------------------------------------------------
    def set_tracks(self, tracks: list[Track], keep_playing: bool = False):
        """Replace the playlist. keep_playing: if the current song is in the new list,
        it carries on uninterrupted and the album continues from there. The same goes for a
        player paused with the device let go (suspend: the sleep timer's end, a lost device…) or
        stopped after a song: play carries on from the same spot, or with the song lined up."""
        with self._lock:
            current = None
            if keep_playing and 0 <= self.index < len(self.tracks):
                path = self.tracks[self.index].path
                current = self._find(tracks, path)
            if keep_playing and self.queued_now is not None:
                # A queued song is playing (or paused at a spot): it carries on; the list carries on
                # after it from the same song as before (if it's still there).
                path = self.tracks[self.index].path if 0 <= self.index < len(self.tracks) else None
                self.tracks = tracks
                self._let_go_of_paths()
                found = self._find(tracks, path) if path is not None else None
                self.index = found if found is not None else -1
                self._rebuild_order(first=self.index if self.index >= 0 else None)
                self._relineup()
                return
            if current is None:
                self._close_session()
            elif self._session is not None:
                self._session.renumber(current)
            elif self._preparing is not None:
                self._preparing.at = current
            # (else paused with the device let go, or stopped: its spot — or a queued song lined up to
            # carry on with — stays as it is)
            self.tracks = tracks
            self._let_go_of_paths()
            self.index = current if current is not None else (0 if len(tracks) else -1)
            self._rebuild_order(first=current)
            self._message = ""
            self._relineup()

    def _let_go_of_paths(self):
        """The paths of a list no longer playing aren't kept (with that list: it may be a big one)."""
        found = self._paths
        if found is not None and found[0] is not self.tracks:
            self._paths = None

    def _find(self, tracks, path) -> int | None:
        """The first place of the song with this path in `tracks` (None: not there), through a dict
        made once per list rather than a look at every song. A list read from the library database
        finds it itself (among the songs read: it never reads the database for this). No lock: the
        dict is replaced whole, so the decoder can ask too."""
        finder = getattr(tracks, "index_of_path", None)
        if finder is not None:
            return finder(path)
        found = self._paths
        if found is None or found[0] is not tracks:
            places: dict = {}
            for i, track in enumerate(tracks):
                places.setdefault(track.path, i)
            found = self._paths = (tracks, places)
        return found[1].get(path)

    def order_around(self, index: int, before: int = 1, after: int = 8) -> list[int]:
        """List positions round `index` in play order — `before` it and `after` it (round the ends
        with repeat all) — which a list read a page at a time keeps read, so the engine finds every
        song it asks for already there. No lock (it reads lists replaced whole)."""
        order = self._order
        count = len(order)
        if not count or not 0 <= index < len(self.tracks):
            return []
        try:
            at = order.index(index)
        except ValueError:
            return [index]
        span = range(at - before, at + after + 1)
        if self.repeat == "all":
            return list(dict.fromkeys(order[p % count] for p in span))
        return [order[p] for p in span if 0 <= p < count]

    def _relineup(self):
        """What plays next may have changed: the session re-checks the track it has lined up (and
        the network playback cache, what to download next)."""
        if self._session is not None:
            self._session.relineup()
        self._plan_changed()

    # -- the network playback cache ------------------------------------------------
    def set_preparer(self, preparer):
        """Install the network playback cache's preparer (None, or netcache.DIRECT: every song read
        directly, as before). Takes effect from the next song started."""
        with self._lock:
            self._preparer = preparer
            if hasattr(preparer, "should_yield"):   # the cache's checking backs off while playback needs the time
                preparer.should_yield = self._buffer_low

    def _buffer_low(self) -> bool:
        """Lock-free: playing, with less than half the buffer decoded."""
        session = self._session
        return session is not None and not session.paused and session.buffer_fill < 0.5

    def _plan_changed(self):
        preparer = self._preparer
        if preparer is not None:
            preparer.plan_changed()   # (only sets an Event)

    def _position_hint(self, path, seconds: float):
        preparer = self._preparer
        if preparer is not None:
            preparer.position_hint(path, seconds)

    def active(self) -> bool:
        """Something plays, is paused, or waits to start (for the network, or after the device was
        let go); False once stopped. No lock."""
        return (self._session is not None or self._preparing is not None or self._suspended_at is not None
                or self._starting)

    def upcoming(self, n: int) -> list:
        """The song playing (or about to: preparing; or paused), then up to `n` that follow it, as
        the engine will play them (Up next first): [(list position or queued song, path), …]. Stops
        where playback is set to stop, at repeat one and at a song already listed; [] when stopped.
        No lock: it reads lists that are replaced whole, as the decoder does (the network playback
        cache calls it)."""
        if not self.active():
            return []
        at = self._current()
        track = self._track_at(at)
        if track is None:
            return []
        result = [(at, track.path)]
        seen_at, seen_paths = {id(at) if isinstance(at, Queued) else at}, {track.path}
        while len(result) <= n and not self._stops_after(at):
            following = self._next_after(at)
            track = self._track_at(following)
            key = id(following) if isinstance(following, Queued) else following
            if track is None or key in seen_at or track.path in seen_paths:
                break
            result.append((following, track.path))
            seen_at.add(key)
            seen_paths.add(track.path)
            at = following
        return result

    def session_paths(self) -> list:
        """The songs of the list playing and of Up next, while something plays, is paused or waits
        to start; [] when stopped. No lock (lists replaced whole, as upcoming reads them): the
        network playback cache keeps these copies under "Keep cached tracks: until playback
        finishes"."""
        if not self.active():
            return []
        tracks = self.tracks
        loaded = getattr(tracks, "loaded", None)   # (a list read a page at a time: the songs read)
        return [track.path for track in (loaded() if loaded is not None else tracks)] + \
            [queued.track.path for queued in self.queue]

    # -- Up next -----------------------------------------------------------------
    # Each change takes the lock (the player's thread rewrites the list too, as a queued song
    # starts) and still assigns a new list, so the decoder can read it without the lock.
    def play_next(self, tracks: list[Track]):
        """Put songs at the front of Up next (in the order given): they play when this song ends."""
        playable = [Queued(t) for t in tracks if not t.missing and not t.unsupported]
        with self._lock:
            self.queue = playable + self.queue
            self._relineup()
        return len(playable)

    def add_to_queue(self, tracks: list[Track]):
        """Put songs at the end of Up next."""
        playable = [Queued(t) for t in tracks if not t.missing and not t.unsupported]
        with self._lock:
            self.queue = self.queue + playable
            self._relineup()
        return len(playable)

    def up_next(self) -> list[Track]:
        return [q.track for q in self.queue]

    def remove_from_queue(self, position: int):
        with self._lock:
            queue = list(self.queue)
            if 0 <= position < len(queue):
                queue.pop(position)
                self.queue = queue
                self._relineup()

    def remove_queued(self, entries) -> tuple[list, list]:
        """Take several songs out of Up next at once: these entries (Queued), the ones still in it
        (one that has played since is left alone, and nothing else moves into its place). Returns
        the list before and after, for `restore_queue`."""
        gone = {id(q) for q in entries}
        with self._lock:
            before = self.queue
            after = [q for q in before if id(q) not in gone]
            if len(after) != len(before):
                self.queue = after
                self._relineup()
            return before, self.queue

    def restore_queue(self, before: list, after: list) -> bool:
        """Up next as `before` again (undo of `remove_queued`), if it's still `after`: nothing has
        changed it since."""
        with self._lock:
            if self.queue is not after:
                return False
            self.queue = before
            self._relineup()
            return True

    def clear_queue(self):
        with self._lock:
            self.queue = []
            self._relineup()

    def play_queued(self, entry: Queued):
        """Play a song from Up next straight away (it leaves the list): this entry, if it's still
        there (one that has played since isn't started again, nor its neighbour in its place)."""
        with self._lock:
            if any(q is entry for q in self.queue):
                self._start(entry)

    def _current(self):
        """What's playing: the queued song, or the list position."""
        return self.queued_now if self.queued_now is not None else self.index

    def _track_at(self, at) -> Track | None:
        if isinstance(at, Queued):
            return at.track
        return self.tracks[at] if at is not None and 0 <= at < len(self.tracks) else None

    def _next_after(self, at, repeat_one: bool = True):
        """What plays after `at` (a list position, or a queued song): the next song in Up next
        first, else the list carries on — after the song the queue interrupted, when `at` was a
        queued song. Repeat one replays the list's song (Up next still goes first). The current
        song (`_current`) moves on only when the next one is heard (`_joined`)."""
        queue = self.queue  # (a snapshot: the decoder calls this without the lock)
        if isinstance(at, Queued):
            mine = next((i for i, q in enumerate(queue) if q is at), -1)
            rest = queue[mine + 1:]  # (still listed = lined up but not heard yet: the ones after it)
            if rest:
                return rest[0]
            base = self.index
            if repeat_one and self.repeat == "one" and 0 <= base < len(self.tracks):
                return base
            return self._step(base, 1)
        if queue and at == self._current():
            # Only after the song being heard: a list song the decoder has already lined up
            # *after* the queued ones (it reads a few seconds ahead) just carries on the list.
            return queue[0]
        return at if repeat_one and self.repeat == "one" else self._step(at, 1)

    def following_path(self):
        """The file that plays after the current song (for the clipping guard's look-ahead)."""
        track = self._track_at(self._next_after(self._current()))
        return track.path if track is not None else None

    # -- play order ----------------------------------------------------------
    def set_shuffle(self, on: bool):
        """Shuffle on: the song playing now carries on, everything else follows in a random order."""
        with self._lock:
            self.shuffle = on
            self._rebuild_order(first=self.index if self._session else None)
            self._relineup()

    def set_trim(self, path, gain: float):
        """Turn one track down by `gain` (<= 1.0) while it plays: the clipping guard for
        recordings whose peaks would go over full scale in the converter. Set it for the next
        track too, so it's already in place when that track joins on. None clears them all."""
        if path is None:
            self._trims = {}
            return
        trims = {p: g for p, g in self._trims.items() if p != path}
        trims[path] = min(1.0, gain)
        while len(trims) > TRIMS_KEPT:
            trims.pop(next(iter(trims)))
        self._trims = trims  # replaced whole: the audio thread reads it without a lock

    def set_levelling(self, path, level=None):
        """Volume levelling for one file (a levelling.Levelling: its `gain` may be above 1.0 when
        the track's peak leaves room), set ahead for the next track like the clipping guard. The
        guard still applies: a track it turns down gets the smaller of the two gains (trim_for).
        `path` None clears them all (levelling switched off: every track as before)."""
        if path is None:
            self._levels = {}
            return
        levels = {p: v for p, v in self._levels.items() if p != path}
        if level is not None:
            levels[path] = level
        while len(levels) > TRIMS_KEPT:
            levels.pop(next(iter(levels)))
        self._levels = levels  # replaced whole: the audio thread reads it without a lock

    def set_encoded(self, path, kind: str):
        """Mark a file as an encoded stream ("DTS" / "HDCD", see passthrough.py) that any change of
        level would break: it plays at its own level, whatever the clipping guard or levelling ask
        for (trim_for). "" unmarks it. Set ahead for the next track, like the guard."""
        encoded = {p: k for p, k in self._encoded.items() if p != path}
        if kind:
            encoded[path] = kind
        while len(encoded) > TRIMS_KEPT:
            encoded.pop(next(iter(encoded)))
        self._encoded = encoded  # replaced whole: the audio thread reads it without a lock

    def encoded_for(self, path) -> str:
        return self._encoded.get(path, "")

    def levelling_for(self, path):
        return self._levels.get(path)

    def guard_for(self, path) -> float:
        return self._trims.get(path, 1.0)

    def trim_for(self, path) -> float:
        """A file's own gain: its clipping guard, or its levelling gain when that's smaller. A
        track the guard leaves alone (1.0: it wouldn't clip) takes its levelling gain as it is,
        turned up included (levelling holds that under the track's own peak itself). An encoded
        stream (set_encoded) gets none: 1.0."""
        if path in self._encoded:
            return 1.0
        guard = self._trims.get(path, 1.0)
        level = self._levels.get(path)
        if level is None:
            return guard
        return level.gain if guard >= 1.0 else min(guard, level.gain)

    def _current_trim(self) -> float:
        track = self._track_at(self._current())
        return self.trim_for(track.path) if track is not None else 1.0

    def set_repeat(self, mode: str):
        if mode in REPEAT_MODES:
            with self._lock:
                self.repeat = mode
                self._relineup()

    def _rebuild_order(self, first: int | None = None):
        """The play order: the list's own (a range: nothing held per song) or a shuffled one (a
        compact array, `first` at its head)."""
        count = len(self.tracks)
        if not self.shuffle:
            self._order = range(count)
            return
        rest = array("l", range(count))
        if first is not None and 0 <= first < count:
            rest[first] = rest[-1]   # (first moves out; the last fills its place)
            rest = rest[:-1]
        random.shuffle(rest)
        self._order = (array("l", [first]) + rest) if first is not None and 0 <= first < count else rest

    def _step(self, index: int, step: int) -> int | None:
        """The track after (step=1) or before (step=-1) `index` in play order. None past either
        end, unless repeating everything, when it wraps round."""
        if not self._order:
            return None
        try:
            position = self._order.index(index) + step
        except ValueError:
            position = 0 if step > 0 else len(self._order) - 1
        if 0 <= position < len(self._order):
            return self._order[position]
        return self._order[position % len(self._order)] if self.repeat == "all" else None

    def _follow(self, index):
        """(index, path) of what plays after tracks[index] (or after a queued song), for the
        decoder as a track nears its end; Up next comes first, so a queued song joins on
        gaplessly too. Called without the lock (the decoder mustn't wait on it), so it reads a
        snapshot. Nothing joins on after a song that playback is to stop after."""
        if self._stops_after(index):
            return None
        following = self._next_after(index)
        track = self._track_at(following)
        if track is None:
            return None
        return following, track.path

    # -- stop after this song / album, and the sleep timer's fade ----------------------
    def set_stop_after(self, mode: str | None):
        """Stop cleanly at the end of the song playing ("song") or at the end of its album
        ("album": the song after it is from another album, or there's none); None = carry on.
        It applies to whatever is playing when the end comes, and ends the gapless chain there
        (the decoder drops a next song it has lined up but not yet sent)."""
        with self._lock:
            self.stop_after = mode if mode in STOP_AFTER_MODES else None
            self._relineup()

    def _stops_after(self, at) -> bool:
        """Does playback stop at the end of `at` (a list position or a queued song)? "song": only
        the song playing (`_current`). "album": any song whose next one is from another album, or
        after which the list goes round again (repeat all / repeat one: the album has played to
        its end) — the decoder reads ahead, so with short songs it may be asking about one lined
        up already."""
        mode = self.stop_after
        if mode is None or at is None:
            return False
        if mode == "song":
            return at == self._current()   # (a queued song is compared by identity)
        after = self._next_after(at)
        track, following = self._track_at(at), self._track_at(after)
        if (track is None or following is None
                or album_folder(audiofile.real_path(following.path)) != album_folder(audiofile.real_path(track.path))):
            return True
        return not isinstance(at, Queued) and not isinstance(after, Queued) and self._goes_round(at, after)

    def _goes_round(self, at: int, after: int) -> bool:
        """Does the list start again between list positions `at` and `after` (the next one comes
        no later in play order: repeat all wrapping round, or repeat one)?"""
        order = self._order  # (a snapshot: the decoder calls this without the lock)
        try:
            return order.index(after) <= order.index(at)
        except ValueError:
            return False

    def fade_out(self, seconds: float) -> bool:
        """Fade the music out over `seconds` (evenly in dB down to FADE_FLOOR_DB, then silence),
        then pause, letting go of the device: the sleep timer. Every block sent at the fade's
        gain is recorded in the proof as a change of level. False when nothing is playing."""
        with self._lock:
            session = self._session
            prep = self._preparing
            # (A song about to start by itself, waiting for the network, counts as playing: its
            # fade keeps its clock, so the timer still ends the music.)
            waiting = session is None and prep is not None and prep.auto
            if (session is None or session.paused or seconds <= 0) and not (waiting and seconds > 0):
                return False
            self._fade = (self._clock(), float(seconds))
            return True

    def cancel_fade(self):
        self._fade = None

    def _fade_factor(self) -> float:
        fade = self._fade
        if fade is None:
            return 1.0
        done = (self._clock() - fade[0]) / fade[1]
        if done <= 0:
            return 1.0
        return 0.0 if done >= 1 else 10 ** (FADE_FLOOR_DB * done / 20)

    def _faded(self):
        """The fade has run its course: pause at this spot with the device let go, as suspend().
        (Waiting for the network meanwhile: it no longer starts by itself.)"""
        session = self._session
        self._fade = None
        if session is None:
            prep = self._preparing
            if prep is not None:
                prep.auto = False
                self._message = "Sleep timer: faded out and paused · press play to carry on"
            return
        position, queued = session.position, self.queued_now
        self._close_session()
        self._suspended_at, self.queued_now = position, queued
        self._message = "Sleep timer: faded out and paused · press play to carry on"

    # -- transport -----------------------------------------------------------
    def play(self, index: int | None = None, start_at: float = 0.0):
        """Start `index` (or resume / start the current track), optionally part-way in."""
        with self._lock:
            if not self.tracks and not (index is None and (self.queue or self.queued_now)):
                return
            if index is None and self._preparing is not None:
                # Waiting for the network: play starts it now if a few seconds are here (else it
                # goes on waiting, starting by itself again — for another 5 minutes).
                prep = self._preparing
                self._start(prep.at, prep.start_at, prep.transition, force=True, resume=prep.resume)
                return
            if index is None:
                if self._session and self._session.paused:
                    self._session.set_paused(False)
                    return
                if self._session:
                    return
                if self._resume_with is not None:  # stopped after a song with one from Up next to come
                    resume = self._resume_with
                    if not any(q is resume for q in self.queue):  # taken out of Up next since
                        resume = self.queue[0] if self.queue else self._step(self.index, 1)
                    self._start(resume if resume is not None else (self._order[0] if self._order else 0))
                    return
                start_at = self._suspended_at or start_at  # carry on where suspend() left it
                if self.queued_now is not None and self._suspended_at is not None:
                    self._start(self.queued_now, start_at=start_at)
                    return
                if not self.tracks:  # nothing in the list, but songs waiting in Up next
                    if self.queue:
                        self._start(self.queue[0])
                    return
                index = max(self.index, 0)
            elif self.shuffle:
                self._rebuild_order(first=index)  # a song picked by hand starts a fresh shuffle from it
            self._start(index, start_at=start_at)

    def pause(self):
        with self._lock:
            self._fade = None   # (pausing by hand ends a sleep timer's fade: play resumes at full level)
            if self._session:
                self._session.set_paused(True)
            elif self._preparing is not None:
                self._preparing.auto = False   # it won't start by itself (it keeps downloading)

    def suspend(self) -> bool:
        """Pause and let go of the sound device, for something else to play on it (the ABX test).
        Pausing only stops the stream; closing it is the sure way to free the device (bit-perfect
        mode included) for another stream. The player shows as paused at the same spot, and play
        carries on from there."""
        with self._lock:
            session = self._session
            if session is None:
                if self._preparing is not None:   # (waiting for the network: it mustn't start by itself now)
                    self._preparing.auto = False
                    self._fade = None
                    return True
                return False
            position, queued = session.position, self.queued_now
            self._close_session()
            self._suspended_at, self.queued_now = position, queued  # play() carries on with the same song
            self._fade = None
            return True

    def toggle_pause(self):
        with self._lock:
            if self._session and not self._session.paused:
                self._fade = None
                self._session.set_paused(True)
            elif self._preparing is not None and self._preparing.auto:   # (starting by itself counts as playing)
                self.pause()
            else:
                self.play()

    def stop(self):
        with self._lock:
            self.stop_after, self._fade = None, None
            self._carried_stalls = None
            self._close_session()

    def next(self):
        with self._lock:
            following = self._next_after(self._current(), repeat_one=False)
            if following is not None:
                self._start(following)

    def previous(self):
        with self._lock:
            # From a queued song, "the one before" is the list's song it interrupted.
            if self.queued_now is not None:
                before = self.index if 0 <= self.index < len(self.tracks) else None
            else:
                before = self._step(self.index, -1)
            if self._session and self._session.position > RESTART_THRESHOLD:
                self._session.seek(0)
            elif before is not None:
                self._start(before)
            elif self._session:
                self._session.seek(0)

    def seek(self, seconds: float):
        with self._lock:
            if self._session:
                self._session.seek(seconds)
                if self._session.local.remote:
                    self._position_hint(self._session._path, seconds)
            elif self._preparing is not None:
                self._move_preparing(max(0.0, seconds))
            elif self._suspended_at is not None:
                self._suspended_at = max(0.0, seconds)

    def set_volume(self, volume: float):
        """0.0–1.0 slider position. Squared so the slider feels even to the ear.
        The top of the slider snaps to exactly 1.0 so full volume stays bit-perfect."""
        volume = min(1.0, max(0.0, volume))
        self._volume = 1.0 if volume > 0.995 else volume

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def exclusive(self) -> bool:
        return self._exclusive

    _CURRENT = object()

    def refresh_hardware_volume(self, device=_CURRENT) -> bool | None:
        """Re-check whether the (current, or about-to-be-chosen) output device has its own
        volume control. Call before switching bit-perfect on or changing device. For the system
        default, the device checked is noted: bit-perfect plays only there (see check_default)."""
        name = self._device if device is self._CURRENT else device
        if name is None:
            # (Its name first: if Windows changes the default during the check, the change is caught.)
            self._checked_default = devices.default_output_name()
        self.amp_volume = (name if name is not None else self._checked_default) in self.amp_outputs
        self.hardware_volume = devices.has_hardware_volume(name)
        self.volume_check_due = False
        return self.hardware_volume

    def set_amp_outputs(self, names) -> None:
        """The outputs whose volume is set on an amplifier they feed (by name); `amp_volume` follows
        for the output in use (the system default: the one last checked)."""
        self.amp_outputs = frozenset(names)
        current = self._device if self._device is not None else self._checked_default
        self.amp_volume = isinstance(current, str) and current in self.amp_outputs

    def output_name(self) -> str | None:
        """The output in use by name: the chosen one, or the system default's (now)."""
        return self._device if self._device is not None else devices.default_output_name()

    def check_default(self) -> str | None:
        """Bit-perfect on the system default: the device to play on — the one whose volume control
        was checked (refresh_hardware_volume), by name — as long as it's still Windows' default.
        If the system has made another device the default since, this raises devices.DefaultChanged,
        and nothing plays in bit-perfect until that device has been checked too (`volume_check_due`):
        it may have no volume control of its own, and bit-perfect sends the file at full scale."""
        name = devices.default_output_name()
        with self._lock:
            if name == self._checked_default:
                return name
            self.hardware_volume = None   # (not known for this device: the ear checks treat it as none)
            self.amp_volume = False
            self.volume_check_due = True
        raise devices.DefaultChanged(f"{DEFAULT_OWNER} default output is now {name or 'another device'}")

    @property
    def output_device(self) -> str | None:
        return self._device

    def set_output_device(self, name: str | None):
        """None = follow the system default. The current track carries on from the same spot."""
        with self._lock:
            if name == self._device:
                return
            self._device = name
            self._restart_current()

    def set_exclusive(self, on: bool):
        """Switch output mode; the current track carries on from the same spot."""
        with self._lock:
            if on == self._exclusive:
                return
            self._exclusive = on
            self._restart_current()

    def _restart_current(self):
        """Reopen the current track on the current output settings, keeping position."""
        session = self._session
        if session is None:
            return
        position, was_paused = session.position, session.paused
        self._start(self._current(), start_at=position)
        if was_paused and self._session:
            self._session.set_paused(True)

    def status(self) -> Status:
        with self._lock:
            track = self._track_at(self._current())
            session = self._session
            prep = self._preparing
            output = None
            if session is None and prep is not None and track is not None:
                state = State.PAUSED  # waiting for the network to start (Status.network says more)
                position, duration = prep.start_at, track.duration
            elif session is None and self._suspended_at is not None and track is not None:
                state = State.PAUSED  # suspended (see suspend): paused, with the device let go
                position, duration = self._suspended_at, track.duration
            elif session is None:
                state = State.STOPPED
                position, duration = 0.0, (track.duration if track else 0.0)
            else:
                # Waiting for the network: paused, at the last sample heard (where it carries on).
                wait = session.network_wait
                state = State.PAUSED if session.paused or wait is not None else State.PLAYING
                position, duration = session.position, session.duration
                output = OutputInfo(session.device_name, session.samplerate, session.mixer_rate, session.exclusive,
                                    self.hardware_volume if session.exclusive else None, session.device_bits,
                                    session.exclusive and self.amp_volume)
            message = self._message or (session.message if session else "")
            # index -1 while a queued song plays: it isn't one of the list's songs
            index = -1 if self.queued_now is not None else self.index
            status = Status(state, index, track, min(position, duration), duration,
                            message, output, self._volume ** 2, self._current_trim())
            if track is not None:
                status.encoded = self.encoded_for(track.path)
                if not status.encoded:   # (an encoded stream gets neither: trim_for)
                    status.guard = self.guard_for(track.path)
                    status.levelling = self.levelling_for(track.path)
            status.fading = self._fade is not None and (session is not None or prep is not None)
            status.stop_after = self.stop_after or ""
            status.network = self._network_view(track, session, prep)
            if prep is not None:
                status.transition = prep.transition
            if session is not None:
                status.levels = session.levels if state is State.PLAYING else (0.0,) * session.channels
                status.track_peak = session.track_peak
                status.buffer = session.buffer_fill
                status.dropouts = session.dropouts
                status.proof = session.sent.summary()
                status.health = Health(**vars(session.health))
                status.transition = self.transition
            status.events = list(self.events)
            return status

    def close(self):
        """Stop playing for good: the player's control thread ends too (after close, nothing more
        is played)."""
        self.stop()
        self._ended_events.put(("quit", None))

    def monitor(self) -> queue.Queue:
        """A live feed of every block sent to the device (see _Session._feed_monitors), until
        `unmonitor`. Costs a copy per block only while someone is watching."""
        feed: queue.Queue = queue.Queue(maxsize=64)
        self._monitors.append(feed)
        return feed

    def unmonitor(self, feed: queue.Queue):
        if feed in self._monitors:
            self._monitors.remove(feed)

    def tap(self, seconds: float):
        """Start recording what's sent to the device for `seconds` (shared mode only), for
        the Windows check. Returns (tap, samplerate, device name) or None."""
        with self._lock:
            session = self._session
            if session is None or session.exclusive or session.paused:
                return None
            return session.start_tap(seconds), session.samplerate, session.device_name

    # -- internals -----------------------------------------------------------
    def _gain(self, path=None) -> float:
        """The gain for a file (the track playing if none given): volume, then its own gain (the
        clipping guard / volume levelling), then the sleep timer's fade while there is one."""
        trim = self._current_trim() if path is None else self.trim_for(path)
        gain = self._volume ** 2 * trim
        return gain * self._fade_factor() if self._fade is not None else gain

    def _start(self, index, start_at: float = 0.0, transition: str = "started", force: bool = False,
               resume: bool = False, deadline: float | None = None):
        """Start playing tracks[index] (or a queued song, which then leaves Up next); skips
        forward past files that won't open. A song on a network drive not ready yet waits
        ("Preparing…", see _wait_to_start) instead: no device is opened for it until it starts.
        `force`: play was pressed while it waited (it starts if a few seconds are here).
        `deadline`: a start by itself (the cache said it may) keeps the wait's own limit if it has
        to wait again, so it never starts by itself more than AUTO_RESUME_S after that wait began."""
        self._starting = True   # (active() meanwhile: the cache keeps the song about to play wanted)
        try:
            self._start_now(index, start_at, transition, force, resume, deadline)
        finally:
            self._starting = False

    def _start_now(self, index, start_at: float, transition: str, force: bool, resume: bool,
                   deadline: float | None = None):
        self._close_session(restore=False)
        if devices.formats_changed():
            # Bit-perfect changed a device's rate and format: put it back unless it's about to be
            # used bit-perfect again (no relocking the DAC twice between tracks).
            devices.restore_formats(keep=devices.core_audio_device(self._device) if self._exclusive else None)
        self._message = ""
        self._fade = self._resume_with = None
        self.transition = transition
        self._plan_changed()
        tried: set = set()
        while index is not None and self._track_at(index) is not None:
            key = ("queued", id(index)) if isinstance(index, Queued) else index
            if key in tried:
                break
            tried.add(key)
            track = self._track_at(index)
            if isinstance(index, Queued):
                self.queue = [q for q in self.queue if q is not index]
                self.queued_now = index
            else:
                self.index, self.queued_now = index, None
            local = self._prepare_play(track.path, start_at, force)
            if not isinstance(local, audiofile.LocalTrack):
                if not hasattr(local, "permanent"):   # NotReady: wait for it (shown paused at its spot)
                    self._wait_to_start(index, start_at, transition, resume=resume, deadline=deadline)
                    return
                if local.permanent:   # gone, refused: skipped, like a file that won't open
                    log.warning("cannot play %s: %s", track.path, local.reason)
                    self._message = f"Skipped {audiofile.display_name(track.path)}: {local.reason}"
                    index = self._next_after(index, repeat_one=False)
                    start_at = 0.0
                    continue
                self._message = local.reason   # (the disk is full, say): stopped here, paused at the spot
                self._suspended_at = start_at
                return
            try:
                device = self._device
                if self._exclusive and device is None:
                    device = self.check_default()   # (bit-perfect: only the device whose volume was checked)
                session = _Session(local, self._gain, lambda s: self._ended_events.put(("end", s)),
                                   device=device, want_exclusive=self._exclusive, start_at=start_at,
                                   events=self.events, monitors=self._monitors, index=index, follow=self._follow,
                                   on_advance=lambda s, previous, new: self._ended_events.put(
                                       ("advance", s, previous, new)),
                                   on_lost=lambda s: self._ended_events.put(("lost", s)),
                                   preparer=self._preparer,
                                   on_refetch=lambda s: self._ended_events.put(("refetch", s)))
                carried, self._carried_stalls = self._carried_stalls, None
                if carried is not None and carried[0] == session._path:
                    session._src.stalls = list(carried[1])   # (the same song, taken back after a network pause)
                session.start()
            except audiofile.Waiting:   # (its opening bytes weren't all here after all: wait for them)
                self._wait_to_start(index, start_at, transition, resume=resume, deadline=deadline)
                return
            except sd.PortAudioError as exc:
                # The sound device, not the file, is the problem: skipping tracks won't help. Shown
                # paused at the same spot, so play tries again from there.
                log.warning("cannot open output for %s: %s", track.path, exc)
                if isinstance(exc, devices.DefaultChanged):
                    self._message = (f"{exc}, so bit-perfect stopped here to check its volume first · press play "
                                     "to carry on")
                elif isinstance(exc, devices.DeviceMissing):
                    self._message = (f"{exc}. Bit-perfect mode plays only on the output you chose: connect it, "
                                     "or pick another under Output, then press play.")
                else:
                    self._message = f"Can't open the sound device ({exc}). Check Output, then press play."
                self._suspended_at = start_at
                return
            except Exception as exc:  # noqa: BLE001 — the file, not the device (damaged, unsupported): skip it
                log.warning("cannot play %s: %s", track.path, exc)
                self._message = f"Skipped {audiofile.display_name(track.path)}: {exc}"
                index = self._next_after(index, repeat_one=False)
                start_at = 0.0
                continue
            self._session = session
            return
        self.index = self._order[0] if self._order else 0
        self.queued_now = None
        self._message = self._message or "End of album"

    def _close_session(self, restore: bool = True):
        """Close the stream; `restore` also puts back any device format bit-perfect changed."""
        self._suspended_at = None
        self.queued_now = None
        prep, self._preparing = self._preparing, None
        if prep is not None and prep.wait_token is not None and self._preparer is not None:
            self._preparer.forget(prep.wait_token)
        if self._session:
            self._session.close()
            self._session = None
        if restore:
            devices.restore_formats()
        self._plan_changed()   # (the cache lets go of what this was downloading, unless it plays on)

    # -- songs waiting for the network (the network playback cache) ------------------------
    def _prepare_play(self, path, start_at: float, force: bool):
        """The preparer's answer for a song about to play: a LocalTrack, NotReady or Unavailable
        (no I/O: a look-up in the cache's memory). With no cache, the file itself."""
        preparer = self._preparer
        if preparer is None:
            return audiofile.LocalTrack.plain(path)
        try:
            return preparer.prepare(path, role="play", at=start_at, force=force)
        except Exception:  # noqa: BLE001 — a fault in the cache mustn't stop the song: read it directly
            log.exception("preparing %s", path)
            return audiofile.LocalTrack.plain(path)

    def _wait_to_start(self, at, start_at: float, transition: str, resume: bool = False,
                       deadline: float | None = None):
        """`at` (already the current song) isn't ready to play: show it paused at its spot, and start
        it by itself when the cache says it may (a "ready" event) — within AUTO_RESUME_S."""
        track = self._track_at(at)
        now = self._clock()
        self._prepare_serial += 1
        prep = Preparing(at, start_at, transition, self._prepare_serial, now,
                         now + AUTO_RESUME_S if deadline is None else deadline, resume=resume)
        self._preparing = prep
        token = prep.token
        try:
            prep.wait_token = self._preparer.notify_when_startable(
                track.path, start_at, post=lambda: self._ended_events.put(("ready", None, token)))
        except Exception:  # noqa: BLE001 — the watch tick looks again every second anyway
            log.exception("waiting for %s", track.path)

    def _move_preparing(self, seconds: float):
        """A seek while a song waits for the network: it will start from there."""
        prep = self._preparing
        track = self._track_at(prep.at)
        prep.start_at = seconds
        if track is None:
            return
        self._position_hint(track.path, seconds)
        preparer, token = self._preparer, prep.token
        if prep.wait_token is not None:
            preparer.forget(prep.wait_token)
        prep.wait_token = preparer.notify_when_startable(
            track.path, seconds, post=lambda: self._ended_events.put(("ready", None, token)))

    def _ready(self, token):
        """The cache says the song waiting may start now (on the player's own thread)."""
        prep = self._preparing
        if prep is None or prep.token != token or self._session is not None:
            return
        if not prep.auto or self._clock() > prep.deadline:
            prep.auto = False
            track = self._track_at(prep.at)
            name = f"“{track.title}”" if track is not None and track.title else "The song"
            self._message = ("The network is back · press play to carry on" if prep.resume
                             else f"{name} is ready: press play")
            return
        fade = self._fade   # (a sleep timer's fade carries on: the song doesn't start louder)
        self._start(prep.at, prep.start_at, prep.transition, resume=prep.resume, deadline=prep.deadline)
        if fade is not None and (self._session is not None or self._preparing is not None):
            self._fade = fade

    def _watch_preparing(self):
        """Every watch tick while a song waits for the network: its automatic start lapses after
        AUTO_RESUME_S; if the cache's word that it may start went astray, it starts anyway; and if
        it turns out it can't be played at all (its file has gone, say), it's skipped (or stopped
        at) saying why, as when that's known at once — never left "preparing" for minutes."""
        prep = self._preparing
        if prep is None or not prep.auto or self._session is not None:
            return
        if self._clock() > prep.deadline:
            prep.auto = False
            self._message = ("The network didn't come back within 5 minutes · press play to carry on"
                             if prep.resume else "Still downloading after 5 minutes · press play to start it")
            return
        track = self._track_at(prep.at)
        if track is None:
            return
        try:
            outlook = self._preparer.outlook(track.path, prep.start_at)
        except Exception:  # noqa: BLE001
            log.exception("looking at %s", track.path)
            return
        if outlook is None:
            self._ready(prep.token)
        elif hasattr(outlook, "permanent"):   # Unavailable: _start says why, and skips it or stops there
            fade = self._fade
            self._start(prep.at, prep.start_at, prep.transition, resume=prep.resume, deadline=prep.deadline)
            if fade is not None and (self._session is not None or self._preparing is not None):
                self._fade = fade

    def _release_for_network(self, session: _Session, refetch: bool = False):
        """Silent for HOLD_RELEASE_S waiting for the network: let go of the sound device (in
        bit-perfect mode other apps are silent while it's held), keep the spot, and carry on by
        itself — the same device, the same volume — if the network is back within AUTO_RESUME_S
        of the silence starting. `refetch`: the copy's bytes went missing from this computer — let
        go at once and start the song again from the same spot once enough of a fresh copy is here
        ("Preparing…"; paused by hand, it waits for play). The pauses for the network so far carry
        over to the song's next session either way."""
        position = session.position   # (the last sample heard: where it carries on)
        silent_for = session.waiting_for(time.perf_counter())
        at, queued, paused = self._current(), self.queued_now, session.paused
        stalls = (session._path, list(session._src.stalls))
        self._close_session()
        self.queued_now = queued
        self._carried_stalls = stalls
        self._position_hint(session._path, position)   # (a fresh copy is fetched from where it carries on)
        if refetch:
            log.info("%s: its copy on this computer went missing; started again from %.1f s on a fresh copy",
                     session._track_name, position)
            self._wait_to_start(at, position, "carried on from a fresh copy (the one on this computer went missing)")
            if paused and self._preparing is not None:
                self._preparing.auto = False
            return
        log.info("%s: no music from the network for %.0f s; the sound device is let go", session._track_name,
                 silent_for)
        self._wait_to_start(at, position, "carried on after a pause for the network", resume=True,
                            deadline=self._clock() - silent_for + AUTO_RESUME_S)

    def _network_view(self, track, session, prep) -> NetworkView | None:
        """Status.network: how a song on a network drive is getting on (None for a local song)."""
        preparer = self._preparer
        if preparer is None or track is None:
            return None
        if session is not None and not session.local.remote:
            return None
        if session is None and prep is None:
            return None
        try:
            if prep is not None:
                outlook = preparer.outlook(track.path, prep.start_at)
                view = NetworkView("released" if prep.resume else "preparing", prep.auto, at=prep.start_at)
                if outlook is not None and hasattr(outlook, "phase"):
                    if not prep.resume and outlook.phase in ("downloading", "checking"):
                        view.phase = outlook.phase
                    view.percent, view.detail = outlook.percent, outlook.detail
                elif outlook is not None:   # Unavailable
                    view.detail = outlook.reason
            else:
                wait = session.network_wait
                view = NetworkView("playing", not session.paused, at=session.position)
                if wait is not None:
                    view.phase = "fetching" if wait.seek else "waiting"
                    outlook = preparer.outlook(session.local, wait.frame / session.samplerate)
                    if outlook is not None and hasattr(outlook, "phase"):
                        view.percent, view.detail = outlook.percent, outlook.detail
                view.copy = session.local.copy_state()
            progress = preparer.progress(track.path)
            if progress is not None:
                view.cached, view.speed = progress.fraction, progress.speed
                view.checks = getattr(progress, "checks", "")
                view.quality = getattr(progress, "quality", "")
                view.spans = tuple(getattr(progress, "spans", ()))
            following = self.following_path()
            if following is not None:
                ahead = preparer.progress(following)
                view.next_cached = ahead.fraction if ahead is not None else None
            return view
        except Exception:  # noqa: BLE001 — only what the window shows
            log.exception("network playback status")
            return None

    def _joined(self, session: _Session, previous: _Source, source: _Source):
        """A track joined on gaplessly: it's now the one playing."""
        tracks = self.tracks
        if isinstance(source.index, Queued):  # a song from Up next: it leaves the list now it's heard
            self.queue = [q for q in self.queue if q is not source.index]
            self.queued_now = source.index
        elif 0 <= source.index < len(tracks) and tracks[source.index].path == source.path:
            self.index, self.queued_now = source.index, None
        else:  # the list changed while it was lined up
            index = self._find(tracks, source.path)
            if index is not None:
                self.index = source.index = index
            else:  # taken out of the list: what follows carries on from where the list was
                source.index = self.index
            self.queued_now = None
        self.transition = "gapless"
        self._message = ""
        session.retire(previous)
        self._plan_changed()

    def _lost(self, session: _Session):
        """The device stopped taking audio (unplugged, switched off, asleep): let go of it and show
        paused at the same spot, saying why. Never carry on on another device by itself — in
        bit-perfect mode that could be far too loud."""
        position, queued, name = session.position, self.queued_now, session.device_name
        self._close_session()
        self._suspended_at, self.queued_now = position, queued  # play() carries on with the same song
        self._fade = None
        self._message = f"{name} stopped playing (disconnected?): press play to carry on"

    def _ended(self, session: _Session):
        """The chain of tracks has played out: start what's next (with a gap), or stop at the end."""
        current = self._current()
        following = self._next_after(current)
        if self._stops_after(current):
            self._stopped_after(current, following)
            return
        # A track that gave no music ends at once: repeating it (or going round a list of them)
        # would start stream after stream for ever.
        self._silent_run = 0 if session.delivered else self._silent_run + 1
        if following is not None and self._silent_run and (
                following == current or self._silent_run > len(self.tracks) + len(self.queue)):
            track = self._track_at(current)
            self._close_session()
            self._message = (f"{audiofile.display_name(track.path)} has no music that can be played"
                             if following == current and track is not None
                             else "Nothing here has any music that can be played")
            return
        if following is not None:
            fade = self._fade   # a sleep timer's fade carries on into the next song (it isn't a fresh start)
            self._start(following, transition=session.handoff or "started")
            if self._session is not None or self._preparing is not None:
                self._fade = fade
        else:
            self._close_session()
            self.index = self._order[0] if self._order else 0  # ready to play it again
            self.queued_now = None
            self._message = "End of album"

    def _stopped_after(self, current, following):
        """Playback has reached the end of the song (or album) it was to stop after: stop there,
        with what comes next ready, so play carries on from it."""
        mode, track = self.stop_after, self._track_at(current)
        self.stop_after = None
        self._close_session()
        if isinstance(following, Queued):
            self._resume_with = following
        elif following is not None:
            self.index = following
        else:
            self.index = self._order[0] if self._order else 0  # the end of the list: ready to play it again
        name = f"“{track.title}”" if track is not None and track.title else "this song"
        self._message = ("Stopped at the end of the album, as asked" if mode == "album"
                         else f"Stopped after {name}, as asked")

    def _advance_loop(self):
        # Runs on its own thread: PortAudio's finished-callback must not open streams itself. Between
        # events it watches for a device that has stopped asking for audio without saying so.
        _init_com_for_this_thread()
        while True:
            try:
                kind, session, *details = self._ended_events.get(timeout=WATCH_SECONDS)
            except queue.Empty:
                kind, session, details = "watch", None, []
            if kind == "quit":   # (close)
                return
            try:
                with self._lock:
                    if kind == "ready":   # a song waiting for the network may start (no session yet)
                        self._ready(details[0])
                        continue
                    if kind == "watch":
                        session = self._session
                        now = time.perf_counter()
                        if session is not None and session.stalled(now):
                            log.warning("%s: no requests for audio in %.0f s", session.device_name, STALL_SECONDS)
                            self._lost(session)
                        elif self._fade is not None and self._fade_factor() == 0.0:
                            self._faded()
                        elif session is not None and session.waiting_for(now) > HOLD_RELEASE_S:
                            self._release_for_network(session)
                        self._watch_preparing()
                        continue
                    if session is not self._session:
                        continue  # user already moved on
                    if kind == "advance":
                        self._joined(session, *details)
                    elif kind == "lost":
                        self._lost(session)
                    elif kind == "refetch":
                        self._release_for_network(session, refetch=True)
                    else:
                        self._ended(session)
            except Exception:  # noqa: BLE001 — this thread must never die: nothing would move on again
                log.exception("player control")
                self._message = "Something went wrong moving on to the next track: press play to carry on"

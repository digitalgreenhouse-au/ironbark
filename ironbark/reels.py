"""Tape reels (Experimental): the physics of a reel-to-reel tape deck, following the song playing.
No UI code (reels_window.py draws it).

How a real deck moves its tape
- The capstan pulls the tape past the heads at a constant speed (7½ or 15 inches per second).
  The reels don't set the speed: each one turns as fast as it must to feed or take up tape at
  that speed, so a reel's turning speed is the tape speed over its pack's circumference —
  rpm = 60 · v / (2π · r). The full reel turns slowly, the nearly empty one fast, and both
  change all the way through.
- Tape wound on a reel is a spiral: each turn adds one tape thickness t to the radius. The pack's
  end-on area is the tape's length times its thickness, so with l metres wound on a core of
  radius r_core:  π (r² - r_core²) = l · t   →   r(l) = √(r_core² + l·t/π).
- The angle a reel has turned while winding on l metres is ∫ dl / r(l), which works out exactly
  as 2π · (r(l) - r_core) / t: the number of turns of tape on it. So a reel's angle is a function
  of where the tape is, and the reels can never drift from the song.

Reel and tape figures
- Speeds: 7½ ips = 19.05 cm/s, 15 ips = 38.1 cm/s (Wikipedia, "Audio tape specifications").
- Long-play tape (1 mil base, about 35 µm in all) fills a 10½-inch reel with 3,600 ft and a
  7-inch reel with 1,800 ft (same source). Standard-play tape (1.5 mil base) comes as 2,500 ft
  on a 10½-inch reel and 1,200 ft on a 7-inch one (makers' listings, e.g. Capture 914).
- No published figure for the cores' outside diameter was found, so each core is worked out
  from the long-play figures: the full pack reaches just inside the flanges (PACK_CLEARANCE).
  That gives cores of about 138 mm (10½-inch) and 71 mm (7-inch).
- Standard-play tape's thickness then follows from 2,500 ft just filling the same reel: about
  50 µm (2.0 mil) in all, which agrees with measured 1.5 mil studio tapes (≈1.99 mil).
- A seek is drawn as the deck spooling there: speeding up, then slowing to land on the spot. A real
  deck can take a minute or two to wind a whole reel; here a spool takes about WIND_TIME, however far,
  so the reels are back with the music at once (they turn very fast on a long one).

The tape holds the album playing (the songs next to it in the player's list from the same album
folder), packed onto as many reels as it takes without splitting a song where it can be helped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

INCH = 0.0254   # metres
FOOT = 12 * INCH

SPEEDS = {"7.5": 7.5 * INCH, "15": 15 * INCH}   # tape speed, metres a second
SPEED_NAMES = {"7.5": "7½ ips", "15": "15 ips"}

LONG_PLAY_THICKNESS = 35e-6   # metres in all (base + coating), 1 mil base
PACK_CLEARANCE = 0.003        # metres between a full pack and the flanges' rim (assumed)

WIND_TIME = 1.2         # seconds a spool (a seek) takes, however far
WIND_ACCEL = 6.0        # metres a second², spooling up and down, at least
PLAY_ACCEL = 2.0        # metres a second², starting and stopping (play / pause)
FOLLOW_RATE = 3.0       # per second: how fast a small gap from the song's position is closed
WIND_OVER = 0.5         # metres: a gap bigger than this spools (a seek); smaller is followed


@dataclass(frozen=True)
class ReelSize:
    key: str
    name: str
    flange_radius: float       # metres
    long_play_length: float    # metres of long-play tape that fill it
    standard_length: float     # metres of standard-play tape sold on it
    standard_name: str

    @property
    def full_radius(self) -> float:
        return self.flange_radius - PACK_CLEARANCE

    @property
    def core_radius(self) -> float:
        """Worked out from the long-play figure: that much tape fills the reel to `full_radius`."""
        return math.sqrt(self.full_radius ** 2 - self.long_play_length * LONG_PLAY_THICKNESS / math.pi)

    @property
    def thickness(self) -> float:
        """Standard-play tape's thickness in all, from its sold length filling the same space."""
        return math.pi * (self.full_radius ** 2 - self.core_radius ** 2) / self.standard_length


REELS = {
    "10.5": ReelSize("10.5", "10½-inch", 10.5 * INCH / 2, 3600 * FOOT, 2500 * FOOT, "2,500 ft"),
    "7": ReelSize("7", "7-inch", 7 * INCH / 2, 1800 * FOOT, 1200 * FOOT, "1,200 ft"),
}


def pack_radius(reel: ReelSize, length: float) -> float:
    """The outside radius of `length` metres of tape wound on the reel."""
    return math.sqrt(reel.core_radius ** 2 + max(0.0, length) * reel.thickness / math.pi)


def turns(reel: ReelSize, length: float) -> float:
    """How many times the reel turns while `length` metres are wound onto it from empty."""
    return (pack_radius(reel, length) - reel.core_radius) / reel.thickness


def rpm(speed: float, radius: float) -> float:
    """A reel's turns a minute when tape leaves or joins its pack (of this radius) at `speed`."""
    return 60.0 * speed / (2 * math.pi * radius)


# -- the tape: which songs, on which reel --------------------------------------------------------------

@dataclass(frozen=True)
class Spool:
    """One reel of the album's tape: what's recorded on it, in seconds of the album."""
    key: tuple                 # what's on it (changes = a new reel is loaded)
    number: int                # 1, 2, …
    count: int                 # reels in the album
    start: float               # where it begins, in seconds of the album
    seconds: float             # how much is recorded on it


def album_key(track) -> tuple:
    return (str(track.path.parent).casefold(), (track.album or "").casefold())


def album_run(tracks, index: int, track) -> tuple[list[float], int]:
    """The album around the song playing: its songs' lengths in order, and which one is playing.
    `tracks` is the player's list and `index` the song's place in it (-1: a song from Up next,
    then it's alone on its tape). A song with no known length ends the run."""
    if track is None:
        return [], -1
    if not (0 <= index < len(tracks)) or tracks[index] is not track and tracks[index].path != track.path:
        return [max(0.0, track.duration)], 0
    key = album_key(track)
    first = index
    while first > 0 and album_key(tracks[first - 1]) == key and tracks[first - 1].duration > 0:
        first -= 1
    last = index
    while last + 1 < len(tracks) and album_key(tracks[last + 1]) == key and tracks[last + 1].duration > 0:
        last += 1
    return [max(0.0, t.duration) for t in tracks[first:last + 1]], index - first


def pack_reels(lengths: list[float], capacity: float) -> list[tuple[float, float]]:
    """Put songs (seconds each) on reels holding `capacity` seconds: (start, seconds) per reel.
    A song goes on the next reel when it won't fit; one longer than a whole reel is cut across reels."""
    reels: list[tuple[float, float]] = []
    start = used = 0.0
    for length in lengths:
        if used > 0 and used + length > capacity:
            reels.append((start, used))
            start, used = start + used, 0.0
        while used + length > capacity:   # (used is 0 here) a song longer than a whole reel
            reels.append((start, capacity))
            start += capacity
            length -= capacity
        used += length
    if used > 0 or not reels:
        reels.append((start, used))
    return reels


def spool_for(lengths: list[float], playing: int, position: float, capacity: float,
              album: tuple) -> tuple[Spool, float]:
    """The reel holding the song at `position` seconds into song `playing`, and the seconds into that reel."""
    if not lengths:
        return Spool(album + (0,), 1, 1, 0.0, 0.0), 0.0
    at = sum(lengths[:playing]) + max(0.0, position)
    reels = pack_reels(lengths, capacity)
    number = len(reels)
    for n, (start, seconds) in enumerate(reels, 1):
        if at < start + seconds or n == len(reels):
            number = n
            break
    start, seconds = reels[number - 1]
    return Spool(album + (number,), number, len(reels), start, seconds), min(max(0.0, at - start), seconds)


# -- the transport: where the tape is, moving like a deck --------------------------------------------

class Transport:
    """The tape's place (metres played off the supply reel) and speed, chasing where the song is.
    While playing it runs at the tape speed and quietly closes any small gap; a pause or play
    starts and stops it within a fraction of a second; a jump (seek) spools there quickly, speeding
    up and slowing down to land on the spot."""

    def __init__(self):
        self.place = 0.0     # metres
        self.speed = 0.0     # metres a second (+ forwards)
        self.key = None
        self.winding = 0.0   # the spool's acceleration while one is under way (0: none)

    def load(self, key, place: float, speed: float) -> None:
        """A new reel threaded at `place`, already moving at `speed`."""
        self.key, self.place, self.speed, self.winding = key, place, speed, 0.0

    def step(self, dt: float, target: float, playing: bool, tape_speed: float, length: float) -> None:
        if dt <= 0:
            return
        dt = min(dt, 0.25)   # after a stall (window dragged, PC busy): no huge leap
        gap = target - self.place
        if abs(gap) > WIND_OVER or self.winding and abs(gap) > 0.05:
            # Spool: speed up, then slow in time to stop on the spot (v² = 2·a·s). The acceleration is
            # set as it starts, so the whole way takes about WIND_TIME (half speeding up, half slowing).
            if not self.winding:
                self.winding = max(WIND_ACCEL, 4 * abs(gap) / WIND_TIME ** 2)
            accel = self.winding
            want = math.copysign(math.sqrt(2 * accel * abs(gap)), gap) + (tape_speed if playing else 0.0)
        else:
            self.winding = 0.0
            # Paused: the reels brake to a stop (never creep back to close the few centimetres they
            # ran on; that's taken up once the music plays again).
            want = tape_speed + FOLLOW_RATE * gap if playing else 0.0
            accel = PLAY_ACCEL if abs(gap) < 0.05 or not playing else WIND_ACCEL
        change = want - self.speed
        most = accel * dt
        self.speed += max(-most, min(most, change))
        self.place += self.speed * dt
        if self.winding and (target - self.place) * gap <= 0:
            # A spool lands on the spot (a frame's step at full spooling speed would carry it past).
            self.place, self.speed, self.winding = target, (tape_speed if playing else 0.0), 0.0
        if self.place <= 0.0 or self.place >= length:
            self.place = min(max(self.place, 0.0), length)
            self.speed, self.winding = 0.0, 0.0


@dataclass(frozen=True)
class ReelState:
    """One reel at a moment: its pack's radius, the angle it has turned (radians, clockwise),
    and its turns a minute."""
    radius: float
    angle: float
    rpm: float


def reel_states(reel: ReelSize, length: float, place: float, speed: float) -> tuple[ReelState, ReelState]:
    """(supply, take-up) with `length` metres of tape on the album reel and `place` metres played.
    Both reels turn clockwise while the tape plays forwards (the tape leaves the supply reel down
    its right side and climbs onto the take-up reel's left side)."""
    place = min(max(place, 0.0), length)
    supply_radius = pack_radius(reel, length - place)
    take_radius = pack_radius(reel, place)
    supply_angle = 2 * math.pi * (turns(reel, length) - turns(reel, length - place))
    take_angle = 2 * math.pi * turns(reel, place)
    return (ReelState(supply_radius, supply_angle, rpm(speed, supply_radius)),
            ReelState(take_radius, take_angle, rpm(speed, take_radius)))

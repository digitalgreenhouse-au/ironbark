"""Playback engine: plays a list of tracks in order. No UI code in here.

Each stream gets a _Session: a decoder thread reads blocks from the file into a queue,
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
- exclusive (Windows/WASAPI): the player takes the device at the file's own
  rate and sends left-justified int32 samples with dithering off, so at 100%
  volume the device receives the file's samples untouched ("bit-perfect").
  Other apps go silent while it plays. Falls back to shared if the device
  refuses — but never to another device: if the chosen one isn't connected, it
  doesn't play (full scale on a device with no volume control of its own could
  be far too loud).

When things go wrong the player says so and leaves "playing": a stretch of a file that
won't decode is skipped and reported; a device that stops asking for audio (unplugged,
switched off) leaves the player paused at the same spot.

All public Player methods are safe to call from any thread.
"""

from __future__ import annotations

import collections
import logging
import math
import queue
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import sounddevice as sd

from . import audiofile, devices
from .library import Track, format_rate
from .proof import Proof, SentRecord, apply_gain

log = logging.getLogger(__name__)

BLOCK_FRAMES = 4096   # frames per sound-card callback
QUEUE_BLOCKS = 24     # decoded blocks buffered ahead, at the least (~2.2 s at 44.1 kHz)
BUFFER_SECONDS = 2.2  # ...or this much music, whichever is more (hi-res files need more blocks)
RESTART_THRESHOLD = 3.0  # "previous" within this many seconds goes back a track
REPEAT_MODES = ("off", "all", "one")
BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32,  # integer files' sample sizes
        "ALAC_16": 16, "ALAC_20": 20, "ALAC_24": 24, "ALAC_32": 32}
TRIMS_KEPT = 8        # clipping-guard settings remembered (the track playing and the ones lined up)
DAMAGE_SKIPS = 5      # a stretch that won't decode: skip on past it, twice as far each try, this many tries
STALL_SECONDS = 3.0   # playing, but no request from the sound card for this long: the device has gone
WATCH_SECONDS = 1.0   # how often the player's own thread checks for that
CLOSE_WAIT = 0.25     # how long closing waits for the decoder to let go of its files

_READING, _ENDED, _IDLE = "reading", "ended", "idle"   # the decoder's state (see _Session._decode)
_NOT_YET = object()   # _Source.lined_up before the decoder has read to the file's end


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
    trim: float = 1.0                 # clipping guard for this track (1.0 = none)
    levels: tuple[float, ...] = ()    # peak of the latest block per channel, 0..1 (before volume)
    track_peak: float = 0.0           # highest peak so far this track, 0..1
    buffer: float = 0.0               # decoded audio waiting, as a fraction of the buffer
    dropouts: int = 0                 # times the sound card ran dry this track
    proof: Proof | None = None        # what was sent, checked against the file (see proof.py)
    health: "Health | None" = None    # how close playback came to running dry, this track
    events: list = field(default_factory=list)  # recent dropouts / near misses / damage (PlaybackEvent), newest last
    transition: str = ""              # how this track began: "gapless", "started", or why there was a gap


@dataclass
class Health:
    """Safety margins for the track playing, measured once the buffer has filled."""
    buffer_low: float | None = None    # least music decoded and waiting (seconds)
    driver_low: float | None = None    # least music queued at the sound driver when it asked for more (s)
    longest_wait: float = 0.0          # longest gap between the driver's requests (seconds)
    usual_wait: float = 0.0            # what that gap should be: one block's length
    slowest_read: float = 0.0          # slowest read of one block from the file (seconds)
    near_misses: int = 0


@dataclass
class PlaybackEvent:
    when: float      # time.time()
    track: str
    position: float  # seconds into the track
    kind: str        # "dropout", "near miss", "damaged" or "cut short"
    cause: str
    count: int = 1   # a stutter (gaps less than 2 s apart) is one event with a count
    silent: float = 0.0  # seconds of silence, all its gaps together
    last: float = 0.0    # time.time() of its latest gap


def _init_com_for_this_thread() -> None:
    """WASAPI exclusive streams opened from a thread without COM set up fail with
    "Invalid device" — and leave PortAudio refusing exclusive mode on every thread after."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class _Source:
    """One file in a session's chain: the open file, where it sits in the play list, and the
    fingerprints of what's been sent from it (its bit-perfect proof). A CUE sheet track is its
    stretch of the file (audiofile.CuePath): frame 0 is the track's first sample, `offset` is
    where that sits in the whole file, and reading ends on its last sample, so the next track of
    the same file joins on gaplessly like any other."""

    def __init__(self, path, index: int = -1):
        self.path, self.index = path, index
        self.file = audiofile.open(path)  # the one decode path (a CUE track: just its stretch)
        self.samplerate, self.channels, self.total_frames = self.file.samplerate, self.file.channels, self.file.frames
        self.bits = BITS.get(self.file.subtype)  # None for floating-point files and MP3s
        self.name = audiofile.display_name(path)
        cue = audiofile.cue_of(path)
        self.offset = cue.start if cue else 0  # where frame 0 sits in the file (CUE tracks)
        self.sent: SentRecord | None = None
        self.heard = False    # the callback has started sending it
        self.dropped = False  # lined up, then dropped before it was heard (the play list changed)
        # Once the decoder has read to its end: the path it lined up next (None = nothing), the file it
        # opened for that (None if it couldn't join on), where reading stopped and how much came out.
        self.lined_up = _NOT_YET
        self.next_source: _Source | None = None
        self.read_to = 0
        self.got = 0

    def close(self):
        if self.sent is not None:
            self.sent.stop()
        self.file.close()


class _Blocks(queue.Queue):
    """The decoded blocks waiting for the sound card. `discard` takes back the ones lined up for a
    file that isn't going to play after all, leaving the rest in order."""

    def discard(self, unwanted) -> int:
        with self.mutex:
            kept = [item for item in self.queue if not unwanted(item)]
            removed = len(self.queue) - len(kept)
            if removed:
                self.queue.clear()
                self.queue.extend(kept)
                self.not_full.notify_all()
        return removed


class _Session:
    """Plays a chain of files on one output stream. Owns the decoder thread and the stream.

    When the decoder reaches the end of a file it asks the player what comes next (`follow`);
    if that file has the same sample rate and channel count it carries straight on into it.
    The audio callback fills each request from whatever is queued, so the last samples of one
    track and the first of the next can share a request: nothing in between. When the next
    file can't join on, the chain ends there and the player opens a new stream for it."""

    def __init__(self, path, gain_ref, on_end, device=None, want_exclusive=False, start_at=0.0, events=None,
                 monitors=None, index: int = -1, follow=None, on_advance=None, on_lost=None):
        first = _Source(path, index)
        first.heard = True
        self._src = first          # the file being heard
        self._reading = first      # the file the decoder is reading (the next one, near the end)
        self._sources = [first]    # every file open in this session
        self._retired: list[_Source] = []  # finished with: for the decoder to close
        self.samplerate = first.samplerate
        self.channels = first.channels
        self._gain_ref = gain_ref  # callable(path) -> the linear gain for that file right now
        self._on_end = on_end
        self._on_lost = on_lost or (lambda session: None)
        self._follow = follow      # callable(index) -> (index, path) of what plays after it, or None
        self._on_advance = on_advance or (lambda session, previous, source: None)
        self.handoff = ""          # why the next track couldn't join on, when it couldn't

        # The buffer holds about BUFFER_SECONDS of music whatever the rate (0.26 s at 384 kHz in a fixed
        # number of blocks would be far too little to ride out a slow read).
        self._queue_blocks = max(QUEUE_BLOCKS, math.ceil(BUFFER_SECONDS * self.samplerate / BLOCK_FRAMES))
        self._near_miss = self._queue_blocks // 4  # buffer this low = a near miss (a warning before anything is heard)
        self._queue: _Blocks = _Blocks(maxsize=self._queue_blocks)
        self._lock = threading.Lock()
        self._generation = 0       # bumps on every seek; stale blocks are dropped
        self._seek_request: int | None = None
        self._relineup_wanted = False
        self._stop = threading.Event()
        self._decoder_started = False
        self.paused = False
        self.frames_played = 0     # in the file being heard
        self._ended = False
        self._pending = None       # the rest of a queued block, carried into the next request
        self._draining = False     # the decoder has queued the end of the chain
        self.note = ""             # e.g. why exclusive mode wasn't possible
        self.levels: tuple[float, ...] = (0.0,) * self.channels
        self.track_peak = 0.0
        self.dropouts = 0
        self.delivered = False     # any music has gone out to the sound card
        self._flowing = False  # audio has been delivered since the last start/seek
        self.health = Health()
        self._primed = False   # the buffer has filled since the last start/seek: margins count from here
        self._in_danger = False
        self._last_callback: float | None = None
        self._running_since: float | None = None  # when the stream (re)started: see stalled()
        self._last_read = 0.0
        self._starved_since: float | None = None  # when the music ran out, during a gap
        self._last_underflow = 0.0
        self._events = events if events is not None else collections.deque(maxlen=50)
        self._tap: dict | None = None  # a short recording of what's sent, for the Windows check
        self._monitors = monitors if monitors is not None else []  # queues for the meters / PCM viewer

        try:
            index = devices.find_device(device)
            if index is None and device is not None:
                if want_exclusive:
                    raise devices.DeviceMissing(f"{device} isn't connected")
                self.note = f"{device} isn't connected — playing on the system default"
                index = devices.find_device(None)
            outputs = devices.max_output_channels(index)
            if outputs and first.channels > outputs:
                raise audiofile.Unsupported(f"it has {first.channels} channels, and the output plays {outputs}")
            self._stream, self.exclusive = self._open_stream(index, want_exclusive)
        except BaseException:
            first.close()
            raise
        self.dtype = "int32" if self.exclusive else "float32"
        self.device_name = self._lookup_device_name()
        self.mixer_rate = devices.mixer_rate(index)
        # What the device really takes in exclusive mode: PortAudio converts to a narrower format by itself.
        self.device_bits = devices.exclusive_bits(self._stream) if self.exclusive else None
        self.health.usual_wait = BLOCK_FRAMES / self.samplerate
        try:
            self._latency = float(self._stream.latency)
        except (TypeError, ValueError, sd.PortAudioError):
            log.debug("the output's latency couldn't be read; taking 0.1 s", exc_info=True)
            self._latency = 0.1
        # Every block sent is fingerprinted, and checked against a second, independent decode.
        first.sent = SentRecord(path, self.samplerate, self.dtype, BLOCK_FRAMES)
        audiofile.set_cancel(first.file, self._superseded)
        if start_at > 0:
            self.seek(start_at)
        self._decoder = threading.Thread(target=self._decode_loop, name="flac-decoder", daemon=True)

    # About the file being heard.
    total_frames = property(lambda self: self._src.total_frames)
    bits = property(lambda self: self._src.bits)
    sent = property(lambda self: self._src.sent)
    _path = property(lambda self: self._src.path)
    _track_name = property(lambda self: self._src.name)

    def _stream_kwargs(self, dtype):
        return dict(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype=dtype,
            blocksize=BLOCK_FRAMES,
            latency="high",
            callback=self._callback,
            finished_callback=self._finished,
        )

    def _open_stream(self, index, want_exclusive):
        if want_exclusive:
            if not devices.exclusive_supported() or index is None:
                self.note = "Bit-perfect mode isn't available on this system"
            else:
                stream = None
                try:
                    stream = sd.OutputStream(
                        device=index,
                        extra_settings=sd.WasapiSettings(exclusive=True),
                        dither_off=True,
                        clip_off=True,
                        **self._stream_kwargs("int32"),
                    )
                    stream.start()
                    devices.holding(stream)
                    return stream, True
                except sd.PortAudioError as exc:
                    log.info("exclusive mode refused: %s", exc)
                    if stream is not None:
                        stream.close(ignore_errors=True)
                    self.note = (f"Device won't take {format_rate(self.samplerate)} in bit-perfect mode "
                                 f"(or another app holds it) — playing shared")
        # Shared. On Windows: WASAPI with auto-convert, so Windows resamples to its mixer rate.
        extra = sd.WasapiSettings(auto_convert=True) if devices.IS_WINDOWS and index is not None else None
        stream = sd.OutputStream(device=index, extra_settings=extra, **self._stream_kwargs("float32"))
        try:
            stream.start()
        except BaseException:
            try:
                stream.close(ignore_errors=True)
            except Exception:  # noqa: BLE001 — already failing: the start's error is the one to report
                log.exception("closing a stream that wouldn't start")
            raise
        devices.holding(stream)
        return stream, False

    def start(self):
        self._decoder_started = True
        self._running_since = time.perf_counter()
        self._decoder.start()
        self._src.sent.start_checking()

    def _lookup_device_name(self) -> str:
        try:
            return sd.query_devices(self._stream.device)["name"]
        except (sd.PortAudioError, ValueError, TypeError):
            log.debug("the output's name couldn't be read", exc_info=True)
            return "Output device"

    def renumber(self, index: int):
        """The play list changed around the song playing: this is where it now sits."""
        self._src.index = index

    def relineup(self):
        """The play list, Up next or repeat changed: the decoder checks what it has lined up to
        follow the track being heard, and lines it up again if it's no longer what plays next."""
        self._relineup_wanted = True

    @property
    def message(self) -> str:
        """What the status line should say about this session: its note, else a warning when the
        device takes fewer bits than the track holds (PortAudio cuts them down: not bit-perfect)."""
        if self.note:
            return self.note
        bits = self._src.bits
        if self.exclusive and self.device_bits and bits and bits > self.device_bits:
            return (f"The device takes {self.device_bits}-bit samples, so this {bits}-bit track loses its "
                    "lowest bits on the way: not bit-perfect")
        return ""

    # -- decoder thread ------------------------------------------------------
    def _decode_loop(self):
        """The decoder thread. It owns every file the session opens: they're closed here when it
        stops, never by another thread while a read may be under way in this one."""
        try:
            self._decode()
        finally:
            with self._lock:
                sources, self._sources = self._sources + self._retired, []
                self._retired = []
            for source in sources:
                source.close()

    def _superseded(self) -> bool:
        """Something newer wants the decoder (a seek, or closing): a slow seek can give up."""
        return self._stop.is_set() or self._seek_request is not None

    def _decode(self):
        reading, position, got, state = self._src, 0, 0, _READING
        self._check_length(reading)
        while not self._stop.is_set():
            with self._lock:
                seek_to, self._seek_request = self._seek_request, None
                relineup, self._relineup_wanted = self._relineup_wanted, False
                if seek_to is not None:  # (seek() has already moved the generation on)
                    reading = self._reading = self._src  # a seek is always within the file being heard
                    seek_to = min(seek_to, max(0, reading.total_frames - 1))
                    self.frames_played = seek_to
                    self._draining = False
                generation = self._generation
            try:
                self._close_retired()
                if seek_to is not None:
                    self._drain()
                    position, got, state = seek_to, 0, _READING
                    if not self._seek_in(reading, seek_to):
                        state = _ENDED  # can't be read from there: the file ends here
                    if self._superseded():
                        continue  # given up part-way: a newer seek (or closing) wins
                elif relineup:
                    moved = self._relineup_now()
                    if moved is not None:
                        (reading, position, got), state = moved, _ENDED
                if state == _IDLE:
                    self._stop.wait(0.05)  # the end of the chain is queued: until a seek, a change or close
                    continue
                if state == _ENDED:
                    reading, position, got, state = self._move_on(reading, position, got, generation)
                    continue
                block, position = self._read(reading, position)
                if block is None:
                    state = _ENDED
                    continue
                self._put((generation, reading, position, block))
                position += len(block)
                got += len(block)
            except Exception as exc:  # noqa: BLE001 — never leave the player waiting on a dead decoder
                log.exception("decoding %s", reading.name)
                self._damaged(reading, position, f"couldn't be decoded ({exc}), so it ended there")
                self._draining = True
                self._put((generation, reading, position, None))  # the end of the chain: the player moves on
                state = _IDLE

    def _read(self, source: _Source, position: int):
        """The next block of `source` from `position`: (block, where it starts), or (None, position)
        at its end. A stretch that won't decode is skipped — each try skipping twice as far — and
        reported; if it can't get past, the file ends there."""
        skip, damaged_at = BLOCK_FRAMES, None
        for attempt in range(DAMAGE_SKIPS + 1):
            if attempt:  # the last try failed: skip on past the damage
                position += skip
                skip *= 2
                if position >= source.total_frames:
                    break
                if not self._reopen(source, position):
                    continue
            started = time.perf_counter()
            try:
                block = audiofile.read(source.file, BLOCK_FRAMES, self.dtype)
            except (RuntimeError, ValueError, OSError) as exc:  # damaged (soundfile's errors are RuntimeErrors)
                if damaged_at is None:
                    damaged_at = position
                    log.warning("reading %s at frame %d: %s", source.name, position, exc)
                continue
            self._last_read = time.perf_counter() - started
            self.health.slowest_read = max(self.health.slowest_read, self._last_read)
            if damaged_at is not None:
                skipped = (position - damaged_at) / self.samplerate
                self._damaged(source, damaged_at, f"couldn't be decoded here, so {skipped:.1f} s was skipped")
            return (block if len(block) else None), position
        self._damaged(source, damaged_at, "couldn't be decoded from here, so the rest of it was skipped")
        return None, position

    def _reopen(self, source: _Source, frame: int) -> bool:
        """Open `source`'s file afresh at `frame`: after a decode error libsndfile's handle won't
        seek any more, though a new one can seek past the damage. False if that fails too."""
        try:
            fresh = audiofile.open(source.path)
        except Exception:  # noqa: BLE001 — (gone, or damaged at the start too)
            log.info("couldn't reopen %s after a decode error", source.name, exc_info=True)
            return False
        try:
            fresh.seek(frame)
        except Exception:  # noqa: BLE001 — still damaged there
            log.info("couldn't seek past the damage in %s", source.name, exc_info=True)
            fresh.close()
            return False
        old, source.file = source.file, fresh
        audiofile.set_cancel(fresh, self._superseded)
        old.close()
        return True

    def _seek_in(self, source: _Source, frame: int) -> bool:
        """Move `source` to `frame`. False if it can't be read from there (damaged)."""
        try:
            source.file.seek(frame)
            return True
        except (RuntimeError, ValueError, OSError) as exc:  # (soundfile's errors are RuntimeErrors)
            log.warning("seeking in %s: %s", source.name, exc)
            self._damaged(source, frame, "couldn't be read from here, so it ended there")
            return False

    def _move_on(self, reading: _Source, position: int, got: int, generation: int):
        """`reading` has ended: carry straight on into the next file when it can join on, else
        queue the end of the chain. Returns the decoder's new (reading, position, got, state)."""
        reading.read_to, reading.got = position, got
        following = self._next_source(reading)
        if following is None:
            self._draining = True
            self._put((generation, reading, position, None))  # the end of the chain
            return reading, position, got, _IDLE
        with self._lock:
            if self._generation != generation:
                return reading, position, got, _ENDED  # a seek came in meanwhile: it wins (next time round)
            self._reading = following
        self._check_length(following)
        if not self._seek_in(following, 0):
            return following, 0, 0, _ENDED
        return following, 0, 0, _READING

    def _next_source(self, after: _Source) -> _Source | None:
        """Open the file that plays after `after`, if it can join on gaplessly. Notes on `after`
        what was lined up (see _relineup_now)."""
        cached, after.next_source = after.next_source, None
        after.lined_up = None
        if not after.got:
            # Nothing came out of it (an empty file, or one that can't be read from there): never chain
            # on from it, or repeat would open the same file again and again. The player moves on itself.
            self.handoff = (f"{after.name} has no music in it" if not after.read_to
                            else f"{after.name} couldn't be read any further")
            self._drop(cached)
            return None
        following = None
        if self._follow is not None:
            try:
                following = self._follow(after.index)
            except Exception:  # noqa: BLE001 — at worst this track ends with a gap
                log.exception("what plays next")
        if following is None:
            self._drop(cached)
            return None
        index, path = following
        after.lined_up = path
        if cached is not None and cached.path == path and not cached.heard and not cached.dropped:
            cached.index = index
            after.next_source = cached
            return cached  # opened already (then a seek took the decoder back)
        self._drop(cached)  # lined up before, but something else plays next now
        try:
            source = _Source(path, index)
        except Exception as exc:  # noqa: BLE001 — at worst this track ends with a gap
            log.warning("reading ahead %s: %s", path, exc)
            self.handoff = "the next file couldn't be read ahead of time"
            return None
        if source.samplerate != self.samplerate:
            self.handoff = (f"the sample rate changes ({format_rate(self.samplerate)} → "
                            f"{format_rate(source.samplerate)}), so the sound device was reopened")
        elif source.channels != self.channels:
            self.handoff = (f"the number of channels changes ({self.channels} → {source.channels}), so the "
                            "sound device was reopened")
        else:
            try:
                source.sent = SentRecord(path, self.samplerate, self.dtype, BLOCK_FRAMES)
                source.sent.start_checking()
            except Exception:  # noqa: BLE001 — (no thread to spare): it just starts with a gap instead
                log.exception("checking %s", path)
                source.close()
                return None
            audiofile.set_cancel(source.file, self._superseded)
            with self._lock:
                self._sources.append(source)
            after.next_source = source
            return source
        source.close()
        return None

    def _relineup_now(self):
        """The play list, Up next or repeat changed. Walk on from the track being heard through
        what's lined up after it; at the first file that isn't what plays next any more, drop
        what follows (it hasn't been heard) and line up again from there. Returns the decoder's
        new (reading, position, got), or None when nothing needs to change — or it's too late:
        the next track or the end of the chain is already going out."""
        after = self._src
        while after.lined_up is not _NOT_YET:  # the decoder has read to its end and lined something up
            if not after.got:
                return None  # nothing came out of it: the chain ends there, whatever plays next
            try:
                want = self._follow(after.index) if self._follow is not None else None
            except Exception:  # noqa: BLE001 — leave things as they are
                log.exception("what plays next")
                return None
            if (want[1] if want else None) != after.lined_up:
                break  # out of date from here
            following = after.next_source
            if following is None:
                return None  # the same as before, and the chain ends after it
            following.index = want[0]  # (where it sits in the list may have changed)
            after = following
        else:
            return None  # not read to its end yet: the decoder asks what's next when it gets there
        pending = self._pending
        if pending is not None and pending[3] is None:
            return None  # the end of the chain is going out already: the player starts what's next itself
        stale = []
        source = after.next_source
        while source is not None:
            stale.append(source)
            source = source.next_source
        for source in stale:
            source.dropped = True  # (the callback skips anything of theirs still queued)
        if any(source.heard for source in stale):
            for source in stale:
                source.dropped = False
            return None
        self._queue.discard(lambda item: item[3] is None or item[1].dropped)
        for source in stale:
            self._drop(source)
        after.lined_up, after.next_source = _NOT_YET, None
        with self._lock:
            self._reading = after
        self._draining = False
        return after, after.read_to, after.got

    def _drop(self, source: _Source | None):
        """Close a file (and what was lined up after it) that won't play after all. Decoder thread only."""
        while source is not None and not source.heard:
            source.dropped = True
            with self._lock:
                if source in self._sources:
                    self._sources.remove(source)
            source.close()
            following = source.next_source
            source.next_source = None
            source = following

    def _close_retired(self):
        with self._lock:
            retired, self._retired = self._retired, []
        for source in retired:
            source.close()

    def _check_length(self, source: _Source):
        """An MP3 with no length header can stop early (see audiofile.mp3_shortfall): say so."""
        try:
            short = audiofile.mp3_shortfall(source.file, source.path)
        except Exception:  # noqa: BLE001 — only a warning: never worth stopping playback for
            log.exception("checking the length of %s", source.name)
            return
        if short is None:
            return
        can, held = (frames / source.samplerate for frames in short)
        cause = (f"this MP3 has no length header, so only its first {_clock(can)} of {_clock(held)} "
                 "can be decoded")
        now = time.time()
        self._events.append(PlaybackEvent(now, source.name, can, "cut short", cause, last=now))
        self.note = f"{source.name}: {cause}"

    def _damaged(self, source: _Source, frame: int, what: str):
        """A stretch of a file that won't decode: noted in the playback report and the status line."""
        at = frame / self.samplerate
        now = time.time()
        self._events.append(PlaybackEvent(now, source.name, at, "damaged", f"the file {what}", last=now))
        self.note = f"{source.name} is damaged at {_clock(at)}: it {what}"

    def _put(self, item):
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=0.05)
                return
            except queue.Full:
                if self._seek_request is not None:
                    return  # a seek is waiting; this block is stale anyway

    def _drain(self):
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    # -- audio thread --------------------------------------------------------
    def _callback(self, outdata, frames, time_info, status):
        now = time.perf_counter()
        if self._last_callback is not None and self._primed:
            self.health.longest_wait = max(self.health.longest_wait, now - self._last_callback)
        self._last_callback = now
        if self._primed:  # how much the driver still had queued when it asked for more
            ahead = time_info.outputBufferDacTime - time_info.currentTime
            if 0 < ahead < 10 and (self.health.driver_low is None or ahead < self.health.driver_low):
                self.health.driver_low = ahead
        if status.output_underflow and self._flowing and now - self._last_underflow > 1.0:
            # The sound card itself ran short (heard as a click or pop). Counted once a second at
            # most: one hiccup often reports on several requests in a row.
            self._last_underflow = now
            self.dropouts += 1
            ready = self._queue.qsize() * BLOCK_FRAMES / self.samplerate
            self._event("dropout", f"the sound driver ran short, though the player had {ready:.1f} s of "
                                   "music ready: Windows or the driver was held up (a busy computer)")
        if self.paused:
            outdata.fill(0)
            return
        generation = self._generation
        filled = 0
        peaks = None
        while filled < frames:
            item, self._pending = self._pending, None
            if item is None or item[0] != generation:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    outdata[filled:] = 0  # decoder fell behind; play silence
                    self._starving(now)
                    break
                if item[0] != generation:
                    continue  # from before a seek
            _, source, start, block = item
            if block is None:  # the end of the chain
                if filled:
                    self._pending = item  # this request's music plays out first; stop on the next one
                    outdata[filled:] = 0
                    break
                outdata.fill(0)
                self._flowing = False
                self._ended = True
                raise sd.CallbackStop
            if source is not self._src:
                if source.dropped:
                    continue  # lined up, then dropped before it was heard (the play list changed)
                self._cross_to(source)
                peaks = None  # the levels and track peak now belong to the new track
            take = min(len(block), frames - filled)
            if take < len(block):
                self._pending = (generation, source, start + take, block[take:])
                block = block[:take]
            out = outdata[filled: filled + take]
            gain = self._gain_ref(source.path)
            apply_gain(block, gain, out=out)  # untouched at full volume (see proof.py)
            source.sent.add(generation, start, out, gain)
            # max/min rather than abs(): abs() of the most negative int32 overflows
            p = np.maximum(block.max(axis=0).astype(np.float64), -block.min(axis=0).astype(np.float64))
            peaks = p if peaks is None else np.maximum(peaks, p)
            if self._monitors:
                self._feed_monitors(source, start, block, out, time_info, now, filled)
            self.frames_played = start + take
            filled += take
            self._arrived(now)
        if peaks is not None:
            if self.dtype == "int32":
                peaks = peaks / 2**31
            self.levels = tuple(float(x) for x in peaks)
            self.track_peak = max(self.track_peak, max(self.levels))
        if filled:
            self._watch_buffer()
            if self._tap is not None:
                self._record_tap(outdata)

    def _starving(self, now: float):
        if self._starved_since is not None:
            return  # the same gap, still going: one dropout, however long
        if self._flowing:  # an empty buffer at start-up or after a seek is expected
            self.dropouts += 1
            self._starved_since = now
            if self._last_read > BLOCK_FRAMES / self.samplerate / 2:
                cause = (f"reading the file was slow (one block took {self._last_read * 1000:.0f} ms): "
                         "a slow or sleeping drive")
            else:
                cause = "the player's decoding fell behind: the computer was busy"
            self._event("dropout", cause)

    def _arrived(self, now: float):
        self._flowing = True
        self.delivered = True
        if self._starved_since is not None:  # the music is back: note how long the gap lasted
            gap = now - self._starved_since
            self._starved_since = None
            if self._events and self._events[-1].kind == "dropout":
                self._events[-1].silent += gap

    def _cross_to(self, source: _Source):
        """The next track's first samples are going out now (gapless): it's the one playing."""
        previous = self._src
        self._src = source
        source.heard = True
        self.track_peak = 0.0
        self.dropouts = 0
        self.health = Health(usual_wait=BLOCK_FRAMES / self.samplerate)
        self._in_danger = False
        self._on_advance(self, previous, source)  # only queues a note for the player's thread

    def _feed_monitors(self, source, start, block, sent, time_info, now, offset):
        """A copy of exactly what was sent (and the file's own samples), stamped with the moment
        it will be heard — for the meters and the PCM viewer. Only while one of them is open."""
        ahead = time_info.outputBufferDacTime - time_info.currentTime
        heard = now + (ahead if 0 < ahead < 10 else self._latency) + offset / self.samplerate
        item = {"frame": start, "offset": source.offset, "dac_time": heard, "rate": self.samplerate,
                "bits": source.bits, "source": block.copy(), "sent": sent.copy(), "track": source.name,
                "path": source.path}
        for feed in list(self._monitors):
            try:
                feed.put_nowait(item)
            except queue.Full:
                pass  # a viewer that's fallen behind just misses a block

    def _watch_buffer(self):
        """Note the buffer's low point, and a near miss when it runs low (before anything is heard)."""
        waiting = self._queue.qsize()
        if not self._primed:
            self._primed = waiting >= self._queue_blocks // 2
            return
        seconds = waiting * BLOCK_FRAMES / self.samplerate
        if self._draining:
            return  # at the very end the buffer empties naturally: nothing more to decode
        if self.health.buffer_low is None or seconds < self.health.buffer_low:
            self.health.buffer_low = seconds
        if waiting < self._near_miss and not self._in_danger:
            self._in_danger = True
            self.health.near_misses += 1
            self._event("near miss", f"only {seconds:.2f} s of music was ready (the last file read took "
                                     f"{self._last_read * 1000:.0f} ms)")
        elif waiting >= self._queue_blocks // 2:
            self._in_danger = False

    def _event(self, kind: str, cause: str):
        now = time.time()
        last = self._events[-1] if self._events else None
        if (kind == "dropout" and last is not None and last.kind == kind and last.track == self._track_name
                and now - last.last < 2.0):
            last.count += 1  # the same stutter carrying on
            last.last, last.cause = now, cause
            return
        self._events.append(PlaybackEvent(now, self._track_name, self.position, kind, cause, last=now))

    # -- recording what's sent, for the Windows check --------------------------
    def start_tap(self, seconds: float) -> dict:
        tap = {"audio": np.zeros((int(seconds * self.samplerate), self.channels), np.float32),
               "filled": 0, "done": threading.Event()}
        self._tap = tap
        return tap

    def _record_tap(self, outdata):
        tap = self._tap
        take = min(len(tap["audio"]) - tap["filled"], len(outdata))
        tap["audio"][tap["filled"]: tap["filled"] + take] = outdata[:take]
        tap["filled"] += take
        if tap["filled"] >= len(tap["audio"]):
            self._tap = None
            tap["done"].set()

    def _finished(self):
        if self._stop.is_set() or self.paused:
            return  # closed, or paused (stopping the stream for a pause ends up here too)
        if self._ended:
            self._on_end(self)
        else:
            self._on_lost(self)  # the stream stopped by itself: the device has gone

    def stalled(self, now: float) -> bool:
        """Playing, but the sound card hasn't asked for anything for STALL_SECONDS: the device
        has gone (unplugged, switched off, asleep) without PortAudio saying so."""
        if self.paused or self._ended or self._running_since is None:
            return False
        last = self._last_callback if self._last_callback is not None else self._running_since
        return now - max(last, self._running_since) > STALL_SECONDS

    # -- control -------------------------------------------------------------
    def set_paused(self, paused: bool):
        """Pausing stops the stream so the sound device is free for other apps."""
        if paused == self.paused:
            return
        self.paused = paused
        self._last_callback = None  # the pause itself isn't a late request
        self._primed = False
        try:
            if paused:
                self._stream.stop()
            else:
                self._running_since = time.perf_counter()
                self._stream.start()
        except sd.PortAudioError as exc:
            log.exception("pause/resume")
            if not paused:
                self.paused = True  # it isn't playing: say so, rather than show "playing" in silence
                self.note = f"The sound device wouldn't start again ({exc}): check Output, then press play"

    def seek(self, seconds: float):
        frame = int(max(0.0, seconds) * self.samplerate)
        frame = min(frame, max(0, self.total_frames - 1))
        with self._lock:
            self._seek_request = frame
            self._generation += 1  # from now on the callback ignores anything queued before the seek
            self.frames_played = frame
            self._flowing = False
            self._primed = False
            self._starved_since = None

    def retire(self, source: _Source):
        """A file that's finished playing: the decoder closes it (called from the player's thread)."""
        with self._lock:
            if source is self._src or source is self._reading or not source.heard or source not in self._sources:
                return  # still in use (or already closed): the session closes it at the end
            self._sources.remove(source)
            self._retired.append(source)

    @property
    def position(self) -> float:
        return self.frames_played / self.samplerate

    @property
    def buffer_fill(self) -> float:
        return self._queue.qsize() / self._queue_blocks

    @property
    def duration(self) -> float:
        return self.total_frames / self.samplerate

    def close(self):
        """Stop the stream at once. The decoder winds down on its own thread and closes the files
        (usually within milliseconds; waited for briefly, so they're normally closed on return)."""
        self._stop.set()
        try:
            self._stream.abort()
            self._stream.close()
        except sd.PortAudioError:
            log.exception("closing output stream")
        finally:
            devices.released(self._stream)
        if not self._decoder_started:
            with self._lock:
                sources, self._sources = self._sources, []
            for source in sources:
                source.close()
        elif self._decoder is not threading.current_thread():
            self._decoder.join(timeout=CLOSE_WAIT)


class Queued:
    """One song in the Up next list. Its own object, so the same song queued twice is two entries;
    a session's `_Source.index` is one of these (instead of a list position) while it's playing."""

    __slots__ = ("track",)

    def __init__(self, track: Track):
        self.track = track

    def __repr__(self):
        return f"Queued({self.track.title!r})"


class Player:
    def __init__(self):
        self.tracks: list[Track] = []
        self.index = -1
        # Up next ("Play next" / "Add to queue"): songs that play before the list carries on.
        # Replaced whole on every change, so the decoder can read it without the lock. While a
        # queued song plays, `index` stays on the list's song it interrupted (where the list
        # carries on from) and `queued_now` is the song.
        self.queue: list[Queued] = []
        self.queued_now: Queued | None = None
        self._volume = 0.8
        self._exclusive = False
        self._device: str | None = None  # output device name; None = system default
        self.hardware_volume: bool | None = None  # see refresh_hardware_volume()
        self._session: _Session | None = None
        self._lock = threading.RLock()
        self._message = ""
        # Play order: the list's own order, or a shuffled one. Repeat "all" wraps round it;
        # "one" plays the same track again when it ends.
        self.shuffle = False
        self.repeat = "off"
        # Clipping guard: a small turn-down for loud tracks (see set_trim), by file.
        self._trims: dict = {}
        self._order: list[int] = []
        self.transition = ""  # how the track playing began (Status.transition)
        self.events: collections.deque = collections.deque(maxlen=50)  # dropouts / near misses, all tracks
        self._monitors: list[queue.Queue] = []  # see monitor()
        self._suspended_at: float | None = None  # see suspend(): paused with the device let go
        self._silent_run = 0  # tracks in a row that ended with nothing heard (see _ended)
        # ("end", session) / ("advance", session, previous, new) / ("lost", session)
        self._ended_events: queue.Queue = queue.Queue()
        threading.Thread(target=self._advance_loop, name="player-control", daemon=True).start()

    # -- loading -------------------------------------------------------------
    def set_tracks(self, tracks: list[Track], keep_playing: bool = False):
        """Replace the playlist. keep_playing: if the current song is in the new list,
        it carries on uninterrupted and the album continues from there."""
        with self._lock:
            current = None
            if keep_playing and self._session and 0 <= self.index < len(self.tracks):
                path = self.tracks[self.index].path
                current = next((i for i, t in enumerate(tracks) if t.path == path), None)
            if keep_playing and self._session and self.queued_now is not None:
                # A queued song is playing: it carries on; the list carries on after it from the
                # same song as before (if it's still there).
                path = self.tracks[self.index].path if 0 <= self.index < len(self.tracks) else None
                self.tracks = list(tracks)
                self.index = next((i for i, t in enumerate(tracks) if t.path == path), -1)
                self._rebuild_order(first=self.index if self.index >= 0 else None)
                self._relineup()
                return
            if current is None:
                self._close_session()
            else:
                self._session.renumber(current)
            self.tracks = list(tracks)
            self.index = current if current is not None else (0 if self.tracks else -1)
            self._rebuild_order(first=current)
            self._message = ""
            self._relineup()

    def _relineup(self):
        """What plays next may have changed: the session re-checks the track it has lined up."""
        if self._session is not None:
            self._session.relineup()

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

    def clear_queue(self):
        with self._lock:
            self.queue = []
            self._relineup()

    def play_queued(self, position: int):
        """Play a song from Up next straight away (it leaves the list)."""
        with self._lock:
            queue = self.queue
            if 0 <= position < len(queue):
                self._start(queue[position])

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

    def trim_for(self, path) -> float:
        return self._trims.get(path, 1.0)

    def _current_trim(self) -> float:
        track = self._track_at(self._current())
        return self.trim_for(track.path) if track is not None else 1.0

    def set_repeat(self, mode: str):
        if mode in REPEAT_MODES:
            with self._lock:
                self.repeat = mode
                self._relineup()

    def _rebuild_order(self, first: int | None = None):
        count = len(self.tracks)
        if not self.shuffle:
            self._order = list(range(count))
            return
        rest = [i for i in range(count) if i != first]
        random.shuffle(rest)
        self._order = ([first] if first is not None and 0 <= first < count else []) + rest

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
        snapshot."""
        following = self._next_after(index)
        track = self._track_at(following)
        if track is None:
            return None
        return following, track.path

    # -- transport -----------------------------------------------------------
    def play(self, index: int | None = None, start_at: float = 0.0):
        """Start `index` (or resume / start the current track), optionally part-way in."""
        with self._lock:
            if not self.tracks and not (index is None and (self.queue or self.queued_now)):
                return
            if index is None:
                if self._session and self._session.paused:
                    self._session.set_paused(False)
                    return
                if self._session:
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
            if self._session:
                self._session.set_paused(True)

    def suspend(self) -> bool:
        """Pause and let go of the sound device, for something else to play on it (the ABX test).
        Pausing only stops the stream; closing it is the sure way to free the device (bit-perfect
        mode included) for another stream. The player shows as paused at the same spot, and play
        carries on from there."""
        with self._lock:
            session = self._session
            if session is None:
                return False
            position, queued = session.position, self.queued_now
            self._close_session()
            self._suspended_at, self.queued_now = position, queued  # play() carries on with the same song
            return True

    def toggle_pause(self):
        with self._lock:
            if self._session and not self._session.paused:
                self._session.set_paused(True)
            else:
                self.play()

    def stop(self):
        with self._lock:
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
        volume control. Call before switching bit-perfect on or changing device."""
        self.hardware_volume = devices.has_hardware_volume(self._device if device is self._CURRENT else device)
        return self.hardware_volume

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
            output = None
            if session is None and self._suspended_at is not None and track is not None:
                state = State.PAUSED  # suspended (see suspend): paused, with the device let go
                position, duration = self._suspended_at, track.duration
            elif session is None:
                state = State.STOPPED
                position, duration = 0.0, (track.duration if track else 0.0)
            else:
                state = State.PAUSED if session.paused else State.PLAYING
                position, duration = session.position, session.duration
                output = OutputInfo(session.device_name, session.samplerate, session.mixer_rate, session.exclusive,
                                    self.hardware_volume if session.exclusive else None, session.device_bits)
            message = self._message or (session.message if session else "")
            # index -1 while a queued song plays: it isn't one of the list's songs
            index = -1 if self.queued_now is not None else self.index
            status = Status(state, index, track, min(position, duration), duration,
                            message, output, self._volume ** 2, self._current_trim())
            if session is not None:
                status.levels = session.levels if not session.paused else (0.0,) * session.channels
                status.track_peak = session.track_peak
                status.buffer = session.buffer_fill
                status.dropouts = session.dropouts
                status.proof = session.sent.summary()
                status.health = Health(**vars(session.health))
                status.transition = self.transition
            status.events = list(self.events)
            return status

    def close(self):
        self.stop()

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
        """The gain for a file (the track playing if none given): volume, then its clipping guard."""
        if path is None:
            return self._volume ** 2 * self._current_trim()
        return self._volume ** 2 * self.trim_for(path)

    def _start(self, index, start_at: float = 0.0, transition: str = "started"):
        """Start playing tracks[index] (or a queued song, which then leaves Up next); skips
        forward past files that won't open."""
        self._close_session()
        self._message = ""
        self.transition = transition
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
            try:
                session = _Session(track.path, self._gain, lambda s: self._ended_events.put(("end", s)),
                                   device=self._device, want_exclusive=self._exclusive, start_at=start_at,
                                   events=self.events, monitors=self._monitors, index=index, follow=self._follow,
                                   on_advance=lambda s, previous, new: self._ended_events.put(
                                       ("advance", s, previous, new)),
                                   on_lost=lambda s: self._ended_events.put(("lost", s)))
                session.start()
            except sd.PortAudioError as exc:
                # The sound device, not the file, is the problem: skipping tracks won't help. Shown
                # paused at the same spot, so play tries again from there.
                log.warning("cannot open output for %s: %s", track.path, exc)
                if isinstance(exc, devices.DeviceMissing):
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

    def _close_session(self):
        self._suspended_at = None
        self.queued_now = None
        if self._session:
            self._session.close()
            self._session = None

    def _joined(self, session: _Session, previous: _Source, source: _Source):
        """A track joined on gaplessly: it's now the one playing."""
        tracks = self.tracks
        if isinstance(source.index, Queued):  # a song from Up next: it leaves the list now it's heard
            self.queue = [q for q in self.queue if q is not source.index]
            self.queued_now = source.index
        elif 0 <= source.index < len(tracks) and tracks[source.index].path == source.path:
            self.index, self.queued_now = source.index, None
        else:  # the list changed while it was lined up
            index = next((i for i, t in enumerate(tracks) if t.path == source.path), None)
            if index is not None:
                self.index = source.index = index
            else:  # taken out of the list: what follows carries on from where the list was
                source.index = self.index
            self.queued_now = None
        self.transition = "gapless"
        self._message = ""
        session.retire(previous)

    def _lost(self, session: _Session):
        """The device stopped taking audio (unplugged, switched off, asleep): let go of it and show
        paused at the same spot, saying why. Never carry on on another device by itself — in
        bit-perfect mode that could be far too loud."""
        position, queued, name = session.position, self.queued_now, session.device_name
        self._close_session()
        self._suspended_at, self.queued_now = position, queued  # play() carries on with the same song
        self._message = f"{name} stopped playing (disconnected?): press play to carry on"

    def _ended(self, session: _Session):
        """The chain of tracks has played out: start what's next (with a gap), or stop at the end."""
        current = self._current()
        following = self._next_after(current)
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
            self._start(following, transition=session.handoff or "started")
        else:
            self._close_session()
            self.index = self._order[0] if self._order else 0  # ready to play it again
            self.queued_now = None
            self._message = "End of album"

    def _advance_loop(self):
        # Runs on its own thread: PortAudio's finished-callback must not open streams itself. Between
        # events it watches for a device that has stopped asking for audio without saying so.
        _init_com_for_this_thread()
        while True:
            try:
                kind, session, *details = self._ended_events.get(timeout=WATCH_SECONDS)
            except queue.Empty:
                kind, session, details = "watch", None, []
            try:
                with self._lock:
                    if kind == "watch":
                        session = self._session
                        if session is not None and session.stalled(time.perf_counter()):
                            log.warning("%s: no requests for audio in %.0f s", session.device_name, STALL_SECONDS)
                            self._lost(session)
                        continue
                    if session is not self._session:
                        continue  # user already moved on
                    if kind == "advance":
                        self._joined(session, *details)
                    elif kind == "lost":
                        self._lost(session)
                    else:
                        self._ended(session)
            except Exception:  # noqa: BLE001 — this thread must never die: nothing would move on again
                log.exception("player control")
                self._message = "Something went wrong moving on to the next track: press play to carry on"

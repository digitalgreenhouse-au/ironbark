"""One open output stream: the decoder thread, the block queue and the sound card's callback.

Each stream gets a _Session: a decoder thread reads blocks from the file into a queue, and the
sound-card callback fills each request from that queue. Reading the file off the audio thread keeps
playback smooth even when the window is busy. The decoder thread owns the files it reads: it alone
closes them, so no file is ever closed under a read in progress.

Gapless: near the end of a track the decoder carries straight on into the next one when it has
the same sample rate and channel count, on the same stream, so there's no silence between them.
A track that can't join on (a different sample rate, say) gets a new stream, with the short gap
that reopening the device takes; the player (engine.py) says why. When the play list, Up next or
repeat changes after the next track was lined up (but before it's heard), it's lined up again.

The Player in engine.py owns sessions and is what the rest of the program talks to; it re-exports
everything here, so `engine.BLOCK_FRAMES` and `engine._Session` still name the same things.
"""
from __future__ import annotations

import collections
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import sounddevice as sd

from . import audiofile, devices, platforms
from .library import format_rate
from .proof import SentRecord, apply_gain

_SYSTEM = "Windows" if platforms.IS_WINDOWS else "the system"   # (named in what the player says)

log = logging.getLogger(__name__)


BLOCK_FRAMES = 4096   # frames per sound-card callback


QUEUE_BLOCKS = 24     # decoded blocks buffered ahead, at the least (~2.2 s at 44.1 kHz)


BUFFER_SECONDS = 2.2  # ...or this much music, whichever is more (hi-res files need more blocks)


BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32,  # integer files' sample sizes
        "ALAC_16": 16, "ALAC_20": 20, "ALAC_24": 24, "ALAC_32": 32}


DAMAGE_SKIPS = 5      # a stretch that won't decode: skip on past it, twice as far each try, this many tries


STALL_SECONDS = 3.0   # playing, but no request from the sound card for this long: the device has gone


CLOSE_WAIT = 0.25     # how long closing waits for the decoder to let go of its files


# Waiting for a copy still downloading (the network playback cache):
PATIENCE_MIN = 0.05       # how long one read may wait for its bytes: this fraction of the music


PATIENCE_MAX = 0.5        # queued, within these bounds (0.5 s with the usual 2.2 s buffer, so it


PATIENCE_FRACTION = 0.4   # gives up while most of the buffer is still there to ride it out)


STALL_COMMIT_S = 0.5      # this little music left queued: commit to an audible pause


STALL_STEP_WINDOW_S = 300.0  # pauses on a track within this long make each resume wait longer


JOIN_GIVE_UP_S = 0.3      # the next song still downloading: joined gaplessly only while this much is queued


_NOT_YET = object()   # _Source.lined_up before the decoder has read to the file's end


PACKED_24_BITS = 24   # a device that takes fewer bits than this from int32 is asked for packed 24-bit too


def pack_int24(samples: np.ndarray, raw) -> None:
    """Left-justified int32 samples into `raw` (a sound card's buffer) as packed 24-bit samples:
    three bytes each, little-endian — each sample's top three bytes. A track of 24 bits or fewer
    has nothing in the bottom byte, so it goes out exactly; anything deeper loses that byte here,
    as it would in PortAudio's own conversion."""
    out = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
    out[...] = samples.reshape(-1).view(np.uint8).reshape(-1, 4)[:, 1:]


def _close_unstarted(stream) -> None:
    """Close a stream opened only to see what format the device takes."""
    try:
        stream.close()
    except sd.PortAudioError:
        log.exception("closing a stream that was only tried")


_AWAIT = object()     # _next_source: the next song is still downloading (the decoder waits for it)


QUIET_WINDOW_S = 0.05   # s: how far back from a cut the quietest sample is looked for

_READING, _ENDED, _IDLE = "reading", "ended", "idle"   # the decoder's state (see _Session._decode)


_WAITING, _AWAIT_NEXT = "waiting", "await-next"        # ... for the network: this song's bytes, the next song's


def quiet_point(block, rate: int, window_s: float = QUIET_WINDOW_S) -> int:
    """Index k in `block` (frames × channels, integer or float) within its last `window_s` where the
    loudest channel is nearest zero — where to cut before a pause, so the last sample heard is
    a quiet one. The latest such sample, to re-decode as little as possible."""
    frames = len(block)
    if frames == 0:
        return 0
    span = max(1, min(frames, int(round(window_s * rate))))
    tail = np.asarray(block[frames - span:])
    level = np.abs(tail.astype(np.float64))
    if level.ndim > 1:
        level = level.max(axis=1)
    lowest = level.min()
    k = int(np.flatnonzero(level == lowest)[-1])
    return frames - span + k


@dataclass
class WaitInfo:
    """A session waiting for the network: the decoder reached bytes not downloaded yet."""
    since: float               # time.perf_counter() when the read gave up
    frame: int                 # where playback carries on from (once committed: the cut)
    reason: str                # audiofile.Waiting's reason; "lost" once the copy here is known to be gone
    serial: int = 0            # the stall marker's number, once committed
    committed: bool = False    # the pause will be heard: the stall marker is queued
    silent_since: float | None = None   # the first request answered with silence (perf_counter)
    seek: bool = False         # it began at a seek (the bytes there haven't come yet)
    source: object = field(default=None, repr=False, compare=False)   # the song it waits in (_Source)


class _Stall:
    """Queued after the last music before a pause for the network (see _Session._commit). The
    callback plays exact zeros on it until the decoder has carried on from the cut."""
    __slots__ = ("serial", "silent_since")

    def __init__(self, serial: int):
        self.serial = serial
        self.silent_since: float | None = None   # the first request answered with silence on it


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
    kind: str        # "dropout", "near miss", "damaged", "cut short", "network pause" or "fetched again"
                     # (the last two are never dropouts)
    cause: str
    count: int = 1   # a stutter (gaps less than 2 s apart) is one event with a count
    silent: float = 0.0  # seconds of silence, all its gaps together
    last: float = 0.0    # time.time() of its latest gap


def _init_com_for_this_thread() -> None:
    """WASAPI exclusive streams opened from a thread without COM set up fail with
    "Invalid device" — and leave PortAudio refusing exclusive mode on every thread after."""
    if platforms.IS_WINDOWS:
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
    the same file joins on gaplessly like any other.

    `local` is what to read (audiofile.LocalTrack: the file itself, or one version of a cached
    copy, held — leased — while it plays); `path` is the song's identity, as the play list has it,
    for everything keyed by song (trims, levelling, what's lined up, the meters). Every reopen and
    the proof read the same `local`. A handle on a copy still downloading that gave up waiting is
    `dead`: it's opened afresh before it's used again."""

    def __init__(self, local, index: int = -1):
        self.local = audiofile.LocalTrack.plain(local)
        self.path, self.index = self.local.source, index
        self.local.acquire()
        try:
            self.file = audiofile.open(self.local)  # the one decode path (a CUE track: just its stretch)
        except BaseException:
            self.local.release()
            raise
        self._closed = False
        self.dead = False
        self.stalls: list[float] = []   # when each pause for the network began (perf_counter)
        self.length_due = False         # an MP3 copy still downloading: its length is checked once it's complete
        self.samplerate, self.channels, self.total_frames = self.file.samplerate, self.file.channels, self.file.frames
        self.bits = BITS.get(self.file.subtype)  # None for floating-point files and MP3s
        self.name = audiofile.display_name(self.path)
        cue = audiofile.cue_of(self.path)
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
        if self._closed:
            return
        self._closed = True
        if self.sent is not None:
            self.sent.stop()
        try:
            self.file.close()
        finally:
            self.local.release()

    def recent_stalls(self, now: float) -> int:
        return sum(1 for when in self.stalls if now - when <= STALL_STEP_WINDOW_S)


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
    file can't join on, the chain ends there and the player opens a new stream for it.

    `path` is the first song (a path, or the audiofile.LocalTrack the preparer gave for it).
    `preparer` (the network playback cache; None = every song read directly) is asked about the
    next song before it's opened, and whether a song waiting for the network may carry on."""

    def __init__(self, path, gain_ref, on_end, device=None, want_exclusive=False, start_at=0.0, events=None,
                 monitors=None, index: int = -1, follow=None, on_advance=None, on_lost=None, preparer=None,
                 on_refetch=None):
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
        self._on_refetch = on_refetch or (lambda session: None)   # the copy's bytes went missing: start it again
        self._refetch_asked = False
        self._follow = follow      # callable(index) -> (index, path) of what plays after it, or None
        self._on_advance = on_advance or (lambda session, previous, source: None)
        self._preparer = preparer
        self.handoff = ""          # why the next track couldn't join on, when it couldn't
        # Waiting for the network: only the decoder sets it (see `waiting`); the callback notes the
        # markers it has played past instead of clearing it, so it can never wipe out a wait the
        # decoder has just begun.
        self._wait: WaitInfo | None = None
        self._serial = 0           # stall markers queued so far
        self._resumed_serial = 0   # the decoder has carried on after every stall up to this one
        self._passed_serial = 0    # the callback has played on past every stall marker up to this one
        self._awaiting: tuple | None = None    # (index, path) of a next song still downloading

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
        self._nudge = threading.Event()   # wakes the decoder from a wait (a seek, a change, closing)
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
        # The format the stream was opened with: "float32" (shared), "int32", or "int24" (packed
        # 24-bit, for a device that takes nothing wider; see _open_exclusive). The samples
        # themselves are int32 in either exclusive format (self.dtype): int24 packs them last thing.
        self.stream_format = "float32"
        self._unpacked = np.zeros((BLOCK_FRAMES, self.channels), np.int32)   # int24: the samples before packing

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
        self.dtype = "int32" if self.exclusive else "float32"   # the samples (int24 streams pack int32's)
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
        # Every block sent is fingerprinted, and checked against a second, independent decode (of
        # the very same bytes: a cached copy's same version).
        first.sent = SentRecord(first.local, self.samplerate, self.dtype, BLOCK_FRAMES)
        audiofile.set_cancel(first.file, self._superseded)
        if start_at > 0:
            self.seek(start_at)
        self._decoder = threading.Thread(target=self._decode_loop, name="flac-decoder", daemon=True)

    @property
    def waiting(self) -> WaitInfo | None:
        """Waiting for the network (the decoder reached bytes not downloaded yet), or None — also
        once a committed pause's marker has been played past."""
        wait = self._wait
        if wait is not None and wait.committed and wait.serial <= self._passed_serial:
            return None
        return wait

    @waiting.setter
    def waiting(self, wait: WaitInfo | None) -> None:
        self._wait = wait   # (the decoder's thread only)

    # About the file being heard.
    total_frames = property(lambda self: self._src.total_frames)
    bits = property(lambda self: self._src.bits)
    sent = property(lambda self: self._src.sent)
    _path = property(lambda self: self._src.path)
    _track_name = property(lambda self: self._src.name)

    def _stream_kwargs(self, dtype, callback=None):
        return dict(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype=dtype,
            blocksize=BLOCK_FRAMES,
            latency="high",
            callback=callback or self._callback,
            finished_callback=self._finished,
        )

    def _exclusive_stream(self, index, fmt: str):
        """An exclusive stream (opened, not started) taking `fmt`: "int32", or "int24" — packed
        3-byte samples, which numpy has no type for, so a raw stream fed by _packed_callback."""
        if fmt == "int24":
            kind, callback = sd.RawOutputStream, self._packed_callback
        else:
            kind, callback = sd.OutputStream, self._callback
        return kind(device=index, extra_settings=sd.WasapiSettings(exclusive=True), dither_off=True,
                    clip_off=True, **self._stream_kwargs(fmt, callback))

    def _open_exclusive(self, index):
        """(stream, format): the exclusive stream that gets the most bits to the device. int32 is
        asked for first. A device that refuses it gets PortAudio's nearest format instead, but
        PortAudio goes straight to 16-bit and never tries packed 24-bit — the only 24-bit format
        many USB Audio Class 1 devices take. So when int32 lands below 24 bits, packed 24-bit is
        tried too, and kept if the device takes more bits that way. An exclusive stream holds its
        device, so each is closed before the next is opened; none has started (nothing is heard)."""
        stream = self._exclusive_stream(index, "int32")
        bits = devices.exclusive_bits(stream)
        if bits is None or bits >= PACKED_24_BITS:
            return stream, "int32"   # (None: can't tell, so nothing to gain by trying)
        _close_unstarted(stream)
        try:
            packed = self._exclusive_stream(index, "int24")
        except sd.PortAudioError as exc:
            log.info("packed 24-bit refused in exclusive mode: %s", exc)
        else:
            packed_bits = devices.exclusive_bits(packed)
            if packed_bits is not None and packed_bits > bits:
                log.info("the device takes %d-bit samples as packed 24-bit (%d-bit from int32)", packed_bits, bits)
                return packed, "int24"
            _close_unstarted(packed)
        return self._exclusive_stream(index, "int32"), "int32"

    def _open_stream(self, index, want_exclusive):
        if want_exclusive:
            if not devices.exclusive_supported() or index is None:
                self.note = "Bit-perfect mode isn't available on this system"
            else:
                stream = None
                try:
                    stream, self.stream_format = self._open_exclusive(index)
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
        self.stream_format = "float32"
        extra =sd.WasapiSettings(auto_convert=True) if devices.IS_WINDOWS and index is not None else None
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
        self._nudge.set()

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
                    self.waiting, self._awaiting = None, None   # (a pause for the network starts again from here)
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
                        self._awaiting = None
                if state == _IDLE:
                    self._stop.wait(0.05)  # the end of the chain is queued: until a seek, a change or close
                    continue
                if state == _WAITING:
                    state, frame = self._wait_step(reading, generation)
                    if frame is not None:
                        position = frame   # (reopened where it stopped: the cut, once committed)
                    if state == _WAITING:
                        self._nap()
                    continue
                if state == _AWAIT_NEXT:
                    moved = self._await_step(reading, position, got, generation)
                    if moved is None:
                        self._nap()
                    else:
                        reading, position, got, state = moved
                    continue
                if state == _ENDED:
                    reading, position, got, state = self._move_on(reading, position, got, generation)
                    continue
                block, position = self._read(reading, position)
                if block is None:
                    state = _ENDED
                    continue
                if reading.length_due and reading.local.ready_path() is not None:
                    self._check_length(reading)   # (its copy has just become complete)
                self._put((generation, reading, position, block))
                position += len(block)
                got += len(block)
            except audiofile.Waiting as wait:   # (before Exception: waiting for the network is never damage)
                if reading is not self._reading:   # (the next song, just lined up)
                    reading, got = self._reading, 0
                reading.dead = True
                self._begin_wait(reading, wait, seek=seek_to is not None)
                state = _WAITING
            except Exception as exc:  # noqa: BLE001 — never leave the player waiting on a dead decoder
                log.exception("decoding %s", reading.name)
                self._damaged(reading, position, f"couldn't be decoded ({exc}), so it ended there")
                self._draining = True
                self._put((generation, reading, position, None))  # the end of the chain: the player moves on
                state = _IDLE

    def _read(self, source: _Source, position: int):
        """The next block of `source` from `position`: (block, where it starts), or (None, position)
        at its end. A stretch that won't decode is skipped — each try skipping twice as far — and
        reported; if it can't get past, the file ends there. A copy still downloading that hasn't
        the bytes in time raises audiofile.Waiting (never taken for damage)."""
        skip, damaged_at = BLOCK_FRAMES, None
        if source.dead and not self._reopen(source, position):
            self._damaged(source, position, "couldn't be read from here, so it ended there")
            return None, position
        for attempt in range(DAMAGE_SKIPS + 1):
            if attempt:  # the last try failed: skip on past the damage
                position += skip
                skip *= 2
                if position >= source.total_frames:
                    break
                if not self._reopen(source, position):
                    continue
            started = time.perf_counter()
            # (A copy still downloading: each read may wait this long for its bytes, so it gives up
            # while most of the buffer is still there to cover it. Nothing for other files.)
            audiofile.set_patience(source.file, min(PATIENCE_MAX, max(PATIENCE_MIN,
                                                                       PATIENCE_FRACTION * self._queued_seconds())))
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
        """Open `source`'s file afresh at `frame` (the same bytes: the same version of a cached
        copy): after a decode error libsndfile's handle won't seek any more, though a new one can
        seek past the damage; a copy's handle that gave up waiting is dead. False if that fails
        too; audiofile.Waiting (at `frame`) if the bytes there haven't come yet."""
        try:
            fresh = audiofile.open(source.local, cancel=self._superseded)
        except audiofile.Waiting as wait:
            raise audiofile.Waiting(frame, wait.reason) from wait
        except Exception:  # noqa: BLE001 — (gone, or damaged at the start too)
            log.info("couldn't reopen %s after a decode error", source.name, exc_info=True)
            return False
        try:
            fresh.seek(frame)
        except audiofile.Waiting:
            fresh.close()
            raise
        except Exception:  # noqa: BLE001 — still damaged there
            log.info("couldn't seek past the damage in %s", source.name, exc_info=True)
            fresh.close()
            return False
        old, source.file = source.file, fresh
        source.dead = False
        audiofile.set_cancel(fresh, self._superseded)
        old.close()
        return True

    def _seek_in(self, source: _Source, frame: int) -> bool:
        """Move `source` to `frame`. False if it can't be read from there (damaged)."""
        try:
            if source.dead:
                if self._reopen(source, frame):
                    return True
                raise RuntimeError("it couldn't be opened again")
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
        if following is _AWAIT:
            return reading, position, got, _AWAIT_NEXT   # the next song is still downloading
        return self._carry_into(reading, following, position, got, generation)

    def _carry_into(self, reading: _Source, following: _Source | None, position: int, got: int, generation: int):
        """Carry on from `reading` into `following` (None: the chain ends after `reading`)."""
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

    def _next_source(self, after: _Source):
        """Open the file that plays after `after`, if it can join on gaplessly. Notes on `after`
        what was lined up (see _relineup_now). _AWAIT when the next song is on a network drive and
        not enough of it has downloaded yet (the decoder then waits for it: _await_step)."""
        cached, after.next_source = after.next_source, None
        after.lined_up = None
        self._awaiting = None
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
        local = self._prepare_next(path, "next")
        if local is _AWAIT:
            self._awaiting = (index, path)
            return _AWAIT
        if local is None:
            return None
        return self._open_next(after, index, path, local)

    def _prepare_next(self, path, role: str):
        """The preparer's answer for the next song: a LocalTrack to open, _AWAIT (still downloading)
        or None (it can't be played: the chain ends, and the player says why when it gets there)."""
        if self._preparer is None:
            return audiofile.LocalTrack.plain(path)
        try:
            local = self._preparer.prepare(path, role=role, at=0.0)
        except Exception:  # noqa: BLE001 — at worst this track ends with a gap
            log.exception("preparing %s", path)
            return None
        if isinstance(local, audiofile.LocalTrack):
            return local
        if hasattr(local, "permanent"):   # Unavailable
            self.handoff = f"the next song can't be played ({local.reason})"
            return None
        return _AWAIT

    def _open_next(self, after: _Source, index, path, local) -> _Source | None:
        try:
            source = _Source(local, index)
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
                source.sent = SentRecord(source.local, self.samplerate, self.dtype, BLOCK_FRAMES)
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
        wait = self._wait
        if wait is not None and wait.source in stale:
            # It was waiting for the network in a song that won't play now: that wait is over (a stall
            # marker of it has gone with the song's blocks), and the next song's waits are its own.
            if wait.committed:
                self._resumed_serial = max(self._resumed_serial, wait.serial)
            self.waiting = None
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

    # -- waiting for the network (a copy still downloading) ---------------------
    def _nap(self):
        """The decoder waits for the network: a look every 50 ms, sooner after a seek, change or close."""
        self._nudge.wait(0.05)
        self._nudge.clear()

    def _wake(self):
        """A seek, a change or closing: wake the decoder, even from a read waiting for its bytes
        (that read then gives up at once: something newer wants the file)."""
        self._nudge.set()
        gate = getattr(self._reading.file, "gate", None)
        if isinstance(gate, audiofile.Gate):
            gate.wake()

    def _queued_seconds(self) -> float:
        """Music decoded and waiting for the sound card (blocks are BLOCK_FRAMES long but at ends)."""
        return self._queue.qsize() * BLOCK_FRAMES / self.samplerate

    def _begin_wait(self, source: _Source, wait: audiofile.Waiting, seek: bool = False):
        """A read of `source` gave up waiting for its bytes at `wait.frame`."""
        current = self.waiting
        resumed = current is not None and current.committed and self._resumed_serial >= current.serial
        if current is not None and not seek and not resumed:
            current.reason = wait.reason   # (still the same pause: it carries on from the same spot)
            return
        # A new wait (the decoder may already have carried on from the last pause, its marker still
        # queued: that one is played past, and this one has its own spot to carry on from).
        self.waiting = WaitInfo(time.perf_counter(), wait.frame, wait.reason, seek=seek, source=source)

    def _wait_step(self, source: _Source, generation: int):
        """One look, every 50 ms, while `source` waits for the network. Before the pause is
        committed the music already queued keeps playing: once the bytes are here (enough for a
        few seconds) the decoder carries on unheard. When the queue is nearly empty the pause is
        committed (_commit); after that it carries on once the start rule holds at the cut (asking
        for more after each recent pause). A song whose bytes will never come (the file changed
        or went away) is cut short there. Returns the decoder's (state, frame to read from)."""
        wait = self.waiting
        if wait is None:   # (cleared by a seek meanwhile: read on from there)
            return _READING, None
        preparer = self._preparer
        frame = wait.frame
        if not wait.committed:
            if preparer is not None and preparer.resumable(source.local, frame, 0, quick=True):
                return self._carry_on_from(source, frame, wait)
            if self._queued_seconds() <= STALL_COMMIT_S:
                if preparer is not None and getattr(preparer.outlook(source.local, frame / self.samplerate),
                                                    "phase", "") == "lost":
                    wait.reason = "lost"   # (the copy here went missing: not the network failing)
                self._commit(source, generation)
            return _WAITING, None
        stalls = source.recent_stalls(time.perf_counter())
        if preparer is None or preparer.resumable(source.local, frame, stalls):
            return self._carry_on_from(source, frame, wait)
        outlook = preparer.outlook(source.local, frame / self.samplerate)
        if getattr(outlook, "permanent", False):
            return self._cut_short(source, frame, generation, outlook.reason)
        if (getattr(outlook, "phase", "") == "lost" and not self._refetch_asked
                and (wait.silent_since is not None or self.paused)):
            # Its bytes went missing from this computer: they'll never come to this version, so the
            # player starts the song again from here on a fresh copy (asked once; it closes this
            # session) — once the music queued has played out to the quiet cut, so it never stops
            # part-way through a waveform.
            self._refetch_asked = True
            self._event("fetched again", "the copy on this computer went missing, so playback stopped at a "
                                         "quiet sample (or where it was paused) and carries on from there on a "
                                         "fresh copy")
            self._on_refetch(self)
        return _WAITING, None

    def _carry_on_from(self, source: _Source, frame: int, wait: WaitInfo):
        """The bytes are here: a fresh handle at `frame` (same version of the copy), and read on."""
        if not self._reopen(source, frame):
            self._damaged(source, frame, "couldn't be read from here, so it ended there")
            self._resumed_serial = max(self._resumed_serial, wait.serial)
            if not wait.committed:
                self.waiting = None
            source.read_to = frame
            return _ENDED, frame
        if wait.committed:
            self._resumed_serial = wait.serial   # (the callback plays on past the marker now)
        else:
            self.waiting = None   # a hiccup the buffer covered: nothing was heard
        return _READING, frame

    def _commit(self, source: _Source, generation: int):
        """Commit to an audible pause: shorten the last block queued so the music stops at its
        quietest sample (within QUIET_WINDOW_S), and queue a stall marker after it. The frames
        after the cut are decoded again when it carries on, so nothing is skipped or repeated.
        If the sound card already has that block, the cut is simply the end of what was queued."""
        wait = self.waiting
        cut = wait.frame
        with self._queue.mutex:
            items = self._queue.queue
            if items:
                last = items[-1]
                if last[0] == generation and last[1] is source and isinstance(last[3], np.ndarray) and len(last[3]):
                    _, _, start, block = last
                    k = quiet_point(block, self.samplerate)
                    items[-1] = (generation, source, start, block[: k + 1])
                    cut = start + k + 1
            self._serial += 1
            wait.serial, wait.frame, wait.committed = self._serial, cut, True
            items.append((generation, source, cut, _Stall(self._serial)))
            self._queue.not_empty.notify()
        # (A seek past what's downloaded, or a copy here gone missing, isn't the network failing: it
        # neither makes the next waits longer nor counts against the server.)
        if not wait.seek and wait.reason != "lost":
            source.stalls.append(time.perf_counter())
            note = getattr(self._preparer, "note_stall", None)
            if note is not None:
                try:
                    note(source.local)
                except Exception:  # noqa: BLE001 — only what the cache learns from it
                    log.exception("telling the cache about a pause for the network")

    def _cut_short(self, source: _Source, frame: int, generation: int, why: str):
        """The rest of this song will never arrive (its file changed or went away mid-song): the
        chain ends at `frame`, said plainly; the player moves on. Never taken as a complete song."""
        at = frame / self.samplerate
        now = time.time()
        cause = why if why.startswith(("the ", "it")) else f"the file {why}"   # ("it's no longer on \\server\share")
        self._events.append(PlaybackEvent(now, source.name, at, "cut short", cause, last=now))
        self.note = f"Cut short at {_clock(at)}: {cause}"
        wait = self.waiting
        if wait is not None and wait.committed:
            self._resumed_serial = max(self._resumed_serial, wait.serial)   # (the marker steps aside)
        source.read_to = frame
        self._draining = True
        self._put((generation, source, frame, None))   # the end of the chain
        return _IDLE, frame

    def _await_step(self, after: _Source, position: int, got: int, generation: int):
        """One look, every 50 ms, at a next song still downloading: joined gaplessly once it may
        start while at least JOIN_GIVE_UP_S of this song is still queued; otherwise this song
        simply ends there (no cut: it has played to its end) and the player starts the next one
        when it's ready ("Preparing…"). None while still waiting, else the decoder's new state."""
        awaiting = self._awaiting
        if awaiting is None:
            return after, position, got, _ENDED   # (lined up again meanwhile: ask what's next)
        if self._queued_seconds() >= JOIN_GIVE_UP_S:
            local = self._prepare_next(awaiting[1], "join")
            if local is _AWAIT:
                return None
            self._awaiting = None
            following = None if local is None else self._open_next(after, awaiting[0], awaiting[1], local)
            return self._carry_into(after, following, position, got, generation)
        self._awaiting = None
        after.lined_up, after.next_source = None, None
        self.handoff = "the next song was still downloading, so it started after a short wait"
        return self._carry_into(after, None, position, got, generation)

    def _check_length(self, source: _Source):
        """An MP3 with no length header can stop early (see audiofile.mp3_shortfall): say so. A copy
        still downloading can't be counted yet: it's checked again once it's complete (the decoder
        looks after every block), as long as the song is still being read."""
        source.length_due = False
        try:
            if source.local.remote and source.local.ready_path() is None:
                source.length_due = audiofile.is_mp3(source.file)
                return
            short = audiofile.mp3_shortfall(source.file, source.local)
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
        """Fill one request from the sound card. This is the audio thread.

        The driver calls this on a high-priority thread of its own and wants the samples back
        before the next request falls due; anything slow here is heard as a click. So this
        method, and everything it calls, keeps to the rules of real-time audio:

        - nothing that waits: no blocking queue get (get_nowait only), no sleep, no Event.wait,
          no lock another thread might hold for long;
        - no file, console or network access, and no logging: the player's own log is written
          from the other threads, and what happens here is noted in `_events` instead;
        - nothing that touches the window (tkinter): news for the player's thread goes through
          `_on_advance`, and the meters' feeds are queues that take a block with put_nowait and
          drop it when full, so a slow viewer never slows the music;
        - as little new work per block as possible: the levels and the track peak are the one
          measurement made here, because they must see exactly the samples that were sent.

        Python can't make this truly lock-free (every object is an allocation, and the
        interpreter has a lock of its own), so the safety margin is the depth of the block
        queue, and `health` records how close that margin came to running out.
        tests/test_callback_rules.py holds the callback to these rules.
        """
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
                                   f"music ready: {_SYSTEM} or the driver was held up (a busy computer)")
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
                    if self.waiting is not None:
                        self._silent(now)   # (waiting for the network: a pause, not a dropout)
                    else:
                        self._starving(now)
                    break
                if item[0] != generation:
                    continue  # from before a seek
            _, source, start, block = item
            if isinstance(block, _Stall):   # paused for the network here
                if self._past_stall(block, source):
                    continue
                self._pending = item   # stays on the marker: exact zeros until the music is back
                outdata[filled:] = 0
                self._silent(now, block)   # (the rest of this request is silent already)
                break
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

    def _packed_callback(self, outdata, frames, time_info, status):
        """The callback of a packed 24-bit stream (outdata is raw bytes): the request is filled as
        int32, exactly as for any exclusive stream — so the proof, the levels and the meters see
        the same samples — and then packed. The audio thread's rules (see _callback) hold here too."""
        if frames > len(self._unpacked):   # (never with a fixed block size; allocates only if it happens)
            self._unpacked = np.zeros((frames, self.channels), np.int32)
        samples = self._unpacked[:frames]
        try:
            self._callback(samples, frames, time_info, status)
        finally:   # (the end of the chain stops the stream by raising, after filling the rest with silence)
            pack_int24(samples, outdata)

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

    def _past_stall(self, stall: _Stall, source: _Source) -> bool:
        """At a stall marker in `source`: may the music carry on past it? Once the decoder has
        carried on from the cut — straight away if nothing has been silent yet (seamless: the same
        samples as with no stall), else once the buffer has filled again, or the rest of the song
        is queued (the decoder has read on into the next one, or to its end and waits for the next
        one: nothing more of this song will come) or the rest of the chain is."""
        if self._resumed_serial < stall.serial:
            return False
        silent = stall.silent_since is not None
        read_past = not source.dropped and (self._reading is not source or self._awaiting is not None)
        if silent and not (self._draining or read_past or self._queue.qsize() >= self._queue_blocks - 1):
            return False   # (even if it ran out again meanwhile: one pause, not a stop-start)
        if silent:
            for event in reversed(self._events):
                if event.kind == "network pause":
                    event.silent += time.perf_counter() - stall.silent_since
                    event.last = time.time()
                    break
        self._passed_serial = max(self._passed_serial, stall.serial)   # (the wait it ended is over)
        return True

    def _silent(self, now: float, stall: _Stall | None = None):
        """A request answered with silence while waiting for the network: no dropout, no level, and
        (the first time) a "network pause" in the playback report."""
        self.levels = (0.0,) * self.channels
        first = stall is None or stall.silent_since is None
        if stall is not None and stall.silent_since is None:
            stall.silent_since = now
        wait = self.waiting
        if wait is not None and wait.silent_since is None:
            wait.silent_since = now
            if not first:
                return   # (the same silence, now waiting for later bytes: not a second pause)
            if wait.reason == "lost":
                return   # (not the network: the report says "fetched again" as the fresh copy is asked for)
            why = "the download stopped" if wait.reason == "failed" else "the network didn't keep up"
            self._event("network pause", f"{why}, so playback paused at a quiet sample until the music had "
                                         "downloaded (it plays from the copy on this computer)")

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
        if self._draining or self.waiting is not None:
            return  # at the very end (or waiting for the network) the buffer empties as it should
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

    def waiting_for(self, now: float) -> float:
        """Seconds of silence so far, waiting for the network (0 when not, or paused by hand)."""
        wait = self.waiting
        if wait is None or wait.silent_since is None or self.paused:
            return 0.0
        return max(0.0, now - wait.silent_since)

    @property
    def network_wait(self) -> WaitInfo | None:
        """The pause for the network the listener hears (committed, or already silent), if any."""
        wait = self.waiting
        return wait if wait is not None and (wait.committed or wait.silent_since is not None) else None

    @property
    def local(self):
        return self._src.local

    # -- control -------------------------------------------------------------
    def set_paused(self, paused: bool):
        """Pausing stops the stream so the sound device is free for other apps."""
        if paused == self.paused:
            return
        self.paused = paused
        self._last_callback = None  # the pause itself isn't a late request
        self._primed = False
        wait = self.waiting
        if wait is not None and not paused:
            wait.silent_since = None   # (any silence from here counts afresh)
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
        # Rounded, not cut down: a position handed back as seconds (frames / rate, on a resume) can
        # come back a hair under its whole frame, and cutting that down started one frame early —
        # the frame before the resume heard twice.
        frame = round(max(0.0, seconds) * self.samplerate)
        frame = min(frame, max(0, self.total_frames - 1))
        with self._lock:
            self._seek_request = frame
            self._generation += 1  # from now on the callback ignores anything queued before the seek
            self.frames_played = frame
            self._flowing = False
            self._primed = False
            self._starved_since = None
        self._wake()

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
        self._wake()
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

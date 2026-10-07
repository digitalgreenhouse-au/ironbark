"""Media keys, Windows' media controls (SMTC) and the taskbar buttons: the platform-free parts
(hub, dispatch, key/button decoding, icons) with a fake SMTC, plus the window hook on a real
test window. The full SMTC round trip with Windows (it shows up in Windows' media panel for a
moment) only runs when DG_LIVE_WINDOWS=1 is set — it was run by hand for 0.13.0."""

import ctypes
import dataclasses
import gc
import os
import queue
import subprocess
import sys
import textwrap
import threading
import time
import tkinter as tk
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from ironbark import mediakeys
from ironbark.engine import State
from ironbark.library import Track
from ironbark.mediakeys import NEXT, PAUSE, PLAY, PREVIOUS, STOP, TOGGLE, MediaKeys, NowPlaying, dispatch
from ironbark.ui import PlayerWindow


class FakePlayer:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda: self.calls.append(name)


def actions(player):
    return {name: getattr(player, name) for name in ("toggle_pause", "pause", "start", "next", "previous")}


class FakeSmtc:
    """Stands in for Windows' media controls: records what it's shown, presses from its own thread."""

    def __init__(self, hub):
        self.hub, self.shown, self.closed = hub, [], False

    def show(self, now):
        self.shown.append(now)

    def press(self, *commands):
        thread = threading.Thread(target=lambda: [self.hub.press(c) for c in commands])
        thread.start()
        thread.join()

    def close(self):
        self.closed = True


def test_presses_from_another_thread_reach_the_players_methods():
    hub = MediaKeys()
    smtc = FakeSmtc(hub)
    hub.add(smtc)
    smtc.press(PAUSE, NEXT, PREVIOUS)
    player = FakePlayer()
    for command in hub.drain():
        dispatch(command, "playing", actions(player))
    assert player.calls == ["pause", "next", "previous"]
    assert hub.drain() == []


@pytest.mark.parametrize("command,state,expected", [
    (TOGGLE, "playing", "toggle_pause"), (TOGGLE, "paused", "toggle_pause"), (TOGGLE, "stopped", "start"),
    (PLAY, "paused", "toggle_pause"), (PLAY, "stopped", "start"), (PLAY, "playing", None),
    (PAUSE, "playing", "pause"), (PAUSE, "paused", None), (STOP, "playing", "pause"),
    (NEXT, "playing", "next"), (PREVIOUS, "paused", "previous"), (NEXT, "stopped", None),
    ("nonsense", "playing", None),
])
def test_dispatch(command, state, expected):
    player = FakePlayer()
    assert dispatch(command, state, actions(player)) == expected
    assert player.calls == ([expected] if expected else [])


def test_song_details_are_sent_on_each_change_only():
    hub = MediaKeys()
    smtc = FakeSmtc(hub)
    hub.add(smtc)
    one = NowPlaying("playing", title="So Far Away", artist="Dire Straits", album="Brothers in Arms", track_no=1,
                     path=Path("01.flac"))
    hub.show(one)
    hub.show(one)  # every poll repeats it: passed on once
    hub.show(NowPlaying("paused", **{k: getattr(one, k) for k in ("title", "artist", "album", "track_no", "path")}))
    two = NowPlaying("playing", title="Money for Nothing", artist="Dire Straits", album="Brothers in Arms",
                     track_no=2, path=Path("02.flac"))
    hub.show(two)
    assert [(n.state, n.title) for n in smtc.shown] == [("playing", "So Far Away"), ("paused", "So Far Away"),
                                                        ("playing", "Money for Nothing")]
    assert smtc.shown[1].same_song(smtc.shown[0]) and not smtc.shown[2].same_song(smtc.shown[1])
    late = FakeSmtc(hub)
    hub.add(late)  # a backend switched on later hears what's playing straight away
    assert late.shown == [two]
    hub.close()
    assert smtc.closed and late.closed and hub.backends == []


def test_a_broken_backend_never_stops_the_others():
    hub = MediaKeys()

    class Broken:
        def show(self, now):
            raise RuntimeError("boom")

        def close(self):
            raise RuntimeError("boom")

    good = FakeSmtc(hub)
    hub.add(Broken())
    hub.add(good)
    hub.show(NowPlaying("playing", title="x"))
    assert good.shown and good.shown[0].title == "x"
    hub.close()  # no exception
    assert good.closed


def test_the_window_tells_windows_what_is_playing():
    track = Track(Path("C:/M/01 So Far Away.flac"), "So Far Away", artist="Dire Straits", album="Brothers in Arms",
                  track_no=1)
    w = SimpleNamespace(playing_tab=0, player=None)
    w._media_state = lambda status=None: PlayerWindow._media_state(w, status)
    status = SimpleNamespace(track=track, state=State.PAUSED)
    now = PlayerWindow._media_now(w, track, status)
    assert (now.state, now.title, now.artist, now.album, now.track_no, now.path) == \
        ("paused", "So Far Away", "Dire Straits", "Brothers in Arms", 1, track.path)
    assert PlayerWindow._media_now(w, None, status).state == "closed"


def test_the_window_carries_out_a_press():
    player = FakePlayer()
    started = []
    w = SimpleNamespace(playing_tab=0, toggle_play=lambda: started.append(True),
                        player=SimpleNamespace(status=lambda: SimpleNamespace(track=object(), state=State.PLAYING),
                                               toggle_pause=player.toggle_pause, pause=player.pause,
                                               next=player.next, previous=player.previous))
    w._media_state = lambda status=None: PlayerWindow._media_state(w, status)
    for command in (PAUSE, NEXT, TOGGLE):
        PlayerWindow._media_command(w, command)
    assert player.calls == ["pause", "next", "toggle_pause"]
    w.playing_tab = None  # nothing loaded: play starts the tab on show, like the play button
    w.player.status = lambda: SimpleNamespace(track=None, state=State.STOPPED)
    PlayerWindow._media_command(w, PLAY)
    assert started == [True]
    # A song from Up next playing with no tab behind it: the play/pause key pauses it.
    w.player.status = lambda: SimpleNamespace(track=object(), state=State.PLAYING)
    PlayerWindow._media_command(w, TOGGLE)
    assert started == [True] and player.calls[-1] == "toggle_pause"


def test_media_keys_and_taskbar_buttons_are_decoded():
    assert mediakeys.key_command(0xB3, mediakeys.WM_KEYDOWN) == TOGGLE
    assert mediakeys.key_command(0xB0, mediakeys.WM_KEYDOWN) == NEXT
    assert mediakeys.key_command(0xB1, mediakeys.WM_SYSKEYDOWN) == PREVIOUS
    assert mediakeys.key_command(0xB3, 0x0101) is None        # key up: ignored
    assert mediakeys.key_command(0x41, mediakeys.WM_KEYDOWN) is None   # "A": not ours
    assert mediakeys.thumb_command((mediakeys.THBN_CLICKED << 16) | 2) == TOGGLE
    assert mediakeys.thumb_command((mediakeys.THBN_CLICKED << 16) | 3) == NEXT
    assert mediakeys.thumb_command(2) is None                  # an ordinary WM_COMMAND


def test_the_button_handler_iid_matches_windows_own():
    # TypedEventHandler<SystemMediaTransportControls, SystemMediaTransportControlsButtonPressedEventArgs>,
    # as Windows' metadata projection reported it on 04/10/2026 (PowerShell, .NET reflection).
    assert str(mediakeys.BUTTON_HANDLER_IID) == "0557e996-7b23-5bae-aa81-ea0d671143a4"


def test_button_icons():
    play, pause = mediakeys.button_icon("play", 24), mediakeys.button_icon("pause", 24)
    assert play.size == (24, 24) and play.mode == "RGBA"
    assert play.getchannel("A").getextrema() == (0, 255)   # see-through around a solid shape
    assert play.tobytes() != pause.tobytes()
    for name in ("previous", "next"):
        assert mediakeys.button_icon(name, 16).getbbox() is not None
    with pytest.raises(ValueError):
        mediakeys.button_icon("stop", 16)


# -- Windows: the window hook on a real (test) window -------------------------------------------
windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


@pytest.fixture
def window():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display ({exc})")
    root.title("Ironbark test - media keys")
    root.geometry("200x100+0+0")
    root.update()
    yield root
    root.destroy()
    del root
    gc.collect()  # here, on the main thread: a Tk interpreter freed by another thread's collection aborts


@windows
def test_taskbar_button_clicks_reach_the_queue_through_the_window_hook(window):
    from ironbark import chrome

    hub = MediaKeys()
    hwnd = chrome._hwnd(window)
    created = []
    hook = mediakeys.WindowHook(hwnd, hub.press, lambda: created.append(True))
    post = ctypes.WinDLL("user32").PostMessageW
    post.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    post(hwnd, mediakeys.WM_COMMAND, (mediakeys.THBN_CLICKED << 16) | 3, 0)   # "Next" clicked
    post(hwnd, mediakeys.WM_COMMAND, (mediakeys.THBN_CLICKED << 16) | 1, 0)   # "Previous"
    post(hwnd, hook.taskbar_created, 0, 0)
    for _ in range(20):
        window.update()
        time.sleep(0.01)
    assert hub.drain() == [NEXT, PREVIOUS] and created  # (Windows may send its own too)
    hook.remove()
    post(hwnd, mediakeys.WM_COMMAND, (mediakeys.THBN_CLICKED << 16) | 3, 0)
    window.update()
    assert hub.drain() == []   # Tk's own procedure is back
    window.title("still works")
    window.update()


@windows
def test_keyboard_hook_starts_and_stops_cleanly():
    hub = MediaKeys()
    hook = mediakeys.KeyboardHook(hub.press)
    assert not hook.failed and hook.thread_id
    hook.close()
    assert not hook._thread.is_alive()


# -- switching off and on, and things going wrong (stand-ins for Windows' objects) ------------
class FakeTaskbar:
    """Stands in for ITaskbarList3: records what the buttons are set to."""

    def __init__(self, add_fails=False):
        self.calls, self.add_fails = [], add_fails

    def ThumbBarAddButtons(self, hwnd, count, buttons):
        self.calls.append(("add", [(b.iId, b.dwMask, b.dwFlags) for b in buttons]))
        if self.add_fails:   # Windows lets a window add its buttons only once
            raise OSError("the buttons were added before")

    def ThumbBarUpdateButtons(self, hwnd, count, buttons):
        self.calls.append(("update", [(b.iId, b.dwMask, b.dwFlags) for b in buttons]))


def thumb_bar(taskbar):
    bar = object.__new__(mediakeys.ThumbBar)   # without CoCreateInstance: the taskbar is a stand-in
    bar.hwnd, bar.taskbar, bar.added, bar.playing = 0, taskbar, False, None
    bar.icons = {name: mediakeys._hicon(mediakeys.button_icon(name, 16))
                 for name in ("previous", "play", "pause", "next")}
    return bar


@windows
def test_taskbar_buttons_hide_when_switched_off_and_come_back_working():
    first = FakeTaskbar()
    bar = thumb_bar(first)
    assert bar.add(True, True)
    bar.close()   # Media controls off: the buttons can't be removed from the window, so they hide
    kind, slots = first.calls[-1]
    assert kind == "update" and [s[0] for s in slots] == [1, 2, 3]
    assert all(mask == mediakeys.THB_FLAGS and flags == mediakeys.THBF_HIDDEN for _, mask, flags in slots)
    assert bar.icons == {} and bar.taskbar is None and not bar.added
    again = FakeTaskbar(add_fails=True)   # switched on again: the window already has its buttons
    bar = thumb_bar(again)
    assert bar.add(False, True) and bar.added
    assert again.calls[-1][0] == "update" and all(flags == mediakeys.THBF_ENABLED for *_, flags in again.calls[-1][1])
    bar.update(True, True)   # and play / pause follows the player again
    assert len(again.calls) == 3 and again.calls[-1][0] == "update"
    bar.close()


@windows
def test_a_lone_surrogate_in_a_title_still_reaches_windows():
    # A file named with half of a UTF-16 pair (Windows allows it) gives a title Python can't encode.
    with mediakeys._HString("bad \udc80 name") as handle:
        back = mediakeys._winrt().combase.WindowsGetStringRawBuffer(handle, None)
    assert back == "bad � name"


def test_a_failed_update_is_sent_again_in_full():
    seen = []

    class Flaky:
        def show(self, now, previous, cover):
            seen.append(previous)
            if len(seen) == 1:
                raise OSError("Windows refused it")

    backend = object.__new__(mediakeys.WindowsMedia)
    backend.smtc, backend.thumbs, backend._jobs = Flaky(), None, queue.SimpleQueue()
    song = NowPlaying("playing", title="So Far Away", artist="Dire Straits")
    for job in (("show", song), ("show", dataclasses.replace(song, state="paused")), ("close", None)):
        backend._jobs.put(job)
    backend._serve()
    assert seen == [None, None]   # the second time Windows is told the whole song, not just "paused"


def test_closing_the_media_controls_lets_go_of_everything_at_once():
    class Anything:
        def __getattr__(self, name):
            return lambda *args: None

    smtc = object.__new__(mediakeys.Smtc)   # without Windows: stand-ins for its COM objects
    smtc.smtc, smtc.updater, smtc._token = Anything(), Anything(), 0
    smtc._streams, smtc._thumbnail = object(), object()
    smtc._handler = SimpleNamespace(on_button=smtc._button)   # the handler points back at it
    smtc.close()
    assert smtc._handler is None and smtc._streams is None
    gone = weakref.ref(smtc)
    gc.disable()
    try:
        del smtc
        assert gone() is None   # freed here and now, not by a later collection on some other thread
    finally:
        gc.enable()


@windows
def test_the_worker_takes_the_keyboard_hook_off_even_after_close_stopped_waiting(monkeypatch):
    taken_off = threading.Event()

    class SlowHook:
        def __init__(self, on_command):
            time.sleep(0.3)   # still starting when close() stops waiting

        def close(self):
            taken_off.set()

    def no_media_controls(hwnd, on_command):
        raise OSError("no media controls here")

    monkeypatch.setattr(mediakeys, "Smtc", no_media_controls)
    monkeypatch.setattr(mediakeys, "KeyboardHook", SlowHook)
    monkeypatch.setattr(mediakeys.WindowsMedia, "CLOSE_WAIT", 0.05)
    backend = mediakeys.WindowsMedia(MediaKeys(), 0x7001, use_hook=False)
    backend.close()
    assert backend._thread.is_alive()   # close() gave up waiting …
    assert taken_off.wait(5)            # … and the worker still took the hook off when it got there
    backend._thread.join(5)


@windows
def test_a_new_instance_waits_for_the_windows_previous_one_to_finish_closing(monkeypatch):
    events = []

    class SlowToClose:
        def __init__(self, hwnd, on_command):
            events.append("open")

        def show(self, *args):
            pass

        def close(self):
            time.sleep(0.3)
            events.append("closed")

    monkeypatch.setattr(mediakeys, "Smtc", SlowToClose)
    monkeypatch.setattr(mediakeys.WindowsMedia, "CLOSE_WAIT", 0.05)
    first = mediakeys.WindowsMedia(MediaKeys(), 0x7002, keyboard_fallback=False, use_hook=False)
    assert first.started.wait(5)
    first.close()   # Media controls off, and straight back on, before the first has finished closing
    second = mediakeys.WindowsMedia(MediaKeys(), 0x7002, keyboard_fallback=False, use_hook=False)
    assert second.started.wait(5)
    second.close()
    second._thread.join(5)
    assert events == ["open", "closed", "open", "closed"]   # the old one's close never lands on the new


@windows
def test_the_worker_is_in_the_multithreaded_apartment_even_when_nothing_imported_comtypes_yet():
    # A fresh process: comtypes sets up COM for whichever thread imports it first.
    script = textwrap.dedent("""
        import ctypes, sys
        from ironbark import mediakeys
        assert "comtypes" not in sys.modules
        seen = []

        class Probe:
            def __init__(self, hwnd, on_command):
                kind, qualifier = ctypes.c_int(-1), ctypes.c_int(0)
                ctypes.WinDLL("ole32").CoGetApartmentType(ctypes.byref(kind), ctypes.byref(qualifier))
                seen.append(kind.value)
                raise OSError("only looking")

        mediakeys.Smtc = Probe
        backend = mediakeys.WindowsMedia(mediakeys.MediaKeys(), 0x7003, keyboard_fallback=False, use_hook=False)
        backend.started.wait(10)
        backend.close()
        print(seen[0])
    """)
    run = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert run.stdout.strip() == "1", run.stderr   # APTTYPE_MTA


@windows
@pytest.mark.skipif(os.environ.get("DG_LIVE_WINDOWS") != "1", reason="set DG_LIVE_WINDOWS=1 (shows in Windows' media panel)")
def test_windows_media_controls_round_trip(window):
    from ironbark import chrome

    hub = MediaKeys()
    backend = mediakeys.start_windows(hub, chrome._hwnd(window))
    assert backend.started.wait(5) and backend.smtc_ok.is_set(), backend.problems
    hub.show(NowPlaying("playing", title="Test ✓", artist="Someone", album="Album", track_no=2))
    deadline = time.time() + 3
    back = None
    while time.time() < deadline:
        back = backend.read_back()
        if back and back["title"] == "Test ✓":
            break
        window.update()
    assert back["title"] == "Test ✓" and back["artist"] == "Someone" and back["status"] == mediakeys.STATUS_PLAYING
    assert backend.thumbs is not None and backend.thumbs.added
    hub.close()

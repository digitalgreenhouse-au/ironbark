"""Media keys, Windows' media controls (SMTC) and the taskbar buttons: the platform-free parts
(hub, dispatch, key/button decoding, icons) with a fake SMTC, plus the window hook on a real
test window. The full SMTC round trip with Windows (it shows up in Windows' media panel for a
moment) only runs when DG_LIVE_WINDOWS=1 is set — it was run by hand for 0.13.0."""

import ctypes
import gc
import os
import sys
import threading
import time
import tkinter as tk
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
    PlayerWindow._media_command(w, PLAY)
    assert started == [True]


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

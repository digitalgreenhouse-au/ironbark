"""MPRIS on Linux (mpris.py), over a real D-Bus session bus, as the desktop's media keys and sound
menu reach it: the player takes its bus name, answers property reads, turns MPRIS calls into the
hub's words, and announces a new song or state. Linux only, with a session bus (the test container
runs under dbus-run-session); skipped without one."""

import os
import sys
import time

import pytest

from ironbark import mediakeys, platforms

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
                                reason="Linux with a D-Bus session bus")

jeepney = pytest.importorskip("jeepney")
from jeepney import DBusAddress, MatchRule, MessageType, message_bus, new_method_call  # noqa: E402
from jeepney.io.blocking import open_dbus_connection  # noqa: E402

from ironbark import mpris  # noqa: E402

ADDRESS = DBusAddress(mpris.PATH, bus_name=mpris.BUS_NAME, interface=mpris.PLAYER)
PROPS = DBusAddress(mpris.PATH, bus_name=mpris.BUS_NAME, interface=mpris.PROPERTIES)


def _wait(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        time.sleep(0.02)
    return condition()


@pytest.fixture
def player(tmp_path, monkeypatch):
    # (conftest stands in for mpris.start; MprisMedia itself is used here, on the test's own bus)
    monkeypatch.setattr(mediakeys, "cover_png", lambda path: b"\x89PNG fake cover" if path else None)
    hub = mediakeys.MediaKeys()
    backend = mpris.MprisMedia(hub, cover_folder=tmp_path / "covers")
    assert backend.started.wait(5)
    assert backend.ok, backend.problem
    hub.add(backend)
    client = open_dbus_connection(bus="SESSION")
    yield hub, backend, client
    client.close()
    hub.close()


def _get(client, interface, name):
    """A property read, as the desktop's panel does it: org.freedesktop.DBus.Properties.Get."""
    reply = client.send_and_get_reply(new_method_call(PROPS, "Get", "ss", (interface, name)), timeout=5)
    assert reply.header.message_type == MessageType.method_return, reply.body
    return reply.body[0][1]


def test_the_desktop_finds_the_player_by_its_bus_name(player):
    hub, backend, client = player
    names = client.send_and_get_reply(message_bus.ListNames(), timeout=5).body[0]
    assert mpris.BUS_NAME in names
    assert _get(client, mpris.ROOT, "Identity") == "Ironbark"
    assert _get(client, mpris.ROOT, "DesktopEntry") == "ironbark"
    intro = client.send_and_get_reply(new_method_call(
        DBusAddress(mpris.PATH, bus_name=mpris.BUS_NAME, interface=mpris.INTROSPECTABLE), "Introspect"),
        timeout=5).body[0]
    assert "org.mpris.MediaPlayer2.Player" in intro


def test_media_key_calls_become_the_hubs_words(player):
    hub, backend, client = player
    for method in ("PlayPause", "Next", "Previous", "Pause", "Play", "Stop"):
        reply = client.send_and_get_reply(new_method_call(ADDRESS, method), timeout=5)
        assert reply.header.message_type == MessageType.method_return
    got = []
    assert _wait(lambda: got.extend(hub.drain()) or len(got) >= 6)
    assert got == [mediakeys.TOGGLE, mediakeys.NEXT, mediakeys.PREVIOUS, mediakeys.PAUSE, mediakeys.PLAY,
                   mediakeys.STOP]


def test_what_plays_is_shown_and_announced(player, tmp_path):
    hub, backend, client = player
    rule = MatchRule(type="signal", interface=mpris.PROPERTIES, member="PropertiesChanged", path=mpris.PATH)
    client.send_and_get_reply(message_bus.AddMatch(rule), timeout=5)
    assert _get(client, mpris.PLAYER, "PlaybackStatus") == "Stopped"
    song = tmp_path / "Café" / "01 Song.flac"
    hub.show(mediakeys.NowPlaying("playing", title="Song", artist="Artist", album="Album", album_artist="Artist",
                                  track_no=1, path=song))
    changed = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and changed is None:
        try:
            message = client.receive(timeout=0.5)
        except TimeoutError:
            continue
        if message.header.message_type == MessageType.signal:
            changed = message.body[1]
    assert changed is not None and changed["PlaybackStatus"] == ("s", "Playing")
    meta = changed["Metadata"][1]
    assert meta["xesam:title"] == ("s", "Song") and meta["xesam:artist"] == ("as", ["Artist"])
    assert meta["xesam:trackNumber"] == ("i", 1)
    art = meta["mpris:artUrl"][1]
    assert art.startswith("file://") and (tmp_path / "covers").is_dir()
    assert _wait(lambda: _get(client, mpris.PLAYER, "PlaybackStatus") == "Playing")
    hub.show(mediakeys.NowPlaying("closed"))
    assert _wait(lambda: _get(client, mpris.PLAYER, "Metadata")["mpris:trackid"][1] == mpris.NO_TRACK)


def test_a_second_copy_doesnt_take_the_name_and_says_why(player):
    second = mpris.MprisMedia(mediakeys.MediaKeys())
    assert second.started.wait(5)
    assert not second.ok and "taken" in second.problem
    second.close()


def test_closing_gives_the_name_back(player):
    hub, backend, client = player
    hub.close()
    assert _wait(lambda: mpris.BUS_NAME not in client.send_and_get_reply(message_bus.ListNames(), timeout=5).body[0])


def test_without_a_session_bus_its_simply_off(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/bus")
    backend = mpris.MprisMedia(mediakeys.MediaKeys())
    assert backend.started.wait(5)
    assert not backend.ok and backend.problem
    backend.close()


def test_the_flag_and_settings_offer_media_keys_on_linux():
    assert platforms.MEDIA_CONTROLS and mpris.AVAILABLE and not mediakeys.AVAILABLE

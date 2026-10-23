"""MPRIS on any platform, through a stand-in for jeepney and the session bus: a malformed call from
another program is refused with a D-Bus error, and the media controls keep working after it."""

import queue
import sys
import time
import types

import pytest

from ironbark import mediakeys, mpris


class _Message:
    def __init__(self, member, interface, body=(), path=mpris.PATH):
        self.header = types.SimpleNamespace(message_type="method_call",
                                            fields={"member": member, "interface": interface, "path": path})
        self.body = body


class _Bus:
    """The session bus as the backend's connection sees it: calls come in, replies are kept."""

    def __init__(self):
        self.incoming: queue.SimpleQueue = queue.SimpleQueue()
        self.replies: queue.SimpleQueue = queue.SimpleQueue()

    def send_and_get_reply(self, message, timeout=None):
        return types.SimpleNamespace(body=(1,))   # RequestName: we're the primary owner

    def receive(self, timeout=None):
        try:
            return self.incoming.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None

    def send(self, reply):
        self.replies.put(reply)

    def close(self):
        pass

    def call(self, message):
        self.incoming.put(message)
        return self.replies.get(timeout=5)


@pytest.fixture
def fake_jeepney(monkeypatch):
    jeepney = types.ModuleType("jeepney")
    jeepney.HeaderFields = types.SimpleNamespace(member="member", interface="interface", path="path")
    jeepney.MessageType = types.SimpleNamespace(method_call="method_call")
    jeepney.new_error = lambda message, name, signature=None, body=(): ("error", name, body)
    jeepney.new_method_return = lambda message, signature=None, body=(): ("return", signature, body)
    jeepney.DBusAddress = lambda *args, **kwargs: None
    jeepney.new_signal = lambda *args, **kwargs: ("signal",)
    bus_messages = types.ModuleType("jeepney.bus_messages")
    bus_messages.message_bus = types.SimpleNamespace(RequestName=lambda name, flags: ("RequestName", name))
    jeepney.bus_messages = bus_messages
    monkeypatch.setitem(sys.modules, "jeepney", jeepney)
    monkeypatch.setitem(sys.modules, "jeepney.bus_messages", bus_messages)


@pytest.fixture
def backend(fake_jeepney):
    bus = _Bus()
    hub = mediakeys.MediaKeys()
    media = mpris.MprisMedia(hub, connect=lambda: bus)
    assert media.started.wait(5) and media.ok, media.problem
    yield media, bus, hub
    media.close()


@pytest.mark.parametrize("member, body", [("Get", ()), ("Get", (mpris.PLAYER,)), ("GetAll", ()),
                                          ("Get", (42, "PlaybackStatus"))])
def test_a_properties_call_without_its_arguments_is_refused_and_the_controls_keep_working(backend, member, body):
    media, bus, hub = backend
    reply = bus.call(_Message(member, mpris.PROPERTIES, body))
    assert reply[:2] == ("error", "org.freedesktop.DBus.Error.InvalidArgs")
    assert media._thread.is_alive() and media.ok
    assert bus.call(_Message("PlayPause", mpris.PLAYER))[0] == "return"
    deadline = time.monotonic() + 2
    while not (pressed := hub.drain()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pressed == [mediakeys.TOGGLE]


def test_a_call_that_fails_unexpectedly_is_refused_too(backend, monkeypatch):
    media, bus, hub = backend
    monkeypatch.setattr(mpris, "player_properties", lambda now, art: 1 / 0)
    reply = bus.call(_Message("GetAll", mpris.PROPERTIES, (mpris.PLAYER,)))
    assert reply[:2] == ("error", "org.freedesktop.DBus.Error.InvalidArgs")
    assert media._thread.is_alive()
    assert bus.call(_Message("Get", mpris.PROPERTIES, (mpris.ROOT, "Identity")))[0] == "return"

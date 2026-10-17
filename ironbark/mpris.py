"""Linux: the desktop's media controls — MPRIS (org.mpris.MediaPlayer2), the D-Bus interface every
Linux desktop's media keys, sound menu and lock screen use. With it, the keyboard's play/pause,
next and previous keys (and a headset's buttons) reach the player even when it isn't the active
window, and the panel's sound applet shows the song, artist and cover with working buttons.

No Windows code and no Tk here: `MprisMedia` is a backend for `mediakeys.MediaKeys`, like
`mediakeys.WindowsMedia`. Everything D-Bus runs on one thread of its own ("media-controls") with its
own connection to the session bus (jeepney: pure Python, MIT). A button press only puts a word in
the hub's queue (`hub.press`), which the window drains on its own thread; what's playing arrives
through `show()`, which only queues it for that thread. Anything that fails is logged and the
backend switches itself off; the player works the same without it.

The cover is handed over as a file: MPRIS names it by a `file://` address, so it's written to the
cache folder (`cover.png`, the one file, replaced for each song).
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
from pathlib import Path

from . import APP_NAME, mediakeys, platforms

log = logging.getLogger(__name__)

AVAILABLE = platforms.IS_LINUX
BUS_NAME = "org.mpris.MediaPlayer2.ironbark"
PATH = "/org/mpris/MediaPlayer2"
ROOT = "org.mpris.MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
PROPERTIES = "org.freedesktop.DBus.Properties"
INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
DESKTOP_ENTRY = "ironbark"   # ironbark.desktop, as packaging/linux installs it

STATUS = {"playing": "Playing", "paused": "Paused", "stopped": "Stopped", "closed": "Stopped"}
# MPRIS methods and the hub's words for them (mediakeys.dispatch decides what each does now).
METHODS = {"Play": mediakeys.PLAY, "Pause": mediakeys.PAUSE, "PlayPause": mediakeys.TOGGLE,
           "Stop": mediakeys.STOP, "Next": mediakeys.NEXT, "Previous": mediakeys.PREVIOUS}

INTROSPECTION = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
 <interface name="org.freedesktop.DBus.Introspectable">
  <method name="Introspect"><arg name="xml" type="s" direction="out"/></method>
 </interface>
 <interface name="org.freedesktop.DBus.Properties">
  <method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>
  <method name="Set"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/></method>
  <signal name="PropertiesChanged"><arg type="s"/><arg type="a{sv}"/><arg type="as"/></signal>
 </interface>
 <interface name="org.mpris.MediaPlayer2">
  <method name="Raise"/><method name="Quit"/>
  <property name="CanQuit" type="b" access="read"/><property name="CanRaise" type="b" access="read"/>
  <property name="HasTrackList" type="b" access="read"/><property name="Identity" type="s" access="read"/>
  <property name="DesktopEntry" type="s" access="read"/>
  <property name="SupportedUriSchemes" type="as" access="read"/>
  <property name="SupportedMimeTypes" type="as" access="read"/>
 </interface>
 <interface name="org.mpris.MediaPlayer2.Player">
  <method name="Next"/><method name="Previous"/><method name="Pause"/><method name="PlayPause"/>
  <method name="Stop"/><method name="Play"/>
  <method name="Seek"><arg type="x" direction="in"/></method>
  <method name="SetPosition"><arg type="o" direction="in"/><arg type="x" direction="in"/></method>
  <method name="OpenUri"><arg type="s" direction="in"/></method>
  <signal name="Seeked"><arg type="x"/></signal>
  <property name="PlaybackStatus" type="s" access="read"/><property name="Rate" type="d" access="read"/>
  <property name="Metadata" type="a{sv}" access="read"/><property name="Volume" type="d" access="read"/>
  <property name="Position" type="x" access="read"/><property name="MinimumRate" type="d" access="read"/>
  <property name="MaximumRate" type="d" access="read"/><property name="CanGoNext" type="b" access="read"/>
  <property name="CanGoPrevious" type="b" access="read"/><property name="CanPlay" type="b" access="read"/>
  <property name="CanPause" type="b" access="read"/><property name="CanSeek" type="b" access="read"/>
  <property name="CanControl" type="b" access="read"/>
 </interface>
</node>
"""


def _track_id(now: mediakeys.NowPlaying) -> str:
    """An object path for the song (MPRIS wants one): stable for the same song, no odd characters."""
    if now.state == "closed":
        return NO_TRACK
    text = "|".join((now.title, now.artist, now.album, os.fspath(now.path) if now.path else ""))
    return "/org/ironbark/track/t" + hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def metadata(now: mediakeys.NowPlaying, art_url: str | None) -> dict:
    """The song as MPRIS metadata: {name: (signature, value)}, jeepney's way of writing a variant."""
    found: dict = {"mpris:trackid": ("o", _track_id(now))}
    if now.state == "closed":
        return found
    if now.title:
        found["xesam:title"] = ("s", now.title)
    if now.artist:
        found["xesam:artist"] = ("as", [now.artist])
    if now.album:
        found["xesam:album"] = ("s", now.album)
    if now.album_artist:
        found["xesam:albumArtist"] = ("as", [now.album_artist])
    if now.track_no:
        found["xesam:trackNumber"] = ("i", int(now.track_no))
    if now.path is not None:
        found["xesam:url"] = ("s", Path(now.path).as_uri() if Path(now.path).is_absolute() else os.fspath(now.path))
    if art_url:
        found["mpris:artUrl"] = ("s", art_url)
    return found


def player_properties(now: mediakeys.NowPlaying, art_url: str | None) -> dict:
    loaded = now.state != "closed"
    return {
        "PlaybackStatus": ("s", STATUS.get(now.state, "Stopped")),
        "Rate": ("d", 1.0), "MinimumRate": ("d", 1.0), "MaximumRate": ("d", 1.0),
        "Metadata": ("a{sv}", metadata(now, art_url)),
        "Volume": ("d", 1.0),
        "Position": ("x", 0),   # (not kept up to date: CanSeek is false, so nothing relies on it)
        "CanGoNext": ("b", loaded), "CanGoPrevious": ("b", loaded),
        "CanPlay": ("b", True), "CanPause": ("b", loaded), "CanSeek": ("b", False),
        "CanControl": ("b", True),
    }


ROOT_PROPERTIES = {
    "CanQuit": ("b", False), "CanRaise": ("b", False), "HasTrackList": ("b", False),
    "Identity": ("s", APP_NAME), "DesktopEntry": ("s", DESKTOP_ENTRY),
    "SupportedUriSchemes": ("as", []), "SupportedMimeTypes": ("as", []),
}


class MprisMedia:
    """The Linux backend for MediaKeys. Create and close it on the Tk thread; `show` only queues.
    `connect` is replaced in tests (a jeepney blocking connection to the session bus)."""

    CLOSE_WAIT = 2.0

    def __init__(self, hub: mediakeys.MediaKeys, cover_folder: Path | None = None, connect=None):
        self.hub = hub
        self.cover_folder = cover_folder
        self._connect = connect
        self.started = threading.Event()
        self.ok = False
        self.problem = ""
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._now = mediakeys.NowPlaying()
        self._art: str | None = None
        self._stop = threading.Event()
        self._conn = None
        self._thread = threading.Thread(target=self._run, name="media-controls", daemon=True)
        self._thread.start()

    # -- the window's side -----------------------------------------------------------------
    def show(self, now: mediakeys.NowPlaying):
        self._jobs.put(now)

    def close(self):
        self._stop.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.close()   # (wakes the thread's receive)
            except Exception:   # noqa: BLE001
                pass
        self._thread.join(self.CLOSE_WAIT)

    # -- the thread ------------------------------------------------------------------------
    def _open(self):
        if self._connect is not None:
            return self._connect()
        from jeepney.io.blocking import open_dbus_connection

        return open_dbus_connection(bus="SESSION")

    def _run(self):
        try:
            from jeepney import HeaderFields, MessageType
            from jeepney.bus_messages import message_bus

            self._conn = conn = self._open()
            reply = conn.send_and_get_reply(message_bus.RequestName(BUS_NAME, 0x4), timeout=5)   # (do not queue)
            if reply.body[0] not in (1, 4):   # primary owner, or already it
                raise OSError(f"the name {BUS_NAME} is taken (another copy of {APP_NAME}?)")
            self.ok = True
        except Exception as exc:   # noqa: BLE001 — no session bus, no jeepney: simply off
            self.problem = str(exc) or type(exc).__name__
            log.warning("media controls (MPRIS) not available: %s", self.problem)
            self.started.set()
            self._close_conn()
            return
        self.started.set()
        log.info("media controls: MPRIS is on (%s)", BUS_NAME)
        try:
            while not self._stop.is_set():
                self._take_jobs()
                try:
                    message = conn.receive(timeout=0.1)
                except TimeoutError:
                    continue
                if message.header.message_type == MessageType.method_call:
                    self._answer(message, HeaderFields)
        except Exception:   # noqa: BLE001 — the bus went away (logged out?), or we closed it
            if not self._stop.is_set():
                log.warning("media controls: MPRIS stopped", exc_info=True)
        finally:
            self.ok = False
            self._close_conn()

    def _close_conn(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:   # noqa: BLE001
                pass

    def _take_jobs(self):
        latest = None
        while True:
            try:
                latest = self._jobs.get_nowait()
            except queue.Empty:
                break
        if latest is None or latest == self._now:
            return
        before, self._now = self._now, latest
        changed = {}
        if not latest.same_song(before) or self._art is None and latest.state != "closed":
            self._art = self._cover_url(latest)
            changed["Metadata"] = ("a{sv}", metadata(latest, self._art))
        props = player_properties(latest, self._art)
        if latest.state != before.state:
            for name in ("PlaybackStatus", "CanGoNext", "CanGoPrevious", "CanPause"):
                changed[name] = props[name]
            changed.setdefault("Metadata", props["Metadata"])
        if changed:
            self._signal_changed(changed)

    def _cover_url(self, now: mediakeys.NowPlaying) -> str | None:
        if self.cover_folder is None or now.state == "closed":
            return None
        png = mediakeys.cover_png(Path(now.path) if now.path else None)
        if png is None:
            return None
        try:
            self.cover_folder.mkdir(parents=True, exist_ok=True)
            # A new name each time (the panel caches by address), the one before removed.
            target = self.cover_folder / f"cover-{hashlib.sha1(png).hexdigest()[:12]}.png"
            for old in self.cover_folder.glob("cover-*.png"):
                if old != target:
                    old.unlink(missing_ok=True)
            target.write_bytes(png)
            return target.as_uri()
        except OSError:
            log.info("media controls: couldn't write the cover", exc_info=True)
            return None

    def _signal_changed(self, changed: dict):
        from jeepney import DBusAddress, new_signal

        signal = new_signal(DBusAddress(PATH, interface=PROPERTIES), "PropertiesChanged", "sa{sv}as",
                            (PLAYER, changed, []))
        try:
            self._conn.send(signal)
        except Exception:   # noqa: BLE001
            log.info("media controls: couldn't announce a change", exc_info=True)

    def _answer(self, message, fields):
        from jeepney import new_error, new_method_return

        header = message.header.fields
        member = header.get(fields.member)
        interface = header.get(fields.interface)
        path = header.get(fields.path)
        reply = None
        if path != PATH:
            reply = new_error(message, "org.freedesktop.DBus.Error.UnknownObject", "s", (f"No object {path}",))
        elif interface == INTROSPECTABLE and member == "Introspect":
            reply = new_method_return(message, "s", (INTROSPECTION,))
        elif interface == PROPERTIES and member in ("Get", "GetAll"):
            reply = self._properties(message, member, new_method_return, new_error)
        elif interface == PROPERTIES and member == "Set":
            reply = new_error(message, "org.freedesktop.DBus.Error.PropertyReadOnly", "s",
                              ("Ironbark's properties can't be set from here",))
        elif interface in (PLAYER, None) and member in METHODS:
            self.hub.press(METHODS[member])
            reply = new_method_return(message)
        elif interface in (PLAYER, ROOT, None) and member in ("Seek", "SetPosition", "OpenUri", "Raise", "Quit"):
            reply = new_method_return(message)   # (offered as unavailable by the Can* properties: ignored)
        else:
            reply = new_error(message, "org.freedesktop.DBus.Error.UnknownMethod", "s",
                              (f"No method {interface}.{member}",))
        self._conn.send(reply)

    def _properties(self, message, member, new_method_return, new_error):
        interface = message.body[0]
        table = ROOT_PROPERTIES if interface == ROOT else (
            player_properties(self._now, self._art) if interface == PLAYER else None)
        if table is None:
            return new_error(message, "org.freedesktop.DBus.Error.UnknownInterface", "s", (f"No interface {interface}",))
        if member == "GetAll":
            return new_method_return(message, "a{sv}", (table,))
        name = message.body[1]
        if name not in table:
            return new_error(message, "org.freedesktop.DBus.Error.UnknownProperty", "s", (f"No property {name}",))
        return new_method_return(message, "v", (table[name],))


def start(hub: mediakeys.MediaKeys, cover_folder: Path | None = None) -> MprisMedia | None:
    """Switch on the desktop's media controls. None (and logged) if it can't be done; the player
    works the same without them."""
    if not AVAILABLE:
        return None
    try:
        backend = MprisMedia(hub, cover_folder)
    except Exception:   # noqa: BLE001
        log.exception("media controls: couldn't start")
        return None
    hub.add(backend)
    return backend

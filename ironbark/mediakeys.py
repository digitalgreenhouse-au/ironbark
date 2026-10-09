"""Media keys, Windows' own media controls and the taskbar preview's buttons.

- **Windows media controls** — SMTC (System Media Transport Controls): the box beside the
  volume pop-up and in Quick Settings shows the song, artist and cover, with working buttons.
  Windows also sends the keyboard's play/pause, next and previous keys (and headset buttons) to
  whichever app's SMTC is current, so with SMTC in place the media keys work even when the player
  isn't the active window — without the player watching the keyboard at all.
- **Taskbar preview buttons** — previous / play-pause / next under the thumbnail when you hover
  the taskbar icon (ITaskbarList3's thumbnail toolbar).
- **Keyboard hook (fallback only)** — if SMTC can't be set up, a low-level keyboard hook watches
  for the four media keys only, and passes every key on untouched. Never RegisterHotKey: that
  would take the media keys away from every other app.

No new package: SMTC is a WinRT (Windows Runtime) API, reached here through comtypes and the
Windows DLLs (combase, shcore, shlwapi, user32, gdi32). The one WinRT binding that was checked,
pywinrt (MIT), has had no release since June 2025 and no Python 3.14 wheels, so it failed the
project's "actively maintained" rule for new dependencies. Every GUID and vtable order below was read from
Windows' own metadata (C:\\Windows\\System32\\WinMetadata, via PowerShell reflection, 04/10/2026)
or published headers (Microsoft Learn, Wine's IDL files).

Threads (no blocking Win32 calls inside Tk callbacks — a modal Win32 loop there hangs the app):
- All SMTC and taskbar calls run on one worker thread ("media-controls"), never the Tk thread.
- Button presses arrive on Windows' threads (SMTC), in the window procedure (taskbar buttons)
  or on the hook thread. Each only puts a word in `MediaKeys`' queue; the window drains it with
  `drain()` from its own `after` loop. Nothing here ever calls Tk.
- Anything that fails is logged (crash.log) and switched off; the player carries on without it.

Mac (later): the equivalent is MPNowPlayingInfoCenter + MPRemoteCommandCenter (MediaPlayer
framework, via PyObjC). `MediaKeys` and `dispatch` are platform-free and stay as they are.
"""

from __future__ import annotations

import ctypes
import io
import logging
import queue
import threading
import types
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import artwork, platforms, theme

log = logging.getLogger(__name__)

AVAILABLE = platforms.MEDIA_CONTROLS

# What a button or key asks for (the words that travel through the queue).
PLAY, PAUSE, TOGGLE, NEXT, PREVIOUS, STOP = "play", "pause", "toggle", "next", "previous", "stop"


@dataclass(frozen=True)
class NowPlaying:
    """What Windows is told is playing. state: "playing", "paused", "stopped" (a song is loaded
    but not going) or "closed" (nothing loaded: Windows hides the player from its media panel)."""
    state: str = "closed"
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_no: int = 0
    path: Path | None = None   # the song's file: the cover is looked up from it (artwork.py)

    def same_song(self, other: NowPlaying | None) -> bool:
        return other is not None and (self.title, self.artist, self.album, self.album_artist,
                                      self.track_no, self.path) == (other.title, other.artist, other.album,
                                                                    other.album_artist, other.track_no, other.path)


class MediaKeys:
    """The meeting point, with no Windows code: backends `press()` buttons from any thread; the
    window `drain()`s them on its own thread and tells every backend what's playing with
    `show()` (only changes are passed on)."""

    def __init__(self):
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self.backends: list = []
        self._last: NowPlaying | None = None

    def press(self, command: str) -> None:
        """Safe from any thread: just queues the word."""
        self._queue.put(command)

    def drain(self) -> list[str]:
        found = []
        while True:
            try:
                found.append(self._queue.get_nowait())
            except queue.Empty:
                return found

    def add(self, backend) -> None:
        self.backends.append(backend)
        if self._last is not None:
            self._safely(backend.show, self._last)

    def show(self, now: NowPlaying) -> None:
        if now == self._last:
            return
        self._last = now
        for backend in list(self.backends):
            self._safely(backend.show, now)

    def close(self) -> None:
        for backend in self.backends:
            self._safely(backend.close)
        self.backends = []
        self._last = None

    @staticmethod
    def _safely(call, *args):
        try:
            call(*args)
        except Exception:  # a broken backend must never take the player with it
            log.exception("media controls: %s failed", getattr(call, "__qualname__", call))


def dispatch(command: str, state: str, actions: dict[str, Callable[[], None]]) -> str | None:
    """Carry out a button / key press. state: the player's "playing", "paused" or "stopped".
    actions: "toggle_pause", "pause", "start" (play the tab on show, as the play button does),
    "next", "previous". Returns the action used (None = nothing to do), for tests and the log.
    Stop pauses rather than stops, so the place in the song isn't lost."""
    if command == TOGGLE:
        chosen = "toggle_pause" if state in ("playing", "paused") else "start"
    elif command == PLAY:
        chosen = {"paused": "toggle_pause", "stopped": "start"}.get(state)
    elif command in (PAUSE, STOP):
        chosen = "pause" if state == "playing" else None
    elif command in (NEXT, PREVIOUS):
        chosen = command if state in ("playing", "paused") else None
    else:
        chosen = None
    if chosen is not None:
        actions[chosen]()
    return chosen


# -- icons for the taskbar buttons (Pillow, no Windows code) ---------------------------------
def button_icon(name: str, size: int, colour: str | None = None):
    """A size×size RGBA picture for a taskbar button: "previous", "play", "pause" or "next".
    Drawn 4× larger and scaled down, so the edges are smooth. `colour`: "#rrggbb" (default
    theme.TASKBAR_GLYPH_ON_DARK)."""
    from PIL import Image, ImageDraw

    k = 4
    s = size * k
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    fill = theme.rgb(colour or theme.TASKBAR_GLYPH_ON_DARK) + (255,)
    m = s * 0.2  # margin
    top, bottom, mid = m, s - m, s / 2
    if name == "play":
        draw.polygon([(m + s * 0.06, top), (s - m + s * 0.04, mid), (m + s * 0.06, bottom)], fill=fill)
    elif name == "pause":
        w = s * 0.18
        draw.rectangle([mid - w * 1.5, top, mid - w * 0.5, bottom], fill=fill)
        draw.rectangle([mid + w * 0.5, top, mid + w * 1.5, bottom], fill=fill)
    elif name in ("next", "previous"):
        bar = s * 0.1
        if name == "next":
            draw.polygon([(m, top), (s - m - bar, mid), (m, bottom)], fill=fill)
            draw.rectangle([s - m - bar, top, s - m, bottom], fill=fill)
        else:
            draw.polygon([(s - m, top), (m + bar, mid), (s - m, bottom)], fill=fill)
            draw.rectangle([m, top, m + bar, bottom], fill=fill)
    else:
        raise ValueError(name)
    return image.resize((size, size), Image.LANCZOS)


def cover_png(path: Path | None, size: int = 300) -> bytes | None:
    """The song's cover as PNG bytes for Windows' media panel (artwork.py finds it)."""
    if path is None:
        return None
    try:
        image = artwork.cover_for(Path(path), size)
    except Exception:
        log.exception("media controls: cover lookup failed")
        return None
    if image is None:
        return None
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


# -- WinRT identifiers ------------------------------------------------------------------------
# Read from Windows' own metadata (WinMetadata\Windows.Media.winmd, Windows.Storage.winmd) on
# 04/10/2026 — see the module docstring. tests/test_mediakeys.py re-checks the computed one.
IID_IINSPECTABLE = "{AF86E2E0-B12D-4C6A-9C5A-D7AA65101E90}"
IID_SMTC_INTEROP = "{DDB0472D-C911-4A1F-86D9-DC3D71A95F5A}"       # systemmediatransportcontrolsinterop.h
IID_SMTC = "{99FA3FF4-1742-42A6-902E-087D41F965EC}"
IID_DISPLAY_UPDATER = "{8ABBC53E-FA55-4ECF-AD8E-C984E5DD1550}"
IID_MUSIC_PROPERTIES = "{6BBF0C59-D0A0-4D26-92A0-F978E1D18E7B}"
IID_MUSIC_PROPERTIES2 = "{00368462-97D3-44B9-B00F-008AFCEFAF18}"
IID_BUTTON_ARGS = "{B7F47116-A56F-4DC8-9E11-92031F4A87C2}"
IID_STREAM_REFERENCE_STATICS = "{857309DC-3FBF-4E7D-986F-EF3B1A07A964}"
IID_RANDOM_ACCESS_STREAM = "{905A0FE1-BC53-11DF-8C49-001E4FC686DA}"
IID_STREAM_REFERENCE = "{33EE3134-1DD6-4E3A-8067-D1C162E8642B}"
IID_AGILE_OBJECT = "{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}"
TYPED_EVENT_HANDLER = "{9de1c534-6ae1-11e0-84e1-18a905bcc53f}"     # Windows.Foundation.TypedEventHandler`2
# WinRT names a generic interface's IID by hashing its signature (RFC 4122 version 5, SHA-1,
# in this namespace). Checked: matches what Windows' own projection reports for
# TypedEventHandler<SystemMediaTransportControls, ...ButtonPressedEventArgs>.
_WINRT_PINTERFACE_NAMESPACE = uuid.UUID("11f47ad5-7b73-42c0-abae-878b1e16adee")


def pinterface_iid(generic: str, *args: tuple[str, str]) -> uuid.UUID:
    """IID of a WinRT generic interface/delegate. args: (runtime class name, its default
    interface IID) for each runtime-class type argument."""
    parts = ";".join(f"rc({name};{iid.lower()})" for name, iid in args)
    return uuid.uuid5(_WINRT_PINTERFACE_NAMESPACE, f"pinterface({generic.lower()};{parts})")


BUTTON_HANDLER_IID = pinterface_iid(
    TYPED_EVENT_HANDLER,
    ("Windows.Media.SystemMediaTransportControls", "{99fa3ff4-1742-42a6-902e-087d41f965ec}"),
    ("Windows.Media.SystemMediaTransportControlsButtonPressedEventArgs", "{b7f47116-a56f-4dc8-9e11-92031f4a87c2}"),
)

# Enumerations (same metadata).
STATUS_CLOSED, STATUS_STOPPED, STATUS_PLAYING, STATUS_PAUSED = 0, 2, 3, 4
TYPE_MUSIC = 1
BUTTONS = {0: PLAY, 1: PAUSE, 2: STOP, 6: NEXT, 7: PREVIOUS}   # SystemMediaTransportControlsButton
_STATUS = {"playing": STATUS_PLAYING, "paused": STATUS_PAUSED, "stopped": STATUS_STOPPED, "closed": STATUS_CLOSED}

# Taskbar thumbnail toolbar (shobjidl_core.h; Wine shobjidl.idl; THBN_CLICKED from CommCtrl.h).
CLSID_TASKBAR_LIST = "{56FDF344-FD6D-11D0-958A-006097C9A090}"
IID_TASKBAR_LIST3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
THB_ICON, THB_TOOLTIP, THB_FLAGS = 0x2, 0x4, 0x8
THBF_ENABLED, THBF_DISABLED, THBF_HIDDEN = 0x0, 0x1, 0x8
THBN_CLICKED = 0x1800
WM_COMMAND = 0x0111
THUMB_BUTTONS = {1: PREVIOUS, 2: TOGGLE, 3: NEXT}   # button id -> command

# Keyboard (Winuser.h virtual-key codes, Microsoft Learn).
WH_KEYBOARD_LL, WM_KEYDOWN, WM_SYSKEYDOWN, WM_QUIT = 13, 0x0100, 0x0104, 0x0012
MEDIA_VKEYS = {0xB0: NEXT, 0xB1: PREVIOUS, 0xB2: STOP, 0xB3: TOGGLE}


def key_command(vk: int, message: int) -> str | None:
    """The hook's whole decision: a media key going down → its command; anything else → None
    (and the key is always passed on to Windows and other apps untouched)."""
    if message not in (WM_KEYDOWN, WM_SYSKEYDOWN):
        return None
    return MEDIA_VKEYS.get(vk)


def thumb_command(wparam: int) -> str | None:
    """WM_COMMAND from the taskbar's thumbnail toolbar: HIWORD = THBN_CLICKED, LOWORD = button id."""
    if (wparam >> 16) & 0xFFFF != THBN_CLICKED:
        return None
    return THUMB_BUTTONS.get(wparam & 0xFFFF)


# ==============================================================================================
# Windows parts. Everything below is only imported/used on Windows.
# ==============================================================================================
def _winrt():
    """Declare the WinRT/COM interfaces (comtypes), once."""
    global _W
    if _W is not None:
        return _W
    from ctypes import HRESULT, POINTER, c_int, c_uint32, c_void_p
    from ctypes.wintypes import BOOL

    from comtypes import GUID, STDMETHOD, IUnknown

    class IInspectable(IUnknown):
        _iid_ = GUID(IID_IINSPECTABLE)
        _methods_ = [
            STDMETHOD(HRESULT, "GetIids", [c_void_p, c_void_p]),
            STDMETHOD(HRESULT, "GetRuntimeClassName", [c_void_p]),
            STDMETHOD(HRESULT, "GetTrustLevel", [c_void_p]),
        ]

    class ISystemMediaTransportControlsInterop(IInspectable):
        _iid_ = GUID(IID_SMTC_INTEROP)
        _methods_ = [STDMETHOD(HRESULT, "GetForWindow", [c_void_p, POINTER(GUID), c_void_p])]

    def flag(name):  # get_X / put_X pair for a Boolean property
        return [STDMETHOD(HRESULT, f"get_{name}", [c_void_p]), STDMETHOD(HRESULT, f"put_{name}", [BOOL])]

    class ISystemMediaTransportControls(IInspectable):
        _iid_ = GUID(IID_SMTC)
        _methods_ = [
            STDMETHOD(HRESULT, "get_PlaybackStatus", [c_void_p]),
            STDMETHOD(HRESULT, "put_PlaybackStatus", [c_int]),
            STDMETHOD(HRESULT, "get_DisplayUpdater", [c_void_p]),
            STDMETHOD(HRESULT, "get_SoundLevel", [c_void_p]),
            *flag("IsEnabled"), *flag("IsPlayEnabled"), *flag("IsStopEnabled"), *flag("IsPauseEnabled"),
            *flag("IsRecordEnabled"), *flag("IsFastForwardEnabled"), *flag("IsRewindEnabled"),
            *flag("IsPreviousEnabled"), *flag("IsNextEnabled"), *flag("IsChannelUpEnabled"),
            *flag("IsChannelDownEnabled"),
            STDMETHOD(HRESULT, "add_ButtonPressed", [c_void_p, c_void_p]),
            STDMETHOD(HRESULT, "remove_ButtonPressed", [ctypes.c_int64]),
            STDMETHOD(HRESULT, "add_PropertyChanged", [c_void_p, c_void_p]),
            STDMETHOD(HRESULT, "remove_PropertyChanged", [ctypes.c_int64]),
        ]

    class ISystemMediaTransportControlsDisplayUpdater(IInspectable):
        _iid_ = GUID(IID_DISPLAY_UPDATER)
        _methods_ = [
            STDMETHOD(HRESULT, "get_Type", [c_void_p]),
            STDMETHOD(HRESULT, "put_Type", [c_int]),
            STDMETHOD(HRESULT, "get_AppMediaId", [c_void_p]),
            STDMETHOD(HRESULT, "put_AppMediaId", [c_void_p]),
            STDMETHOD(HRESULT, "get_Thumbnail", [c_void_p]),
            STDMETHOD(HRESULT, "put_Thumbnail", [c_void_p]),
            STDMETHOD(HRESULT, "get_MusicProperties", [c_void_p]),
            STDMETHOD(HRESULT, "get_VideoProperties", [c_void_p]),
            STDMETHOD(HRESULT, "get_ImageProperties", [c_void_p]),
            STDMETHOD(HRESULT, "CopyFromFileAsync", [c_int, c_void_p, c_void_p]),
            STDMETHOD(HRESULT, "ClearAll", []),
            STDMETHOD(HRESULT, "Update", []),
        ]

    class IMusicDisplayProperties(IInspectable):
        _iid_ = GUID(IID_MUSIC_PROPERTIES)
        _methods_ = [
            STDMETHOD(HRESULT, "get_Title", [c_void_p]), STDMETHOD(HRESULT, "put_Title", [c_void_p]),
            STDMETHOD(HRESULT, "get_AlbumArtist", [c_void_p]), STDMETHOD(HRESULT, "put_AlbumArtist", [c_void_p]),
            STDMETHOD(HRESULT, "get_Artist", [c_void_p]), STDMETHOD(HRESULT, "put_Artist", [c_void_p]),
        ]

    class IMusicDisplayProperties2(IInspectable):
        _iid_ = GUID(IID_MUSIC_PROPERTIES2)
        _methods_ = [
            STDMETHOD(HRESULT, "get_AlbumTitle", [c_void_p]), STDMETHOD(HRESULT, "put_AlbumTitle", [c_void_p]),
            STDMETHOD(HRESULT, "get_TrackNumber", [c_void_p]), STDMETHOD(HRESULT, "put_TrackNumber", [c_uint32]),
            STDMETHOD(HRESULT, "get_Genres", [c_void_p]),
        ]

    class IButtonPressedEventArgs(IInspectable):
        _iid_ = GUID(IID_BUTTON_ARGS)
        _methods_ = [STDMETHOD(HRESULT, "get_Button", [c_void_p])]

    class IRandomAccessStreamReferenceStatics(IInspectable):
        _iid_ = GUID(IID_STREAM_REFERENCE_STATICS)
        _methods_ = [
            STDMETHOD(HRESULT, "CreateFromFile", [c_void_p, c_void_p]),
            STDMETHOD(HRESULT, "CreateFromUri", [c_void_p, c_void_p]),
            STDMETHOD(HRESULT, "CreateFromStream", [c_void_p, c_void_p]),
        ]

    class IButtonPressedHandler(IUnknown):  # TypedEventHandler<SMTC, ButtonPressedEventArgs>
        _iid_ = GUID("{" + str(BUTTON_HANDLER_IID) + "}")
        _methods_ = [STDMETHOD(HRESULT, "Invoke", [c_void_p, c_void_p])]

    class IAgileObject(IUnknown):  # marker: our handler may be called from any thread
        _iid_ = GUID(IID_AGILE_OBJECT)
        _methods_ = []

    class ITaskbarList3(IUnknown):
        _iid_ = GUID(IID_TASKBAR_LIST3)
        _methods_ = [
            # ITaskbarList
            STDMETHOD(HRESULT, "HrInit", []),
            STDMETHOD(HRESULT, "AddTab", [c_void_p]),
            STDMETHOD(HRESULT, "DeleteTab", [c_void_p]),
            STDMETHOD(HRESULT, "ActivateTab", [c_void_p]),
            STDMETHOD(HRESULT, "SetActiveAlt", [c_void_p]),
            # ITaskbarList2
            STDMETHOD(HRESULT, "MarkFullscreenWindow", [c_void_p, BOOL]),
            # ITaskbarList3
            STDMETHOD(HRESULT, "SetProgressValue", [c_void_p, ctypes.c_uint64, ctypes.c_uint64]),
            STDMETHOD(HRESULT, "SetProgressState", [c_void_p, c_int]),
            STDMETHOD(HRESULT, "RegisterTab", [c_void_p, c_void_p]),
            STDMETHOD(HRESULT, "UnregisterTab", [c_void_p]),
            STDMETHOD(HRESULT, "SetTabOrder", [c_void_p, c_void_p]),
            STDMETHOD(HRESULT, "SetTabActive", [c_void_p, c_void_p, ctypes.c_uint32]),
            STDMETHOD(HRESULT, "ThumbBarAddButtons", [c_void_p, ctypes.c_uint, c_void_p]),
            STDMETHOD(HRESULT, "ThumbBarUpdateButtons", [c_void_p, ctypes.c_uint, c_void_p]),
            STDMETHOD(HRESULT, "ThumbBarSetImageList", [c_void_p, c_void_p]),
            STDMETHOD(HRESULT, "SetOverlayIcon", [c_void_p, c_void_p, ctypes.c_wchar_p]),
            STDMETHOD(HRESULT, "SetThumbnailTooltip", [c_void_p, ctypes.c_wchar_p]),
            STDMETHOD(HRESULT, "SetThumbnailClip", [c_void_p, c_void_p]),
        ]

    class THUMBBUTTON(ctypes.Structure):
        _fields_ = [("dwMask", ctypes.c_uint32), ("iId", ctypes.c_uint), ("iBitmap", ctypes.c_uint),
                    ("hIcon", c_void_p), ("szTip", ctypes.c_wchar * 260), ("dwFlags", ctypes.c_uint32)]

    combase = ctypes.WinDLL("combase")
    combase.WindowsCreateString.argtypes = [ctypes.c_wchar_p, c_uint32, c_void_p]
    combase.WindowsCreateString.restype = HRESULT
    combase.WindowsDeleteString.argtypes = [c_void_p]
    combase.WindowsDeleteString.restype = HRESULT
    combase.WindowsGetStringRawBuffer.argtypes = [c_void_p, c_void_p]
    combase.WindowsGetStringRawBuffer.restype = ctypes.c_wchar_p
    combase.RoGetActivationFactory.argtypes = [c_void_p, POINTER(GUID), c_void_p]
    combase.RoGetActivationFactory.restype = HRESULT
    shlwapi = ctypes.WinDLL("shlwapi")
    shlwapi.SHCreateMemStream.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    shlwapi.SHCreateMemStream.restype = c_void_p
    shcore = ctypes.WinDLL("shcore")
    shcore.CreateRandomAccessStreamOverStream.argtypes = [c_void_p, c_int, POINTER(GUID), c_void_p]
    shcore.CreateRandomAccessStreamOverStream.restype = HRESULT

    _W = types.SimpleNamespace(**{k: v for k, v in locals().items() if k != "flag"})
    return _W


_W = None
_DLLS: dict = {}


def _dll(name: str):
    """Our own handle on a Windows DLL, so the argument types set here never change how the same
    functions behave elsewhere in the player (ctypes.windll shares one copy per process)."""
    if name not in _DLLS:
        _DLLS[name] = ctypes.WinDLL(name)
    return _DLLS[name]


_APARTMENTS = {0: "single-threaded", 1: "multithreaded", 2: "neutral", 3: "main single-threaded"}  # APTTYPE


def _apartment() -> str:
    """Which COM apartment this thread is in (CoGetApartmentType), for the log."""
    kind, qualifier = ctypes.c_int(-1), ctypes.c_int(0)
    try:
        failed = _dll("ole32").CoGetApartmentType(ctypes.byref(kind), ctypes.byref(qualifier))
    except (AttributeError, OSError):
        return "unknown"
    return "no" if failed else _APARTMENTS.get(kind.value, f"unknown ({kind.value})")


class _HString:
    """A WinRT string (HSTRING) for passing in; freed on exit."""

    def __init__(self, text: str):
        # A file name can hold half of a UTF-16 pair (Windows allows it), which can't be encoded:
        # it becomes the replacement character U+FFFD, so the text always reaches Windows and the
        # length below stays right.
        self.text = (text or "").encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
        self.handle = ctypes.c_void_p()

    def __enter__(self):
        w = _winrt()
        units = len(self.text.encode("utf-16-le")) // 2  # length in UTF-16 units (emoji count twice)
        w.combase.WindowsCreateString(self.text, units, ctypes.byref(self.handle))
        return self.handle

    def __exit__(self, *_):
        if self.handle:
            _winrt().combase.WindowsDeleteString(self.handle)


def read_hstring(handle: ctypes.c_void_p) -> str:
    """Text of an HSTRING we were handed (and free it)."""
    w = _winrt()
    if not handle:
        return ""
    try:
        return w.combase.WindowsGetStringRawBuffer(handle, None) or ""
    finally:
        w.combase.WindowsDeleteString(handle)


def _factory(class_name: str, interface):
    from ctypes import POINTER

    w = _winrt()
    result = POINTER(interface)()
    with _HString(class_name) as name:
        w.combase.RoGetActivationFactory(name, ctypes.byref(interface._iid_), ctypes.byref(result))
    return result


def _handler_class():
    """The ButtonPressed handler, a small COM object (comtypes) that only queues the button."""
    from comtypes import COMObject

    w = _winrt()

    class ButtonHandler(COMObject):
        _com_interfaces_ = [w.IButtonPressedHandler, w.IAgileObject]

        def __init__(self, on_button: Callable[[int], None]):
            super().__init__()
            self.on_button = on_button

        def Invoke(self, this, sender, args):
            # Runs on one of Windows' threads. Read which button, queue it, return.
            try:
                from ctypes import POINTER

                from comtypes import IUnknown

                unknown = ctypes.cast(args, POINTER(IUnknown))
                unknown.AddRef()  # the cast doesn't own a reference; this one is released with it
                pressed = unknown.QueryInterface(w.IButtonPressedEventArgs)
                button = ctypes.c_int(-1)
                pressed.get_Button(ctypes.byref(button))
                self.on_button(button.value)
            except Exception:
                log.exception("media controls: reading a button press failed")
            return 0

    return ButtonHandler


class Smtc:
    """Windows' media controls for one window. Create and use on ONE thread (the worker)."""

    def __init__(self, hwnd: int, on_command: Callable[[str], None]):
        from ctypes import POINTER

        w = _winrt()
        interop = _factory("Windows.Media.SystemMediaTransportControls", w.ISystemMediaTransportControlsInterop)
        self.smtc = POINTER(w.ISystemMediaTransportControls)()
        interop.GetForWindow(ctypes.c_void_p(hwnd), ctypes.byref(w.ISystemMediaTransportControls._iid_),
                             ctypes.byref(self.smtc))
        self.updater = POINTER(w.ISystemMediaTransportControlsDisplayUpdater)()
        self.smtc.get_DisplayUpdater(ctypes.byref(self.updater))
        self._music()  # fails early if this Windows can't show music details
        self._streams = _factory("Windows.Storage.Streams.RandomAccessStreamReference",
                                 w.IRandomAccessStreamReferenceStatics)
        self._thumbnail = None  # keep the current cover's stream alive while it's shown
        for name in ("IsEnabled", "IsPlayEnabled", "IsPauseEnabled", "IsNextEnabled", "IsPreviousEnabled",
                     "IsStopEnabled"):
            getattr(self.smtc, f"put_{name}")(True)
        self.on_command = on_command
        self._handler = _handler_class()(self._button)
        self._token = ctypes.c_int64(0)
        handler = self._handler.QueryInterface(w.IButtonPressedHandler)
        self.smtc.add_ButtonPressed(handler, ctypes.byref(self._token))
        self.smtc.put_PlaybackStatus(STATUS_CLOSED)

    def _music(self):
        """The music fields (title, artist; album and track number via the second interface).
        Fetched fresh each time: they can only be reached while the type is Music, and ClearAll
        resets the type."""
        from ctypes import POINTER

        w = _winrt()
        self.updater.put_Type(TYPE_MUSIC)
        music = POINTER(w.IMusicDisplayProperties)()
        self.updater.get_MusicProperties(ctypes.byref(music))
        try:
            music2 = music.QueryInterface(w.IMusicDisplayProperties2)
        except Exception:
            music2 = None
        return music, music2

    def _button(self, button: int):
        command = BUTTONS.get(button)
        if command:
            self.on_command(command)

    def show(self, now: NowPlaying, previous: NowPlaying | None, cover: bytes | None):
        if not now.same_song(previous):
            # The window's controls are one object shared by every instance on it: an earlier
            # instance that closed late may have switched them off.
            self.smtc.put_IsEnabled(True)
            self.updater.ClearAll()
            if now.state != "closed":
                music, music2 = self._music()
                for setter, text in ((music.put_Title, now.title), (music.put_Artist, now.artist),
                                     (music.put_AlbumArtist, now.album_artist)):
                    with _HString(text) as h:
                        setter(h)
                if music2 is not None:
                    with _HString(now.album) as h:
                        music2.put_AlbumTitle(h)
                    music2.put_TrackNumber(max(0, int(now.track_no or 0)))
                self._set_cover(cover)
            self.updater.Update()
        self.smtc.put_PlaybackStatus(_STATUS.get(now.state, STATUS_CLOSED))

    def _set_cover(self, png: bytes | None):
        from ctypes import POINTER

        from comtypes import IUnknown

        w = _winrt()
        self._thumbnail = None
        if not png:
            return
        raw = w.shlwapi.SHCreateMemStream(png, len(png))
        if not raw:
            return
        stream = ctypes.cast(raw, POINTER(IUnknown))  # owns the reference SHCreateMemStream gave us
        ras = POINTER(IUnknown)()
        iid = w.GUID(IID_RANDOM_ACCESS_STREAM)
        w.shcore.CreateRandomAccessStreamOverStream(stream, 0, ctypes.byref(iid), ctypes.byref(ras))
        reference = POINTER(IUnknown)()
        self._streams.CreateFromStream(ras, ctypes.byref(reference))
        self.updater.put_Thumbnail(reference)
        self._thumbnail = (stream, ras, reference)

    def read_back(self) -> dict:
        """What Windows holds now (for tests and the live check)."""
        status = ctypes.c_int(-1)
        self.smtc.get_PlaybackStatus(ctypes.byref(status))
        title, artist = ctypes.c_void_p(), ctypes.c_void_p()
        kind = ctypes.c_int(-1)
        self.updater.get_Type(ctypes.byref(kind))
        if kind.value == TYPE_MUSIC:
            music, _ = self._music()
            music.get_Title(ctypes.byref(title))
            music.get_Artist(ctypes.byref(artist))
        return {"status": status.value, "title": read_hstring(title), "artist": read_hstring(artist),
                "cover": self._thumbnail is not None}

    def close(self):
        try:
            self.smtc.remove_ButtonPressed(self._token)
            self.updater.ClearAll()
            self.updater.Update()
            self.smtc.put_PlaybackStatus(STATUS_CLOSED)
            self.smtc.put_IsEnabled(False)
        finally:
            # Let go of every COM object here, on the thread that made them, before it leaves COM.
            # The handler points back at this object (on_button), so without breaking that loop
            # they'd wait for the garbage collector, on whichever thread it happens to run.
            if self._handler is not None:
                self._handler.on_button = lambda button: None
            self._handler = self._streams = self._thumbnail = None
            self.updater = self.smtc = None


class ThumbBar:
    """The previous / play-pause / next buttons under the taskbar thumbnail. Worker thread only;
    the clicks come back to the window as WM_COMMAND (see WindowHook)."""

    def __init__(self, hwnd: int):
        from comtypes import CLSCTX_INPROC_SERVER, GUID, CoCreateInstance

        w = _winrt()
        self.hwnd = hwnd
        self.taskbar = CoCreateInstance(GUID(CLSID_TASKBAR_LIST), interface=w.ITaskbarList3,
                                        clsctx=CLSCTX_INPROC_SERVER)
        self.taskbar.HrInit()
        size = _dll("user32").GetSystemMetrics(49) or 16  # SM_CXSMICON (scaled: the process is DPI-aware)
        colour = theme.TASKBAR_GLYPH_ON_LIGHT if _light_taskbar() else theme.TASKBAR_GLYPH_ON_DARK
        self.icons = {name: _hicon(button_icon(name, size, colour)) for name in ("previous", "play", "pause", "next")}
        self.added = False
        self.playing = None

    def _buttons(self, playing: bool, enabled: bool):
        w = _winrt()
        array = (w.THUMBBUTTON * 3)()
        specs = ((1, "previous", "Previous"), (2, "pause" if playing else "play", "Pause" if playing else "Play"),
                 (3, "next", "Next"))
        for slot, (ident, icon, tip) in zip(array, specs):
            slot.dwMask = THB_ICON | THB_TOOLTIP | THB_FLAGS
            slot.iId = ident
            slot.hIcon = self.icons[icon]
            slot.szTip = tip
            slot.dwFlags = THBF_ENABLED if enabled else THBF_DISABLED
        return array

    def add(self, playing: bool, enabled: bool) -> bool:
        """Put the buttons up (once per taskbar button; again after Explorer restarts). Windows
        lets a window add its buttons only once and never removes them, so when they're already
        there (Media controls switched off and on again) adding may fail or be ignored: the
        update that follows shows them either way."""
        hwnd = ctypes.c_void_p(self.hwnd)
        buttons = self._buttons(playing, enabled)
        try:
            self.taskbar.ThumbBarAddButtons(hwnd, 3, buttons)
        except Exception as exc:  # COMError / OSError: already there, or no taskbar button yet
            log.info("taskbar buttons not added (%s); showing the existing ones", exc)
        try:
            self.taskbar.ThumbBarUpdateButtons(hwnd, 3, buttons)
        except Exception as exc:  # the taskbar button may not exist yet
            log.info("taskbar buttons not added yet: %s", exc)
            return False
        self.added, self.playing = True, (playing, enabled)
        return True

    def update(self, playing: bool, enabled: bool):
        if not self.added or self.playing == (playing, enabled):
            return
        self.taskbar.ThumbBarUpdateButtons(ctypes.c_void_p(self.hwnd), 3, self._buttons(playing, enabled))
        self.playing = (playing, enabled)

    def close(self):
        """Hide the buttons (Windows keeps a window's buttons until the window closes, so they'd
        stay under the thumbnail doing nothing), then free their icons."""
        try:
            if self.added and self.taskbar is not None:
                w = _winrt()
                hidden = (w.THUMBBUTTON * 3)()
                for ident, slot in enumerate(hidden, start=1):
                    slot.dwMask, slot.iId, slot.dwFlags = THB_FLAGS, ident, THBF_HIDDEN
                self.taskbar.ThumbBarUpdateButtons(ctypes.c_void_p(self.hwnd), 3, hidden)
        finally:
            for icon in self.icons.values():
                _dll("user32").DestroyIcon(ctypes.c_void_p(icon))
            self.icons = {}
            self.taskbar = None
            self.added = False


def _light_taskbar() -> bool:
    """Is Windows' taskbar light (then the buttons are drawn dark)? Read-only registry look."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except OSError:
        return False


def _hicon(image) -> int:
    """An HICON from an RGBA Pillow image (32-bit colour with alpha + an empty mask)."""
    from ctypes import wintypes as wt

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                    ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]

    gdi32, user32 = _dll("gdi32"), _dll("user32")
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wt.HANDLE, wt.DWORD]
    gdi32.CreateBitmap.restype = wt.HBITMAP
    gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    user32.CreateIconIndirect.restype = wt.HICON
    user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]

    w, h = image.size
    header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h,  # top-down
                              biPlanes=1, biBitCount=32, biCompression=0)
    bits = ctypes.c_void_p()
    colour = gdi32.CreateDIBSection(None, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    data = image.convert("RGBA").tobytes("raw", "BGRA")
    ctypes.memmove(bits, data, len(data))
    mask = gdi32.CreateBitmap(w, h, 1, 1, None)
    info = ICONINFO(fIcon=True, hbmMask=mask, hbmColor=colour)
    icon = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(mask)
    gdi32.DeleteObject(colour)
    return icon or 0


class WindowHook:
    """Listens to the player window's own messages for two things: a taskbar-thumbnail button
    click (WM_COMMAND, THBN_CLICKED) and "TaskbarButtonCreated" (the taskbar button appeared, or
    Explorer restarted: the buttons must be added again). Installed and removed on the Tk thread.

    The window procedure only reads the message and queues a word — it never calls Tk (that
    would upset tkinter's bookkeeping of who holds Python's lock) and never raises; every other
    message goes straight on to Tk's own procedure."""

    _keep: list = []  # callbacks are never freed: a late message must never find one gone

    def __init__(self, hwnd: int, on_command: Callable[[str], None], on_taskbar_created: Callable[[], None]):
        from ctypes import wintypes as wt

        user32 = _dll("user32")
        self.hwnd = hwnd
        self.on_command, self.on_taskbar_created = on_command, on_taskbar_created
        user32.RegisterWindowMessageW.restype = wt.UINT
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarButtonCreated")
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.GetWindowLongPtrW.restype = ctypes.c_void_p
        user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
        self._user32 = user32
        self.previous = None

        def procedure(hwnd, message, wparam, lparam):
            try:
                if message == WM_COMMAND:
                    command = thumb_command(wparam)
                    if command:
                        self.on_command(command)
                        return 0
                elif message == self.taskbar_created:
                    self.on_taskbar_created()
            except BaseException:  # noqa: BLE001 — must never escape into Windows
                pass
            return user32.CallWindowProcW(self.previous, hwnd, message, wparam, lparam)

        self._proc = proc_type(procedure)
        WindowHook._keep.append(self._proc)
        self._proc_address = ctypes.cast(self._proc, ctypes.c_void_p).value
        self.previous = user32.SetWindowLongPtrW(hwnd, -4, self._proc_address)  # GWLP_WNDPROC
        if not self.previous:
            raise OSError("couldn't hook the window's messages")

    def remove(self):
        """Put Tk's own window procedure back (only if ours is still the one in place)."""
        if self.previous and self._user32.GetWindowLongPtrW(self.hwnd, -4) == self._proc_address:
            self._user32.SetWindowLongPtrW(self.hwnd, -4, self.previous)
        self.on_command = lambda command: None
        self.on_taskbar_created = lambda: None


class KeyboardHook:
    """Fallback for when Windows' media controls can't be set up: watch for the four media keys
    on a thread of its own. Every key — media keys included — is passed on untouched, so other
    apps and Windows see exactly what they would without the player."""

    def __init__(self, on_command: Callable[[str], None]):
        self.on_command = on_command
        self.thread_id = 0
        self.ok = threading.Event()        # set once the hook is in place (or failed)
        self.failed = False
        self._closing = threading.Event()
        self._thread = threading.Thread(target=self._run, name="media-keys-hook", daemon=True)
        self._thread.start()
        self.ok.wait(2.0)

    def _run(self):
        from ctypes import wintypes as wt

        user32, kernel32 = _dll("user32"), _dll("kernel32")

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        hook_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        def procedure(code, wparam, lparam):
            try:
                if code == 0:  # HC_ACTION
                    command = key_command(KBDLLHOOKSTRUCT.from_address(lparam).vkCode, wparam)
                    if command:
                        self.on_command(command)
            except BaseException:  # noqa: BLE001
                pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._proc = hook_type(procedure)
        WindowHook._keep.append(self._proc)
        try:
            self.thread_id = kernel32.GetCurrentThreadId()
            hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
            if not hook:
                raise OSError(f"SetWindowsHookExW failed ({ctypes.GetLastError()})")
        except Exception:
            log.exception("media keys: keyboard hook couldn't start")
            self.failed = True
            self.ok.set()
            return
        self.ok.set()
        if not self._closing.is_set():   # (closed while starting: the hook comes straight off)
            message = wt.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:  # until WM_QUIT
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        user32.UnhookWindowsHookEx(hook)

    def close(self):
        """Take the hook off. Safe while the hook thread is still starting: either it hears
        WM_QUIT once it's running, or it sees `_closing` as soon as the hook is in."""
        self._closing.set()
        if self.ok.wait(2.0) and self.thread_id and not self.failed:
            _dll("user32").PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
            self._thread.join(1.0)


class WindowsMedia:
    """The Windows backend for MediaKeys: SMTC + taskbar buttons on a worker thread, the window
    hook on the Tk thread, and the keyboard hook if SMTC isn't available. Create and close it on
    the Tk thread; `show` may be called from there too (it only queues).

    Windows hands every instance for one window the same media-controls object, so a new
    instance's worker waits for the previous one's to finish closing (Media controls switched
    off and quickly on again) — otherwise the old worker's late close would switch the new one
    off."""

    CLOSE_WAIT = 2.0     # seconds close() waits for the worker (it finishes on its own after that)
    FOLLOW_WAIT = 30.0   # seconds a new worker waits for the window's previous one

    _workers: dict[int, threading.Thread] = {}   # hwnd -> the latest worker for that window

    def __init__(self, hub: MediaKeys, hwnd: int, keyboard_fallback: bool = True, use_hook=True):
        # comtypes sets up COM for whichever thread imports it first, as a single-threaded
        # apartment. Importing it here, on the Tk thread, keeps that off the worker, which
        # needs the multithreaded one.
        import comtypes  # noqa: F401

        self.hub, self.hwnd = hub, hwnd
        self.keyboard_fallback = keyboard_fallback
        self.smtc_ok = threading.Event()
        self.started = threading.Event()
        self.smtc: Smtc | None = None
        self.thumbs: ThumbBar | None = None
        self.keyboard: KeyboardHook | None = None
        self.problems: list[str] = []
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self.window_hook: WindowHook | None = None
        if use_hook:
            try:
                self.window_hook = WindowHook(hwnd, hub.press, lambda: self._jobs.put(("taskbar", None)))
            except Exception as exc:
                self.problems.append(f"taskbar buttons: {exc}")
                log.warning("media controls: window hook not installed (%s); no taskbar buttons", exc)
        self._thread = threading.Thread(target=self._run, name="media-controls", daemon=True)
        self._before = WindowsMedia._workers.get(hwnd)
        WindowsMedia._workers[hwnd] = self._thread
        self._thread.start()

    def show(self, now: NowPlaying):
        self._jobs.put(("show", now))

    def close(self):
        """The worker closes everything it opened (the keyboard hook too), even when that takes
        longer than this waits."""
        if self.window_hook is not None:
            self.window_hook.remove()
        self._jobs.put(("close", None))
        self._thread.join(self.CLOSE_WAIT)

    def read_back(self, timeout: float = 2.0) -> dict | None:
        """Ask the worker what Windows' media controls hold now (tests / live check)."""
        answer: queue.SimpleQueue = queue.SimpleQueue()
        self._jobs.put(("read", answer))
        try:
            return answer.get(timeout=timeout)
        except queue.Empty:
            return None

    # -- the worker thread -------------------------------------------------------------------
    def _run(self):
        import comtypes

        if self._before is not None:
            self._before.join(self.FOLLOW_WAIT)
            if self._before.is_alive():
                log.warning("media controls: the previous instance is still closing; starting anyway")
            self._before = None
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            joined = True
        except OSError as exc:
            joined = False
            log.warning("media controls: couldn't join the multithreaded COM apartment (%s); this thread "
                        "is in the %s apartment", exc, _apartment())
        try:
            try:
                self.smtc = Smtc(self.hwnd, self.hub.press)
                self.smtc_ok.set()
            except Exception as exc:
                self.problems.append(f"Windows media controls: {exc}")
                log.warning("media controls: Windows' media controls unavailable (%s)", exc, exc_info=True)
                if self.keyboard_fallback:
                    self.keyboard = KeyboardHook(self.hub.press)
            if self.window_hook is not None:
                try:
                    self.thumbs = ThumbBar(self.hwnd)
                    self.thumbs.add(False, False)
                except Exception as exc:
                    self.problems.append(f"taskbar buttons: {exc}")
                    log.warning("media controls: taskbar buttons unavailable (%s)", exc, exc_info=True)
                    self.thumbs = None
            self.started.set()
            self._serve()
        except Exception:
            log.exception("media controls: worker stopped")
        finally:
            self.started.set()
            for part in (self.smtc, self.thumbs, self.keyboard):
                if part is not None:
                    try:
                        part.close()
                    except Exception:
                        log.exception("media controls: closing failed")
            self.smtc = self.thumbs = self.keyboard = None
            if joined:
                try:
                    comtypes.CoUninitialize()
                except OSError:
                    pass

    def _serve(self):
        shown: NowPlaying | None = None       # what Windows' media panel holds now
        latest: NowPlaying | None = None      # what it was last asked to show
        cover_for: Path | None = None
        cover: bytes | None = None
        while True:
            kind, value = self._jobs.get()
            if kind == "close":
                return
            if kind == "read":
                try:
                    value.put(self.smtc.read_back() if self.smtc is not None else None)
                except Exception as exc:
                    value.put({"error": str(exc)})
                continue
            if kind == "taskbar":
                if self.thumbs is not None:
                    playing, enabled = latest is not None and latest.state == "playing", \
                        latest is not None and latest.state != "closed"
                    self.thumbs.add(playing, enabled)
                continue
            now: NowPlaying = value
            latest = now
            if self.smtc is not None:
                try:
                    if now.path != cover_for:
                        cover_for, cover = now.path, cover_png(now.path)
                    self.smtc.show(now, shown, cover)
                    shown = now
                except Exception:
                    # Not shown: the next change sends this song's details again in full.
                    shown = None
                    log.exception("media controls: update failed")
            if self.thumbs is not None:
                try:
                    if not self.thumbs.added:
                        self.thumbs.add(now.state == "playing", now.state != "closed")
                    self.thumbs.update(now.state == "playing", now.state != "closed")
                except Exception:
                    log.exception("media controls: taskbar buttons update failed")


def start_windows(hub: MediaKeys, hwnd: int) -> WindowsMedia | None:
    """Switch on everything Windows offers for `hwnd` (the player's top-level window). Returns
    None (and logs why) if it can't; the player works the same without it."""
    if not AVAILABLE:
        return None
    try:
        backend = WindowsMedia(hub, hwnd)
    except Exception:
        log.exception("media controls: couldn't start")
        return None
    hub.add(backend)
    return backend

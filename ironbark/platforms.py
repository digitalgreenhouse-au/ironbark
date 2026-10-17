"""Which platform Ironbark runs on, and what that platform gives it: the one place the differences
are named. Everything else asks here (`platforms.IS_WINDOWS`, `platforms.MEDIA_CONTROLS`) rather than
testing sys.platform itself, so a port starts by reading this file and searching for its names.

The Windows implementations live in the modules named beside each flag. Where another platform has
its own (the Mac's Bin and Linux's Trash in recycle.py), the flag says which; where it has none
yet, the feature is simply absent there and the program says nothing about it.
"""
import sys

NAME = ("windows" if sys.platform == "win32" else "mac" if sys.platform == "darwin"
        else "linux" if sys.platform.startswith("linux") else "other")
IS_WINDOWS = NAME == "windows"
IS_MAC = NAME == "mac"
IS_LINUX = NAME == "linux"

# What the platform provides.
CUSTOM_TITLE_BAR = IS_WINDOWS      # chrome.py draws the title bar itself (the Win32 caption is removed)
MEDIA_CONTROLS = IS_WINDOWS or IS_LINUX   # media keys and the desktop's media panel: mediakeys.py
                                         # (Windows' SMTC, taskbar buttons); mpris.py on Linux (MPRIS)
EXCLUSIVE_OUTPUT = IS_WINDOWS or IS_MAC or IS_LINUX   # devices.py / session.py: "bit-perfect" —
# WASAPI exclusive mode; on a Mac coreaudio.py hogs the device at the file's rate; on Linux the
# sound card's own ALSA device (hw:), past the sound server, which takes it while it plays
HARDWARE_VOLUME = IS_WINDOWS       # devices.py: the device's own volume control, through pycaw
LOOPBACK_RECORDING = IS_WINDOWS or IS_LINUX   # tools/loopback.py: what the mixer plays, for the check —
                                             # Windows' loopback; the sound server's monitor on Linux
# Who mixes and records the shared sound, in the check's words (proof.py, tools/details.py, Test
# signals): "Windows played…", "Windows' recording"; on Linux the sound server (PipeWire / PulseAudio).
SOUND_SYSTEM = "Windows" if IS_WINDOWS else "the sound server"
SOUND_SYSTEMS = "Windows'" if IS_WINDOWS else "the sound server's"
# tools/audition.py through Test signals: Core Audio hangs an input stream opened beside an output
# stream on the same device (the read never returns), so playing and recording one device (an
# interface's output cabled back to its input) goes through a single duplex stream there.
DUPLEX_SAME_DEVICE = IS_MAC
# quitsignal.py: the system's request to close (SIGTERM, at logout / shutdown or from `kill`) runs
# the ordinary close, so settings are saved and a device bit-perfect changed is put back. Windows
# sends no such signal to a windowed program.
QUIT_ON_SIGTERM = not IS_WINDOWS
NAMED_PIPE_INSTANCE = IS_WINDOWS   # instance.py: one copy at a time through a named pipe (a socket elsewhere)
RECYCLE_BIN = "Bin" if IS_MAC else "Trash" if IS_LINUX else "Recycle Bin"   # recycle.py: what the bin is called
# x11.py through winstate.py: X11 window managers (Cinnamon, GNOME) keep a minimised window mapped,
# so Tk calls it "normal" and can't bring it back; the window manager is asked instead.
MINIMISED_STAYS_MAPPED = IS_LINUX

# How Tk names things that differ by windowing system.
# The keyboard's menu key. X11 calls it Menu: "App" is no X11 key name, and Tk refuses a binding to it.
MENU_KEY = "<App>" if IS_WINDOWS or IS_MAC else "<Menu>"

# engine.py: paused in bit-perfect for PAUSE_RELEASE_S, the device is let go (other apps are silent
# while it's held; play carries on from the same spot). On Windows the exclusive stream stays open
# while paused, as it always has: let go, another app could take the device and bit-perfect would
# come back as shared.
PAUSE_RELEASES_DEVICE = IS_LINUX or IS_MAC

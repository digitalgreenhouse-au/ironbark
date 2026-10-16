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
MEDIA_CONTROLS = IS_WINDOWS        # mediakeys.py: media keys, Windows' media panel, taskbar buttons
EXCLUSIVE_OUTPUT = IS_WINDOWS or IS_MAC   # devices.py / session.py: "bit-perfect" — WASAPI exclusive mode;
                                         # on a Mac coreaudio.py hogs the device at the file's rate
HARDWARE_VOLUME = IS_WINDOWS       # devices.py: the device's own volume control, through pycaw
LOOPBACK_RECORDING = IS_WINDOWS    # tools/loopback.py: Windows' loopback, for the Windows check
NAMED_PIPE_INSTANCE = IS_WINDOWS   # instance.py: one copy at a time through a named pipe (a socket elsewhere)
RECYCLE_BIN = "Bin" if IS_MAC else "Trash" if IS_LINUX else "Recycle Bin"   # recycle.py: what the bin is called
# x11.py through winstate.py: X11 window managers (Cinnamon, GNOME) keep a minimised window mapped,
# so Tk calls it "normal" and can't bring it back; the window manager is asked instead.
MINIMISED_STAYS_MAPPED = IS_LINUX

# How Tk names things that differ by windowing system.
# The keyboard's menu key. X11 calls it Menu: "App" is no X11 key name, and Tk refuses a binding to it.
MENU_KEY = "<App>" if IS_WINDOWS or IS_MAC else "<Menu>"

"""Which platform Ironbark runs on, and what that platform gives it: the one place the differences
are named. Everything else asks here (`platforms.IS_WINDOWS`, `platforms.MEDIA_CONTROLS`) rather than
testing sys.platform itself, so a port starts by reading this file and searching for its names.

The Windows implementations live in the modules named beside each flag. Where another platform has
its own (the Mac's Bin in recycle.py), the flag says which; where it has none yet, the feature is
simply absent there and the program says nothing about it.
"""
import sys

NAME = "windows" if sys.platform == "win32" else "mac" if sys.platform == "darwin" else "other"
IS_WINDOWS = NAME == "windows"
IS_MAC = NAME == "mac"

# What the platform provides.
CUSTOM_TITLE_BAR = IS_WINDOWS      # chrome.py draws the title bar itself (the Win32 caption is removed)
MEDIA_CONTROLS = IS_WINDOWS        # mediakeys.py: media keys, Windows' media panel, taskbar buttons
EXCLUSIVE_OUTPUT = IS_WINDOWS      # devices.py / engine.py: WASAPI exclusive mode ("bit-perfect")
HARDWARE_VOLUME = IS_WINDOWS       # devices.py: the device's own volume control, through pycaw
LOOPBACK_RECORDING = IS_WINDOWS    # tools/loopback.py: Windows' loopback, for the Windows check
NAMED_PIPE_INSTANCE = IS_WINDOWS   # instance.py: one copy at a time through a named pipe (a socket elsewhere)
RECYCLE_BIN = "Bin" if IS_MAC else "Recycle Bin"   # recycle.py: what the bin is called; both have one

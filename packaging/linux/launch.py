"""The packaged Linux program's starting point (PyInstaller needs a script, not a package)."""

import multiprocessing
import os
import sys

PORTAUDIO = "libportaudio.so.2"


def use_own_portaudio() -> None:
    """Play through the copy of the sound library (PortAudio) that comes with the program, the one
    it was built and tested with. sounddevice asks for it by name ("portaudio"), and the system's
    answer would be whichever copy is installed, or none at all on a computer without it."""
    import ctypes.util

    own = os.path.join(sys._MEIPASS, PORTAUDIO)
    find = ctypes.util.find_library

    def find_library(name):
        return own if name == "portaudio" and os.path.isfile(own) else find(name)

    ctypes.util.find_library = find_library


def run() -> None:
    if getattr(sys, "frozen", False):   # (before anything loads sounddevice, in workers too)
        use_own_portaudio()
    # A process started by multiprocessing runs its task here and exits, instead of opening a
    # second player. (Does nothing in the program's own process.)
    multiprocessing.freeze_support()
    from ironbark.__main__ import main

    main()


if __name__ == "__main__":
    run()

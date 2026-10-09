"""The packaged program's starting point (PyInstaller needs a script, not a package)."""

import multiprocessing
import os
import sys

# Windows loads a library only from a path of at most 250 characters (measured on Windows 11, through
# ctypes and cffi alike, even with long paths switched on: 251 is refused as too long), so the bundled
# libraries can't be loaded from too deep a folder. The deepest one loaded by its own path sits this
# far below the folder holding Ironbark.exe. (numpy's maths library sits deeper, but Windows finds it
# as a part of numpy's own module, and from there it loads even at 260 characters.)
LONGEST_LOAD = 250
DEEPEST = r"_internal\_sounddevice_data\portaudio-binaries\libportaudio64bit.dll"
ERROR_FILENAME_EXCED_RANGE = 206

TOO_DEEP = ("Ironbark can't start from this folder: its path is too long for Windows to load "
            "Ironbark's own files.\n\n{folder}\n\nMove the Ironbark folder somewhere with a shorter "
            "path, for example C:\\Ironbark, and start it from there.")


def path_too_long(error: OSError, folder: str) -> bool:
    """Did loading a bundled library fail because the folder is too deep?"""
    for reason in _reasons(error):
        if getattr(reason, "winerror", None) == ERROR_FILENAME_EXCED_RANGE:
            return True
        if "error 0x%x" % ERROR_FILENAME_EXCED_RANGE in str(reason).lower():   # how cffi reports it
            return True
    return len(os.path.join(folder, DEEPEST)) > LONGEST_LOAD


def _reasons(error: BaseException):
    """The error and the ones it was raised from. In the packaged program ctypes raises an error of
    PyInstaller's own ("Failed to load dynlib/dll"), with no Windows error code: Windows' own error
    is its cause."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def use_own_portaudio() -> None:
    """Play through the copy of the sound library (PortAudio) that comes with the program, the one it
    was built and tested with. sounddevice would first take any portaudio.dll in a folder on the
    PATH (another program's copy, built who knows how), and with SD_ENABLE_ASIO set it asks for an
    ASIO build of its own that the program doesn't carry, so the player couldn't start at all."""
    os.environ.pop("SD_ENABLE_ASIO", None)
    import ctypes.util

    find = ctypes.util.find_library

    def find_library(name):
        return None if "portaudio" in name.lower() else find(name)

    ctypes.util.find_library = find_library


def tell(text: str) -> None:
    """A plain Windows message box (the packaged program has no console)."""
    import ctypes

    ctypes.WinDLL("user32").MessageBoxW(None, text, "Ironbark", 0x10)   # MB_ICONERROR


def check_folder(folder: str) -> None:
    """The player loads its sound libraries only once it knows it will open a window (a launch that
    hands its files to the copy already open never does), and then a failure is shown as a general
    "couldn't start". So the deepest of them is loaded here first: a folder too deep for Windows is
    named plainly, whichever launch meets it."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.WinDLL(os.path.join(folder, DEEPEST))
    except OSError as error:
        if path_too_long(error, folder):
            tell(TOO_DEEP.format(folder=folder))
            sys.exit(1)
        # (anything else: the player's own start-up reports it, with its details)


def run() -> None:
    if getattr(sys, "frozen", False):   # (before anything loads sounddevice, in workers too)
        use_own_portaudio()
    # A process started by multiprocessing runs its task here and exits, instead of opening a
    # second player. (Does nothing in the program's own process.)
    multiprocessing.freeze_support()
    folder = os.path.dirname(os.path.abspath(sys.executable))
    check_folder(folder)
    try:
        from ironbark.__main__ import main
    except OSError as error:
        if not path_too_long(error, folder):
            raise
        tell(TOO_DEEP.format(folder=folder))
        sys.exit(1)
    main()


if __name__ == "__main__":
    run()

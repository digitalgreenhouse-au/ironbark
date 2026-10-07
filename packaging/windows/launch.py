"""The packaged program's starting point (PyInstaller needs a script, not a package)."""

import multiprocessing
import os
import sys

# Windows' classic limit on a file path (MAX_PATH): the bundled libraries can't be loaded from a
# deeper folder. The deepest of them sits this far below the folder holding Ironbark.exe.
MAX_PATH = 260
DEEPEST = r"_internal\_sounddevice_data\portaudio-binaries\libportaudio64bit.dll"
ERROR_FILENAME_EXCED_RANGE = 206

TOO_DEEP = ("Ironbark can't start from this folder: its path is too long for Windows to load "
            "Ironbark's own files.\n\n{folder}\n\nMove the Ironbark folder somewhere with a shorter "
            "path, for example C:\\Ironbark, and start it from there.")


def path_too_long(error: OSError, folder: str) -> bool:
    """Did loading a bundled library fail because the folder is too deep?"""
    if getattr(error, "winerror", None) == ERROR_FILENAME_EXCED_RANGE:
        return True
    if "error 0x%x" % ERROR_FILENAME_EXCED_RANGE in str(error).lower():   # how cffi reports it
        return True
    return len(os.path.join(folder, DEEPEST)) >= MAX_PATH


def tell(text: str) -> None:
    """A plain Windows message box (the packaged program has no console)."""
    import ctypes

    ctypes.WinDLL("user32").MessageBoxW(None, text, "Ironbark", 0x10)   # MB_ICONERROR


def run() -> None:
    # A process started by multiprocessing runs its task here and exits, instead of opening a
    # second player. (Does nothing in the program's own process.)
    multiprocessing.freeze_support()
    folder = os.path.dirname(os.path.abspath(sys.executable))
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

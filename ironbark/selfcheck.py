"""`ironbark --self-check`: proves a copy of the player works, without playing anything.

Made for the packaged builds (PyInstaller), where a missing DLL or data file only shows up when the
program runs: the build script and the release workflow run the *built* `Ironbark.exe` with this
option, and a failure stops the build. Also useful to anyone whose copy won't start.

Checks: libsndfile loads and round-trips a tiny FLAC made in memory (bit for bit); PortAudio loads
and lists the system's audio (WASAPI on Windows) — no stream is opened, so nothing is heard; the icon
files are in the package; the main window opens, takes drag and drop, and closes.

Writes nothing of the user's: while the window is open, the settings folder (and with it playlists,
the library index and logs) is pointed at an empty temporary folder, deleted afterwards. The packaged program has no console, so the report can
also go to a file (`--self-check report.txt`). Exit code 0 = every check passed, 1 = one failed.
"""

from __future__ import annotations

import gc
import io
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from . import APP_NAME, __version__


def run(report_path: str | None = None) -> int:
    lines = [f"{APP_NAME} {__version__} self-check"
             + (" (packaged)" if getattr(sys, "frozen", False) else "")]
    failed = False
    for name, check in (("libsndfile", _check_sndfile), ("PortAudio", _check_portaudio),
                        ("icon files", _check_icons), ("main window", _check_window)):
        try:
            detail = check()
            lines.append(f"OK    {name}: {detail}")
        except Exception as error:  # every failure is reported, none ends the check early
            failed = True
            lines.append(f"FAIL  {name}: {error!r}")
            lines.extend("      " + line for line in traceback.format_exc().rstrip().splitlines())
    lines.append("FAILED" if failed else "PASSED")
    text = "\n".join(lines) + "\n"
    if sys.stdout is not None:      # None in the packaged program (no console)
        sys.stdout.write(text)
        sys.stdout.flush()
    if report_path:
        with open(report_path, "w", encoding="utf-8") as file:
            file.write(text)
    return 1 if failed else 0


def _check_sndfile() -> str:
    import numpy as np
    import soundfile as sf

    rate = 44100
    # 0.1 s of 24-bit samples, held as int32 the way libsndfile hands them over (low 8 bits zero)
    ramp = (np.arange(rate // 10, dtype=np.int32) * 997 % (1 << 24) - (1 << 23)) << 8
    signal = np.stack([ramp, ramp[::-1]], axis=1)
    data = io.BytesIO()
    sf.write(data, signal, rate, format="FLAC", subtype="PCM_24")
    data.seek(0)
    back, back_rate = sf.read(data, dtype="int32")
    if back_rate != rate or not np.array_equal(back, signal):
        raise RuntimeError("the FLAC made in memory didn't decode bit for bit")
    return f"{sf.__libsndfile_version__}, FLAC round trip bit for bit"


def _check_portaudio() -> str:
    import sounddevice as sd

    apis = [api["name"] for api in sd.query_hostapis()]
    if sys.platform == "win32" and "Windows WASAPI" not in apis:
        raise RuntimeError(f"no WASAPI among {apis}")
    outputs = sum(1 for device in sd.query_devices() if device["max_output_channels"] > 0)
    return f"{sd.get_portaudio_version()[1]}; {', '.join(apis)}; {outputs} outputs"


def _check_icons() -> str:
    from . import appicon

    for size in appicon.SIZES:
        appicon.load(size)
    if not (appicon.assets() / appicon.ICO_NAME).is_file():
        raise FileNotFoundError(appicon.ICO_NAME)
    return f"{len(appicon.SIZES)} sizes + {appicon.ICO_NAME}"


def _check_window() -> str:
    import tkinter as tk

    from . import appicon, settings
    from .engine import Player
    from .ui import PlayerWindow

    home = Path(tempfile.mkdtemp(prefix="ironbark-selfcheck-"))
    real_settings_path = settings.settings_path   # settings, playlists, index, logs all follow it
    try:
        settings.settings_path = lambda: home / "settings.json"
        appicon.set_app_id()
        root = tk.Tk()
        errors = []    # errors in the window code (Tk would only print them)
        root.report_callback_exception = lambda kind, value, tb: errors.append(
            "".join(traceback.format_exception(kind, value, tb)))
        window = PlayerWindow(root, Player())
        root.update()
        try:
            drops = root.tk.call("package", "present", "tkdnd")
        except tk.TclError:
            drops = None
        window.quit()
        del window, root
        gc.collect()   # free the window's Tk objects now, on Tk's thread (freed on another, Tk aborts)
        if errors:
            raise RuntimeError("error in the window code:\n" + errors[0])
        if drops is None:
            raise RuntimeError("the window opened, but drag and drop (tkdnd) didn't load")
        return f"opened and closed; Tk {tk.TkVersion}, drag and drop (tkdnd {drops})"
    finally:
        settings.settings_path = real_settings_path
        shutil.rmtree(home, ignore_errors=True)

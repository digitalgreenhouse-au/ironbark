"""`ironbark --self-check`: proves a copy of the player works, without playing anything.

Made for the packaged builds (PyInstaller), where a missing DLL or data file only shows up when the
program runs: the build script (packaging/windows/build.ps1, which the release workflow in
.github/workflows/release.yml runs) runs the *built* `Ironbark.exe` with this option, and a
failure stops the build. Also useful to anyone whose copy won't start.

Checks: libsndfile loads and round-trips a tiny FLAC made in memory (bit for bit); PortAudio loads
(in the packaged program, the copy that comes with it) and lists the system's audio (WASAPI on
Windows) — no stream is opened, so nothing is heard; on
Windows, pycaw/comtypes read Windows' own list of outputs, and Windows' media controls and taskbar
buttons can be reached (nothing is shown); XML reader the rip check needs; the library database works — SQLite with its write-ahead log
(WAL) and the song search index (FTS5), which a different Python's SQLite might lack; the indexer
reads a library through a worker process (in the packaged program that's Ironbark.exe started
again, which must run the task and not open a second player — or load Tk); the icon files are in
the package; the bundled components' licences are there for About → Licences; one player at a
time works (a second launch is told one is running and hands its files over — under a name of the
check's own, so a player that's really running is never reached); the main window opens, takes
drag and drop, and closes. Most of these fail quietly inside the player (logged, feature off), so
only this check catches them missing from a build.

Writes nothing of the user's: while the window is open, the settings folder (and with it playlists,
the library database and logs) is pointed at an empty temporary folder, deleted afterwards. Run
from the command line (`hidden=True`), its windows are made on a hidden Windows desktop of their
own: real windows, opened, laid out and closed, but never shown in front of whatever the person at
the computer is doing. The packaged program has no console, so the report can also go to a file
(`--self-check report.txt`). Exit code 0 = every check passed, 1 = one failed.
"""

from __future__ import annotations

import gc
import io
import json
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path

from . import APP_NAME, __version__, platforms

HIDDEN_DESKTOP = "IronbarkSelfCheck"
_hidden = False   # this run's windows are on the hidden desktop


def run(report_path: str | None = None, hidden: bool = False) -> int:
    """Every check, in turn. `hidden`: first move this thread to a hidden desktop (it must own no
    window yet — so before any check, as some make hidden windows of their own)."""
    global _hidden
    if hidden:
        _hidden = _use_hidden_desktop()
    lines = [f"{APP_NAME} {__version__} self-check"
             + (" (packaged)" if getattr(sys, "frozen", False) else "")]
    failed = False
    for name, check in (("libsndfile", _check_sndfile), ("PortAudio", _check_portaudio),
                        ("Windows audio", _check_windows_audio), ("media controls", _check_media_controls),
                        ("library database", _check_library_db),
                        ("library indexer", _check_indexer), ("library check", _check_library_check),
                        ("icon files", _check_icons),
                        ("licences", _check_licences), ("single instance", _check_single_instance),
                        ("main window", _check_window)):
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


def _use_hidden_desktop() -> bool:
    """Move this thread to a desktop of its own, so the windows the checks open never appear on
    screen. Windows only; False (carry on on the normal desktop) when it can't be done — the
    thread already owns a window, or this session may not make desktops."""
    if not platforms.IS_WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.SetThreadDesktop.restype = wintypes.BOOL
    user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    generic_all = 0x10000000
    desktop = user32.CreateDesktopW(HIDDEN_DESKTOP, None, None, 0, generic_all, None)
    if not desktop:
        return False
    if not user32.SetThreadDesktop(desktop):
        user32.CloseDesktop(desktop)
        return False
    return True   # (the handle stays open until the program ends: closing it would strand the windows)


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

    where = ""
    if getattr(sys, "frozen", False):
        # The packaged program plays through the PortAudio it was built and tested with, never a
        # copy another program left on the PATH (packaging/windows/launch.py sees to that).
        if not Path(sd._libname).resolve().is_relative_to(Path(sys._MEIPASS).resolve()):
            raise RuntimeError(f"PortAudio was loaded from outside the program: {sd._libname}")
        where = "; the program's own copy"
    apis = [api["name"] for api in sd.query_hostapis()]
    if platforms.IS_WINDOWS and "Windows WASAPI" not in apis:
        raise RuntimeError(f"no WASAPI among {apis}")
    outputs = sum(1 for device in sd.query_devices() if device["max_output_channels"] > 0)
    return f"{sd.get_portaudio_version()[1]}{where}; {', '.join(apis)}; {outputs} outputs"


def _check_windows_audio() -> str:
    """pycaw / comtypes: Windows' own output list (device names, hardware volume and the Windows
    check's recording all go through them). Asked on a thread of its own, as the player does."""
    if not platforms.IS_WINDOWS:
        return "not used on this system"
    from importlib import metadata

    from . import devices

    outcome: dict = {}

    def work():
        try:
            import comtypes

            comtypes.CoInitialize()
            outcome["outputs"] = len(devices._active_outputs())   # the player's own Core Audio query
        except Exception as error:  # reported by the caller
            outcome["error"] = error

    thread = threading.Thread(target=work, name="self-check-core-audio", daemon=True)
    thread.start()
    thread.join(10)
    if "error" in outcome:
        raise outcome["error"]
    if "outputs" not in outcome:
        raise TimeoutError("Windows' list of outputs didn't answer in 10 s")
    return (f"pycaw {metadata.version('pycaw')}, comtypes {metadata.version('comtypes')}; "
            f"{outcome['outputs']} active outputs")


def _check_media_controls() -> str:
    """Windows' media controls (SMTC) and the taskbar's thumbnail buttons can be reached. Only
    the starting points are made: nothing is attached to a window, so nothing appears."""
    if not platforms.IS_WINDOWS:
        return "not used on this system"
    import comtypes
    from comtypes import CLSCTX_INPROC_SERVER, GUID, CoCreateInstance

    from . import mediakeys

    try:
        comtypes.CoInitialize()   # (when this thread already has COM, this just counts once more)
        entered = True
    except OSError:               # this thread is in the other kind of apartment: that works too
        entered = False
    controls = taskbar = None
    try:
        w = mediakeys._winrt()
        controls = mediakeys._factory("Windows.Media.SystemMediaTransportControls",
                                      w.ISystemMediaTransportControlsInterop)
        taskbar = CoCreateInstance(GUID(mediakeys.CLSID_TASKBAR_LIST), interface=w.ITaskbarList3,
                                   clsctx=CLSCTX_INPROC_SERVER)
        if not controls or not taskbar:
            raise RuntimeError("Windows handed back nothing")
    finally:
        del controls, taskbar     # released before this thread lets go of COM
        if entered:
            comtypes.CoUninitialize()
    return "media controls (WinRT) and taskbar buttons (ITaskbarList3) available"


def _check_library_db() -> str:
    """The library database, made in an empty temporary folder: SQLite keeps it with a write-ahead
    log (WAL, so searches never wait for the indexer), the song search index (FTS5 with the
    unicode61 word splitter) can be made, and a song stored there is found by a search that has
    to fold its letters ("lodz orsted" finds "Łódź" by Ørsted)."""
    import sqlite3

    from . import library_db
    from .library import Track

    problem = library_db.fts_problem()
    if problem:
        raise RuntimeError(problem)
    home = Path(tempfile.mkdtemp(prefix="ironbark-selfcheck-db-"))
    try:
        root = home / "Music"
        with library_db.LibraryDB.open(home / library_db.FILE_NAME, [root]) as db:
            mode = db._con().execute("PRAGMA journal_mode").fetchone()[0]
            if mode != "wal":
                raise RuntimeError(f"the database's log is {mode!r}, not WAL")
            song = Track(path=root / "Album" / "01 Song.flac", title="Łódź", artist="Ørsted")
            db.apply(library_db.folder_result(root, root / "Album", {"01 Song.flac": (1, 1, 0.0)},
                                              {"01 Song.flac": [song]}))
            if [hit.track.title for hit in db.search_songs("lodz orsted").rows] != ["Łódź"]:
                raise RuntimeError("the song search didn't find the test song (Polish and Danish letters) "
                                   "from \"lodz orsted\"")
            if db.quick_check() != "ok":
                raise RuntimeError("SQLite's own check of the new database failed")
        # (plain ASCII: the report may go to a console that can't show other letters)
        return f"SQLite {sqlite3.sqlite_version}, WAL, song search (FTS5, unicode61) ignores accents"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _check_indexer() -> str:
    """The indexer, on a tiny library made in an empty temporary folder: its tags read in a worker
    process (as a big library's first look reads them), stored, and found again; and the worker
    process itself checked — it must have loaded neither Tk nor the player."""
    import numpy as np
    import soundfile as sf

    from . import indexer, library_db

    home = Path(tempfile.mkdtemp(prefix="ironbark-selfcheck-index-"))
    try:
        root = home / "Music"
        for n in range(1, 4):
            folder = root / f"Album {n}"
            folder.mkdir(parents=True)
            sf.write(folder / "01 Song.flac", np.zeros((4410, 2), dtype=np.int16), 44100, format="FLAC")
        pool = indexer.make_pool(1)
        if pool is None:
            raise RuntimeError("no worker process could be started")
        try:
            loaded = pool.submit(indexer.worker_check).result(timeout=120)
        finally:
            pool.shutdown(wait=True)
        if loaded["tkinter"] or loaded["player"]:
            raise RuntimeError(f"a worker process loaded what it mustn't: {loaded}")
        with library_db.LibraryDB.open(home / library_db.FILE_NAME) as db:
            look = indexer.Indexer(db, pool_min=0, fast=1, gentle=1)
            diff = look.check([root])
            if diff.error or len(diff.added) != 3 or not look.used_pool:
                raise RuntimeError(f"the indexer's first look went wrong: {diff}")
            if len(db.albums()) != 3 or not look.check([root]).nothing:
                raise RuntimeError("the indexer didn't store the test library")
        return "3 albums read in a worker process (no Tk, no player there), stored and checked again"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _check_library_check() -> str:
    """The whole-library quality check, on two songs made in an empty temporary folder: checked in a
    worker process (as a library's are), one found clipped, both stored."""
    import numpy as np
    import soundfile as sf

    from . import indexer, library_check, library_db

    home = Path(tempfile.mkdtemp(prefix="ironbark-selfcheck-quality-"))
    try:
        root = home / "Music"
        rng = np.random.default_rng(1)
        quiet = (0.1 * rng.standard_normal((44100, 2))).clip(-0.9, 0.9)
        loud = quiet.copy()
        loud[22050: 22055] = 1.0
        for name, data in (("Quiet", quiet), ("Loud", loud)):
            (root / name).mkdir(parents=True)
            sf.write(root / name / "01 Song.flac", data, 44100, format="FLAC", subtype="PCM_16")
        with library_db.LibraryDB.open(home / library_db.FILE_NAME) as db:
            indexer.Indexer(db).check([root])
            checker = library_check.Checker(lambda: db, workers=1)
            checker.start()
            if not checker.idle(120) or checker.status.error:
                raise RuntimeError(f"the check didn't finish: {checker.status}")
            checker.close()
            found = {hit.track.path.parent.name: look.words for hit, look in library_check.needs_look(db)}
            if len(db.check_states()) != 2 or found != {"Loud": ("clipping",)}:
                raise RuntimeError(f"the check found the wrong things: {found}")
        return "2 songs checked in a worker process, the clipped one found, both stored"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _check_icons() -> str:
    from . import appicon

    for size in appicon.SIZES:
        appicon.load(size)
    if not (appicon.assets() / appicon.ICO_NAME).is_file():
        raise FileNotFoundError(appicon.ICO_NAME)
    return f"{len(appicon.SIZES)} sizes + {appicon.ICO_NAME}"


def _check_licences() -> str:
    """About → Licences lists the bundled components' licences: in the packaged program, the
    licenses folder the build puts beside Ironbark.exe (with the summary, THIRD_PARTY_LICENSES.md,
    first); run from source, the source tree's own files. Without them the list would be empty."""
    from . import help_window

    documents = help_window.licence_documents()
    if not documents:
        raise FileNotFoundError("no licence files for About → Licences"
                                + (" (no licenses folder beside the program)" if getattr(sys, "frozen", False)
                                   else ""))
    if documents[0][1].name != help_window.THIRD_PARTY:
        raise FileNotFoundError(f"{help_window.THIRD_PARTY} is missing from the licences")
    empty = [path.name for _name, path in documents if path.stat().st_size == 0]
    if empty:
        raise RuntimeError(f"empty licence files: {', '.join(empty)}")
    where = "the licenses folder" if help_window.licence_folder() is not None else "the source tree"
    return f"{len(documents)} files for About -> Licences, from {where}"


def _check_single_instance() -> str:
    import uuid

    from . import instance

    key = f"{APP_NAME}-selfcheck-{uuid.uuid4().hex[:12]}"
    folder = Path(tempfile.mkdtemp(prefix="ironbark-selfcheck-"))   # (the lock file's place off Windows)
    try:
        running = instance.claim(key, folder)
        try:
            later = instance.claim(key, folder)
            if not running.first or later.first:
                raise RuntimeError("a second launch wasn't told a player is already running")
            if not later.forward(["check.flac"], front=False, timeout=5):
                raise RuntimeError("a second launch couldn't hand its files to the running player")
            if running.requests.get(timeout=5) != instance.Request(("check.flac",), False):
                raise RuntimeError("the running player received something other than what was sent")
        finally:
            running.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return "named mutex + pipe" if platforms.IS_WINDOWS else "lock file + local socket"


def _check_window() -> str:
    import tkinter as tk

    from . import appicon, settings
    from .engine import Player
    from .ui import PlayerWindow

    home = Path(tempfile.mkdtemp(prefix="ironbark-selfcheck-"))
    real_settings_path = settings.settings_path   # settings, playlists, index, logs all follow it
    try:
        settings.settings_path = lambda: home / "settings.json"
        # The check window registers nothing system-wide (Windows' media panel, the taskbar's
        # buttons, a keyboard hook) and doesn't start looking for new music; "media controls"
        # above checks those pieces are there without taking them over. Its library is an empty
        # folder of its own, so it never starts reading the user's Music folder (a big one would
        # start worker processes), and the network playback cache stays off.
        music = home / "Music"
        music.mkdir()
        (home / "settings.json").write_text(
            json.dumps({"media_controls": False, "library_folders": [str(music)],
                        "library_folder": str(music), "library_check": "off", "local_network": False}),
            encoding="utf-8")
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
        where = " (on a hidden desktop)" if _hidden else ""
        return f"opened and closed{where}; Tk {tk.TkVersion}, drag and drop (tkdnd {drops})"
    finally:
        from . import library_index

        library_index.close_shared()   # (the window's library database is in `home`: let go of it first)
        settings.settings_path = real_settings_path
        shutil.rmtree(home, ignore_errors=True)

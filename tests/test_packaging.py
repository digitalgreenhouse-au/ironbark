"""The Windows installer script, the packaged program's own sound library (PortAudio) and its
"folder too deep" message. No installer is run and nothing is installed: the script is read, and
compiled when Inno Setup is on this computer; the launcher's parts are checked on their own."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from ironbark import APP_NAME, netcache, selfcheck, settings

REPO = Path(__file__).resolve().parents[1]
WINDOWS = REPO / "packaging" / "windows"
ISS = (WINDOWS / "ironbark.iss").read_text(encoding="utf-8")


def load_launcher():
    spec = importlib.util.spec_from_file_location("ironbark_launch_packaging", WINDOWS / "launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_value(name: str) -> str | None:
    section = ISS.split("\n[Setup]", 1)[1].split("\n[", 1)[0]
    found = re.search(rf"^{name}=(.*)$", section, re.M)
    return found.group(1).strip() if found else None


def pascal(name: str) -> str:
    """One procedure or function of the [Code] section, from its first line to its closing 'end;'."""
    code = ISS.split("\n[Code]", 1)[1]
    start = re.search(rf"^(procedure|function) {name}\b", code, re.M).start()
    return code[start:start + re.search(r"^end;", code[start:], re.M).end()]


def inno_compiler() -> str | None:
    places = [shutil.which("iscc")] + [os.path.join(os.environ.get(var, ""), "Inno Setup 6", "ISCC.exe")
                                       for var in ("ProgramFiles(x86)", "ProgramFiles")]
    places.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"))
    return next((place for place in places if place and os.path.isfile(place)), None)


# -- uninstalling ---------------------------------------------------------------------------------
def test_uninstalling_offers_to_remove_every_folder_the_player_keeps_things_in():
    # On Windows the settings folder is under %APPDATA% and the library database and the network
    # playback cache under %LOCALAPPDATA% (Inno's {userappdata} and {localappdata}).
    assert settings.settings_path().parent.name == APP_NAME and settings.cache_folder().name == APP_NAME
    assert netcache.folder() == settings.cache_folder() / "NetCache"
    step = pascal("CurUninstallStepChanged")
    folders = re.findall(r"(\w+) := ExpandConstant\('\{(userappdata|localappdata)\}\\" + APP_NAME + r"'\)", step)
    assert sorted(kind for _, kind in folders) == ["localappdata", "userappdata"]
    for variable, _ in folders:
        assert f"DirExists({variable})" in step               # either folder there: the question is asked
        assert f"DelTree({variable}, True, True, True)" in step   # Yes removes both
    assert "if not DirExists(" not in step     # (no single folder missing skips the question)
    assert "library database" in step and "NetCache" in step


@pytest.mark.skipif(inno_compiler() is None, reason="Inno Setup 6 isn't installed here")
def test_the_installer_script_compiles(tmp_path):
    program = tmp_path / "Ironbark"
    program.mkdir()
    (program / "Ironbark.exe").write_bytes(b"")
    made = subprocess.run([inno_compiler(), "/Q", "/DAppVersion=0.0.0", f"/DSourceDir={program}",
                           f"/DOutputDir={tmp_path}", str(WINDOWS / "ironbark.iss")],
                          capture_output=True, text=True, timeout=300,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert made.returncode == 0, made.stdout + made.stderr
    assert (tmp_path / "Ironbark-Setup-0.0.0.exe").is_file()


# -- upgrading while the player is open -----------------------------------------------------------
def test_setup_doesnt_promise_to_reopen_a_player_windows_cant_reopen():
    # Restart Manager closes the player as its close button would (WM_CLOSE). Setup can then only
    # start again a program that registered with Windows for it; otherwise its "Setup will attempt
    # to restart the applications" would be untrue.
    assert setup_value("CloseApplications") == "yes"
    registers = any("RegisterApplicationRestart" in path.read_text(encoding="utf-8")
                    for path in (REPO / "ironbark").rglob("*.py"))
    assert registers or setup_value("RestartApplications") == "no"


# -- the packaged program's own PortAudio ---------------------------------------------------------
def test_the_packaged_program_ignores_other_portaudio_copies_and_the_asio_switch(monkeypatch):
    import ctypes.util

    monkeypatch.setattr(ctypes.util, "find_library", lambda name: rf"C:\Elsewhere\{name}.dll")
    monkeypatch.setenv("SD_ENABLE_ASIO", "1")
    load_launcher().use_own_portaudio()
    assert "SD_ENABLE_ASIO" not in os.environ
    for name in ("portaudio", "bin\\libportaudio-2.dll", "lib/libportaudio.dylib"):   # what sounddevice asks for
        assert ctypes.util.find_library(name) is None
    assert ctypes.util.find_library("sndfile") == r"C:\Elsewhere\sndfile.dll"   # nothing else changes


def test_the_launcher_keeps_to_its_own_portaudio_only_when_packaged(monkeypatch):
    calls = []
    fake = types.ModuleType("ironbark.__main__")
    fake.main = lambda: calls.append("main")
    monkeypatch.setitem(sys.modules, "ironbark.__main__", fake)
    launch = load_launcher()
    monkeypatch.setattr(launch, "use_own_portaudio", lambda: calls.append("own portaudio"))
    monkeypatch.setattr(launch, "check_folder", lambda folder: None)
    launch.run()
    assert calls == ["main"]
    calls.clear()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    launch.run()
    assert calls == ["own portaudio", "main"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_sounddevice_then_loads_the_copy_it_came_with(tmp_path):
    # A real sounddevice in a fresh process, with SD_ENABLE_ASIO set and another portaudio.dll in a
    # folder on the PATH. (It only lists the sound devices: nothing is played.)
    import _sounddevice_data

    bundled = Path(next(iter(_sounddevice_data.__path__))) / "portaudio-binaries" / "libportaudio64bit.dll"
    other = tmp_path / "other"
    other.mkdir()
    shutil.copyfile(bundled, other / "portaudio.dll")
    script = ("import importlib.util, sys\n"
              f"spec = importlib.util.spec_from_file_location('launch', {str(WINDOWS / 'launch.py')!r})\n"
              "launch = importlib.util.module_from_spec(spec); spec.loader.exec_module(launch)\n"
              "launch.use_own_portaudio()\n"
              "import sounddevice\n"
              "print(sounddevice._libname)\n")
    environment = dict(os.environ, SD_ENABLE_ASIO="1", PATH=f"{other}{os.pathsep}{os.environ.get('PATH', '')}")
    ran = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                         env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert ran.returncode == 0, ran.stderr
    assert os.path.normcase(ran.stdout.strip()) == os.path.normcase(str(bundled))


def test_the_self_check_fails_a_packaged_program_playing_through_another_portaudio(monkeypatch, tmp_path):
    import sounddevice

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)   # the program's own folder
    with pytest.raises(RuntimeError, match="outside the program"):
        selfcheck._check_portaudio()
    monkeypatch.setattr(sys, "_MEIPASS", str(Path(sounddevice._libname).parent))
    assert "the program's own copy" in selfcheck._check_portaudio()


# -- a folder too deep ----------------------------------------------------------------------------
def packaged_load_error(winerror: int) -> OSError:
    """What ctypes raises in the packaged program for a library Windows won't load: PyInstaller's
    own error, with Windows' as its cause (and no error code of its own)."""
    try:
        try:
            raise OSError(None, "The filename or extension is too long", None, winerror)
        except OSError as windows:
            raise OSError("Failed to load dynlib/dll 'libportaudio64bit.dll'. Most likely this dynlib/dll "
                          "was not found when the application was frozen.") from windows
    except OSError as wrapped:
        return wrapped


def test_a_folder_too_deep_is_found_behind_the_packaged_programs_own_error():
    launch = load_launcher()
    short = r"C:\Ironbark"
    assert launch.path_too_long(packaged_load_error(206), short)
    assert not launch.path_too_long(packaged_load_error(126), short)   # (not found: some other problem)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_the_packaged_program_names_a_folder_too_deep_before_starting(monkeypatch):
    # Measured with a real build: unzipped into a folder 182 characters long, its PortAudio (68
    # characters further down, 251 in all) won't load, and the packaged program's error says only
    # "Failed to load dynlib/dll".
    started = []
    fake = types.ModuleType("ironbark.__main__")
    fake.main = lambda: started.append(True)
    monkeypatch.setitem(sys.modules, "ironbark.__main__", fake)
    launch = load_launcher()
    told = []
    monkeypatch.setattr(launch, "tell", told.append)
    folder = "C:\\" + "d" * 179
    monkeypatch.setattr(launch.sys, "executable", folder + "\\Ironbark.exe")
    import ctypes

    def refused(path):
        raise packaged_load_error(206)
    monkeypatch.setattr(ctypes, "WinDLL", refused)
    with pytest.raises(SystemExit) as stop:
        launch.run()
    assert stop.value.code == 1 and "shorter" in told[0] and folder in told[0] and started == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_loads_a_library_from_a_path_exactly_as_long_as_the_launcher_allows(tmp_path):
    # The launcher's figure is Windows' own: a library whose full path is that long loads, one
    # character more is refused as too long (even with Windows' long paths switched on).
    import ctypes

    import _sounddevice_data

    launch = load_launcher()
    library = Path(next(iter(_sounddevice_data.__path__))) / "portaudio-binaries" / "libportaudio64bit.dll"
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.FreeLibrary.argtypes = [ctypes.c_void_p]
    for length in (launch.LONGEST_LOAD, launch.LONGEST_LOAD + 1):
        base = tmp_path / str(length)
        pad = length - len(str(base)) - 2 - len(library.name)
        if pad < 1:
            pytest.skip("the temporary folder is already too deep")
        copy = base / ("d" * pad) / library.name
        copy.parent.mkdir(parents=True)
        shutil.copyfile(library, copy)
        assert len(str(copy)) == length
        if length == launch.LONGEST_LOAD:
            kernel32.FreeLibrary(ctypes.WinDLL(str(copy))._handle)
        else:
            with pytest.raises(OSError) as refused:
                ctypes.WinDLL(str(copy))
            assert launch.path_too_long(refused.value, r"C:\Ironbark")

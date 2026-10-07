"""The packaged program's launcher, the build's licence check, the declared dependencies and the
GitHub workflows (no PyInstaller run here: the parts are checked on their own)."""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WINDOWS = REPO / "packaging" / "windows"
KEPT = REPO / "packaging" / "licenses"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- packaging/windows/launch.py ----------------------------------------------------------------
def test_loading_the_launcher_doesnt_start_the_player(monkeypatch):
    # A process started by multiprocessing loads the program's script again: it mustn't open a player.
    started = []
    fake = types.ModuleType("ironbark.__main__")
    fake.main = lambda: started.append(True)
    monkeypatch.setitem(sys.modules, "ironbark.__main__", fake)
    launch = load("ironbark_launch", WINDOWS / "launch.py")
    assert started == []
    launch.run()
    assert started == [True]


class Unloadable(types.ModuleType):
    """Importing the player fails the way a bundled DLL that can't be loaded makes it fail."""

    message = "cannot load library 'C:\\deep\\libportaudio64bit.dll': error 0xce"

    def __getattr__(self, name):
        raise OSError(self.message)


def test_a_folder_too_deep_for_windows_gets_a_plain_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "ironbark.__main__", Unloadable("ironbark.__main__"))
    launch = load("ironbark_launch", WINDOWS / "launch.py")
    told = []
    monkeypatch.setattr(launch, "tell", told.append)
    with pytest.raises(SystemExit) as stop:
        launch.run()
    assert stop.value.code == 1 and "shorter" in told[0]


def test_other_start_up_errors_still_show_as_they_are(monkeypatch):
    module = Unloadable("ironbark.__main__")
    module.message = "cannot load library 'libsndfile_x64.dll': error 0x7e"
    monkeypatch.setitem(sys.modules, "ironbark.__main__", module)
    launch = load("ironbark_launch", WINDOWS / "launch.py")
    monkeypatch.setattr(launch, "tell", lambda text: pytest.fail("not a path problem"))
    monkeypatch.setattr(launch.sys, "executable", r"C:\Ironbark\Ironbark.exe")
    with pytest.raises(OSError, match="0x7e"):
        launch.run()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_a_folder_too_deep_is_named_before_the_player_starts(monkeypatch):
    # The player itself loads the sound libraries only once it opens its window, so the launcher
    # tries the deepest one first: the plain message, never the general "couldn't start" box.
    started = []
    fake = types.ModuleType("ironbark.__main__")
    fake.main = lambda: started.append(True)
    monkeypatch.setitem(sys.modules, "ironbark.__main__", fake)
    launch = load("ironbark_launch", WINDOWS / "launch.py")
    told = []
    monkeypatch.setattr(launch, "tell", told.append)
    deep = "C:\\" + "d" * (launch.MAX_PATH - len(launch.DEEPEST))
    monkeypatch.setattr(launch.sys, "executable", deep + "\\Ironbark.exe")
    with pytest.raises(SystemExit) as stop:
        launch.run()
    assert stop.value.code == 1 and "shorter" in told[0] and deep in told[0] and started == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_another_library_problem_is_left_to_the_players_own_start_up(monkeypatch):
    started = []
    fake = types.ModuleType("ironbark.__main__")
    fake.main = lambda: started.append(True)
    monkeypatch.setitem(sys.modules, "ironbark.__main__", fake)
    launch = load("ironbark_launch", WINDOWS / "launch.py")
    monkeypatch.setattr(launch, "tell", lambda text: pytest.fail("not a path problem"))
    monkeypatch.setattr(launch.sys, "executable", r"C:\Ironbark\Ironbark.exe")
    import ctypes

    def unloadable(path):
        raise OSError("cannot load library: error 0x7e")
    monkeypatch.setattr(ctypes, "WinDLL", unloadable)
    launch.run()
    assert started == [True]


def test_what_counts_as_a_path_too_long():
    launch = load("ironbark_launch", WINDOWS / "launch.py")
    short = r"C:\Ironbark"
    assert launch.path_too_long(OSError("cannot load library 'x.dll': error 0xce"), short)
    error = OSError()
    error.winerror = 206
    assert launch.path_too_long(error, short)
    assert not launch.path_too_long(OSError("error 0x7e"), short)
    deep = "C:\\" + "d" * (launch.MAX_PATH - len(launch.DEEPEST) - 3)
    assert launch.path_too_long(OSError("error 0x7e"), deep)


# -- the build's licence check ------------------------------------------------------------------
@pytest.fixture
def licence_check():
    return load("licence_check", WINDOWS / "licence_check.py")


def test_every_bundled_library_needs_its_licence_file(tmp_path, licence_check):
    app = tmp_path / "Ironbark"
    for relative in ("python312.dll", "libffi-8.dll", "_ssl.pyd", "_soundfile_data/libsndfile_x64.dll",
                     "something_new.dll"):
        path = app / "_internal" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    (app / "licenses").mkdir()
    for name in ("Python-LICENSE.txt", "zlib-LICENSE.txt", "libffi-LICENSE.txt"):
        (app / "licenses" / name).write_text("…", encoding="utf-8")
    problems = licence_check.gaps(app)
    assert "something_new.dll: no licence listed for it in licence_check.COVERED_BY" in problems
    assert "_ssl.pyd: licenses\\OpenSSL-LICENSE.txt is missing" in problems
    assert any(p.startswith("_soundfile_data/libsndfile_x64.dll: licenses\\LAME-notice.txt") for p in problems)
    assert not any(p.startswith(("python312.dll", "libffi-8.dll")) for p in problems)


def test_the_library_databases_sqlite_is_covered(licence_check):
    assert licence_check.needed("sqlite3.dll") == ["SQLite-public-domain.txt"]
    assert licence_check.needed("_sqlite3.pyd") == ["Python-LICENSE.txt", "SQLite-public-domain.txt"]


# Licence files ironbark.spec makes itself (from the Python install, soundfile, the repo).
MADE_BY_THE_SPEC = {"Python-LICENSE.txt", "tk8.6-license.terms.txt", "libsndfile-COPYING.txt",
                    "THIRD_PARTY_LICENSES.md"}


def test_every_licence_a_rule_names_is_kept_here_or_made_by_the_build(licence_check):
    kept = {path.name for path in KEPT.glob("*.txt")}
    for _, names in licence_check.COVERED_BY:
        for name in names:
            if re.search(r"\.(txt|md)$", name):
                assert name in kept | MADE_BY_THE_SPEC, name
    used = {name for _, names in licence_check.COVERED_BY for name in names}
    assert kept <= used, f"kept but not tied to any library: {kept - used}"


def test_each_kept_licence_says_where_it_came_from():
    for path in KEPT.glob("*.txt"):
        header = path.read_text(encoding="utf-8").split("=" * 20, 1)[0]
        assert "Source" in header and "https://" in header and "copied " in header, path.name


def test_the_spec_copies_the_kept_licences_and_runs_the_check():
    spec = (WINDOWS / "ironbark.spec").read_text(encoding="utf-8")
    assert '"packaging" / "licenses"' in spec and "licence_check.gaps(" in spec


# -- dependencies and workflows -------------------------------------------------------------------
def test_comtypes_is_declared_since_the_player_uses_it_directly():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert any(re.match(r"comtypes\s*>=\s*1\.4\.8\s*;\s*sys_platform\s*==\s*'win32'", d)
               for d in project["dependencies"])


def test_github_actions_are_pinned_to_a_commit():
    workflows = sorted((REPO / ".github" / "workflows").glob("*.yml"))
    assert {w.name for w in workflows} >= {"tests.yml", "release.yml"}
    for workflow in workflows:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            used = re.search(r"uses:\s*(\S+)", line)
            if used:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", used.group(1)), f"{workflow.name}: {line}"


def test_the_release_workflow_runs_the_build_script():
    text = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "packaging\\windows\\build.ps1" in text and "if-no-files-found: error" in text

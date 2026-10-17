"""The Linux build's parts (packaging/linux): the launcher's own PortAudio, the licence check's rules
(no GPL library ships) and the desktop launcher entry. Nothing is built here; build.sh does that."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LINUX = REPO / "packaging" / "linux"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"ironbark_linux_{name}", LINUX / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_packaged_program_plays_through_its_own_portaudio(monkeypatch, tmp_path):
    import ctypes.util

    (tmp_path / "libportaudio.so.2").write_bytes(b"")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: f"/usr/lib/lib{name}.so")
    load("launch").use_own_portaudio()
    assert ctypes.util.find_library("portaudio") == str(tmp_path / "libportaudio.so.2")
    assert ctypes.util.find_library("X11") == "/usr/lib/libX11.so"   # nothing else changes


def test_the_launcher_keeps_to_its_own_portaudio_only_when_packaged(monkeypatch):
    calls = []
    fake = types.ModuleType("ironbark.__main__")
    fake.main = lambda: calls.append("main")
    monkeypatch.setitem(sys.modules, "ironbark.__main__", fake)
    launch = load("launch")
    monkeypatch.setattr(launch, "use_own_portaudio", lambda: calls.append("own portaudio"))
    launch.run()
    assert calls == ["main"]
    calls.clear()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    launch.run()
    assert calls == ["own portaudio", "main"]


def test_no_gpl_library_ships_but_the_lgpl_and_the_gcc_runtime_may():
    check = load("licence_check")
    for licence in ("GPL-2", "GPL-2+", "GPL-3+", "GPL-2.0-or-later"):
        assert check.gpl_only(licence), licence
    for licence in ("LGPL-2.1+", "MIT", "BSD-3-clause", "GPL-3+ with the GCC Runtime Library Exception",
                    "GPL-2+ or MIT", "Apache-2.0", "PSF-2.0", ""):
        assert not check.gpl_only(licence), licence


def test_the_licence_is_read_from_a_machine_readable_copyright_file():
    check = load("licence_check")
    text = ("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n\n"
            "Files: *\nCopyright: 2020 Someone\nLicense: GPL-2+\n\n"
            "Files: debian/*\nLicense: MIT\n")
    assert check._licence_of(text) == "GPL-2+"
    assert check._licence_of("Copyright 2020 Someone. Permission is hereby granted …") == ""


def test_the_desktops_own_libraries_are_left_out_and_only_at_the_top():
    check = load("licence_check")
    for name in ("libX11.so.6", "libxcb-render.so.0", "libfontconfig.so.1", "libasound.so.2", "libjack.so.0",
                 "libdb-5.3.so", "libmvec.so.1"):
        assert check.from_the_desktop(name), name
    for name in ("libportaudio.so.2", "libtk8.6.so", "pillow.libs/libxcb-ad31f5a3.so.1.1.0"):
        assert not check.from_the_desktop(name), name


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="desktop-file-utils isn't installed here")
def test_the_launcher_entry_is_valid_and_names_the_window_class():
    entry = LINUX / "ironbark.desktop"
    found = subprocess.run(["desktop-file-validate", str(entry)], capture_output=True, text=True)
    assert found.returncode == 0 and not found.stdout.strip(), found.stdout
    assert "StartupWMClass=Ironbark\n" in entry.read_text(encoding="utf-8")


def test_every_runtime_dependency_for_linux_ships_its_licence():
    """The build copies the licence files of the packages in the spec's SHIPPED list: a dependency
    missing from it would ship without its licence (MIT and BSD ask for the notice to go along)."""
    import re
    import tomllib

    spec = (LINUX / "ironbark.spec").read_text(encoding="utf-8")
    shipped = {name.lower() for name in re.findall(r'"([^"]+)"', re.search(r"SHIPPED = \[(.*?)\]", spec).group(1))}
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for requirement in project["dependencies"]:
        name, _, marker = requirement.partition(";")
        if "sys_platform" in marker and "linux" not in marker:
            continue   # (Windows or Mac only)
        name = re.split(r"[<>=!~\[ ]", name.strip(), maxsplit=1)[0].lower()
        assert name in shipped, f"{name} ships on Linux but its licence isn't copied (SHIPPED in ironbark.spec)"

"""Which licence covers each native library in the Linux build, and a check that none is missing or
GPL. ironbark.spec runs `gaps()` after copying the licences and stops the build if it finds any.

Two kinds of library end up in the build. Those that come inside a Python package (numpy's,
Pillow's, libsndfile, tkdnd) are covered by that package's licence, by the rules in COVERED_BY.
The rest come from the distribution (Python itself, Tcl/Tk, PortAudio, OpenSSL …):
`copy_system_licences` finds the package each came from (dpkg) and copies that package's
copyright file, as the distribution ships it, into licenses/, with SYSTEM-LIBRARIES.txt saying
which is which. A library whose package's licence is the GPL (not the LGPL, and not with a
runtime exception) stops the build: Ironbark is MIT and has no GPL dependency.
"""

from __future__ import annotations

import re
import subprocess
import sysconfig
from pathlib import Path

# The desktop's own libraries, left out of the build (see ironbark.spec), by the name PyInstaller
# gives them at the top of the folder. Also the C library's libmvec (it must be the system's own),
# and Berkeley DB, which the distribution's PortAudio is linked to (with JACK) but never calls: its
# Sleepycat licence is a copyleft one, so it isn't shipped; Debian, Ubuntu and Mint all have it.
FROM_THE_DESKTOP = re.compile(
    r"lib(X11|X11-xcb|xcb(-[a-z0-9-]+)?|Xau|Xdmcp|Xext|Xft|Xrender|Xss|Xinerama|Xrandr|Xcursor|Xfixes|Xi"
    r"|fontconfig|freetype|harfbuzz|asound|jack|GL|GLX|EGL|OpenGL|GLdispatch|wayland-[a-z]+|drm"
    r"|mvec|db-5\.3)\.so(\.\d+)*")

# Libraries nothing names as needed but the program loads itself: PortAudio (sounddevice, by its
# name) and Python (the program's starter).
LOADED_BY_NAME = re.compile(r"libportaudio\.so\.2|libpython3\.\d+\.so\.1\.0")

# The licences of the distribution packages whose copyright file doesn't say in the machine-readable
# form, read by hand from that file. A package not here and not machine-readable stops the build
# until someone has read its copyright file and added it.
KNOWN_LICENCES = {
    "libportaudio2": "MIT",
    "libpython3.12t64": "PSF-2.0",
    "libtcl8.6": "Tcl/Tk licence (BSD-style)",
    "libtk8.6": "Tcl/Tk licence (BSD-style)",
    "libgcc-s1": "GPL-3+ with the GCC Runtime Library Exception",
    "libstdc++6": "GPL-3+ with the GCC Runtime Library Exception",
}

# The licence files (in Ironbark/licenses) that cover each library that came inside a Python
# package, matched against its path under _internal. A name without an extension is a package's
# folder of licence files (its dist-info).
COVERED_BY: list[tuple[str, list[str]]] = [
    (r"_soundfile_data/libsndfile_x86_64\.so",
     ["libsndfile-COPYING.txt", "libFLAC-COPYING.Xiph.txt", "libogg-COPYING.txt", "libvorbis-COPYING.txt",
      "libopus-COPYING.txt", "mpg123-COPYING.txt", "LAME-notice.txt"]),
    (r"tkinterdnd2/tkdnd/linux-x64/[^/]+\.so", ["tkdnd-license.terms.txt", "tkinterdnd2"]),
    (r"numpy(\.libs)?/.+\.so(\.\d+)*", ["numpy"]),   # numpy's licence lists OpenBLAS, LAPACK, the GCC runtime
    (r"(PIL|pillow\.libs)/.+\.so(\.\d+)*", ["pillow"]),   # Pillow's lists the image libraries it bundles
    (r"_cffi_backend\.[^/]+\.so", ["cffi"]),
]

MANIFEST = "SYSTEM-LIBRARIES.txt"
WHEEL_LIBRARIES = re.compile(r"(?P<package>[a-z0-9_]+)\.libs")
_LIBRARY_DIRS = ("/usr/lib/x86_64-linux-gnu/", "/lib/x86_64-linux-gnu/", "/usr/lib/", "/lib/")


def from_the_desktop(name: str) -> bool:
    return "/" not in name and FROM_THE_DESKTOP.fullmatch(name) is not None


def _top_library(name: str) -> bool:
    return "/" not in name and re.fullmatch(r"lib.+\.so(\.\d+)*", name) is not None


def needed_only(binaries: list) -> list:
    """`binaries` (PyInstaller's (name, source, kind) list) less the top-level libraries nothing
    left in it needs: PyInstaller collected them for a library since left out (JACK's, X11's)."""
    by_name = {entry[0]: entry for entry in binaries}
    wanted = {name for name in by_name if not _top_library(name) or LOADED_BY_NAME.fullmatch(name)}
    todo = list(wanted)
    while todo:
        for name in _needs(by_name[todo.pop()][1]):
            if name in by_name and name not in wanted:
                wanted.add(name)
                todo.append(name)
    return [entry for entry in binaries if entry[0] in wanted]


def _needs(path: str) -> list[str]:
    """The libraries `path` itself names as needed (its NEEDED entries; ldd would also list theirs)."""
    found = subprocess.run(["readelf", "-d", "-W", path], capture_output=True, text=True)
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", found.stdout)


def wheel_libraries() -> dict[str, str]:
    """The libraries Python packages carry in their own <package>.libs folder (numpy's OpenBLAS,
    Pillow's image libraries): PyInstaller puts them at the top of the build, under these names.
    {name: the package whose licence covers it}."""
    found = {}
    for folder in Path(sysconfig.get_paths()["purelib"]).glob("*.libs"):
        package = WHEEL_LIBRARIES.fullmatch(folder.name)
        if package:
            found.update({path.name: package["package"] for path in folder.iterdir()})
    return found


def _native(app: Path):
    """Each library in the build: (its path, its path under _internal)."""
    internal = app / "_internal"
    for path in sorted(internal.rglob("*.so*")):
        if path.is_file() and re.search(r"\.so(\.\d+)*$", path.name):
            yield path, path.relative_to(internal).as_posix()


def needed(relative: str, wheels: dict[str, str] | None = None) -> list[str] | None:
    """The licence files a library from a Python package needs, or None when no rule covers it."""
    for pattern, files in COVERED_BY:
        if re.fullmatch(pattern, relative):
            return files
    package = (wheel_libraries() if wheels is None else wheels).get(relative)
    return [package] if package else None


def _package_of(relative: str) -> str | None:
    """The distribution package a library came from: by its name in the library folders (dpkg),
    and for Python's own modules (lib-dynload), Python's."""
    if relative.startswith("python3.") and "/lib-dynload/" in relative or relative.startswith("lib-dynload/"):
        relative = "libpython3"
    name = Path(relative).name
    if name == "libpython3":
        found = subprocess.run(["dpkg-query", "-S", "*/libpython3.*.so.1.0"], capture_output=True, text=True)
    else:
        found = subprocess.run(["dpkg-query", "-S", f"*/{name}"], capture_output=True, text=True)
    for line in found.stdout.splitlines():
        package, _, where = line.partition(": ")
        if where.startswith(_LIBRARY_DIRS):
            return package.split(":")[0]
    return None


def _licence_of(copyright: str) -> str:
    """The licence the copyright file gives the package as a whole (machine-readable format: the
    "Files: *" stanza), or "" when it doesn't say in that form."""
    for stanza in re.split(r"\n\s*\n", copyright):
        if re.search(r"^Files:\s*\*\s*$", stanza, re.M):
            found = re.search(r"^License:\s*(.+)$", stanza, re.M)
            return found.group(1).strip() if found else ""
    return ""


def gpl_only(licence: str) -> bool:
    """GPL, and not the LGPL or the GPL with a runtime exception (the GCC runtime's)?"""
    return bool(re.search(r"(^|[^L])GPL", licence)) and not re.search(r"LGPL|exception| or |MIT|BSD", licence, re.I)


def copy_system_licences(app: Path) -> None:
    """Copy into licenses/ the copyright file of each distribution package a library in the build came
    from, and write SYSTEM-LIBRARIES.txt: each library, its package and that package's licence."""
    out = app / "licenses"
    lines = ["Libraries in this build that come from the Linux distribution it was built on, the package",
             "each came from, and that package's licence (its copyright file is beside this one).", ""]
    wheels = wheel_libraries()
    for _path, relative in _native(app):
        if needed(relative, wheels) is not None:
            continue
        package = _package_of(relative)
        if package is None:
            lines.append(f"{relative}: ?")
            continue
        source = Path("/usr/share/doc") / package / "copyright"
        if source.is_file():
            target = out / f"{package}-copyright.txt"
            if not target.exists():
                target.write_text(source.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            licence = _licence_of(target.read_text(encoding="utf-8")) or KNOWN_LICENCES.get(package, "")
        else:
            licence = ""
        lines.append(f"{relative}: {package} ({licence or 'not known'})")
    (out / MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8")


def gaps(app: Path) -> list[str]:
    """What's wrong in a built Ironbark folder: a library no rule or package covers, a licence file
    that isn't in licenses/, a GPL library. Empty = all covered."""
    licences = app / "licenses"
    listed: dict[str, str] = {}
    manifest = licences / MANIFEST
    if manifest.is_file():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            relative, sep, rest = line.partition(": ")
            if sep:
                listed[relative] = rest
    problems = []
    wheels = wheel_libraries()
    for _path, relative in _native(app):
        files = needed(relative, wheels)
        if files is not None:
            problems += [f"{relative}: licenses/{name} is missing" for name in files if not (licences / name).exists()]
            continue
        entry = listed.get(relative, "?")
        package = entry.split(" ", 1)[0]
        if entry == "?":
            problems.append(f"{relative}: no rule in licence_check.COVERED_BY and no distribution package found")
        elif not (licences / f"{package}-copyright.txt").is_file():
            problems.append(f"{relative}: licenses/{package}-copyright.txt is missing")
        else:
            licence = (_licence_of((licences / f"{package}-copyright.txt").read_text(encoding="utf-8"))
                       or KNOWN_LICENCES.get(package, ""))
            if not licence:
                problems.append(f"{relative}: from {package}, whose copyright file doesn't name its licence "
                                "in the machine-readable form: read it, then add it to KNOWN_LICENCES")
            elif gpl_only(licence):
                problems.append(f"{relative}: from {package}, whose licence is {licence} (no GPL in Ironbark)")
    return problems

"""Which licence file covers each native library in the Windows build, and a check that none is
missing. ironbark.spec runs `gaps()` after copying the licences and stops the build if it finds
any: a new DLL (a new dependency, or a new version of one that bundles more) then needs its
licence added to packaging/licenses and a rule here before the build can ship."""

from __future__ import annotations

import re
from pathlib import Path

# The licence files (in Ironbark\licenses) that cover each bundled .dll / .pyd, matched against its
# path under _internal (forward slashes, any case). The first rule that matches a file applies. A
# name without ".txt" / ".md" / ".terms" is a package's folder of licence files (its dist-info).
COVERED_BY: list[tuple[str, list[str]]] = [
    # Microsoft's Visual C++ runtime (redistributable; named in THIRD_PARTY_LICENSES.md)
    (r"vcruntime140(_1)?\.dll|numpy\.libs/msvcp140-[0-9a-f]+\.dll", ["THIRD_PARTY_LICENSES.md"]),
    # CPython, and the standard modules that carry another project's code
    (r"python3\d*\.dll", ["Python-LICENSE.txt", "zlib-LICENSE.txt"]),
    # (Python's own OpenSSL and zlib, by the names its builds give them: "-x64" or not, and zlib as its
    # own zlib1.dll in some builds — the python.org 3.12 on GitHub's Windows runners — not in python3*.dll)
    (r"libcrypto-3(-x64)?\.dll|libssl-3(-x64)?\.dll", ["OpenSSL-LICENSE.txt"]),
    (r"zlib1\.dll", ["zlib-LICENSE.txt"]),
    (r"_ssl\.pyd|_hashlib\.pyd", ["Python-LICENSE.txt", "OpenSSL-LICENSE.txt"]),
    (r"libffi-8\.dll", ["libffi-LICENSE.txt"]),
    (r"_ctypes\.pyd", ["Python-LICENSE.txt", "libffi-LICENSE.txt"]),
    (r"pyexpat\.pyd|_elementtree\.pyd", ["Python-LICENSE.txt", "expat-COPYING.txt"]),
    (r"_decimal\.pyd", ["Python-LICENSE.txt", "mpdecimal-LICENSE.txt"]),
    (r"_lzma\.pyd", ["Python-LICENSE.txt", "xz-liblzma-COPYING.0BSD.txt"]),
    (r"sqlite3\.dll", ["SQLite-public-domain.txt"]),
    (r"_sqlite3\.pyd", ["Python-LICENSE.txt", "SQLite-public-domain.txt"]),
    (r"tcl86t\.dll", ["Tcl-license.terms.txt"]),
    (r"tk86t\.dll", ["tk8.6-license.terms.txt"]),
    (r"_tkinter\.pyd", ["Python-LICENSE.txt", "Tcl-license.terms.txt", "tk8.6-license.terms.txt"]),
    (r"[^/]+\.pyd", ["Python-LICENSE.txt"]),          # the other standard modules (bzip2 is in it too)
    # Bundled with the Python packages
    (r"_soundfile_data/libsndfile_x64\.dll",
     ["libsndfile-COPYING.txt", "libFLAC-COPYING.Xiph.txt", "libogg-COPYING.txt", "libvorbis-COPYING.txt",
      "libopus-COPYING.txt", "mpg123-COPYING.txt", "LAME-notice.txt"]),
    (r"_sounddevice_data/portaudio-binaries/libportaudio64bit\.dll", ["PortAudio-LICENSE.txt"]),
    (r"tkinterdnd2/tkdnd/[^/]+/[^/]+\.dll", ["tkdnd-license.terms.txt", "tkinterdnd2"]),
    (r"numpy(\.libs)?/.+\.(dll|pyd)", ["numpy"]),     # numpy's licence lists OpenBLAS, LAPACK, the GCC runtime
    (r"PIL/[^/]+\.pyd", ["pillow"]),                  # Pillow's licence lists the image libraries it bundles
    (r"psutil/[^/]+\.pyd", ["psutil"]),
    (r"_cffi_backend\.[^/]+\.pyd", ["cffi"]),
]

NATIVE = (".dll", ".pyd")


def needed(relative: str) -> list[str] | None:
    """The licence files a bundled native file needs, or None when no rule covers it."""
    for pattern, files in COVERED_BY:
        if re.fullmatch(pattern, relative, re.IGNORECASE):
            return files
    return None


def gaps(app: Path) -> list[str]:
    """What's missing in a built Ironbark folder: native files no rule covers, and licence
    files a rule names that aren't in its licenses folder. Empty = all covered."""
    internal, licences = app / "_internal", app / "licenses"
    problems = []
    for path in sorted(internal.rglob("*")):
        if path.suffix.lower() not in NATIVE or not path.is_file():
            continue
        relative = path.relative_to(internal).as_posix()
        files = needed(relative)
        if files is None:
            problems.append(f"{relative}: no licence listed for it in licence_check.COVERED_BY")
            continue
        problems += [f"{relative}: licenses\\{name} is missing" for name in files if not (licences / name).exists()]
    return problems

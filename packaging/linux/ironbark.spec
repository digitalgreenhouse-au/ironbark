# PyInstaller recipe for the Linux build: a folder holding the Ironbark program, Python, the
# libraries and the app's own files, so nobody needs Python installed. Run through build.sh (next
# to this file), which then makes the tarball, the AppImage and the checksums.
#
# Built on the distribution's own Python and Tk (python3-tk), never uv's: uv's Tk on Linux is built
# without Xft, so every font is the old bitmap "fixed" (build.sh checks). One folder, as on Windows:
# it starts faster, and libsndfile stays a separate library anyone can replace (its LGPL asks that).
#
# The desktop's own libraries are left out, not copied: X11 and its fonts (they must match the
# running X server and the system's font settings), and ALSA and JACK (their plug-ins and servers
# are the system's). Every computer that runs a desktop has them.

import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

REPO = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, SPECPATH)
import licence_check  # noqa: E402

a = Analysis(
    [str(Path(SPECPATH) / "launch.py")],
    pathex=[str(REPO)],
    datas=collect_data_files("ironbark", includes=["assets/*"]),
    # Pillow's ImageTk finds Tk through this module on Linux, imported in a way PyInstaller doesn't see.
    # jeepney (mpris.py, the media keys) is imported only once they're switched on, inside a function.
    hiddenimports=["PIL._tkinter_finder", "jeepney", "jeepney.io.blocking", "jeepney.bus_messages"],
    # yaml: numpy's optional config printout imports it when the development tools have installed it.
    # readline, curses and dbm.gnu: the distribution's Python builds them against GPL libraries
    # (libreadline, libgdbm); Ironbark uses none of them.
    excludes=["pytest", "_pytest", "setuptools", "pip", "yaml", "readline", "curses", "_curses",
              "_curses_panel", "dbm.gnu", "_gdbm"],
)

a.binaries = [b for b in a.binaries if not licence_check.from_the_desktop(b[0])]
# ... and what PyInstaller collected only for those (X11's own libraries, JACK's).
a.binaries = licence_check.needed_only(a.binaries)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Ironbark",
    console=False,
    strip=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Ironbark")


# -- licences: Ironbark's own, and every bundled component's, in Ironbark/licenses -----------------
app = Path(DISTPATH) / "Ironbark"
out = app / "licenses"
shutil.rmtree(out, ignore_errors=True)
out.mkdir(parents=True)
shutil.copy2(REPO / "LICENSE", out / "Ironbark-LICENSE.txt")
shutil.copy2(REPO / "THIRD_PARTY_LICENSES.md", out)

SHIPPED = ["numpy", "pillow", "soundfile", "sounddevice", "cffi", "pycparser", "tkinterdnd2", "pyinstaller"]
for name in SHIPPED:
    dist = metadata.distribution(name)
    for file in dist.files or []:
        parts = Path(str(file)).parts
        if parts and parts[0].endswith(".dist-info") and re.search(r"LICEN[CS]E|COPYING", parts[-1], re.I):
            target = out / dist.metadata["Name"] / Path(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dist.locate_file(file), target)

import _soundfile_data  # noqa: E402  (libsndfile's own licence travels with the library)

shutil.copy2(Path(_soundfile_data.__file__).parent / "COPYING", out / "libsndfile-COPYING.txt")

# The native libraries whose licences no Python package carries: the codecs inside libsndfile and
# tkdnd, kept in packaging/licenses as on Windows ...
for text in sorted((REPO / "packaging" / "licenses").glob("*.txt")):
    shutil.copy2(text, out / text.name)
# ... and the distribution's own (Python, Tcl/Tk, PortAudio, OpenSSL …): each package's copyright
# file, as the distribution ships it, and a list of which library came from which package.
licence_check.copy_system_licences(app)

# Every library in the build must have its licence beside it, and none may be GPL, or the build stops here.
missing = licence_check.gaps(app)
if missing:
    raise SystemExit("Licence check failed (add the licence, or a rule in packaging/linux/licence_check.py):\n  "
                     + "\n  ".join(missing))

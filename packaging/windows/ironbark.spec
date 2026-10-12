# PyInstaller recipe for the Windows build: a folder holding Ironbark.exe, Python, the libraries
# and the app's own files, so nobody needs Python installed. Run through build.ps1 (next to this
# file), which then makes the installer and the portable zip.
#
# One-folder, not one-file: it starts faster (nothing to unpack on every launch), libsndfile stays a
# separate DLL anyone can replace (which its LGPL licence asks), and one-file programs are more often
# flagged by antivirus software by mistake. No UPX compression, for the same antivirus reason.

import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32 import versioninfo as vi

REPO = Path(SPECPATH).resolve().parents[1]
version = re.search(r'__version__ = "([^"]+)"', (REPO / "ironbark" / "__init__.py").read_text()).group(1)
numbers = tuple(int(n) for n in re.match(r"(\d+)\.(\d+)\.(\d+)", version).groups()) + (0,)

a = Analysis(
    [str(Path(SPECPATH) / "launch.py")],
    pathex=[str(REPO)],
    datas=collect_data_files("ironbark", includes=["assets/*"]),
    # yaml: numpy's optional config printout imports it when the development tools have installed it;
    # Ironbark never uses it (and it would need a licence of its own in the package).
    excludes=["pytest", "_pytest", "setuptools", "pip", "yaml"],
)

# sounddevice's hook takes every PortAudio build it carries. Keep only 64-bit Windows without ASIO:
# the ASIO builds contain Steinberg's ASIO SDK, under Steinberg's own licence, and Ironbark doesn't use ASIO.
PORTAUDIO = "libportaudio64bit.dll"


def keep(entry):
    return "portaudio-binaries/libportaudio" not in entry[0].replace("\\", "/") or entry[0].endswith(PORTAUDIO)


a.binaries = [b for b in a.binaries if keep(b)]
a.datas = [d for d in a.datas if keep(d)]    # the Mac build (.dylib) arrives as a data file

pyz = PYZ(a.pure)

# Windows "Details" (file properties): publisher, product name, version.
details = vi.VSVersionInfo(
    ffi=vi.FixedFileInfo(filevers=numbers, prodvers=numbers),
    kids=[
        vi.StringFileInfo([vi.StringTable("0c0904b0", [   # English (Australia), Unicode
            vi.StringStruct("CompanyName", "Digital Greenhouse"),
            vi.StringStruct("FileDescription", "Ironbark music player"),
            vi.StringStruct("FileVersion", version),
            vi.StringStruct("InternalName", "Ironbark"),
            vi.StringStruct("LegalCopyright", "© Digital Greenhouse. MIT licence."),
            vi.StringStruct("OriginalFilename", "Ironbark.exe"),
            vi.StringStruct("ProductName", "Ironbark"),
            vi.StringStruct("ProductVersion", version),
        ])]),
        vi.VarFileInfo([vi.VarStruct("Translation", [0x0C09, 1200])]),
    ],
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Ironbark",
    icon=str(REPO / "ironbark" / "assets" / "ironbark.ico"),
    version=details,
    console=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Ironbark")


# -- licences: Ironbark's own, and every bundled component's, in Ironbark\licenses ---------------
out = Path(DISTPATH) / "Ironbark" / "licenses"
shutil.rmtree(out, ignore_errors=True)
out.mkdir(parents=True)
shutil.copy2(REPO / "LICENSE", out / "Ironbark-LICENSE.txt")
shutil.copy2(REPO / "THIRD_PARTY_LICENSES.md", out)

SHIPPED = ["numpy", "pillow", "soundfile", "sounddevice", "cffi", "pycparser", "typing_extensions",
           "tkinterdnd2", "pycaw", "comtypes", "psutil", "pyinstaller"]
for name in SHIPPED:
    dist = metadata.distribution(name)
    for file in dist.files or []:
        parts = Path(str(file)).parts
        if parts and parts[0].endswith(".dist-info") and re.search(r"LICEN[CS]E|COPYING", parts[-1], re.I):
            target = out / dist.metadata["Name"] / Path(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dist.locate_file(file), target)

import _soundfile_data  # noqa: E402  (libsndfile's own licence travels with the DLL)

shutil.copy2(Path(_soundfile_data.__file__).parent / "COPYING", out / "libsndfile-COPYING.txt")
python_home = Path(sys.base_prefix)
shutil.copy2(python_home / "LICENSE.txt", out / "Python-LICENSE.txt")
for terms in sorted((python_home / "tcl").glob("t*8.6/license.terms")):
    shutil.copy2(terms, out / f"{terms.parent.name}-license.terms.txt")

# The native libraries whose licences no Python package carries (OpenSSL, libffi, Tcl, PortAudio,
# tkdnd, the codecs inside libsndfile, …): kept in packaging\licenses, each with where it came from.
for text in sorted((REPO / "packaging" / "licenses").glob("*.txt")):
    shutil.copy2(text, out / text.name)

# Every DLL and .pyd in the build must have its licence beside it, or the build stops here.
sys.path.insert(0, SPECPATH)
import licence_check  # noqa: E402

missing = licence_check.gaps(Path(DISTPATH) / "Ironbark")
if missing:
    raise SystemExit("Licence check failed (add the licence to packaging\\licenses and a rule to "
                     "packaging\\windows\\licence_check.py):\n  " + "\n  ".join(missing))

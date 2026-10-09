# PyInstaller recipe for the Mac app: a folder holding the program, Python, the libraries and the
# app's own files, wrapped as Ironbark.app. Run through build.sh (next to this file), which then
# checks the built app and zips it.
#
# One-folder, not one-file: it starts faster (nothing to unpack on every launch), and libsndfile
# stays a separate, replaceable library inside the app, as its LGPL licence asks. Unsigned.

import re
import sys
from importlib import metadata
from pathlib import Path

REPO = Path(SPECPATH).resolve().parents[1]
PACKAGE = REPO / "ironbark"
version = re.search(r'__version__ = "([^"]+)"', (PACKAGE / "__init__.py").read_text(encoding="utf-8")).group(1)
BUNDLE_ID = "au.com.digitalgreenhouse.ironbark"

# -- licences: Ironbark's own, and every bundled component's, in the app's licenses folder --------
# (About → Licences lists them; the self-check wants THIRD_PARTY_LICENSES.md among them.)
licences = [(str(REPO / "LICENSE"), "licenses"), (str(REPO / "THIRD_PARTY_LICENSES.md"), "licenses")]

SHIPPED = ["numpy", "pillow", "soundfile", "sounddevice", "cffi", "pycparser", "typing_extensions",
           "tkinterdnd2", "pyinstaller"]
for name in SHIPPED:
    dist = metadata.distribution(name)
    for file in dist.files or []:
        parts = Path(str(file)).parts
        if parts and parts[0].endswith(".dist-info") and re.search(r"LICEN[CS]E|COPYING", parts[-1], re.I):
            target = Path("licenses") / dist.metadata["Name"] / Path(*parts[1:-1])
            licences.append((str(dist.locate_file(file)), str(target)))

import _soundfile_data  # noqa: E402  (libsndfile's own licence travels with the library)

licences.append((str(Path(_soundfile_data.__file__).parent / "COPYING"), "licenses"))
python_licence = Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt"
if python_licence.is_file():
    licences.append((str(python_licence), "licenses/python"))
# The native libraries whose licences no Python package carries (Tcl/Tk, PortAudio, tkdnd, the
# codecs inside libsndfile, …): kept in packaging/licenses, each with where it came from.
for text in sorted((REPO / "packaging" / "licenses").glob("*.txt")):
    licences.append((str(text), "licenses"))

a = Analysis(
    [str(Path(SPECPATH) / "ironbark_app.py")],
    pathex=[str(REPO)],
    datas=[(str(PACKAGE / "assets"), "ironbark/assets")] + licences,
    # yaml: numpy's optional config printout imports it when the development tools have installed it;
    # Ironbark never uses it (and it would need a licence of its own in the package).
    excludes=["pytest", "_pytest", "comtypes", "pycaw", "psutil", "setuptools", "pkg_resources", "pip",
              "yaml"],
)

# sounddevice also carries Windows' PortAudio DLLs, including the ASIO builds (Steinberg's licence,
# not used by Ironbark): a Mac app needs none of them.
a.binaries = [entry for entry in a.binaries if not entry[0].lower().endswith(".dll")]
a.datas = [entry for entry in a.datas if not entry[0].lower().endswith(".dll")]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Ironbark",
    console=False,
    argv_emulation=False,
    target_arch=None,           # the machine's own architecture (arm64 or x86_64)
    codesign_identity=None,     # unsigned
    entitlements_file=None,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Ironbark")

app = BUNDLE(
    coll,
    name="Ironbark.app",
    icon=str(PACKAGE / "assets" / "ironbark.icns"),
    bundle_identifier=BUNDLE_ID,
    version=version,
    info_plist={
        "CFBundleName": "Ironbark",
        "CFBundleDisplayName": "Ironbark",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "NSHumanReadableCopyright": "© Digital Greenhouse. MIT licence.",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.music",
    },
)

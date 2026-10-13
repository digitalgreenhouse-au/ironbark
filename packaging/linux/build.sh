#!/usr/bin/env bash
# Builds Ironbark for Linux (x86_64): the packaged program, a tarball (an AppImage too, if asked)
# and checksums.
#
#  1. Makes the build's environment (build/linux/venv) from the distribution's Python, exactly as
#     locked, and checks its Tk draws with the desktop's fonts (Xft). uv's own Python can't be
#     used: its Tk on Linux has no Xft, and every font comes out as the bitmap "fixed".
#  2. Runs the tests (skip with --skip-tests). The window tests need a display.
#  3. PyInstaller packages the app (ironbark.spec) into a folder holding the Ironbark program, with
#     every bundled library's licence in its licenses folder (the build stops if one is missing,
#     or is the GPL).
#  4. Runs the *built* program's --self-check (it needs a display; nothing is played); a failure
#     stops the build.
#  5. Ironbark-X.Y.Z-linux-x86_64.tar.gz: the folder, with install.sh to put it on the desktop's menu.
#  6. With --appimage, Ironbark-X.Y.Z-x86_64.AppImage: one file that runs as it is (skipped, with a
#     warning, when appimagetool, or the AppImage runtime it puts at the front, isn't here). The
#     runtime is $APPIMAGE_RUNTIME, or runtime-x86_64 beside appimagetool: given, so it isn't
#     downloaded. Not made by default: the runtime carries libfuse (LGPL) inside it, and its notice
#     isn't in the AppImage yet.
#  7. SHA256SUMS-linux.txt.
#
# The build runs on the oldest distribution it should run on: the program needs that one's C
# library (glibc) or newer.
#
#   packaging/linux/build.sh [--out-dir DIR] [--skip-tests] [--appimage] [--python /usr/bin/python3]
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
out_dir=dist/linux
skip_tests=0
appimage=0
python=/usr/bin/python3
while [ $# -gt 0 ]; do
    case $1 in
        --out-dir) out_dir=$2; shift 2 ;;
        --skip-tests) skip_tests=1; shift ;;
        --appimage) appimage=1; shift ;;
        --python) python=$2; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
cd "$repo"
mkdir -p "$out_dir"
out_dir=$(cd "$out_dir" && pwd)
work=$repo/build/linux
venv=$work/venv

version=$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' ironbark/__init__.py)
[ -n "$version" ] || { echo "No __version__ in ironbark/__init__.py" >&2; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "This build is for x86_64; this computer is $(uname -m)" >&2; exit 1; }
echo "Building Ironbark $version for Linux -> $out_dir"

step() { printf '\n\033[36m== %s\033[0m\n' "$1"; }

step "Environment (the distribution's Python, exactly as locked)"
UV_PROJECT_ENVIRONMENT=$venv uv sync --locked --python "$python"
run() { "$venv/bin/python" "$@"; }
tk_library=$(run -c "import _tkinter; print(_tkinter.__file__)")
if ! ldd "$tk_library" | grep -q "libtk"; then
    echo "This Python's tkinter isn't linked to a shared Tk ($tk_library): use the distribution's Python (python3-tk)." >&2
    exit 1
fi
libtk=$(ldd "$tk_library" | awk '/libtk/ {print $3}')
if ! ldd "$libtk" | grep -q "libXft"; then
    echo "This Python's Tk ($libtk) is built without Xft: every font would be the bitmap \"fixed\"." >&2
    echo "Use the distribution's Python with python3-tk (--python /usr/bin/python3)." >&2
    exit 1
fi
echo "Python $(run -c 'import sys; print(sys.version.split()[0])'), Tk $(run -c 'import tkinter; print(tkinter.TkVersion)') with Xft ($libtk)"

if [ "$skip_tests" = 0 ]; then
    step "Tests"
    run -m pytest -q
fi

step "Package (PyInstaller)"
run -m PyInstaller packaging/linux/ironbark.spec --noconfirm --clean --distpath "$out_dir" --workpath "$work/pyinstaller"
app=$out_dir/Ironbark
cp packaging/linux/ironbark.desktop packaging/linux/install.sh "$app/"
cp ironbark/assets/icon_256.png "$app/ironbark.png"
chmod +x "$app/install.sh"

step "Self-check of the built program"
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    echo "No display: the self-check opens the main window, so it can't run here." >&2
    exit 1
fi
report=$work/self-check.txt
rm -f "$report"
status=0
"$app/Ironbark" --self-check "$report" >/dev/null 2>&1 || status=$?
[ -f "$report" ] && cat "$report"
[ "$status" = 0 ] || { echo "The self-check failed (exit code $status)" >&2; exit 1; }

files=()
step "Tarball"
tarball=Ironbark-$version-linux-x86_64.tar.gz
tar -C "$out_dir" --owner=0 --group=0 --numeric-owner -czf "$out_dir/$tarball" Ironbark
files+=("$tarball")

appimagetool=
runtime=${APPIMAGE_RUNTIME:-}
if [ "$appimage" = 1 ]; then
    appimagetool=$(command -v appimagetool || command -v appimagetool-x86_64.AppImage || true)
fi
if [ -z "$runtime" ] && [ -n "$appimagetool" ]; then
    runtime=$(dirname "$(readlink -f "$appimagetool")")/runtime-x86_64
fi
if [ -n "$appimagetool" ] && [ -f "$runtime" ]; then
    step "AppImage"
    appdir=$work/Ironbark.AppDir
    rm -rf "$appdir"
    mkdir -p "$appdir/usr/lib"
    cp -a "$app" "$appdir/usr/lib/ironbark"
    rm "$appdir/usr/lib/ironbark/install.sh"   # (the AppImage needs no install; menus: AppImageLauncher, Gear Lever …)
    cp packaging/linux/ironbark.desktop "$appdir/"
    cp ironbark/assets/icon_256.png "$appdir/ironbark.png"
    cat > "$appdir/AppRun" <<'APPRUN'
#!/bin/sh
here=$(dirname "$(readlink -f "$0")")
exec "$here/usr/lib/ironbark/Ironbark" "$@"
APPRUN
    chmod +x "$appdir/AppRun"
    appimage=Ironbark-$version-x86_64.AppImage
    rm -f "$out_dir/$appimage"
    ARCH=x86_64 "$appimagetool" --no-appstream --runtime-file "$runtime" "$appdir" "$out_dir/$appimage"
    files+=("$appimage")
elif [ "$appimage" = 1 ]; then
    echo "appimagetool or its runtime-x86_64 not found: no AppImage made" \
         "(https://github.com/AppImage/appimagetool, https://github.com/AppImage/type2-runtime)." >&2
fi

step "Checksums"
sums=SHA256SUMS-linux.txt
(cd "$out_dir" && sha256sum "${files[@]}" > "$sums" && cat "$sums")

printf '\n\033[32mDone: Ironbark %s\033[0m\n' "$version"
for name in "${files[@]}" "$sums"; do
    printf '  %s  (%s)\n' "$out_dir/$name" "$(du -h "$out_dir/$name" | cut -f1)"
done

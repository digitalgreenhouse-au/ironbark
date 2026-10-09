#!/bin/sh
# Builds Ironbark for the Mac: dist/mac/Ironbark.app, a zip of it and checksums.
#
#   1. Runs the tests (skip with --skip-tests).
#   2. PyInstaller packages the app (ironbark.spec) as Ironbark.app, with every bundled library's
#      licence in its licenses folder.
#   3. Runs the *built* app's own --self-check; a failure stops the build. (On a Mac the check's
#      window opens on screen for a moment: there's no hidden desktop to put it on.)
#   4. Zips the app as Ironbark-X.Y.Z-mac-<arch>.zip and writes SHA256SUMS.txt.
#
# Run from anywhere:  sh packaging/mac/build.sh [--skip-tests] [--no-sync] [--out <folder>]
# --no-sync: don't update the environment first.
# Unsigned: a downloaded copy needs "Open" from the right-click menu the first time (Gatekeeper).
set -eu

cd "$(dirname "$0")/../.."
OUT="dist/mac"
RUN="run --locked"
SYNC=yes
TESTS=yes
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-tests) TESTS=no ;;
        --no-sync) SYNC=no; RUN="run --no-sync" ;;
        --out) OUT="$2"; shift ;;
        *) echo "usage: sh packaging/mac/build.sh [--skip-tests] [--no-sync] [--out <folder>]" >&2; exit 2 ;;
    esac
    shift
done

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ironbark/__init__.py)
[ -n "$VERSION" ] || { echo "error: no __version__ in ironbark/__init__.py" >&2; exit 1; }
ARCH=$(uname -m)
WORK="build/mac"
echo "Building Ironbark $VERSION for the Mac ($ARCH) -> $OUT"

step() { printf '\n== %s\n' "$1"; }

if [ "$SYNC" = yes ]; then
    step "Install exactly as locked"
    uv sync --locked
fi
if [ "$TESTS" = yes ]; then
    step "Tests"
    uv $RUN pytest -q
fi

step "Package (PyInstaller)"
rm -rf "$WORK" "$OUT/Ironbark" "$OUT/Ironbark.app"
uv $RUN pyinstaller packaging/mac/ironbark.spec --noconfirm --clean --distpath "$OUT" --workpath "$WORK"
APP="$OUT/Ironbark.app"
rm -rf "$OUT/Ironbark"   # (the bare folder the .app was made from)

# Nothing Windows-only may ride along (the ASIO DLLs above all).
if find "$APP" -iname '*.dll' | grep -q .; then
    echo "error: Windows DLLs found inside Ironbark.app" >&2
    exit 1
fi

step "Self-check of the built app"
REPORT="$WORK/self-check.txt"
rm -f "$REPORT"
set +e
"$APP/Contents/MacOS/Ironbark" --self-check "$REPORT"
RESULT=$?
set -e
[ -f "$REPORT" ] && cat "$REPORT"
[ "$RESULT" -eq 0 ] || { echo "error: the built app's self-check failed (exit code $RESULT)" >&2; exit 1; }

step "Zip"
ZIP="Ironbark-$VERSION-mac-$ARCH.zip"
rm -f "$OUT/$ZIP"
ditto -c -k --keepParent "$APP" "$OUT/$ZIP"

step "Checksums"
(cd "$OUT" && shasum -a 256 "$ZIP" | tee SHA256SUMS.txt)

printf '\nDone: Ironbark %s\n' "$VERSION"
for name in "$ZIP" SHA256SUMS.txt; do
    printf '  %s  (%s)\n' "$OUT/$name" "$(du -h "$OUT/$name" | cut -f1)"
done

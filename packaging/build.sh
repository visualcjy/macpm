#!/bin/sh
# Build the standalone (no Python required) arm64 binary and a tarball.
#
# Build machine requirements:
#   - Apple Silicon
#   - Python 3.9-3.12 with: pip install pyinstaller dashing psutil humanize
#   - PYTHON=/path/to/python can select the interpreter
#
# Output:
#   dist/macpm
#   dist/macpm-<version>-macos-arm64.tar.gz
#     (<version> comes from macpm.__version__, so it cannot drift)
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST="$ROOT/dist"
PYTHON=${PYTHON:-python3}
VERSION=$(PYTHONPATH="$ROOT" "$PYTHON" -c 'import macpm; print(macpm.__version__)')
NAME="macpm-${VERSION}-macos-arm64"

if [ "$(uname -m)" != "arm64" ]; then
    echo "This build script targets Apple Silicon (arm64)." >&2
    exit 1
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/macpm-build.XXXXXX")
trap 'rm -rf "$WORK"' EXIT HUP INT TERM
export PYINSTALLER_CONFIG_DIR="$WORK/pyinstaller-cache"
export PYTHONPYCACHEPREFIX="$WORK/pycache"
mkdir -p "$DIST"

echo "== building macpm =="
PYTHONPATH="$ROOT" "$PYTHON" -m PyInstaller \
    --noconfirm --onefile --name macpm \
    --target-arch arm64 \
    --distpath "$DIST" --workpath "$WORK/tui" --specpath "$WORK/tui" \
    --paths "$ROOT" \
    "$ROOT/packaging/entry_tui.py"

STAGE="$DIST/$NAME"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp "$DIST/macpm" "$STAGE/"
cp "$ROOT/packaging/install.sh" "$ROOT/packaging/macpm.command" \
   "$ROOT/packaging/README.txt" "$STAGE/"
if [ -f "$ROOT/LICENSE" ]; then
    cp "$ROOT/LICENSE" "$STAGE/"
fi
chmod +x "$STAGE/macpm" "$STAGE/install.sh" "$STAGE/macpm.command"

if [ -n "${CODESIGN_ID:-}" ]; then
    echo "== signing with $CODESIGN_ID =="
    codesign --force --deep --options runtime --sign "$CODESIGN_ID" \
        "$STAGE/macpm"
fi

tar -C "$DIST" -czf "$DIST/$NAME.tar.gz" "$NAME"

echo
echo "Artifacts:"
ls -lh "$DIST/macpm" "$DIST/$NAME.tar.gz"
echo
echo "Check with:"
echo "  env -i PATH=/usr/bin:/bin \"$DIST/macpm\" --help"

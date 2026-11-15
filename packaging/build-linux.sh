#!/bin/sh
# Build standalone Linux binaries (and optionally .deb packages) for macpm.
#
# The build does not use the host Python: every target gets its own
# python-build-standalone interpreter, which is what keeps the bundled runtime
# and the glibc floor (2.17, i.e. CentOS 7 / Ubuntu 16.04 and newer) identical
# on every machine that runs this script.
#
# Usage:
#   packaging/build-linux.sh                 # host architecture only
#   packaging/build-linux.sh both --deb      # aarch64 + x86_64, plus .debs
#   packaging/build-linux.sh x86_64
#
# On an Apple Silicon Mac (or in an ARM Linux VM with Rosetta registered) the
# x86_64 target is built by running the x86_64 interpreter under Rosetta.  That
# needs an objcopy that understands x86-64 ELF:
#   sudo apt-get install binutils-x86-64-linux-gnu
# and, in a VM, the amd64 runtime libraries (multiarch glibc).
#
# Output (in dist/):
#   macpm-<version>-linux-<arch>.tar.gz    binary + LICENSE + README-linux.txt
#   macpm_<version>_<deb-arch>.deb         with --deb
#
# Environment:
#   PYTHON_VERSION=3.13.18       python-build-standalone version
#   PBS_TAG=20261110             python-build-standalone release tag
#   PYINSTALLER_VERSION=6.22.3   pinned for reproducible bundles
#   PBS_CACHE=~/.cache/macpm-build/pbs
#   DIST=<repo>/dist
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST=${DIST:-"$ROOT/dist"}
PYTHON_VERSION=${PYTHON_VERSION:-3.13.18}
PBS_TAG=${PBS_TAG:-20261110}
PYINSTALLER_VERSION=${PYINSTALLER_VERSION:-6.22.3}
PBS_CACHE=${PBS_CACHE:-"$HOME/.cache/macpm-build/pbs"}

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

VERSION=$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$ROOT/macpm/__init__.py")
if [ -z "$VERSION" ]; then
    echo "build-linux: cannot read the version from macpm/__init__.py" >&2
    exit 1
fi

HOST=$(uname -m)
case "$HOST" in
    arm64|aarch64) HOST=aarch64 ;;
    x86_64|amd64) HOST=x86_64 ;;
    *) echo "build-linux: unsupported host architecture: $HOST" >&2; exit 1 ;;
esac

ARCHS=""
WANT_DEB=0
for arg in "$@"; do
    case "$arg" in
        both) ARCHS="$ARCHS aarch64 x86_64" ;;
        arm64|aarch64) ARCHS="$ARCHS aarch64" ;;
        x86_64|amd64) ARCHS="$ARCHS x86_64" ;;
        --deb) WANT_DEB=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "build-linux: unknown argument: $arg" >&2; usage; exit 2 ;;
    esac
done
[ -n "$ARCHS" ] || ARCHS=$HOST

WORK=$(mktemp -d "${TMPDIR:-/tmp}/macpm-linux-build.XXXXXX")
trap 'rm -rf "$WORK"' EXIT HUP INT TERM
export PYINSTALLER_CONFIG_DIR="$WORK/pyinstaller-cache"
export PYTHONPYCACHEPREFIX="$WORK/pycache"

mkdir -p "$DIST" "$PBS_CACHE"

# -- helpers ---------------------------------------------------------------

pbs_url() {
    arch=$1
    echo "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PYTHON_VERSION}%2B${PBS_TAG}-${arch}-unknown-linux-gnu-install_only_stripped.tar.gz"
}

ensure_pbs() {
    arch=$1
    dir="$PBS_CACHE/python-$arch-$PYTHON_VERSION"
    if [ -x "$dir/bin/python3" ]; then
        echo "$dir"
        return 0
    fi
    tarball="cpython-${PYTHON_VERSION}+${PBS_TAG}-${arch}-unknown-linux-gnu-install_only_stripped.tar.gz"
    if [ ! -s "$PBS_CACHE/$tarball" ]; then
        echo "== downloading $tarball" >&2
        curl -fL --retry 3 --retry-all-errors -o "$PBS_CACHE/$tarball.part" "$(pbs_url "$arch")"
        mv "$PBS_CACHE/$tarball.part" "$PBS_CACHE/$tarball"
    fi
    mkdir -p "$dir"
    tar -xzf "$PBS_CACHE/$tarball" -C "$dir" --strip-components=1
    echo "$dir"
}

# PyInstaller writes the payload into the executable with `objcopy
# --add-section` on Linux, so the objcopy on PATH must understand the target
# architecture.  x86-64 targets built on an aarch64 host need the cross tool.
ensure_objcopy() {
    target=$1
    if [ "$target" = "$HOST" ]; then
        return 0
    fi
    if objcopy --info 2>/dev/null | grep -q "elf64-x86-64"; then
        return 0
    fi
    for candidate in x86_64-linux-gnu-objcopy x86_64-unknown-linux-gnu-objcopy; do
        if command -v "$candidate" >/dev/null 2>&1; then
            mkdir -p "$WORK/bin"
            ln -sf "$(command -v "$candidate")" "$WORK/bin/objcopy"
            PATH="$WORK/bin:$PATH"
            export PATH
            return 0
        fi
    done
    echo "build-linux: need an x86-64 objcopy (apt install binutils-x86-64-linux-gnu)" >&2
    exit 1
}

# -- per-architecture build ------------------------------------------------

build_target() {
    target=$1
    case "$target" in
        aarch64) pbs_arch=aarch64; deb_arch=arm64 ;;
        x86_64) pbs_arch=x86_64; deb_arch=amd64 ;;
    esac

    if [ "$target" != "$HOST" ]; then
        if [ "$HOST" = aarch64 ] && [ "$target" = x86_64 ] \
           && [ -e /proc/sys/fs/binfmt_misc/rosetta ]; then
            echo "== $target: running the x86_64 interpreter through Rosetta"
        else
            echo "build-linux: cannot build $target on a $HOST host." >&2
            echo "Run this target on a native $target machine or in a matching container." >&2
            exit 1
        fi
    fi

    ensure_objcopy "$target"
    pbs=$(ensure_pbs "$pbs_arch") || exit 1

    work="$WORK/$target"
    mkdir -p "$work/dist" "$work/build" "$work/spec"
    echo "== $target: creating the build environment (Python $PYTHON_VERSION)"
    "$pbs/bin/python3" -m venv "$work/venv"
    "$work/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade pip
    "$work/venv/bin/pip" install --quiet --disable-pip-version-check \
        "pyinstaller==$PYINSTALLER_VERSION" "$ROOT"

    echo "== $target: running PyInstaller"
    if ! MACPM_ROOT="$ROOT" "$work/venv/bin/python" -m PyInstaller \
            --clean --noconfirm --log-level WARN \
            --distpath "$work/dist" --workpath "$work/build" \
            "$ROOT/packaging/macpm-linux.spec" > "$work/pyinstaller.log" 2>&1; then
        cat "$work/pyinstaller.log" >&2
        echo "build-linux: PyInstaller failed for $target" >&2
        exit 1
    fi

    echo "== $target: checking the binary"
    got=$(env -i PATH=/usr/bin:/bin "$work/dist/macpm" --version)
    echo "   $got"
    case "$got" in
        "macpm v$VERSION") ;;
        *) echo "build-linux: unexpected --version output: $got" >&2; exit 1 ;;
    esac

    name="macpm-$VERSION-linux-$target"
    stage="$work/stage/$name"
    mkdir -p "$stage"
    cp "$work/dist/macpm" "$stage/"
    cp "$ROOT/LICENSE" "$stage/"
    cp "$ROOT/packaging/README-linux.txt" "$stage/"
    chmod +x "$stage/macpm"
    tar -C "$work/stage" -czf "$DIST/$name.tar.gz" "$name"
    echo "   $DIST/$name.tar.gz"

    if [ "$WANT_DEB" = 1 ]; then
        if ! command -v dpkg-deb >/dev/null 2>&1; then
            echo "build-linux: --deb needs dpkg-deb (install dpkg-dev)" >&2
            exit 1
        fi
        pkg="$work/deb"
        mkdir -p "$pkg/DEBIAN" "$pkg/usr/bin" "$pkg/usr/share/doc/macpm"
        cp "$work/dist/macpm" "$pkg/usr/bin/macpm"
        cp "$ROOT/packaging/README-linux.txt" "$pkg/usr/share/doc/macpm/README.txt"
        cp "$ROOT/LICENSE" "$pkg/usr/share/doc/macpm/copyright"
        size=$(du -sk "$pkg/usr" | cut -f1)
        cat > "$pkg/DEBIAN/control" <<EOF
Package: macpm
Version: $VERSION
Architecture: $deb_arch
Maintainer: Jinyu Chen <visualcjy@mail.com>
Installed-Size: $size
Depends: libc6 (>= 2.17), libgcc-s1 | libgcc1
Recommends: iproute2
Section: utils
Priority: optional
Homepage: https://github.com/visualcjy/macpm
Description: Performance monitor for Apple Silicon and Linux
 macpm reads GPU and CPU counters from IOReport on macOS and from /proc and
 /sys on Linux.  The Linux build shows CPU, memory, disk and network plus
 any DRM, nvidia-smi or vendor GPU counters the host exposes.
EOF
        deb="$DIST/macpm_${VERSION}_${deb_arch}.deb"
        dpkg-deb --root-owner-group --build "$pkg" "$deb" >/dev/null
        echo "   $deb"
    fi
}

for arch in $(echo "$ARCHS" | tr ' ' '\n' | sort -u); do
    build_target "$arch"
done

echo
echo "Artifacts:"
ls -lh "$DIST"/macpm-*linux-*.tar.gz "$DIST"/macpm_*.deb 2>/dev/null || true
echo
echo "Check with:"
echo "  env -i PATH=/usr/bin:/bin $DIST/macpm --version   # after extracting a tarball"

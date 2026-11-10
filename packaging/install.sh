#!/bin/sh
# Install the standalone binaries into ~/.local/bin (no sudo, no Python).
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
prefix=${PREFIX:-"$HOME/.local/bin"}

mkdir -p "$prefix"
install -m 755 "$here/macpm" "$prefix/macpm"
install -m 755 "$here/macpm-sensors" "$prefix/macpm-sensors"

# The binaries are ad-hoc signed, not notarized.  This script is run by the
# user from the extracted archive, so drop the download quarantine flag from
# the installed copies to avoid a Gatekeeper prompt on first launch.
if command -v xattr >/dev/null 2>&1; then
    xattr -d com.apple.quarantine "$prefix/macpm" 2>/dev/null || true
    xattr -d com.apple.quarantine "$prefix/macpm-sensors" 2>/dev/null || true
fi

echo "Installed macpm and macpm-sensors to $prefix"
case ":$PATH:" in
    *":$prefix:"*) ;;
    *)
        echo
        echo "Add this to your shell profile (~/.zshrc) and restart the terminal:"
        echo "  export PATH=\"$prefix:\$PATH\""
        ;;
esac

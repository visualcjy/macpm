#!/bin/sh
# Install the standalone binaries into ~/.local/bin (no sudo, no Python).
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
prefix=${PREFIX:-"$HOME/.local/bin"}

mkdir -p "$prefix"
install -m 755 "$here/macpm" "$prefix/macpm"

# The binary is ad-hoc signed, not notarized.  This script is run by the
# user from the extracted archive, so drop the download quarantine flag from
# the installed copy to avoid a Gatekeeper prompt on first launch.
if command -v xattr >/dev/null 2>&1; then
    xattr -d com.apple.quarantine "$prefix/macpm" 2>/dev/null || true
fi

echo "Installed macpm to $prefix"
case ":$PATH:" in
    *":$prefix:"*) ;;
    *)
        echo
        echo "Add this to your shell profile (~/.zshrc) and restart the terminal:"
        echo "  export PATH=\"$prefix:\$PATH\""
        ;;
esac

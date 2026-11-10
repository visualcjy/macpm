#!/bin/sh
# Double-click launcher: opens Terminal and starts macpm from this folder.
cd "$(dirname "$0")" || exit 1
exec ./macpm "$@"

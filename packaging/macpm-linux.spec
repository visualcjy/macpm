# -*- mode: python ; coding: utf-8 -*-
"""One-file Linux build used by packaging/build-linux.sh.

The script points MACPM_ROOT at the checkout and PyInstaller at this file.

A note on libgcc_s: on aarch64 the bundled libpython needs libgcc_s.so.1, and
python-build-standalone does not ship one, so PyInstaller would pick up the
build host's copy (Ubuntu 26.04 -> requires GLIBC_2.35) and silently raise the
binary's glibc floor.  Every glibc distribution ships libgcc_s in its base
system (libgcc-s1 / libgcc), so it is dropped from the bundle and resolved at
run time instead; that keeps the floor at the libpython one (GLIBC_2.17).

This file is only used on Linux; macOS keeps using macpm.spec.
"""

import os

root = os.environ.get("MACPM_ROOT", os.getcwd())

a = Analysis(
    [os.path.join(root, "packaging", "entry_tui.py")],
    pathex=[root],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
a.binaries = [b for b in a.binaries if os.path.basename(b[0]) != "libgcc_s.so.1"]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="macpm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

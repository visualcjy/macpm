"""Per-process GPU time on macOS, from the IOAccelerator user clients.

The GPU's IOAccelerator user clients expose an ``accumulatedGPUTime`` counter in
the IORegistry.  Walking them gives GPU usage per process without root, using
the same per-process GPU counter the system tools read.

The CoreFoundation/IOKit libraries are opened on first use so importing macpm
on Linux never tries to dlopen a macOS-only framework.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_char_p, c_longlong, c_uint32, c_void_p
from typing import Optional

_CF = None
_IOKIT = None

_UTF8 = 0x08000100
_CF_NUMBER_SINT64 = 4


def _load() -> None:
    """Bind CoreFoundation/IOKit the first time a GPU read happens."""
    global _CF, _IOKIT
    if _CF is not None:
        return
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")

    cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
    cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, ctypes.c_long, c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFArrayGetCount.argtypes = [c_void_p]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, ctypes.c_long]
    cf.CFArrayGetValueAtIndex.restype = c_void_p
    cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
    cf.CFDictionaryGetValue.restype = c_void_p
    cf.CFNumberGetValue.argtypes = [c_void_p, ctypes.c_int, c_void_p]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFGetTypeID.argtypes = [c_void_p]
    cf.CFGetTypeID.restype = ctypes.c_ulong
    cf.CFStringGetTypeID.restype = ctypes.c_ulong
    cf.CFArrayGetTypeID.restype = ctypes.c_ulong
    cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
    cf.CFNumberGetTypeID.restype = ctypes.c_ulong
    cf.CFRelease.argtypes = [c_void_p]

    iokit.IOServiceMatching.argtypes = [c_char_p]
    iokit.IOServiceMatching.restype = c_void_p
    iokit.IOServiceGetMatchingServices.argtypes = [c_uint32, c_void_p, POINTER(c_uint32)]
    iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
    iokit.IOIteratorNext.argtypes = [c_uint32]
    iokit.IOIteratorNext.restype = c_uint32
    iokit.IOObjectRelease.argtypes = [c_uint32]
    iokit.IORegistryEntryGetChildIterator.argtypes = [c_uint32, c_char_p, POINTER(c_uint32)]
    iokit.IORegistryEntryGetChildIterator.restype = ctypes.c_int
    iokit.IORegistryEntryCreateCFProperty.argtypes = [c_uint32, c_void_p, c_void_p, c_uint32]
    iokit.IORegistryEntryCreateCFProperty.restype = c_void_p

    _CF, _IOKIT = cf, iokit


def _cfstr(value: str) -> int:
    return _CF.CFStringCreateWithCString(None, value.encode("utf-8"), _UTF8)


def _cf_string(ref) -> str:
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(256)
    if _CF.CFStringGetCString(ref, buf, len(buf), _UTF8):
        return buf.value.decode("utf-8", "replace")
    return ""


def _cf_number(ref) -> Optional[int]:
    out = c_longlong(0)
    if _CF.CFNumberGetValue(ref, _CF_NUMBER_SINT64, byref(out)):
        return out.value
    return None


_CREATOR_KEY = None
_USAGE_KEY = None
_GPU_TIME_KEY = None


def _init_keys() -> None:
    global _CREATOR_KEY, _USAGE_KEY, _GPU_TIME_KEY
    if _CREATOR_KEY is None:
        _CREATOR_KEY = _cfstr("IOUserClientCreator")
        _USAGE_KEY = _cfstr("AppUsage")
        _GPU_TIME_KEY = _cfstr("accumulatedGPUTime")


def _client_pid(client) -> Optional[int]:
    prop = _IOKIT.IORegistryEntryCreateCFProperty(client, _CREATOR_KEY, None, 0)
    if not prop:
        return None
    try:
        if _CF.CFGetTypeID(prop) != _CF.CFStringGetTypeID():
            return None
        creator = _cf_string(prop)
    finally:
        _CF.CFRelease(prop)
    if not creator.startswith("pid "):
        return None
    try:
        return int(creator[4:].split(",", 1)[0])
    except ValueError:
        return None


def _client_gpu_ns(client) -> int:
    prop = _IOKIT.IORegistryEntryCreateCFProperty(client, _USAGE_KEY, None, 0)
    if not prop:
        return 0
    try:
        if _CF.CFGetTypeID(prop) != _CF.CFArrayGetTypeID():
            return 0
        total = 0
        for index in range(_CF.CFArrayGetCount(prop)):
            entry = _CF.CFArrayGetValueAtIndex(prop, index)
            if not entry or _CF.CFGetTypeID(entry) != _CF.CFDictionaryGetTypeID():
                continue
            value = _CF.CFDictionaryGetValue(entry, _GPU_TIME_KEY)
            if value and _CF.CFGetTypeID(value) == _CF.CFNumberGetTypeID():
                ns = _cf_number(value)
                if ns is not None and ns >= 0:
                    total += ns
        return total
    finally:
        _CF.CFRelease(prop)


def read_gpu_times() -> Optional[dict[int, int]]:
    """Accumulated GPU time in ns per pid, or None when the registry fails."""
    _load()
    _init_keys()
    matching = _IOKIT.IOServiceMatching(b"IOAccelerator")
    if not matching:
        return None
    iterator = c_uint32()
    if _IOKIT.IOServiceGetMatchingServices(0, matching, byref(iterator)) != 0:
        return None

    totals: dict[int, int] = {}
    try:
        while True:
            service = _IOKIT.IOIteratorNext(iterator)
            if not service:
                break
            try:
                children = c_uint32()
                if _IOKIT.IORegistryEntryGetChildIterator(
                    service, b"IOService", byref(children)
                ) != 0:
                    continue
                try:
                    while True:
                        client = _IOKIT.IOIteratorNext(children)
                        if not client:
                            break
                        try:
                            try:
                                pid = _client_pid(client)
                                if pid is not None:
                                    totals[pid] = totals.get(pid, 0) + _client_gpu_ns(client)
                            except Exception:
                                pass
                        finally:
                            _IOKIT.IOObjectRelease(client)
                finally:
                    _IOKIT.IOObjectRelease(children)
            finally:
                _IOKIT.IOObjectRelease(service)
    finally:
        _IOKIT.IOObjectRelease(iterator)
    return totals


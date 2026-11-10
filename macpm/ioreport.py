"""Minimal ctypes bindings for Apple's private IOReport framework.

IOReport is the same data source powermetrics uses internally.  Unlike the
powermetrics CLI it does not require root for the channels used by macpm
(CPU/GPU performance states and the Energy Model counters).

The API is private and undocumented.  Keep every CF object's lifetime short
and never keep pointers into a sample after it is released.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from typing import Optional

CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
IO = ctypes.CDLL("/usr/lib/libIOReport.dylib")

c_void_p = ctypes.c_void_p
c_int32 = ctypes.c_int32
c_int64 = ctypes.c_int64
c_uint32 = ctypes.c_uint32
c_uint64 = ctypes.c_uint64

CF.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]
CF.CFStringCreateWithCString.restype = c_void_p
CF.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_long, c_uint32]
CF.CFStringGetCString.restype = ctypes.c_bool
CF.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
CF.CFDictionaryGetValue.restype = c_void_p
CF.CFArrayGetCount.argtypes = [c_void_p]
CF.CFArrayGetCount.restype = ctypes.c_long
CF.CFArrayGetValueAtIndex.argtypes = [c_void_p, ctypes.c_long]
CF.CFArrayGetValueAtIndex.restype = c_void_p
CF.CFArrayCreateMutable.argtypes = [c_void_p, ctypes.c_long, c_void_p]
CF.CFArrayCreateMutable.restype = c_void_p
CF.CFArrayAppendValue.argtypes = [c_void_p, c_void_p]
CF.CFArrayAppendValue.restype = None
CF.CFDictionaryCreateMutableCopy.argtypes = [c_void_p, ctypes.c_long, c_void_p]
CF.CFDictionaryCreateMutableCopy.restype = c_void_p
CF.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
CF.CFDictionarySetValue.restype = None
CF.CFRelease.argtypes = [c_void_p]
CF.CFRelease.restype = None

IO.IOReportCopyChannelsInGroup.argtypes = [
    c_void_p, c_void_p, c_uint64, c_uint64, c_uint64
]
IO.IOReportCopyChannelsInGroup.restype = c_void_p
IO.IOReportCreateSubscription.argtypes = [
    c_void_p, c_void_p, ctypes.POINTER(c_void_p), c_uint64, c_void_p
]
IO.IOReportCreateSubscription.restype = c_void_p
IO.IOReportCreateSamples.argtypes = [c_void_p, c_void_p, c_void_p]
IO.IOReportCreateSamples.restype = c_void_p
IO.IOReportCreateSamplesDelta.argtypes = [c_void_p, c_void_p, c_void_p]
IO.IOReportCreateSamplesDelta.restype = c_void_p
IO.IOReportChannelGetGroup.argtypes = [c_void_p]
IO.IOReportChannelGetGroup.restype = c_void_p
IO.IOReportChannelGetSubGroup.argtypes = [c_void_p]
IO.IOReportChannelGetSubGroup.restype = c_void_p
IO.IOReportChannelGetChannelName.argtypes = [c_void_p]
IO.IOReportChannelGetChannelName.restype = c_void_p
IO.IOReportChannelGetUnitLabel.argtypes = [c_void_p]
IO.IOReportChannelGetUnitLabel.restype = c_void_p
IO.IOReportStateGetCount.argtypes = [c_void_p]
IO.IOReportStateGetCount.restype = c_int32
IO.IOReportStateGetNameForIndex.argtypes = [c_void_p, c_int32]
IO.IOReportStateGetNameForIndex.restype = c_void_p
IO.IOReportStateGetResidency.argtypes = [c_void_p, c_int32]
IO.IOReportStateGetResidency.restype = c_int64
IO.IOReportSimpleGetIntegerValue.argtypes = [c_void_p, c_int32]
IO.IOReportSimpleGetIntegerValue.restype = c_int64

_UTF8 = 0x08000100
_CFSTR_CACHE: dict[str, int] = {}


def cfstr(value: str) -> int:
    """CFStringRef for value, cached for the lifetime of the process."""
    cached = _CFSTR_CACHE.get(value)
    if cached:
        return cached
    ref = CF.CFStringCreateWithCString(None, value.encode("utf-8"), _UTF8)
    if not ref:
        raise RuntimeError(f"CFStringCreateWithCString failed for {value!r}")
    _CFSTR_CACHE[value] = ref
    return ref


def _string(ref: Optional[int]) -> str:
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(512)
    if CF.CFStringGetCString(ref, buf, len(buf), _UTF8):
        return buf.value.decode("utf-8", "replace")
    return ""


@dataclass
class Channel:
    """One channel from an IOReport sample delta."""

    group: str
    subgroup: str
    name: str
    unit: str
    value: int = 0
    states: list[tuple[str, int]] = field(default_factory=list)


def _channels_in_group(group: str, subgroup: Optional[str]) -> int:
    ref = IO.IOReportCopyChannelsInGroup(
        cfstr(group), cfstr(subgroup) if subgroup else None, 0, 0, 0
    )
    if not ref:
        raise RuntimeError(f"IOReportCopyChannelsInGroup({group!r}, {subgroup!r}) failed")
    return ref


def _filter_channels(raw: int, keep) -> int:
    """Return a channels dict containing only channels for which keep(name) is true.

    Subscribing to the full Energy Model group allocates buffers for hundreds of
    channels; macpm only needs CPU/GPU/ANE energy.  Keeping the raw dictionary
    alive (in Subscription) keeps the channel objects referenced by the new
    array valid without CFArray callbacks.
    """
    array = CF.CFDictionaryGetValue(raw, cfstr("IOReportChannels"))
    if not array:
        return raw

    filtered = CF.CFArrayCreateMutable(None, 0, None)
    if not filtered:
        return raw

    count = CF.CFArrayGetCount(array)
    kept = 0
    for index in range(count):
        item = CF.CFArrayGetValueAtIndex(array, index)
        if not item:
            continue
        name = _string(IO.IOReportChannelGetChannelName(item))
        if keep(name):
            CF.CFArrayAppendValue(filtered, item)
            kept += 1

    if kept == 0 or kept == count:
        CF.CFRelease(filtered)
        return raw

    copy = CF.CFDictionaryCreateMutableCopy(None, 0, raw)
    if not copy:
        CF.CFRelease(filtered)
        return raw
    CF.CFDictionarySetValue(copy, cfstr("IOReportChannels"), filtered)
    CF.CFRelease(filtered)
    return copy


class Subscription:
    """A long-lived IOReport subscription for one group/subgroup."""

    def __init__(self, group: str, subgroup: Optional[str] = None, keep=None):
        self.group = group
        self.subgroup = subgroup
        raw = _channels_in_group(group, subgroup)
        self._channels = _filter_channels(raw, keep) if keep is not None else raw
        # If filtering made a copy, the original dict owns the channel objects
        # referenced by the filtered array.
        self._raw_channels = raw if self._channels != raw else None
        extra = c_void_p()
        sub = IO.IOReportCreateSubscription(
            None, self._channels, ctypes.byref(extra), 0, None
        )
        if not sub:
            CF.CFRelease(self._channels)
            raise RuntimeError(f"IOReportCreateSubscription({group!r}, {subgroup!r}) failed")
        self._sub = sub
        if extra and extra.value != sub:
            # The out parameter is an extra reference we do not use.
            CF.CFRelease(extra)
        self._prev: Optional[int] = None

    def delta(self) -> Optional[dict[str, Channel]]:
        """Return channel values since the previous call, or None for the baseline."""
        cur = IO.IOReportCreateSamples(self._sub, self._channels, None)
        if not cur:
            return None
        if self._prev is None:
            self._prev = cur
            return None

        diff = IO.IOReportCreateSamplesDelta(self._prev, cur, None)
        CF.CFRelease(self._prev)
        self._prev = cur
        if not diff:
            return {}

        try:
            return self._parse(diff)
        finally:
            CF.CFRelease(diff)

    def _parse(self, sample: int) -> dict[str, Channel]:
        array = CF.CFDictionaryGetValue(sample, cfstr("IOReportChannels"))
        if not array:
            return {}

        result: dict[str, Channel] = {}
        count = CF.CFArrayGetCount(array)
        for index in range(count):
            item = CF.CFArrayGetValueAtIndex(array, index)
            if not item:
                continue
            name = _string(IO.IOReportChannelGetChannelName(item))
            if not name:
                continue
            channel = Channel(
                group=_string(IO.IOReportChannelGetGroup(item)),
                subgroup=_string(IO.IOReportChannelGetSubGroup(item)),
                name=name,
                unit=_string(IO.IOReportChannelGetUnitLabel(item)),
            )
            state_count = IO.IOReportStateGetCount(item)
            if state_count > 0:
                channel.states = [
                    (
                        _string(IO.IOReportStateGetNameForIndex(item, i)),
                        IO.IOReportStateGetResidency(item, i),
                    )
                    for i in range(state_count)
                ]
            else:
                channel.value = IO.IOReportSimpleGetIntegerValue(item, 0)
            result[name] = channel
        return result

    def close(self) -> None:
        if self._prev:
            CF.CFRelease(self._prev)
            self._prev = None
        if self._channels:
            CF.CFRelease(self._channels)
            self._channels = 0
        if self._raw_channels:
            CF.CFRelease(self._raw_channels)
            self._raw_channels = None
        if getattr(self, "_sub", None):
            CF.CFRelease(self._sub)
            self._sub = 0

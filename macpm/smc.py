"""AppleSMC reader for temperatures, fans and a few power rails.

Reading the SMC through the IOKit user client does not require root.  Writing
(fan control) does, and is deliberately not implemented here.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_size_t,
    c_uint8,
    c_uint16,
    c_uint32,
    c_void_p,
)
from typing import Optional

IOKIT = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
LIBSYSTEM = ctypes.CDLL("/usr/lib/libSystem.B.dylib")

IOKIT.IOServiceMatching.argtypes = [c_char_p]
IOKIT.IOServiceMatching.restype = c_void_p
IOKIT.IOServiceGetMatchingServices.argtypes = [c_uint32, c_void_p, POINTER(c_uint32)]
IOKIT.IOServiceGetMatchingServices.restype = c_uint32
IOKIT.IOIteratorNext.argtypes = [c_uint32]
IOKIT.IOIteratorNext.restype = c_uint32
IOKIT.IOObjectRelease.argtypes = [c_uint32]
IOKIT.IOServiceOpen.argtypes = [c_uint32, c_uint32, c_uint32, POINTER(c_uint32)]
IOKIT.IOServiceOpen.restype = c_uint32
IOKIT.IOServiceClose.argtypes = [c_uint32]
IOKIT.IOConnectCallStructMethod.argtypes = [
    c_uint32,
    c_uint32,
    c_void_p,
    c_size_t,
    c_void_p,
    POINTER(c_size_t),
]
IOKIT.IOConnectCallStructMethod.restype = c_uint32

_MACH_TASK_SELF = c_uint32.in_dll(LIBSYSTEM, "mach_task_self_").value
_KERNEL_INDEX = 2
_READ_KEY_INFO = 9
_READ_BYTES = 5


class _Vers(Structure):
    _fields_ = [
        ("major", c_uint8),
        ("minor", c_uint8),
        ("build", c_uint8),
        ("reserved", c_uint8),
        ("release", c_uint16),
    ]


class _PLimitData(Structure):
    _fields_ = [
        ("version", c_uint16),
        ("length", c_uint16),
        ("cpuPLimit", c_uint32),
        ("gpuPLimit", c_uint32),
        ("memPLimit", c_uint32),
    ]


class _KeyInfo(Structure):
    _fields_ = [
        ("dataSize", c_uint32),
        ("dataType", c_uint32),
        ("dataAttributes", c_uint8),
    ]


class _KeyData(Structure):
    _fields_ = [
        ("key", c_uint32),
        ("vers", _Vers),
        ("pLimitData", _PLimitData),
        ("keyInfo", _KeyInfo),
        ("result", c_uint8),
        ("status", c_uint8),
        ("data8", c_uint8),
        ("data32", c_uint32),
        ("bytes", c_uint8 * 32),
    ]


def _fourcc(key: str) -> int:
    return int.from_bytes(key.encode("ascii"), "big")


# Temperature key candidates by chip family.  SMC key names are not stable
# across generations; only the keys that exist are reported.
_COMMON_TEMPS = ["TC0P", "TC0D", "TC0E", "TC0F", "TC0H", "TC0S", "TB1T", "TB2T"]
_CHIP_TEMPS = {
    "M1": ["Tp09", "Tp0T", "Tp01", "Tp05", "Tp0D", "Tp0H", "Tp0L", "Tp0P", "Tp0X", "Tp0b"],
    "M2": ["Tp1h", "Tp1t", "Tp1p", "Tp1l", "Tp01", "Tp05", "Tp09", "Tp0D", "Tp0X", "Tp0b", "Tp0f", "Tp0j"],
    "M3": ["Te05", "Te0L", "Te0P", "Te0S", "Tf04", "Tf09", "Tf0A", "Tf0B", "Tf0D", "Tf0E", "Tf44", "Tf49", "Tf4A", "Tf4B", "Tf4D", "Tf4E"],
    "M4": ["Te05", "Te09", "Te0H", "Te0S", "Tp01", "Tp05", "Tp09", "Tp0D", "Tp0V", "Tp0Y", "Tp0b", "Tp0e"],
    "M5": ["Tp00", "Tp04", "Tp08", "Tp0C", "Tp0G", "Tp0K", "Tp0O", "Tp0R", "Tp0U", "Tp0X", "Tp0a", "Tp0d", "Tp0g", "Tp0j", "Tp0m", "Tp0p", "Tp0u", "Tp0y"],
}


def _chip_family(chip: str) -> str:
    for family in ("M1", "M2", "M3", "M4", "M5"):
        if family in chip:
            return family
    return "M3"


class SMC:
    def __init__(self):
        self.conn = 0
        iterator = c_uint32()
        matching = IOKIT.IOServiceMatching(b"AppleSMC")
        if not matching:
            raise RuntimeError("AppleSMC service not found")
        result = IOKIT.IOServiceGetMatchingServices(0, matching, byref(iterator))
        if result != 0:
            raise RuntimeError(f"IOServiceGetMatchingServices failed: 0x{result:x}")
        device = IOKIT.IOIteratorNext(iterator)
        IOKIT.IOObjectRelease(iterator)
        if not device:
            raise RuntimeError("AppleSMC device not found")

        conn = c_uint32()
        result = IOKIT.IOServiceOpen(device, _MACH_TASK_SELF, 0, byref(conn))
        IOKIT.IOObjectRelease(device)
        if result != 0:
            raise RuntimeError(f"IOServiceOpen(AppleSMC) failed: 0x{result:x}")
        self.conn = conn.value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self.conn:
            IOKIT.IOServiceClose(self.conn)
            self.conn = 0

    # -- low level ---------------------------------------------------------

    def _call(self, request: _KeyData) -> Optional[_KeyData]:
        response = _KeyData()
        size = c_size_t(ctypes.sizeof(_KeyData))
        result = IOKIT.IOConnectCallStructMethod(
            self.conn,
            _KERNEL_INDEX,
            byref(request),
            ctypes.sizeof(_KeyData),
            byref(response),
            byref(size),
        )
        if result != 0 or response.result != 0:
            return None
        return response

    def key_info(self, key: str) -> Optional[tuple[int, str]]:
        request = _KeyData()
        request.key = _fourcc(key)
        request.data8 = _READ_KEY_INFO
        response = self._call(request)
        if response is None:
            return None
        size = int(response.keyInfo.dataSize)
        data_type = int(response.keyInfo.dataType).to_bytes(4, "big").decode(
            "latin1"
        )
        return size, data_type

    def read_raw(self, key: str) -> Optional[tuple[str, bytes]]:
        info = self.key_info(key)
        if not info:
            return None
        size, data_type = info
        if size <= 0 or size > 32:
            return None
        request = _KeyData()
        request.key = _fourcc(key)
        request.data8 = _READ_BYTES
        request.keyInfo.dataSize = size
        response = self._call(request)
        if response is None:
            return None
        return data_type, bytes(response.bytes[:size])

    # -- decoding ----------------------------------------------------------

    @staticmethod
    def decode(data_type: str, raw: bytes):
        if data_type == "ui8 ":
            return raw[0]
        if data_type == "ui16":
            return int.from_bytes(raw, "big")
        if data_type == "ui32":
            return int.from_bytes(raw, "big")
        if data_type == "flt ":
            return struct.unpack("<f", raw[:4])[0]
        if data_type == "fpe2":
            return (raw[0] << 6) + (raw[1] >> 2)
        if data_type == "sp78":
            return int.from_bytes(raw, "big", signed=True) / 256
        if data_type == "sp87":
            return int.from_bytes(raw, "big", signed=True) / 128
        if data_type == "sp5a":
            return int.from_bytes(raw, "big") / 1024
        if data_type == "sp69":
            return int.from_bytes(raw, "big") / 512
        if data_type == "sp96":
            return int.from_bytes(raw, "big", signed=True) / 64
        if data_type == "spb4":
            return int.from_bytes(raw, "big", signed=True) / 16
        if data_type == "sp1e":
            return int.from_bytes(raw, "big") / 16384
        if data_type == "sp3c":
            return int.from_bytes(raw, "big") / 4096
        if data_type == "sp4b":
            return int.from_bytes(raw, "big") / 2048
        if data_type == "spa5":
            return int.from_bytes(raw, "big") / 32
        if data_type == "spf0":
            return int.from_bytes(raw, "big", signed=True)
        if data_type == "fds":
            text = raw[4:16].decode("ascii", "ignore").strip("\x00 ").strip()
            return text
        return raw.hex()

    def get_value(self, key: str):
        raw = self.read_raw(key)
        if raw is None:
            return None
        data_type, payload = raw
        try:
            return self.decode(data_type, payload)
        except (IndexError, struct.error, ValueError):
            return None

    # -- high level --------------------------------------------------------

    def get_fans(self) -> list[dict]:
        count = self.get_value("FNum")
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        fans = []
        for index in range(min(count, 10)):
            mode = self.get_value(f"F{index}Md")
            if mode is None:
                mode = self.get_value(f"F{index}md")
            fans.append(
                {
                    "id": index,
                    "name": self.get_value(f"F{index}ID") or f"Fan #{index}",
                    "rpm": self.get_value(f"F{index}Ac"),
                    "min_rpm": self.get_value(f"F{index}Mn"),
                    "max_rpm": self.get_value(f"F{index}Mx"),
                    "mode": mode,
                }
            )
        return fans

    def get_temperatures(self, chip: str) -> dict[str, float]:
        family = _chip_family(chip)
        keys = list(dict.fromkeys(_CHIP_TEMPS.get(family, []) + _COMMON_TEMPS))
        values = {}
        for key in keys:
            value = self.get_value(key)
            if isinstance(value, (int, float)) and 0 < value < 130:
                values[key] = round(float(value), 2)
        return values

    def get_power(self) -> dict[str, Optional[float]]:
        result = {}
        for key, label in (("PSTR", "system_w"), ("PPBR", "battery_w")):
            value = self.get_value(key)
            if isinstance(value, (int, float)):
                result[label] = round(float(value), 2)
        return result

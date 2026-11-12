"""Local LAN identity and the public IP / city shown in the default and
network views.

The local part is read without privileges: ``psutil`` provides the interface
addresses and ``/sbin/route`` the default gateway.  The public part is a single
HTTPS request to a geolocation service, made in a background thread and cached
for several minutes; ``--no-public-ip`` disables it entirely.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import psutil

ROUTE = "/sbin/route"
CURL = "/usr/bin/curl"

LOCAL_TTL = 30.0
PUBLIC_TTL = 600.0
PUBLIC_RETRY = 60.0
HTTP_TIMEOUT = 3.0


@dataclass
class LocalNetwork:
    interface: str
    address: str
    prefix: int
    gateway: str

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"


@dataclass
class PublicNetwork:
    address: str
    city: str = ""
    region: str = ""
    country: str = ""
    org: str = ""


def _mask_to_prefix(mask: str) -> Optional[int]:
    """Turn ``255.255.252.0`` into 22; reject non-contiguous masks."""
    try:
        octets = [int(part) for part in mask.split(".")]
    except (AttributeError, ValueError):
        return None
    if len(octets) != 4 or any(octet < 0 or octet > 255 for octet in octets):
        return None
    bits = "".join(f"{octet:08b}" for octet in octets)
    if "01" in bits:
        return None
    return bits.count("1")


def _parse_route(text: str) -> tuple[str, str]:
    """Extract ``(gateway, interface)`` from ``route -n get default``."""
    gateway = ""
    interface = ""
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key = key.strip().lower()
        if key == "gateway":
            gateway = value.strip()
        elif key == "interface":
            interface = value.strip()
    return gateway, interface


def _read_default_route() -> tuple[str, str]:
    try:
        result = subprocess.run(
            [ROUTE, "-n", "get", "default"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return "", ""
    if result.returncode != 0:
        return "", ""
    return _parse_route(result.stdout.decode("utf-8", "replace"))


def read_local(
    route_reader: Callable[[], tuple[str, str]] = _read_default_route,
    addrs_reader: Callable = psutil.net_if_addrs,
) -> Optional[LocalNetwork]:
    """Return the active IPv4 interface, its prefix and the default gateway."""
    gateway, interface = route_reader()
    try:
        addresses = addrs_reader()
    except Exception:
        return None

    order = []
    if interface in addresses:
        order.append(interface)
    order += [name for name in addresses if name not in order and not name.startswith("lo")]

    for name in order:
        for address in addresses[name]:
            if address.family != socket.AF_INET:
                continue
            host = (address.address or "").strip()
            if not host or host.startswith("169.254."):
                continue
            prefix = _mask_to_prefix(address.netmask or "")
            if prefix is None:
                continue
            return LocalNetwork(
                interface=name,
                address=host,
                prefix=prefix,
                gateway=gateway,
            )
    return None


def _parse_ipinfo(data) -> Optional[PublicNetwork]:
    if not isinstance(data, dict):
        return None
    address = str(data.get("ip") or "").strip()
    if not address:
        return None
    return PublicNetwork(
        address=address,
        city=str(data.get("city") or "").strip(),
        region=str(data.get("region") or "").strip(),
        country=str(data.get("country") or "").strip(),
        org=str(data.get("org") or "").strip(),
    )


def _parse_ipwho(data) -> Optional[PublicNetwork]:
    if not isinstance(data, dict) or data.get("success") is False:
        return None
    address = str(data.get("ip") or "").strip()
    if not address:
        return None
    connection = data.get("connection")
    org = ""
    if isinstance(connection, dict):
        org = str(connection.get("isp") or "").strip()
    return PublicNetwork(
        address=address,
        city=str(data.get("city") or "").strip(),
        region=str(data.get("region") or "").strip(),
        country=str(data.get("country_code") or data.get("country") or "").strip(),
        org=org,
    )


# The first service that answers wins.  Both are free, key-less JSON
# endpoints; the request only carries the client's public IP.
_ENDPOINTS = (
    ("https://ipinfo.io/json", _parse_ipinfo),
    ("https://ipwho.is/", _parse_ipwho),
)


def _http_get(url: str, timeout: float = HTTP_TIMEOUT) -> Optional[dict]:
    try:
        result = subprocess.run(
            [CURL, "-fsS", "--connect-timeout", "2", "--max-time", str(int(timeout)), url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1.0,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout.decode("utf-8", "replace"))
    except (UnicodeDecodeError, ValueError):
        return None


def fetch_public(http_get: Callable[[str], Optional[dict]] = _http_get) -> Optional[PublicNetwork]:
    """Try each geolocation endpoint; return the first valid answer."""
    for url, parser in _ENDPOINTS:
        payload = http_get(url)
        if payload is None:
            continue
        info = parser(payload)
        if info is not None:
            return info
    return None


_CN_ISP_PATTERNS = (
    ("UNICOM", "Unicom"),
    ("TELECOM", "Telecom"),
    ("CHINANET", "Telecom"),
    ("MOBILE", "Mobile"),
    ("CMNET", "Mobile"),
    ("CMCC", "Mobile"),
)


def _is_china(country: str) -> bool:
    return (country or "").strip().upper() in ("CN", "CHN", "CHINA")


def _china_isp(org: str) -> str:
    text = (org or "").upper()
    for needle, label in _CN_ISP_PATTERNS:
        if needle in text:
            return label
    return ""


def format_netinfo(
    local: Optional[LocalNetwork],
    public: Optional[PublicNetwork],
    include_country: bool = True,
) -> str:
    """Compact one-line description used in the view's top border."""
    parts = []
    if local is not None:
        parts.append(f"{local.interface} {local.cidr}")
        if local.gateway:
            parts.append(f"gw {local.gateway}")
    if public is not None:
        place_parts = [public.city]
        if include_country and public.country:
            label = public.country
            if _is_china(public.country):
                isp = _china_isp(public.org)
                if isp:
                    label = f"{label} {isp}"
            place_parts.append(label)
        place = ", ".join(part for part in place_parts if part)
        parts.append(f"{public.address} {place}".strip())
    return "  ·  ".join(parts)


class NetworkInfoService:
    """Cached local/public identity with a background public-IP lookup."""

    def __init__(
        self,
        public: bool = True,
        clock: Callable[[], float] = time.monotonic,
        local_reader: Callable[[], Optional[LocalNetwork]] = read_local,
        public_fetcher: Callable[[], Optional[PublicNetwork]] = fetch_public,
    ):
        self.public_enabled = public
        self.clock = clock
        self.local_reader = local_reader
        self.public_fetcher = public_fetcher
        self.local: Optional[LocalNetwork] = None
        self.public: Optional[PublicNetwork] = None
        self._lock = threading.Lock()
        self._local_at = 0.0
        self._public_next = 0.0
        self._thread: Optional[threading.Thread] = None

    def snapshot(self) -> tuple[Optional[LocalNetwork], Optional[PublicNetwork]]:
        with self._lock:
            return self.local, self.public

    def refresh_local(self, force: bool = False) -> None:
        now = self.clock()
        with self._lock:
            if not force and self.local is not None and now - self._local_at < LOCAL_TTL:
                return
        try:
            local = self.local_reader()
        except Exception:
            local = None
        with self._lock:
            self.local = local
            self._local_at = now

    def refresh_public(self, force: bool = False) -> None:
        if not self.public_enabled:
            return
        now = self.clock()
        with self._lock:
            if not force and now < self._public_next:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            thread = threading.Thread(target=self._load_public, daemon=True)
            self._thread = thread
            # Do not start a second request while this one is in flight.
            self._public_next = now + PUBLIC_RETRY
        thread.start()

    def _load_public(self) -> None:
        try:
            info = self.public_fetcher()
        except Exception:
            info = None
        now = self.clock()
        with self._lock:
            if info is not None:
                self.public = info
                self._public_next = now + PUBLIC_TTL
            else:
                self._public_next = now + PUBLIC_RETRY

    def wait(self, timeout: float = 1.0) -> None:
        """Join the lookup thread; used by tests, not by the UI."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

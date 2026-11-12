import socket
import threading
import time
import types
import unittest

from macpm.netinfo import (
    LocalNetwork,
    NetworkInfoService,
    PublicNetwork,
    _mask_to_prefix,
    _parse_ipinfo,
    _parse_ipwho,
    _parse_route,
    fetch_public,
    format_netinfo,
    read_local,
)


def addr(family, address, netmask):
    return types.SimpleNamespace(family=family, address=address, netmask=netmask)


class MaskTests(unittest.TestCase):
    def test_prefix_lengths(self):
        self.assertEqual(_mask_to_prefix("255.255.252.0"), 22)
        self.assertEqual(_mask_to_prefix("255.255.255.0"), 24)
        self.assertEqual(_mask_to_prefix("255.255.255.255"), 32)
        self.assertEqual(_mask_to_prefix("0.0.0.0"), 0)

    def test_invalid_masks(self):
        self.assertIsNone(_mask_to_prefix("255.0.255.0"))
        self.assertIsNone(_mask_to_prefix("nonsense"))
        self.assertIsNone(_mask_to_prefix(None))


class RouteTests(unittest.TestCase):
    def test_gateway_and_interface(self):
        text = """   route to: default
destination: default
       mask: default
    gateway: 10.208.52.1
  interface: en0
      flags: <UP,GATEWAY,DONE>
"""
        self.assertEqual(_parse_route(text), ("10.208.52.1", "en0"))

    def test_missing_route(self):
        self.assertEqual(_parse_route("route: not found"), ("", ""))


class LocalNetworkTests(unittest.TestCase):
    def test_reads_the_active_ipv4_interface(self):
        addresses = {
            "en0": [
                addr(socket.AF_INET6, "fe80::1", "ffff:ffff:ffff:ffff::"),
                addr(socket.AF_INET, "10.208.54.69", "255.255.252.0"),
            ],
            "utun3": [addr(socket.AF_INET, "100.64.0.2", "255.255.255.255")],
        }

        local = read_local(
            route_reader=lambda: ("10.208.52.1", "en0"),
            addrs_reader=lambda: addresses,
        )

        self.assertEqual(
            local, LocalNetwork("en0", "10.208.54.69", 22, "10.208.52.1")
        )

    def test_falls_back_when_the_route_interface_is_unknown(self):
        addresses = {"en0": [addr(socket.AF_INET, "192.168.1.23", "255.255.255.0")]}

        local = read_local(
            route_reader=lambda: ("", "utun9"),
            addrs_reader=lambda: addresses,
        )

        self.assertEqual(local.interface, "en0")
        self.assertEqual(local.gateway, "")

    def test_ignores_loopback_and_link_local_addresses(self):
        addresses = {
            "lo0": [addr(socket.AF_INET, "127.0.0.1", "255.0.0.0")],
            "en5": [addr(socket.AF_INET, "169.254.10.20", "255.255.0.0")],
        }

        self.assertIsNone(
            read_local(
                route_reader=lambda: ("", "utun9"),
                addrs_reader=lambda: addresses,
            )
        )


class PublicParsingTests(unittest.TestCase):
    IPINFO = {
        "ip": "221.7.131.221",
        "city": "Nanning",
        "region": "Guangxi",
        "country": "CN",
        "org": "AS4837 CHINA UNICOM",
    }
    IPWHO = {
        "success": True,
        "ip": "221.7.131.221",
        "city": "Nanning",
        "region": "Guangxi",
        "country": "China",
        "country_code": "CN",
        "connection": {"isp": "China Unicom"},
    }

    def test_ipinfo(self):
        public = _parse_ipinfo(self.IPINFO)

        self.assertEqual(public.address, "221.7.131.221")
        self.assertEqual(public.city, "Nanning")
        self.assertEqual(public.country, "CN")
        self.assertEqual(public.org, "AS4837 CHINA UNICOM")

    def test_ipwho(self):
        public = _parse_ipwho(self.IPWHO)

        self.assertEqual(public.address, "221.7.131.221")
        self.assertEqual(public.country, "CN")
        self.assertEqual(public.org, "China Unicom")

    def test_rejects_failed_or_empty_payloads(self):
        self.assertIsNone(_parse_ipinfo({}))
        self.assertIsNone(_parse_ipinfo(None))
        self.assertIsNone(_parse_ipwho({"success": False, "ip": "1.2.3.4"}))
        self.assertIsNone(_parse_ipwho({"success": True}))

    def test_fetch_public_falls_back_to_the_second_endpoint(self):
        seen = []

        def http_get(url):
            seen.append(url)
            if "ipinfo" in url:
                return None
            return self.IPWHO

        public = fetch_public(http_get)

        self.assertEqual(public.address, "221.7.131.221")
        self.assertEqual(len(seen), 2)

    def test_fetch_public_returns_none_when_everything_fails(self):
        self.assertIsNone(fetch_public(lambda url: None))


class FormatTests(unittest.TestCase):
    def test_full_line(self):
        local = LocalNetwork("en0", "10.208.54.69", 22, "10.208.52.1")
        public = PublicNetwork("221.7.131.221", "Nanning", "Guangxi", "CN")

        self.assertEqual(
            format_netinfo(local, public),
            "en0 10.208.54.69/22  ·  gw 10.208.52.1  ·  221.7.131.221 Nanning, CN",
        )

    def test_chinese_isp_follows_the_country_code(self):
        cases = [
            ("AS4837 CHINA UNICOM China169 Backbone", "Unicom"),
            ("CHINANET-BACKBONE", "Telecom"),
            ("China Telecom", "Telecom"),
            ("China Mobile Communications", "Mobile"),
            ("CMNET", "Mobile"),
        ]
        for org, label in cases:
            public = PublicNetwork("203.0.113.45", "Nanning", "", "CN", org)
            self.assertEqual(
                format_netinfo(None, public),
                f"203.0.113.45 Nanning, CN {label}",
            )

    def test_unknown_chinese_isp_keeps_the_country_code(self):
        public = PublicNetwork("203.0.113.45", "Nanning", "", "CN", "Example Cloud")
        self.assertEqual(format_netinfo(None, public), "203.0.113.45 Nanning, CN")

    def test_non_chinese_address_keeps_the_country_code(self):
        public = PublicNetwork("203.0.113.45", "Tokyo", "", "JP", "China Telecom")
        self.assertEqual(format_netinfo(None, public), "203.0.113.45 Tokyo, JP")

    def test_drops_the_country_and_missing_parts(self):
        local = LocalNetwork("en0", "10.0.0.2", 24, "")
        public = PublicNetwork("203.0.113.9", "Shanghai", "", "CN")

        self.assertEqual(
            format_netinfo(local, public, include_country=False),
            "en0 10.0.0.2/24  ·  203.0.113.9 Shanghai",
        )
        self.assertEqual(format_netinfo(local, None), "en0 10.0.0.2/24")
        self.assertEqual(format_netinfo(None, public), "203.0.113.9 Shanghai, CN")


class ServiceTests(unittest.TestCase):
    def test_public_lookup_is_cached_and_refreshed_after_the_ttl(self):
        now = [100.0]
        calls = []

        def fetch():
            calls.append(1)
            return PublicNetwork("1.2.3.4", "City")

        service = NetworkInfoService(
            clock=lambda: now[0],
            local_reader=lambda: None,
            public_fetcher=fetch,
        )
        service.refresh_public()
        service.wait(2)

        self.assertEqual(service.snapshot()[1].address, "1.2.3.4")
        self.assertEqual(len(calls), 1)

        now[0] = 200.0
        service.refresh_public()
        service.wait(2)
        self.assertEqual(len(calls), 1)

        now[0] = 800.0
        service.refresh_public()
        service.wait(2)
        self.assertEqual(len(calls), 2)

    def test_failure_is_retried_later_without_raising(self):
        service = NetworkInfoService(
            clock=lambda: 100.0,
            local_reader=lambda: None,
            public_fetcher=lambda: None,
        )
        service.refresh_public()
        service.wait(2)

        self.assertIsNone(service.snapshot()[1])

    def test_public_lookup_does_not_block_the_caller(self):
        started = threading.Event()
        release = threading.Event()

        def fetch():
            started.set()
            release.wait(2)
            return None

        service = NetworkInfoService(
            clock=lambda: 100.0,
            local_reader=lambda: None,
            public_fetcher=fetch,
        )

        before = time.monotonic()
        service.refresh_public()
        elapsed = time.monotonic() - before

        self.assertLess(elapsed, 0.2)
        self.assertTrue(started.wait(1.0))
        release.set()
        service.wait(2.0)

    def test_public_lookup_can_be_disabled(self):
        service = NetworkInfoService(
            public=False,
            local_reader=lambda: None,
            public_fetcher=lambda: (_ for _ in ()).throw(AssertionError("called")),
        )
        service.refresh_public()
        service.wait(1)

        self.assertIsNone(service.snapshot()[1])

    def test_local_refresh_respects_its_ttl(self):
        now = [0.0]
        calls = []

        def read():
            calls.append(1)
            return LocalNetwork("en0", "10.0.0.2", 24, "10.0.0.1")

        service = NetworkInfoService(clock=lambda: now[0], local_reader=read, public=False)
        service.refresh_local()
        service.refresh_local()
        self.assertEqual(len(calls), 1)

        now[0] = 31.0
        service.refresh_local()
        self.assertEqual(len(calls), 2)
        self.assertEqual(service.snapshot()[0].cidr, "10.0.0.2/24")


if __name__ == "__main__":
    unittest.main()

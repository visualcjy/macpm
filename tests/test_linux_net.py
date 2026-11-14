import shutil
import tempfile
import unittest

from macpm.linux_backend.net import (
    LinuxNetworkSampler,
    _decode_address,
    parse_proc_net,
    parse_ss_traffic,
)
from macpm.linux_backend.proc import socket_owners
from macpm.linux_backend.sysfs import SysRoot

from tests import linux_fixtures


class AddressTests(unittest.TestCase):
    def test_ipv4_is_little_endian_hex(self):
        self.assertEqual(_decode_address("0100007F:1F90", "4"), ("127.0.0.1", 8080))
        self.assertEqual(_decode_address("00000000:0035", "4"), ("0.0.0.0", 53))

    def test_ipv6_words_are_little_endian(self):
        address, port = _decode_address(
            "00000000000000000000000001000000:0050", "6"
        )

        self.assertEqual(address, "::1")
        self.assertEqual(port, 80)


class ProcNetTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-net-")
        linux_fixtures.build(self.root, "hybrid")
        self.sys = SysRoot(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_tcp_rows(self):
        rows = parse_proc_net(self.sys.read("/proc/net/tcp"), "tcp", "4")

        self.assertEqual(len(rows), 2)
        listen, established = rows[0][0], rows[1][0]
        self.assertEqual(listen.local, "127.0.0.1:8080")
        self.assertEqual(listen.state, "Listen")
        self.assertTrue(listen.is_listen)
        self.assertEqual(established.state, "Established")
        self.assertEqual(established.remote, "127.0.0.1:80")
        self.assertEqual(rows[0][1], 11111)
        self.assertEqual(rows[1][1], 22222)

    def test_udp_rows_have_no_state(self):
        rows = parse_proc_net(self.sys.read("/proc/net/udp"), "udp", "4")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].state, "")
        self.assertEqual(rows[0][0].local, "0.0.0.0:53")

    def test_socket_owners_map_inodes_to_processes(self):
        owners = socket_owners(self.sys)

        self.assertEqual(owners[11111], (4242, "nginx"))
        self.assertEqual(owners[22222], (4242, "nginx"))
        self.assertEqual(owners[44444], (4343, "systemd-resolve"))


class SsTests(unittest.TestCase):
    def test_bytes_are_attributed_to_inodes(self):
        traffic = parse_ss_traffic(linux_fixtures.SS_OUTPUT)

        self.assertEqual(traffic[22222], (1200, 500))
        self.assertEqual(traffic[55555], (4000, 900))

    def test_empty_output(self):
        self.assertEqual(parse_ss_traffic(""), {})


class SamplerTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-net-")
        linux_fixtures.build(self.root, "hybrid")
        self.now = 100.0

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def make_sampler(self, texts):
        samples = iter(texts)
        return LinuxNetworkSampler(
            sysroot=SysRoot(self.root),
            clock=lambda: self.now,
            runner=lambda: next(samples),
        )

    def test_full_sample_counts_states_and_listeners(self):
        sampler = self.make_sampler([linux_fixtures.SS_OUTPUT])

        self.assertTrue(sampler.sample_full())
        self.assertEqual(sampler.tcp_count, 3)
        self.assertEqual(sampler.udp_count, 1)
        self.assertEqual(sampler.states["LISTEN"], 2)
        self.assertEqual(sampler.states["ESTABLISHED"], 1)
        self.assertEqual(len(sampler.listeners), 2)
        self.assertIsNone(sampler.error)

    def test_ownership_is_attached_to_every_socket(self):
        sampler = self.make_sampler([linux_fixtures.SS_OUTPUT])
        sampler.sample_full()

        owned = [row for row in sampler.sockets if row.pid == 4242]
        self.assertEqual(len(owned), 2)
        self.assertTrue(all(row.process == "nginx" for row in owned))

    def test_per_process_rates_come_from_ss_counters(self):
        second = linux_fixtures.SS_OUTPUT.replace(
            "bytes_acked:500 bytes_received:1200",
            "bytes_acked:900 bytes_received:2200",
        )
        sampler = self.make_sampler([linux_fixtures.SS_OUTPUT, second])
        sampler.sample_full()

        self.now = 102.0
        self.assertTrue(sampler.sample_fast())
        nginx = sampler.processes[(4242, "nginx")]

        self.assertEqual(nginx.conns, 2)
        self.assertAlmostEqual(nginx.rx_bps, 500.0)
        self.assertAlmostEqual(nginx.tx_bps, 200.0)

    def test_missing_ss_keeps_the_socket_snapshot(self):
        sampler = self.make_sampler([linux_fixtures.SS_OUTPUT, None])
        sampler.sample_full()

        self.assertFalse(sampler.sample_fast())
        self.assertIsNotNone(sampler.error)
        self.assertEqual(sampler.tcp_count, 3)
        self.assertEqual(len(sampler.listeners), 2)

    def test_no_proc_net_is_an_error(self):
        linux_fixtures.build(self.root, "empty")
        sampler = self.make_sampler([linux_fixtures.SS_OUTPUT])

        self.assertFalse(sampler.sample_full())
        self.assertIsNotNone(sampler.error)


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import types
import unittest

from macpm.netinfo import LocalNetwork, PublicNetwork
from macpm.network import (
    NetworkSampler,
    NetworkView,
    ProcessTraffic,
    SocketRow,
    _display_addr,
    _format_rate,
    _parse_nettop,
    _port_of,
    _state_label,
)
from macpm.tui import _text_width

HEADER = (
    "time,,interface,state,bytes_in,bytes_out,rx_dupe,rx_ooo,re-tx,"
    "rtt_avg,rcvsize,tx_win,tc_class,tc_mgt,cc_algo,P,C,R,W,arch,"
)


def row(label, iface="", state="", rx="", tx=""):
    fields = ["11:00:00.000000", label, iface, state, rx, tx] + [""] * 14
    return ",".join(fields) + ","


FULL_CSV = "\n".join(
    [
        HEADER,
        row("launchd.1"),
        row("tcp4 127.0.0.1:8021<->*:*", "lo0", "Listen"),
        row("tcp6 ::1.8021<->*:*", "lo0", "Listen"),
        row("Google Chrome H.925", rx="5482", tx="27154"),
        row(
            "tcp4 10.0.0.2:53206<->17.57.145.152:5223",
            "en0",
            "Established",
            "5482",
            "26352",
        ),
        row("udp4 *:52707<->*:*", "lo0", "", "0", "802"),
        row("mDNSResponder.711", rx="100", tx="200"),
        row("udp4 *:5353<->*:*", "en0", "", "100", "200"),
    ]
)

FAST_CSV = "\n".join(
    [
        HEADER,
        row("launchd.1"),
        row("Google Chrome H.925", rx="7482", tx="28154"),
        row("mDNSResponder.711", rx="300", tx="400"),
    ]
)


def make_sampler(texts, clock):
    """A sampler whose nettop output comes from a list of text samples."""
    samples = iter(texts)
    return NetworkSampler(clock=clock, runner=lambda args: next(samples))


class ParsingTests(unittest.TestCase):
    def test_sockets_and_process_counters(self):
        sockets, processes = _parse_nettop(FULL_CSV)

        self.assertEqual(len(sockets), 5)
        self.assertEqual(len(processes), 3)
        self.assertEqual(processes[(925, "Google Chrome H")], (5482, 27154))
        self.assertEqual(processes[(711, "mDNSResponder")], (100, 200))

    def test_connection_rows_are_attached_to_their_process(self):
        sockets, _ = _parse_nettop(FULL_CSV)

        chrome = [socket for socket in sockets if socket.pid == 925]
        self.assertEqual(len(chrome), 2)
        self.assertEqual({socket.proto for socket in chrome}, {"tcp", "udp"})
        self.assertEqual(chrome[0].state, "Established")
        self.assertEqual(chrome[0].remote, "17.57.145.152:5223")
        self.assertEqual(chrome[1].local, "*:52707")

    def test_udp_row_with_empty_state_is_not_a_process_row(self):
        sockets, processes = _parse_nettop(FULL_CSV)

        udp = [
            socket
            for socket in sockets
            if socket.proto == "udp" and socket.pid == 711
        ]
        self.assertEqual(len(udp), 1)
        self.assertEqual(udp[0].state, "")
        self.assertEqual(len(processes), 3)

    def test_socket_before_any_process_row_has_no_pid(self):
        text = "\n".join(
            [HEADER, row("tcp4 127.0.0.1:9<->*:*", "lo0", "Listen")]
        )
        sockets, _ = _parse_nettop(text)

        self.assertEqual(len(sockets), 1)
        self.assertIsNone(sockets[0].pid)
        self.assertEqual(sockets[0].process, "")

    def test_missing_or_broken_header_is_ignored(self):
        self.assertEqual(_parse_nettop(""), ([], {}))
        self.assertEqual(_parse_nettop("garbage\n1,2,3\n"), ([], {}))

    def test_state_labels(self):
        self.assertEqual(_state_label("Established"), "ESTABLISHED")
        self.assertEqual(_state_label("CloseWait"), "CLOSE_WAIT")
        self.assertEqual(_state_label("TimeWait"), "TIME_WAIT")
        self.assertEqual(_state_label("Weird"), "WEIRD")

    def test_address_display(self):
        self.assertEqual(_display_addr("127.0.0.1:8021"), "127.0.0.1:8021")
        self.assertEqual(_display_addr("::1.8021"), "[::1]:8021")
        self.assertEqual(_display_addr("*.60358"), "*:60358")
        self.assertEqual(_display_addr("0.0.0.0:8080"), "*:8080")
        self.assertEqual(_display_addr("*:*"), "*:*")
        self.assertEqual(_port_of("::1.8021"), 8021)
        self.assertEqual(_port_of("*:7000"), 7000)

    def test_rate_format(self):
        self.assertEqual(_format_rate(0), "0")
        self.assertEqual(_format_rate(500), "500B")
        self.assertEqual(_format_rate(1024), "1.0K")
        self.assertEqual(_format_rate(10 * 1024), "10K")
        self.assertEqual(_format_rate(1.5 * 1024**2), "1.5M")


class SamplerTests(unittest.TestCase):
    def test_full_sample_counts_states_and_listeners(self):
        sampler = make_sampler([FULL_CSV], lambda: 100.0)

        self.assertTrue(sampler.sample_full())
        self.assertEqual(sampler.tcp_count, 3)
        self.assertEqual(sampler.udp_count, 2)
        self.assertEqual(sampler.states["LISTEN"], 2)
        self.assertEqual(sampler.states["ESTABLISHED"], 1)
        self.assertEqual(len(sampler.listeners), 2)
        self.assertEqual(len(sampler.processes), 3)
        self.assertEqual(sampler.processes[(925, "Google Chrome H")].conns, 2)

    def test_fast_sample_keeps_socket_data_and_adds_rates(self):
        now = [100.0]
        sampler = make_sampler([FULL_CSV, FAST_CSV], lambda: now[0])
        sampler.sample_full()

        now[0] = 102.0
        self.assertTrue(sampler.sample_fast())
        chrome = sampler.processes[(925, "Google Chrome H")]

        self.assertEqual(sampler.tcp_count, 3)
        self.assertEqual(len(sampler.listeners), 2)
        self.assertEqual(chrome.conns, 2)
        self.assertAlmostEqual(chrome.rx_bps, 1000.0)
        self.assertAlmostEqual(chrome.tx_bps, 500.0)

    def test_rates_are_smoothed_between_samples(self):
        now = [100.0]
        text_100 = FULL_CSV
        text_102 = FULL_CSV.replace("5482,27154", "7482,29154").replace(
            "5482,26352", "7482,27154"
        )
        text_104 = text_102.replace("7482,29154", "11482,33154").replace(
            "7482,27154", "11482,29154"
        )
        sampler = make_sampler([text_100, text_102, text_104], lambda: now[0])
        sampler.sample_full()

        now[0] = 102.0
        sampler.sample_full()
        first = sampler.processes[(925, "Google Chrome H")].rx_bps
        now[0] = 104.0
        sampler.sample_full()
        second = sampler.processes[(925, "Google Chrome H")].rx_bps

        self.assertAlmostEqual(first, 1000.0)
        self.assertAlmostEqual(second, 1500.0)

    def test_counter_reset_does_not_report_a_negative_rate(self):
        now = [100.0]
        lower = FULL_CSV.replace("5482,27154", "500,100").replace(
            "5482,26352", "500,100"
        )
        sampler = make_sampler([FULL_CSV, lower], lambda: now[0])
        sampler.sample_full()

        now[0] = 102.0
        sampler.sample_full()
        chrome = sampler.processes[(925, "Google Chrome H")]

        self.assertEqual(chrome.rx_bps, 0.0)
        self.assertEqual(chrome.tx_bps, 0.0)

    def test_nettop_failure_keeps_the_last_snapshot(self):
        sampler = make_sampler([FULL_CSV, None], lambda: 100.0)
        sampler.sample_full()
        self.assertFalse(sampler.sample_full())

        self.assertIsNotNone(sampler.error)
        self.assertEqual(len(sampler.listeners), 2)
        self.assertEqual(sampler.tcp_count, 3)


def fake_netinfo(local=None, public=None):
    return types.SimpleNamespace(
        snapshot=lambda: (local, public),
        refresh_local=lambda *args, **kwargs: None,
        refresh_public=lambda *args, **kwargs: None,
    )


def make_view():
    sampler = types.SimpleNamespace(
        processes={},
        listeners=[],
        states={"LISTEN": 2, "ESTABLISHED": 3},
        tcp_count=5,
        udp_count=2,
        error=None,
        reset=lambda: None,
        sample_full=lambda: True,
    )
    view = NetworkView(sampler=sampler, netinfo=fake_netinfo())
    view._network = {"ibyte_rate": 1024, "obyte_rate": 2048}
    return view


class FakeTerminal:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.normal = ""

    def color(self, value):
        return ""

    def move(self, row, column):
        return f"\x1b[{row + 1};{column + 1}H"


class ViewTests(unittest.TestCase):
    def test_layout_stacks_on_an_80x24_terminal(self):
        view = make_view()

        traffic, listeners = view._layout(80, 24)

        self.assertEqual(traffic, (5, 0, 80, 9))
        self.assertEqual(listeners, (14, 0, 80, 9))
        self.assertEqual(traffic[3] + listeners[3], 18)

    def test_layout_uses_two_columns_on_a_wide_terminal(self):
        view = make_view()

        traffic, listeners = view._layout(120, 30)

        self.assertEqual(listeners[1], 0)
        self.assertEqual(traffic[1], listeners[2])
        self.assertEqual(traffic[2] + listeners[2], 120)
        self.assertEqual(traffic[3], 24)
        self.assertEqual(listeners[0], 5)

    def test_layout_drops_listeners_on_narrow_or_short_terminals(self):
        view = make_view()

        self.assertIsNone(view._layout(50, 24)[1])
        self.assertIsNone(view._layout(80, 12)[1])
        self.assertIsNone(view._layout(80, 8))

    def test_traffic_columns_fill_the_panel(self):
        view = make_view()

        for inner in (76, 60, 40):
            pid_w, process_w, conn_w, rate_w = view._traffic_widths(inner)
            total = pid_w + process_w + 2 * rate_w + (4 if conn_w else 3)
            if conn_w:
                total += conn_w
            self.assertEqual(total, inner)
            self.assertGreaterEqual(process_w, 4)

    def test_listener_columns_fill_the_panel(self):
        view = make_view()

        for inner in (76, 60, 40):
            addr_w, pid_w, process_w = view._listener_widths(inner)
            self.assertEqual(addr_w + pid_w + process_w + 2, inner)

    def test_panes_are_exactly_as_wide_as_requested(self):
        view = make_view()

        lines = view._pane(
            80,
            10,
            "traffic",
            True,
            "PID  PROCESS",
            ["1  launchd"],
            0,
            right_title=" 1-2/3 ",
        )

        self.assertEqual(len(lines), 10)
        for line in lines:
            self.assertEqual(_text_width(line), 80)

    def test_traffic_sort_cycle_and_order(self):
        view = make_view()
        view.sampler.processes = {
            (1, "a"): ProcessTraffic(1, "a", rx_bps=10, tx_bps=5),
            (2, "b"): ProcessTraffic(2, "b", rx_bps=1, tx_bps=1),
        }
        view.traffic_sort = "rx"
        view.traffic_desc = True

        self.assertEqual([row.pid for row in view._traffic_rows()], [1, 2])

        view.cycle_sort()
        self.assertEqual(view.traffic_sort, "tx")

    def test_listener_sort_by_port(self):
        view = make_view()
        view.sampler.listeners = [
            SocketRow("tcp", "4", "*:7000", "*:*", "Listen", "lo0", 1, "a"),
            SocketRow("tcp", "4", "*:5000", "*:*", "Listen", "lo0", 1, "a"),
        ]

        self.assertEqual([_port_of(row.local) for row in view._listener_rows()], [5000, 7000])

    def test_scroll_is_clamped_to_the_visible_rows(self):
        view = make_view()
        view.panes = (0, 1)
        view.bodies = {0: 5, 1: 5}
        view.offsets = {0: 0, 1: 0}
        view.sampler.processes = {
            (pid, f"p{pid}"): ProcessTraffic(pid, f"p{pid}", rx_bps=1)
            for pid in range(20)
        }

        view.scroll(100)
        self.assertEqual(view.offsets[0], 15)

        view.scroll(-100)
        self.assertEqual(view.offsets[0], 0)

    def test_focus_cycles_between_visible_panes(self):
        view = make_view()
        view.panes = (0, 1)
        view.focus = 0

        view.cycle_focus()
        self.assertEqual(view.focus, 1)
        view.cycle_focus()
        self.assertEqual(view.focus, 0)

        view.panes = (0,)
        view.cycle_focus()
        self.assertEqual(view.focus, 0)

    def test_display_renders_both_panes(self):
        view = make_view()
        view.ready = True
        view.sampler.processes = {
            (1, "launchd"): ProcessTraffic(1, "launchd", 2, 100, 200, 10, 20)
        }
        view.sampler.listeners = [
            SocketRow("tcp", "4", "*:7000", "*:*", "Listen", "lo0", 1, "launchd")
        ]
        view._term = FakeTerminal(80, 24)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display({"network": {"ibyte_rate": 1024, "obyte_rate": 2048}})
        output = buffer.getvalue()

        self.assertIn("Network", output)
        self.assertIn("traffic by process 1", output)
        self.assertIn("listening 1", output)
        self.assertIn("launchd", output)

    def test_netinfo_is_shown_in_the_header(self):
        view = make_view()
        view.netinfo = fake_netinfo(
            LocalNetwork("en0", "192.168.1.23", 24, "192.168.1.1"),
            PublicNetwork("203.0.113.45", "Shanghai", "", "CN"),
        )
        view.ready = True
        view._term = FakeTerminal(120, 24)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display()
        output = buffer.getvalue()

        self.assertIn("192.168.1.23/24", output)
        self.assertIn("gw 192.168.1.1", output)
        self.assertIn("203.0.113.45", output)
        self.assertIn("Shanghai, CN", output)

    def test_netinfo_keeps_the_local_address_on_a_narrow_terminal(self):
        view = make_view()
        view.netinfo = fake_netinfo(
            LocalNetwork("en0", "192.168.1.23", 24, "192.168.1.1"),
            PublicNetwork("203.0.113.45", "Shanghai", "", "CN"),
        )
        view.ready = True
        view._term = FakeTerminal(80, 24)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display()
        output = buffer.getvalue()

        self.assertIn("192.168.1.23/24", output)
        self.assertIn("203.0.113.45", output)

    def test_display_hides_listeners_when_narrow(self):
        view = make_view()
        view.ready = True
        view._term = FakeTerminal(50, 24)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            view.display()
        output = buffer.getvalue()

        self.assertIn("traffic by process", output)
        self.assertNotIn("listening", output)

    def test_summary_columns_keep_their_width(self):
        view = make_view()
        view.sampler.tcp_count = 5
        view.sampler.udp_count = 2
        view.sampler.states = {"ESTABLISHED": 3, "LISTEN": 2}
        view.sampler.processes = {
            (pid, f"p{pid}"): ProcessTraffic(pid, f"p{pid}") for pid in range(9)
        }
        first = view._summary_lines()

        view.sampler.tcp_count = 1106
        view.sampler.udp_count = 70
        view.sampler.states = {
            "ESTABLISHED": 49,
            "LISTEN": 27,
            "CLOSE_WAIT": 230,
            "TIME_WAIT": 3,
        }
        view.sampler.processes = {
            (pid, f"p{pid}"): ProcessTraffic(pid, f"p{pid}") for pid in range(238)
        }
        second = view._summary_lines()

        self.assertEqual(len(first[0]), len(second[0]))
        self.assertEqual(len(first[1]), len(second[1]))
        self.assertEqual(first[1].index("processes"), second[1].index("processes"))
        self.assertEqual(first[1].split("processes")[1].strip(), "9")

    def test_summary_line_has_counts_and_rates(self):
        view = make_view()

        first, second = view._summary_lines()

        self.assertIn("TCP    5", first)
        self.assertIn("LISTEN    2", first)
        self.assertIn("UDP    2", first)
        self.assertIn("1.0K/s", second)
        self.assertIn("2.0K/s", second)

    def test_traffic_text_fits_the_columns(self):
        view = make_view()
        widths = view._traffic_widths(76)
        text = view._traffic_text(
            ProcessTraffic(123, "Google Chrome Helper", 4, 1, 2, 1024, 2048),
            widths,
        )

        self.assertEqual(_text_width(text), 76)
        self.assertIn("1.0K", text)

    def test_listener_text_fits_the_columns(self):
        view = make_view()
        widths = view._listener_widths(76)
        socket = SocketRow("tcp", "6", "::1.8021", "*:*", "Listen", "lo0", 1, "launchd")
        text = view._listener_text(socket, widths)

        self.assertEqual(_text_width(text), 76)
        self.assertIn("[::1]:8021", text)


if __name__ == "__main__":
    unittest.main()

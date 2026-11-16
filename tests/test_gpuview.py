import contextlib
import io
import sys
import types
import unittest

from macpm.capabilities import Capabilities, MACOS_CAPABILITIES
from macpm.gpuview import GpuDetails, GpuView, build_cards, build_details
from macpm.processes import ProcessRow


def fake_terminal(width=120, height=30):
    terminal = types.SimpleNamespace()
    terminal.width = width
    terminal.height = height
    terminal.color = lambda value: ""
    terminal.move = lambda row, column: f"\x1b[{row + 1};{column + 1}H"
    return terminal


def linux_caps(**overrides):
    values = dict(platform="linux", gpu_util=True, gpu_freq=True, gpu_power=True)
    values.update(overrides)
    return Capabilities(**values)


def card(index=0, busy=37.0, freq=1200, power=12.0, used=2048.0, total=8192.0, temp=43.0,
         name="AMD GPU (amdgpu)", driver="amdgpu"):
    return {
        "index": index,
        "freq_hz": freq,
        "max_freq_mhz": None,
        "idle_ratio": 1.0 - busy / 100.0,
        "busy_pct": busy,
        "name": name,
        "driver": driver,
        "power_w": power,
        "mem_used_mb": used,
        "mem_total_mb": total,
        "temp_c": temp,
    }


def sample(cards):
    return {
        "timestamp": 1.0,
        "thermal_pressure": "Nominal",
        "processor": {"clusters": [], "gpu_energy": 0,
                      "gpu_energy_available": True, "combined_power": 0},
        "gpu": dict(cards[0], count=len(cards)),
        "gpus": cards,
    }


SINGLE_SAMPLE = sample([card()])
TWO_CARD_SAMPLE = sample([card(0, 37.0, 1200), card(1, 12.0, 900, used=1024.0, temp=41.0)])

MAC_SAMPLE = {
    "timestamp": 1.0,
    "thermal_pressure": "Nominal",
    "processor": {"clusters": [], "gpu_energy": 5000,
                  "gpu_energy_available": True, "combined_power": 0},
    "gpu": {"freq_hz": 1350, "idle_ratio": 0.4, "name": "", "driver": ""},
}


class DetailsTests(unittest.TestCase):
    def test_linux_sample(self):
        details = build_details(SINGLE_SAMPLE, linux_caps())

        self.assertEqual(details.name, "AMD GPU (amdgpu)")
        self.assertEqual(details.busy_pct, 37.0)
        self.assertEqual(details.mem_used_mb, 2048.0)
        self.assertEqual(details.temp_c, 43.0)
        self.assertAlmostEqual(details.mem_pct, 25.0)

    def test_macos_sample_derives_busy_and_power(self):
        sensors = types.SimpleNamespace(gpu_c=52.5)
        details = build_details(MAC_SAMPLE, Capabilities(), interval=1.0, sensors=sensors)

        self.assertEqual(details.name, "Apple GPU")
        self.assertAlmostEqual(details.busy_pct, 60.0)
        self.assertAlmostEqual(details.power_w, 5.0)
        self.assertEqual(details.temp_c, 52.5)

    def test_unavailable_power_stays_na(self):
        details = build_details(
            {"processor": {"gpu_energy": 0, "gpu_energy_available": False},
             "gpu": {"freq_hz": 0, "idle_ratio": 1.0}},
            linux_caps(),
        )

        self.assertIsNone(details.power_w)


class BuildCardsTests(unittest.TestCase):
    def test_every_card_is_built_in_order(self):
        cards = build_cards(TWO_CARD_SAMPLE, linux_caps())

        self.assertEqual([item.index for item in cards], [0, 1])
        self.assertEqual([item.busy_pct for item in cards], [37.0, 12.0])

    def test_single_gpu_sample_still_produces_one_card(self):
        cards = build_cards(MAC_SAMPLE, Capabilities())

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].name, "Apple GPU")

    def test_empty_sample_produces_one_placeholder_card(self):
        cards = build_cards({}, linux_caps())

        self.assertEqual(len(cards), 1)
        self.assertIsNone(cards[0].busy_pct)


def make_view(rows, caps=None, ready=True, gpu_available=True, width=120, height=30,
              proc_sort="pid"):
    view = GpuView.__new__(GpuView)
    view.color = 2
    view.caps = caps or linux_caps()
    view.rows = rows
    view.cards = []
    view._sorted = None
    view.sort = "gpu"
    view.desc = True
    view.proc_sort = proc_sort
    view.offset = 0
    view.ready = ready
    view.paused = False
    view._term = fake_terminal(width, height)
    view._body_height = 0
    view._content_height = 0
    view._lines_total = 0
    view.sampler = types.SimpleNamespace(gpu_available=gpu_available)
    return view


def render(view, sample_data=SINGLE_SAMPLE, sensors=None):
    buffer = io.StringIO()
    args = types.SimpleNamespace(interval=1)
    with contextlib.redirect_stdout(buffer):
        view.display(sample_data, args, sensors)
    return buffer.getvalue()


class SingleCardTests(unittest.TestCase):
    """One GPU keeps the two-box layout: card row + process table."""

    def test_two_boxes_and_process_columns(self):
        rows = [ProcessRow(111, "blender", "alice", 0.0, 0, 42.5),
                ProcessRow(222, "Xorg", "root", 0.0, 0, 3.0)]
        output = render(make_view(rows))

        self.assertIn("GPUs (1)", output)
        self.assertIn("processes by GPU 2", output)
        for needle in ("PID", "NAME", "USER", "CARD", "GPU%", "MEM"):
            self.assertIn(needle, output)
        self.assertIn("blender", output)
        self.assertIn("alice", output)
        self.assertIn("42.5", output)

    def test_unknown_utilisation_renders_a_dash(self):
        rows = [ProcessRow(4242, "VLLM::Worker_TP0", "root", 0.0, 0, 0.0,
                           gpu_card="0", gpu_mem_mb=29180.0, util_known=False)]
        output = render(make_view(rows))

        self.assertIn("—", output)
        self.assertIn("28G", output)

    def test_processes_are_sorted_by_gpu_time(self):
        rows = [ProcessRow(1, "idle", "u", 0.0, 0, 0.0),
                ProcessRow(2, "small", "u", 0.0, 0, 1.0),
                ProcessRow(3, "busy", "u", 0.0, 0, 90.0)]
        view = make_view(rows)

        self.assertEqual([row.name for row in view._visible_rows()], ["busy", "small"])

    def test_sort_cycle_in_single_mode(self):
        view = make_view([])

        seen = []
        for _ in range(4):
            seen.append((view.sort, view.desc))
            view.cycle_sort()

        self.assertEqual(seen, [("gpu", True), ("pid", False),
                                ("name", False), ("user", False)])

    def test_no_gpu_machine(self):
        caps = linux_caps(gpu_util=False, gpu_freq=False, gpu_power=False, proc_gpu=False)
        view = make_view([], caps=caps, ready=False, gpu_available=False)

        output = render(view, sample_data={"processor": {}, "gpu": {}})

        self.assertIn("GPUs (0)", output)
        self.assertIn("no GPU detected", output)

    def test_macos_unified_memory_cell(self):
        view = make_view([], caps=Capabilities())
        sensors = types.SimpleNamespace(gpu_c=52.5)

        output = render(view, sample_data=MAC_SAMPLE, sensors=sensors)

        self.assertIn("unified", output)
        self.assertIn("52°C", output)

    def test_tiny_terminal_draws_nothing(self):
        self.assertEqual(render(make_view([], width=30, height=8)), "")


class MultiCardTests(unittest.TestCase):
    """Several GPUs: one table, two lines per card (metrics + processes)."""

    def test_single_table_with_nested_processes(self):
        rows = [
            ProcessRow(111, "worker-a", "root", 0.0, 0, 0.0,
                       gpu_card="0", gpu_mem_mb=2048.0, util_known=False),
            ProcessRow(222, "worker-b", "root", 0.0, 0, 0.0,
                       gpu_card="1", gpu_mem_mb=1024.0, util_known=False),
        ]
        output = render(make_view(rows), sample_data=TWO_CARD_SAMPLE)

        self.assertIn("GPUs (2)", output)
        self.assertNotIn("processes by GPU", output)
        self.assertIn("└ 111 worker-a", output)
        self.assertIn("└ 222 worker-b", output)

    def test_multi_card_process_appears_under_each_card(self):
        rows = [ProcessRow(7, "EngineCore", "root", 0.0, 0, 0.0,
                           gpu_card="0-1", gpu_mem_mb=4096.0, util_known=False,
                           gpu_list=[0, 1])]
        output = render(make_view(rows), sample_data=TWO_CARD_SAMPLE)

        self.assertEqual(output.count("└ 7 EngineCore"), 2)
        self.assertIn("[0-1]", output)

    def test_overflow_processes_are_summarised(self):
        rows = [
            ProcessRow(100 + index, f"worker-{index}", "root", 0.0, 0, 0.0,
                       gpu_card="0", gpu_mem_mb=1024.0, util_known=False)
            for index in range(10)
        ]
        output = render(make_view(rows), sample_data=TWO_CARD_SAMPLE)

        self.assertIn("+5", output)

    def test_two_lines_per_card_with_scrolling(self):
        cards = [card(index=index) for index in range(6)]
        rows = [
            ProcessRow(index + 1, f"worker-{index}", "root", 0.0, 0, 0.0,
                       gpu_card=str(index), gpu_mem_mb=1024.0, util_known=False)
            for index in range(6)
        ]
        view = make_view(rows, height=12)

        output = render(view, sample_data=sample(cards))

        # 6 cards x 2 lines = 12 body lines, 9 per page.
        self.assertIn("1-9/12", output)
        self.assertIn("Page 1/2", output)

        view.scroll(1)  # a whole page
        second = render(view, sample_data=sample(cards))
        self.assertIn("10-12/12", second)
        self.assertIn("Page 2/2", second)

    def test_columns_are_shared_across_both_lines(self):
        rows = [ProcessRow(111, "worker-a", "root", 0.0, 0, 0.0,
                           gpu_card="0", gpu_mem_mb=2048.0, util_known=False)]
        output = render(make_view(rows), sample_data=TWO_CARD_SAMPLE)

        self.assertIn("BUSY", output)
        self.assertIn("TEMP", output)
        self.assertIn("AMD GPU (amdgpu)", output)

    def test_sort_cycle_switches_process_order(self):
        view = make_view([], proc_sort="pid")
        view.cards = [object(), object()]

        seen = []
        for _ in range(3):
            seen.append(view.proc_sort)
            view.cycle_sort()

        self.assertEqual(seen, ["pid", "name", "mem"])


class InfoBoxTests(unittest.TestCase):
    def test_vendor_identity_is_shown_above_the_cards(self):
        view = make_view([], height=20)
        view._info_source = lambda: {
            "name": "KS58 OCTA-2", "count": 64, "cards": 8, "dies": 8,
            "driver": "2.3.26", "pcie_gen": 4, "pcie_width": 4,
            "vbios": "[N/A]", "vendor": "dlsmi",
        }

        output = render(view, sample_data=TWO_CARD_SAMPLE)

        self.assertIn("GPU INFO", output)
        self.assertIn("KS58 OCTA-2", output)
        self.assertIn("64 devices", output)
        self.assertIn("8 cards", output)
        self.assertIn("8 dies/card", output)
        self.assertIn("driver 2.3.26", output)
        self.assertIn("PCIe 4 x4", output)
        self.assertNotIn("VBIOS", output)

    def test_macos_info_mentions_unified_memory(self):
        view = make_view([], caps=Capabilities(), height=20)
        view._info_source = False

        output = render(view, sample_data=MAC_SAMPLE)

        self.assertIn("GPU INFO", output)
        self.assertIn("unified memory", output)

    def test_no_info_box_in_a_short_terminal(self):
        view = make_view([], height=12)

        output = render(view, sample_data=TWO_CARD_SAMPLE)

        self.assertNotIn("GPU INFO", output)


class MemoryBarTests(unittest.TestCase):
    def test_vram_cell_has_a_bar_and_numbers(self):
        from macpm.gpuview import build_cards

        card_data = build_cards(TWO_CARD_SAMPLE, linux_caps())[0]
        view = make_view([])
        cell = view._card_cells(card_data)["vram"]

        self.assertTrue(cell.startswith("▉") or cell.startswith("▏"))
        self.assertIn("2.0G/8.0G", cell)


class VendorSamplerTests(unittest.TestCase):
    def test_sampler_forwards_model_and_cards(self):
        from macpm.gpuview import _VendorProcessSampler

        records = [
            {"pid": 7, "name": "worker", "gpus": [0, 1], "card": "0-1",
             "mem_mb": 100.0, "util_pct": None, "model": "Qwen3-32B"},
        ]
        sampler = _VendorProcessSampler(lambda: records)

        rows = sampler.sample()

        self.assertEqual(rows[0].model, "Qwen3-32B")
        self.assertEqual(rows[0].gpu_list, [0, 1])
        self.assertEqual(rows[0].gpu_card, "0-1")


class ModelNameTests(unittest.TestCase):
    def rows(self):
        return [ProcessRow(111, "worker-a", "root", 0.0, 0, 0.0, gpu_card="0",
                           gpu_mem_mb=2048.0, util_known=False,
                           model="Qwen3.6-35B-A3B")]

    def test_multi_layout_right_aligns_the_model(self):
        output = render(make_view(self.rows()), sample_data=TWO_CARD_SAMPLE)

        self.assertIn("Qwen3.6-35B-A3B │", output)
        self.assertIn("└ 111 worker-a", output)

    def test_single_layout_gains_a_model_column(self):
        output = render(make_view(self.rows()), sample_data=SINGLE_SAMPLE)

        self.assertIn("MODEL", output)
        self.assertIn("Qwen3.6-35B-A3B │", output)

    def test_column_is_hidden_without_models(self):
        rows = [ProcessRow(111, "worker-a", "root", 0.0, 0, 0.0, gpu_card="0",
                           gpu_mem_mb=2048.0, util_known=False)]
        output = render(make_view(rows), sample_data=SINGLE_SAMPLE)

        self.assertNotIn("MODEL", output)


class VramCellTests(unittest.TestCase):
    def test_vram_cells_have_equal_width_so_bars_line_up(self):
        from macpm.tui import _text_width

        view = make_view([])
        cards = build_cards(
            sample([card(used=68.0, total=32768.0), card(used=29680.0, total=32768.0)]),
            linux_caps(),
        )
        first = view._card_cells(cards[0])["vram"]
        second = view._card_cells(cards[1])["vram"]

        self.assertEqual(_text_width(first), _text_width(second))
        self.assertIn("  68M", first)


class LayoutHelpersTests(unittest.TestCase):
    def test_process_rows_are_clipped_to_the_panel(self):
        from macpm.tui import _text_width

        view = make_view([], width=80)
        columns, widths = view._process_layout(76)
        row = ProcessRow(1, "中文名称" * 10, "a-very-long-user-name", 0.0, 0, 5.0)

        self.assertEqual(_text_width(view._process_row(row, columns, widths)), 76)
        self.assertEqual(_text_width(view._process_header(columns, widths)), 76)


if __name__ == "__main__":
    unittest.main()


class CardColumnsTests(unittest.TestCase):
    def _view(self):
        view = GpuView.__new__(GpuView)
        view.caps = MACOS_CAPABILITIES
        return view

    def test_wide_table_has_utilization_columns(self):
        columns, _ = self._view()._card_layout(116)

        for key in ("mem", "enc", "dec", "pstate"):
            self.assertIn(key, columns)

    def test_multi_gpu_table_hides_encode_and_decode(self):
        columns, _ = self._view()._card_layout(116, multi=True)

        self.assertNotIn("enc", columns)
        self.assertNotIn("dec", columns)
        self.assertIn("mem", columns)
        self.assertIn("pstate", columns)

    def test_narrow_table_drops_utilization_columns_first(self):
        columns, _ = self._view()._card_layout(60)

        for key in ("mem", "enc", "dec", "pstate"):
            self.assertNotIn(key, columns)

    def test_row_uses_three_second_maximum(self):
        card = GpuDetails(index=3, name="RTX 4090", busy_pct=0, busy_recent=48,
                          pstate="P2", mem_util_pct=0, enc_pct=0, dec_pct=0)

        cells = self._view()._card_cells(card)

        self.assertIn("↑48%", cells["busy"])
        self.assertIn("▏", cells["busy"])
        self.assertEqual(cells["pstate"], "P2")


class CardLineTests(unittest.TestCase):
    def test_gpu_cards_get_title_and_mini_bar(self):
        _argv, sys.argv = sys.argv, ["macpm"]
        try:
            from macpm.macpm import DefaultView
        finally:
            sys.argv = _argv

        view = DefaultView.__new__(DefaultView)
        view._term = None
        view._processor_width = lambda width: 58
        view.gpu_cards = [
            types.SimpleNamespace(title="", lines=[]) for _ in range(2)
        ]
        cards = [
            {"index": 0, "busy_recent": 0, "pstate": "P8", "power_w": 24},
            {"index": 3, "busy_recent": 48, "pstate": "P2", "power_w": 70},
        ]

        view._update_gpu_cards({"cards": cards})

        self.assertIn("48%", view.gpu_cards[1].lines[0])
        self.assertIn("G3 P2 70W", view.gpu_cards[1].title)


# -- GPU memory comes from the GPU, never from RSS -------------------------


MAC_SAMPLE_WITH_MEMORY = dict(
    MAC_SAMPLE,
    gpu={
        "freq_hz": 1350,
        "idle_ratio": 0.4,
        "name": "Apple M3 Max",
        "gpu_cores": 30,
        "renderer_pct": 44,
        "tiler_pct": 45,
        "mem_used_mb": 5120.0,
        "mem_total_mb": 65536.0,
    },
)


class NarrowCardTests(unittest.TestCase):
    def test_memory_survives_a_narrow_table(self):
        view = make_view([])
        columns, widths = view._card_layout(74)
        card = build_cards(SINGLE_SAMPLE, linux_caps())[0]

        self.assertIn("vram", columns)
        self.assertEqual(widths["vram"], 14)
        self.assertIn("2G/8G", view._card_cells(card, widths["vram"])["vram"])

    def test_compact_cell_keeps_a_small_bar(self):
        view = make_view([])
        _, widths = view._card_layout(78)
        card = build_cards(SINGLE_SAMPLE, linux_caps())[0]

        cell = view._card_cells(card, widths["vram"])["vram"]

        self.assertTrue(cell.startswith("▉") or cell.startswith("▏"))
        self.assertIn("2G/8G", cell)

    def test_wide_table_keeps_the_ten_cell_bar(self):
        view = make_view([])
        _, widths = view._card_layout(116)
        card = build_cards(SINGLE_SAMPLE, linux_caps())[0]

        cell = view._card_cells(card, widths["vram"])["vram"]

        self.assertEqual(widths["vram"], 22)
        self.assertIn("  2.0G/8.0G", cell)


class CardNameWidthTests(unittest.TestCase):
    def test_short_name_does_not_stretch(self):
        view = make_view([])

        columns, widths = view._card_layout(116, name_cap=14)

        self.assertEqual(widths["name"], 14)
        self.assertGreater(widths["vram"], 22)

    def test_row_still_fills_the_box(self):
        from macpm.tui import _text_width

        view = make_view([])
        cards = build_cards(SINGLE_SAMPLE, linux_caps())
        columns, widths = view._card_layout(116, name_cap=14)

        row = view._card_row(cards[0], columns, widths)

        self.assertEqual(_text_width(row), 116)

    def test_no_cap_keeps_the_old_flexible_name(self):
        view = make_view([])

        _, widths = view._card_layout(116)

        self.assertGreater(widths["name"], 20)


class CardsTitleTests(unittest.TestCase):
    def test_single_card_title_has_no_power_total(self):
        view = make_view([])
        cards = build_cards(sample([card()]), linux_caps())

        line = view._cards_box(cards, 120, 1)[0]

        self.assertIn("GPUs (1)", line)
        self.assertNotIn("12.0W", line)

    def test_multi_card_title_keeps_the_total(self):
        view = make_view([])
        cards = build_cards(TWO_CARD_SAMPLE, linux_caps())

        line = view._cards_box(cards, 120, 2)[0]

        self.assertIn("24W", line)


class MacCardColumnTests(unittest.TestCase):
    def test_macos_hides_the_card_column(self):
        rows = [ProcessRow(111, "blender", "alice", 0.0, 0, 42.5, gpu_card="0")]

        output = render(make_view(rows, caps=Capabilities()), sample_data=MAC_SAMPLE)

        self.assertNotIn("CARD", output)
        self.assertIn("GPU%", output)

    def test_linux_keeps_the_card_column(self):
        rows = [ProcessRow(111, "worker", "root", 0.0, 0, 42.5, gpu_card="0")]

        output = render(make_view(rows), sample_data=SINGLE_SAMPLE)

        self.assertIn("CARD", output)


class VisibilityTests(unittest.TestCase):
    def test_zero_utilisation_with_gpu_memory_stays_visible(self):
        rows = [
            ProcessRow(1, "chrome", "u", 0.0, 0, 0.0, gpu_mem_mb=200.0),
            ProcessRow(2, "tiny", "u", 0.0, 0, 0.0, gpu_mem_mb=4.0),
        ]

        self.assertEqual([row.pid for row in make_view(rows)._visible_rows()], [1])

    def test_busy_rows_still_come_first(self):
        rows = [
            ProcessRow(1, "idle-big", "u", 0.0, 0, 0.0, gpu_mem_mb=200.0),
            ProcessRow(2, "busy", "u", 0.0, 0, 30.0),
        ]

        self.assertEqual([row.pid for row in make_view(rows)._visible_rows()], [2, 1])


class GpuMemoryCellTests(unittest.TestCase):
    def test_unknown_gpu_memory_renders_na_not_rss(self):
        row = ProcessRow(111, "blender", "alice", 0.0, 3 * 1024 ** 3, 42.5,
                         gpu_card="0")

        text = make_view([row])._process_text(row)

        self.assertIn("n/a", text)
        self.assertNotIn("3.0G", text)

    def test_shared_memory_is_a_second_number_not_a_sum(self):
        row = ProcessRow(111, "llama", "root", 0.0, 0, 90.0,
                         gpu_card="0", gpu_mem_mb=13312.0, gpu_shared_mb=1228.0)

        text = make_view([row])._process_text(row)

        self.assertIn("13G", text)
        self.assertNotIn("14G", text)

    def test_integrated_gpu_shows_its_gtt_as_gpu_memory(self):
        row = ProcessRow(111, "llama", "root", 0.0, 0, 90.0,
                         gpu_card="0", gpu_mem_mb=None, gpu_shared_mb=2048.0)

        self.assertIn("2.0G", make_view([row])._process_text(row))

    def test_macos_card_cell_uses_allocated_memory(self):
        cards = build_cards(MAC_SAMPLE_WITH_MEMORY, Capabilities())
        view = make_view([], caps=Capabilities())

        cell = view._card_cells(cards[0])["vram"]

        self.assertTrue(cell.startswith("▉") or cell.startswith("▏"))
        self.assertIn("5.0G/64.0G", cell)

    def test_macos_without_registry_data_keeps_unified(self):
        cards = build_cards(MAC_SAMPLE, Capabilities())
        view = make_view([], caps=Capabilities())

        self.assertEqual(view._card_cells(cards[0])["vram"], "unified")


class MacInfoTests(unittest.TestCase):
    def test_info_box_mentions_cores_and_engine_split(self):
        view = make_view([], caps=Capabilities(), height=20)
        view._info_source = False

        output = render(view, sample_data=MAC_SAMPLE_WITH_MEMORY)

        self.assertIn("30 GPU cores", output)
        self.assertIn("unified memory", output)
        self.assertIn("renderer 44%", output)
        self.assertIn("tiler 45%", output)


class DetailAttachTests(unittest.TestCase):
    def _view(self, rows, details):
        view = make_view(rows)
        view._details_source = lambda: details
        return view

    def test_card_and_memory_are_attached_by_pid(self):
        row = ProcessRow(111, "worker", "root", 0.0, 0, 90.0)
        view = self._view([row], {111: {
            "cards": [0], "vram_bytes": 2048 * 1024 ** 2,
            "gtt_bytes": 512 * 1024 ** 2, "reason": "",
        }})

        rows = view._attach_details([row])

        self.assertEqual(rows[0].gpu_card, "0")
        self.assertEqual(rows[0].gpu_mem_mb, 2048.0)
        self.assertEqual(rows[0].gpu_shared_mb, 512.0)
        self.assertIsNone(rows[0].gpu_note)

    def test_multi_card_label_is_a_range(self):
        row = ProcessRow(7, "EngineCore", "root", 0.0, 0, 90.0)
        view = self._view([row], {7: {
            "cards": [2, 3, 4], "vram_bytes": 1024, "gtt_bytes": 0, "reason": "",
        }})

        rows = view._attach_details([row])

        self.assertEqual(rows[0].gpu_card, "2-4")
        self.assertEqual(rows[0].gpu_list, [2, 3, 4])

    def test_unattributed_memory_stays_na_with_a_reason(self):
        row = ProcessRow(111, "worker", "root", 0.0, 0, 90.0)
        view = self._view([row], {111: {
            "cards": [0], "vram_bytes": None, "gtt_bytes": None,
            "reason": "shared_buffers",
        }})

        rows = view._attach_details([row])

        self.assertIsNone(rows[0].gpu_mem_mb)
        self.assertEqual(rows[0].gpu_note, "shared_buffers")
        self.assertIn("n/a", view._process_text(rows[0]))

    def test_idle_tiny_clients_are_left_out(self):
        row = ProcessRow(111, "idle", "root", 0.0, 0, 0.0)
        view = self._view([row], {111: {
            "cards": [0], "vram_bytes": 0, "gtt_bytes": 0, "reason": "",
        }})

        rows = view._attach_details([row])

        self.assertIsNone(rows[0].gpu_card)

    def test_shared_column_appears_only_when_there_is_shared_memory(self):
        columns, _ = make_view([])._process_layout(76, False, True)
        self.assertIn("shared", columns)

        plain = ProcessRow(111, "worker", "root", 0.0, 0, 90.0,
                           gpu_card="0", gpu_mem_mb=2048.0)
        view = make_view([plain])
        output = render(view)

        self.assertNotIn("SHARED", output)

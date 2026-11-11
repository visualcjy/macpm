import time
import types
import unittest

from macpm.processes import (
    ProcessRow,
    ProcessView,
    _fit,
    _format_mem,
    _parse_ps,
    _parse_ps_time,
    _text_width,
)


def make_view(gpu_available=True):
    view = ProcessView.__new__(ProcessView)
    view.sampler = types.SimpleNamespace(gpu_available=gpu_available)
    view.sort = "cpu"
    view.desc = True
    view.offset = 0
    return view


class FormatTests(unittest.TestCase):
    def test_memory_units(self):
        self.assertEqual(_format_mem(0), "0B")
        self.assertEqual(_format_mem(1024), "1.0K")
        self.assertEqual(_format_mem(10 * 1024), "10K")
        self.assertEqual(_format_mem(1536), "1.5K")
        self.assertEqual(_format_mem(292 * 1024**2), "292M")

    def test_wide_characters_count_as_two_cells(self):
        self.assertEqual(_text_width("abc"), 3)
        self.assertEqual(_text_width("中文"), 4)

    def test_fit_keeps_double_width_text_inside_the_width(self):
        self.assertEqual(_text_width(_fit("中文名称", 5)), 5)
        self.assertEqual(_text_width(_fit("abcdef", 4)), 4)


class PsParsingTests(unittest.TestCase):
    def test_cpu_time_formats(self):
        self.assertEqual(_parse_ps_time("0:00.01"), 0.01)
        self.assertEqual(_parse_ps_time("1:23.45"), 83.45)
        self.assertEqual(_parse_ps_time("1:02:03"), 3723.0)
        self.assertEqual(_parse_ps_time("2-03:04:05"), 183845.0)
        self.assertIsNone(_parse_ps_time("nonsense"))

    def test_rows_keep_names_with_spaces(self):
        text = (
            "  616     0  20000   1:23.45 WindowServer\n"
            "  620   501 111000   0:00.50 loginwindow\n"
            "  999   501   1024   0:00.01 Some Process Name\n"
            "broken\n"
        )

        self.assertEqual(
            _parse_ps(text),
            [
                (616, 0, 20000, 83.45, "WindowServer"),
                (620, 501, 111000, 0.5, "loginwindow"),
                (999, 501, 1024, 0.01, "Some Process Name"),
            ],
        )


class LayoutTests(unittest.TestCase):
    def test_table_fills_the_inner_width(self):
        view = make_view()
        for width in (40, 60, 80, 100, 120):
            columns, widths = view._layout(width)
            used = sum(widths[key] for key in columns) + len(columns) - 1
            self.assertEqual(used, width - 4, (width, columns, widths))

    def test_gpu_column_is_dropped_without_a_gpu_reader(self):
        columns, _ = make_view(gpu_available=False)._layout(100)

        self.assertNotIn("gpu", columns)
        self.assertIn("name", columns)

    def test_wide_process_name_does_not_push_the_columns(self):
        view = make_view()
        columns, widths = view._layout(100)
        row = ProcessRow(1, "有道云笔记", "apple", 1.0, 1024, 0.0)

        self.assertEqual(_text_width(view._row_text(row, columns, widths)), 96)


class SortTests(unittest.TestCase):
    def test_cycle_through_visible_columns(self):
        view = make_view()

        seen = []
        for _ in range(6):
            seen.append((view.sort, view.desc))
            view.cycle_sort()

        self.assertEqual(
            seen,
            [
                ("cpu", True),
                ("mem", True),
                ("gpu", True),
                ("pid", False),
                ("name", False),
                ("user", False),
            ],
        )

    def test_cycle_skips_gpu_when_it_is_unavailable(self):
        view = make_view(gpu_available=False)

        seen = []
        for _ in range(5):
            seen.append(view.sort)
            view.cycle_sort()

        self.assertEqual(seen, ["cpu", "mem", "pid", "name", "user"])


class ThrottleTests(unittest.TestCase):
    def test_process_table_refresh_is_throttled(self):
        view = ProcessView.__new__(ProcessView)
        view.paused = False
        view.ready = False
        view._last_sample = time.monotonic()
        view.refresh_interval = 10.0
        calls = []
        view.sampler = types.SimpleNamespace(sample=lambda: calls.append(1) or [])
        view.rows = []

        view.sample()
        view.sample()
        self.assertEqual(len(calls), 1)

        view._last_sample -= 11
        view.sample()
        self.assertEqual(len(calls), 2)

    def test_paused_table_does_not_sample(self):
        view = ProcessView.__new__(ProcessView)
        view.paused = True
        view.ready = True
        view._last_sample = 0.0
        view.refresh_interval = 1.0
        calls = []
        view.sampler = types.SimpleNamespace(sample=lambda: calls.append(1) or [])
        view.rows = []

        view.sample()

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

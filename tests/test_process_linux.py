"""Linux process sampling: /proc/pid/stat parsing and the column widths."""

import unittest

from macpm.processes import _WIDTHS, _parse_proc_stat


class ProcStatTests(unittest.TestCase):
    def test_comm_with_spaces_and_parens(self):
        raw = (
            b"1234 (python3 (worker)) S 1 1234 1234 0 -1 4194560 1000 0 0 0 "
            b"100 50 0 0 20 0 1 0 99999 1000000 500 18446744073709551615\n"
        )

        name, starttime, ticks = _parse_proc_stat(raw)

        self.assertEqual(name, "python3 (worker)")
        self.assertEqual(starttime, 99999)
        self.assertEqual(ticks, 150)

    def test_short_lines_are_ignored(self):
        self.assertIsNone(_parse_proc_stat(b"12 (x) S 1 2 3"))
        self.assertIsNone(_parse_proc_stat(b"garbage"))


class ArgvNameTests(unittest.TestCase):
    def test_argv0_loses_its_directory(self):
        from macpm.processes import _argv_name

        self.assertEqual(_argv_name(b"/usr/bin/python3\0-m\0x\0"), "python3")
        self.assertEqual(_argv_name(b"sglang::scheduler_TP1\0"), "sglang::scheduler_TP1")
        self.assertEqual(_argv_name(b""), "")


class WidthTests(unittest.TestCase):
    def test_pid_column_fits_seven_digit_pids(self):
        self.assertGreaterEqual(_WIDTHS["pid"], 7)


if __name__ == "__main__":
    unittest.main()

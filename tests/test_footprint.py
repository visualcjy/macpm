"""Per-process GPU memory from footprint(1): parsing, one-shot reads, poller.

The ctypes-free parts are tested everywhere; the reader is exercised with a
fake ``footprint`` process, and one live test runs on this Mac.
"""

import json
import os
import sys
import types
import unittest
from unittest import mock

from macpm.footprint import (
    FootprintPoller,
    graphics_bytes,
    parse_footprint,
    read_process_graphics,
)


class GraphicsBytesTests(unittest.TestCase):
    def test_only_graphics_categories_are_summed(self):
        process = {
            "categories": {
                "IOAccelerator (graphics)": {"dirty": 3_221_962_752},
                "Owned physical footprint (unmapped) (graphics)": {"dirty": 134_217_728},
                "Malloc Small": {"dirty": 212_992},
                "__TEXT": {"dirty": 0, "clean": 409_600},
            }
        }

        self.assertEqual(graphics_bytes(process), 3_356_180_480)

    def test_a_process_without_metal_measures_zero(self):
        process = {"categories": {"Malloc Small": {"dirty": 4096}}}

        self.assertEqual(graphics_bytes(process), 0)

    def test_missing_categories_stay_unknown(self):
        self.assertIsNone(graphics_bytes({}))
        self.assertIsNone(graphics_bytes({"categories": None}))
        self.assertIsNone(graphics_bytes({"categories": []}))

    def test_unreadable_dirty_is_unknown_not_smaller(self):
        process = {"categories": {"IOAccelerator (graphics)": {"dirty": "12"}}}

        self.assertIsNone(graphics_bytes(process))
        process["categories"]["IOAccelerator (graphics)"] = {"dirty": -1}
        self.assertIsNone(graphics_bytes(process))
        process["categories"]["IOAccelerator (graphics)"] = {"clean": 10}
        self.assertIsNone(graphics_bytes(process))


class ParseFootprintTests(unittest.TestCase):
    def test_processes_map_to_pids(self):
        document = {
            "processes": [
                {"pid": 111, "categories": {"IOAccelerator (graphics)": {"dirty": 2048}}},
                {"pid": 222, "categories": {"Malloc Small": {"dirty": 1}}},
            ]
        }

        self.assertEqual(parse_footprint(document), {111: 2048, 222: 0})

    def test_malformed_entries_are_skipped(self):
        document = {
            "processes": [
                {"categories": {}},
                {"pid": "111", "categories": {}},
                {"pid": 333},
                "not-a-process",
            ]
        }

        self.assertEqual(parse_footprint(document), {333: None})

    def test_not_a_document(self):
        self.assertEqual(parse_footprint(None), {})
        self.assertEqual(parse_footprint([]), {})


class ReadProcessGraphicsTests(unittest.TestCase):
    def _fake_run(self, document, returncode=0):
        def run(command, **kwargs):
            path = command[command.index("-j") + 1]
            pid = int(command[command.index("-p") + 1])
            if document is not None:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(document(pid), handle)
            return types.SimpleNamespace(returncode=returncode)

        return run

    def test_reads_the_process_graphics_bytes(self):
        def document(pid):
            return {
                "processes": [
                    {
                        "pid": pid,
                        "categories": {"IOAccelerator (graphics)": {"dirty": 4096}},
                    }
                ]
            }

        with mock.patch("macpm.footprint.subprocess.run", self._fake_run(document)):
            value = read_process_graphics(777)

        self.assertEqual(value, 4096)

    def test_failed_run_is_unknown(self):
        with mock.patch("macpm.footprint.subprocess.run", self._fake_run(lambda pid: {}, 66)):
            self.assertIsNone(read_process_graphics(1))

    def test_malformed_json_is_unknown(self):
        def run(command, **kwargs):
            path = command[command.index("-j") + 1]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            return types.SimpleNamespace(returncode=0)

        with mock.patch("macpm.footprint.subprocess.run", run):
            self.assertIsNone(read_process_graphics(777))

    def test_temporary_file_is_removed(self):
        seen = []

        def run(command, **kwargs):
            path = command[command.index("-j") + 1]
            seen.append(path)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"processes": []}, handle)
            return types.SimpleNamespace(returncode=0)

        with mock.patch("macpm.footprint.subprocess.run", run):
            read_process_graphics(777)

        self.assertTrue(seen)
        self.assertFalse(os.path.exists(seen[0]))


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class PollerTests(unittest.TestCase):
    def _poller(self, measure, start=lambda pid: 1.0, clock=None):
        return FootprintPoller(
            measure=measure,
            start_time=start,
            clock=clock or FakeClock(),
            start=False,
        )

    def test_measures_a_target_and_caches_until_the_interval(self):
        calls = []
        clock = FakeClock()
        poller = self._poller(lambda pid: (calls.append(pid), 2048)[1], clock=clock)
        poller.set_targets([(111, True)])

        poller.poll_once()
        self.assertEqual(poller.get(111), 2048)
        self.assertEqual(calls, [111])

        clock.now = 1.0
        poller.poll_once()
        self.assertEqual(calls, [111])  # still fresh

        clock.now = 2.5
        poller.poll_once()
        self.assertEqual(calls, [111, 111])

    def test_busy_targets_come_first(self):
        calls = []
        poller = self._poller(lambda pid: (calls.append(pid), 1)[1])
        poller.set_targets([(9, False), (5, True), (7, False)])
        poller.MAX_TARGETS = 1

        poller.poll_once()

        self.assertEqual(calls, [5])

    def test_reused_pid_is_measured_again(self):
        clock = FakeClock()
        identity = {111: 1.0}
        poller = self._poller(
            lambda pid: int(identity[pid] * 100), start=lambda pid: identity[pid], clock=clock
        )
        poller.set_targets([(111, True)])
        poller.poll_once()
        self.assertEqual(poller.get(111), 100)

        identity[111] = 2.0  # PID reused by a new process, same tick
        poller.poll_once()

        self.assertEqual(poller.get(111), 200)

    def test_failures_back_off(self):
        calls = []
        clock = FakeClock()
        results = [None, None, None, 4096]

        def measure(pid):
            calls.append(pid)
            return results.pop(0)

        poller = self._poller(measure, clock=clock)
        poller.set_targets([(111, True)])

        for _ in range(3):
            poller.poll_once()
        self.assertIsNone(poller.get(111))
        self.assertEqual(len(calls), 3)

        clock.now = 10.0
        poller.poll_once()  # inside the backoff window
        self.assertEqual(len(calls), 3)

        clock.now = 61.0
        poller.poll_once()
        self.assertEqual(len(calls), 4)
        self.assertEqual(poller.get(111), 4096)

    def test_dropping_a_target_forgets_its_reading(self):
        poller = self._poller(lambda pid: 1)
        poller.set_targets([(111, True)])
        poller.poll_once()

        poller.set_targets([])

        self.assertIsNone(poller.get(111))

    def test_thread_start_and_close(self):
        poller = FootprintPoller(measure=lambda pid: 1, start_time=lambda pid: 1.0)
        poller.set_targets([(111, True)])
        try:
            self.assertIsNotNone(poller._thread)
        finally:
            poller.close()
        self.assertIsNone(poller._thread)


class AttachTests(unittest.TestCase):
    def _view(self, source):
        from macpm.gpuview import GpuView

        view = GpuView.__new__(GpuView)
        view._memory_source = source
        return view

    def test_measured_rows_get_gpu_memory(self):
        from macpm.processes import ProcessRow

        row = ProcessRow(111, "python", "alice", 0.0, 0, 42.0)
        view = self._view(types.SimpleNamespace(
            set_targets=lambda targets: None,
            get=lambda pid: 3 * 1024 ** 3,
            close=lambda: None,
        ))

        rows = view._attach_footprint([row])

        self.assertEqual(rows[0].gpu_mem_mb, 3072.0)
        self.assertIsNone(rows[0].gpu_note)

    def test_unmeasured_rows_stay_unknown(self):
        from macpm.processes import ProcessRow

        row = ProcessRow(111, "WindowServer", "root", 0.0, 0, 42.0)
        view = self._view(types.SimpleNamespace(
            set_targets=lambda targets: None,
            get=lambda pid: None,
            close=lambda: None,
        ))

        rows = view._attach_footprint([row])

        self.assertIsNone(rows[0].gpu_mem_mb)
        self.assertEqual(rows[0].gpu_note, "footprint")

    def test_targets_skip_rows_that_already_have_memory(self):
        from macpm.processes import ProcessRow

        seen = []
        view = self._view(types.SimpleNamespace(
            set_targets=lambda targets: seen.extend(targets),
            get=lambda pid: None,
            close=lambda: None,
        ))
        rows = [
            ProcessRow(1, "a", "u", 0.0, 0, 90.0),
            ProcessRow(2, "b", "u", 0.0, 0, 0.0),
        ]

        view._attach_footprint(rows)

        self.assertEqual(seen, [(1, True), (2, False)])

    def test_close_stops_the_source(self):
        closed = []
        view = self._view(types.SimpleNamespace(
            set_targets=lambda targets: None,
            get=lambda pid: None,
            close=lambda: closed.append(True),
        ))

        view.close()

        self.assertEqual(closed, [True])
        self.assertIsNone(view._memory_source)


@unittest.skipUnless(sys.platform == "darwin", "needs footprint(1) (macOS)")
class LiveFootprintTests(unittest.TestCase):
    def test_reads_this_process(self):
        value = read_process_graphics(os.getpid())

        self.assertIsInstance(value, int)
        self.assertGreaterEqual(value, 0)


if __name__ == "__main__":
    unittest.main()

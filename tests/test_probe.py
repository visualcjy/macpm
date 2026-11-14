import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from macpm import probe
from macpm.linux_backend import gpu as gpu_backend

from tests import linux_fixtures


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-probe-")
        linux_fixtures.build(self.root, "multi")
        self.env = mock.patch.dict(
            os.environ, {"MACPM_SYSROOT": self.root, "MACPM_PLATFORM": "linux"}
        )
        self.env.start()
        gpu_backend.reset_shared()

    def tearDown(self):
        gpu_backend.reset_shared()
        self.env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_collect_finds_every_card(self):
        report = probe.collect()

        cards = report["drm_cards"]
        self.assertEqual(len(cards), 4)
        self.assertEqual([card["driver"] for card in cards], ["amdgpu"] * 4)
        self.assertEqual([card["busy"] for card in cards], [10, 35, 60, 85])
        self.assertIn("gpu_busy_percent", cards[0]["files"])

    def test_collect_reports_the_backend_capabilities(self):
        report = probe.collect()

        caps = report["macpm_capabilities"]
        self.assertEqual(caps["platform"], "linux")
        self.assertTrue(caps["gpu_util"])
        self.assertFalse(caps["ane"])

    def test_human_output_is_readable(self):
        text = probe._human(probe.collect())

        self.assertIn("DRM cards : 4", text)
        self.assertIn("driver=amdgpu", text)
        self.assertIn("macpm     : capability report", text)

    def test_json_run(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = probe.main(["--json"])
        import json

        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["drm_cards"]), 4)


if __name__ == "__main__":
    unittest.main()

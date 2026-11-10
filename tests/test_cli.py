import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def run_module(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-m", "macpm", *args],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )

    def test_version(self):
        result = self.run_module("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("macpm v2.0", result.stdout)

    def test_help(self):
        result = self.run_module("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--interval", result.stdout)
        self.assertIn("--show_cores", result.stdout)


if __name__ == "__main__":
    unittest.main()

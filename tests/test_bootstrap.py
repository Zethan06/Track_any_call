"""Unsupported child interpreters must fail before application execution."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src"


class BootstrapTests(unittest.TestCase):
    def test_old_child_interpreter_stops_before_application(self):
        env = {name: value for name, value in os.environ.items()
               if not name.startswith("CALLNAV_")}
        env["PYTHONPATH"] = str(SOURCE)
        code = (
            "import os, sys\n"
            "from callnav.bootstrap import activate\n"
            "sys.version_info = (3, 10, 20)\n"
            "os.environ['CALLNAV_RUN_DIR'] = 'unused-version-guard'\n"
            "activate()\n"
            "print('APPLICATION_EXECUTED')\n"
        )
        process = subprocess.run(
            [sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(process.returncode, 86, process.stdout + process.stderr)
        self.assertIn("recording requires Python 3.11+", process.stderr)
        self.assertNotIn("APPLICATION_EXECUTED", process.stdout)


if __name__ == "__main__":
    unittest.main()

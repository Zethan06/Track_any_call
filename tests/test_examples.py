"""Verify that the public shell example works in a relocated checkout."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("bash"), "the shell example requires Bash")
class ExampleTests(unittest.TestCase):
    def test_relocated_launch_records_threads_and_children_with_space_paths(self):
        with tempfile.TemporaryDirectory(prefix="callnav examples ") as temporary:
            directory = Path(temporary)
            source = directory / "callnav checkout with spaces" / "src"
            shutil.copytree(PROJECT / "src" / "callnav", source / "callnav",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            example = directory / "user project with spaces"
            shutil.copytree(PROJECT / "examples" / "mini_pipeline", example,
                            ignore=shutil.ignore_patterns(".callnav", "__pycache__", "*.pyc"))
            working = directory / "another working directory"
            working.mkdir()
            python = directory / "python with spaces"
            python.symlink_to(sys.executable)
            output = directory / "recording with spaces"
            env = os.environ.copy()
            env.update(PYTHONPATH=str(source), PYTHON=str(python))
            process = subprocess.run(
                [sys.executable, "-m", "callnav", "record", "--root", str(example),
                 "--output", str(output), "--", "bash", str(example / "launch.sh"),
                 "--iterations", "2"],
                cwd=working, env=env, text=True, capture_output=True,
            )

            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout.count("rollout "), 2)
            self.assertEqual(process.stdout.count("weight_sync "), 2)
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            targets = {}
            for edge in index["edges"]:
                targets.setdefault(edge["target"]["qualname"], []).append(edge)
            self.assertTrue({"generate", "length_reward", "exact_reward", "sgd_update"}
                            .issubset(targets), targets.keys())
            self.assertEqual(sum(edge["count"] for edge in targets["sgd_update"]), 2)
            self.assertEqual(sum(edge["count"] for edge in targets["length_reward"]), 2)
            self.assertGreaterEqual(len(index["processes"]), 3)
            self.assertTrue(all(item["status"] == "complete" for item in index["processes"]))
            self.assertTrue(all(Path(path).is_relative_to(example) for path in index["sources"]))
            self.assertEqual(
                json.loads((example / ".callnav" / "latest.json").read_text(encoding="utf-8")),
                index,
            )
            self.assertFalse((working / ".callnav").exists())


if __name__ == "__main__":
    unittest.main()

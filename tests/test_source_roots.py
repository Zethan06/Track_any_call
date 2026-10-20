"""Explicit dependency roots must work across the complete recording command."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"


class SourceRootsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.dependency = self.base / "dependency"
        self.unrelated = self.base / "unrelated"
        for directory in (self.root, self.dependency, self.unrelated):
            directory.mkdir()
        (self.dependency / "external.py").write_text(
            "def forward(callback):\n    return callback()\n", encoding="utf-8")
        (self.unrelated / "other.py").write_text(
            "def work():\n    return 5\n", encoding="utf-8")
        self.script = self.root / "run.py"
        self.script.write_text(
            "from external import forward\nfrom other import work\n"
            "def local():\n    return 3\n"
            "assert forward(local) == 3\nassert work() == 5\n",
            encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def record(self, name, flags=(), extra_env=None, command=None):
        output = self.base / name
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            map(str, (SOURCE, self.dependency, self.unrelated))))
        env.update(extra_env or {})
        result = subprocess.run(
            [sys.executable, "-m", "callnav", "record", "--root", str(self.root),
             "--output", str(output), *flags, "--", *(command or [sys.executable, str(self.script)])],
            env=env, cwd=self.root, text=True, capture_output=True)
        index = output / "index.json"
        return result, json.loads(index.read_text()) if index.exists() else None, output

    def test_explicit_roots_record_both_directions_without_unrelated_sources(self):
        process, index, output = self.record("included", [
            "--include-root", str(self.dependency), "--include-root", str(self.dependency)])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(index["roots"], [str(self.root), str(self.dependency)])
        self.assertEqual(json.loads((output / "run.json").read_text())["roots"], index["roots"])
        pairs = {(e["caller"]["path"], e["target"]["qualname"]) for e in index["edges"]
                 if not e["caller"].get("ambiguous")}
        self.assertIn((str(self.script), "forward"), pairs)
        self.assertIn((str(self.dependency / "external.py"), "local"), pairs)
        self.assertNotIn(str(self.unrelated / "other.py"), index["sources"])

    def test_parent_recording_environment_does_not_silently_expand_new_run(self):
        process, index, _ = self.record("isolated", extra_env={
            "CALLNAV_INCLUDE_ROOTS": json.dumps([str(self.dependency), str(self.unrelated)])})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(index["roots"], [str(self.root)])
        self.assertNotIn(str(self.dependency / "external.py"), index["sources"])
        self.assertNotIn(str(self.unrelated / "other.py"), index["sources"])

    def test_invalid_dependency_root_fails_before_creating_run(self):
        process, index, output = self.record("invalid", ["--include-root", str(self.base / "missing")])
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertIn("--include-root", process.stderr)
        self.assertIsNone(index)
        self.assertFalse(output.exists())

    def test_source_roots_survive_python_child_bootstrap(self):
        launcher = self.root / "launch.py"
        launcher.write_text("import subprocess, sys\n"
                            "subprocess.run([sys.executable, 'run.py'], check=True)\n",
                            encoding="utf-8")
        process, index, _ = self.record("child", ["--include-root", str(self.dependency)],
                                        command=[sys.executable, str(launcher)])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertGreaterEqual(len(index["processes"]), 2)
        self.assertTrue(any(e["target"]["qualname"] == "forward" and
                            e["target"]["path"] == str(self.dependency / "external.py")
                            for e in index["edges"]))
        self.assertTrue(all(p["errors"] == 0 for p in index["processes"]))


if __name__ == "__main__":
    unittest.main()

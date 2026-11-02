"""Prefix capture preserves command argv and keeps independent run history."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from callnav import __version__

SOURCE = Path(__file__).resolve().parents[1] / "src"


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.environment = {name: value for name, value in os.environ.items()
                            if not name.startswith("CALLNAV_")}
        self.environment["PYTHONPATH"] = str(SOURCE)

    def write(self, name, content):
        path = self.root / name
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return path

    def invoke(self, arguments, *, cwd=None):
        return subprocess.run([sys.executable, "-m", "callnav", *arguments],
            env=self.environment, cwd=cwd or self.root, text=True, capture_output=True, timeout=30)

    def histories(self):
        return sorted((self.root / ".callnav").glob("*/run.json"))

    def test_bash_prefix_records_from_cwd_with_ray_enabled_and_literal_arguments(self):
        self.write("script.py", """
            import json
            import sys
            def work(): return 7
            assert work() == 7
            print(json.dumps(sys.argv[1:]))
        """)
        script = self.write("launch 'with spaces'.sh", 'set -eu\npython script.py "$@"\n')
        arguments = ["a space", "$(touch should-not-exist)", "", "--root", "--version", "-h"]
        process = self.invoke(["bash", str(script), *arguments])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), arguments)
        self.assertFalse((self.root / "should-not-exist").exists())
        manifests = self.histories()
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text())
        self.assertEqual(manifest["command"], ["bash", str(script), *arguments])
        self.assertEqual(manifest["root"], str(self.root))
        self.assertTrue(manifest["ray"])
        index = json.loads(manifests[0].with_name("index.json").read_text())
        self.assertTrue(any(edge["target"]["qualname"] == "work" for edge in index["edges"]))
        catalog = list((self.root / ".callnav/recordings").glob("*.json"))
        self.assertEqual(len(catalog), 1)
        self.assertEqual(json.loads(catalog[0].read_text()), {
            "schema_version": 1, "run_id": manifest["run_id"], "root": str(self.root),
            "run_dir": str(manifests[0].parent), "index_path": str(manifests[0].with_name("index.json")),
        })

    def test_concurrent_same_timestamp_runs_keep_distinct_complete_histories(self):
        script = self.write("run.py", "def actual(): return 1\nassert actual() == 1\n")
        # Freeze only the CLI's wall clock to make both manifests use precisely
        # the same timestamp; child Python instrumentation keeps its real clock.
        launcher = "import callnav.cli as cli; cli.time.time=lambda:1234.0; raise SystemExit(cli.main())"
        processes = [subprocess.Popen([sys.executable, "-c", launcher, sys.executable, str(script)],
            env=self.environment, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(2)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        manifests = self.histories()
        self.assertEqual(len(manifests), 2)
        rows = [json.loads(path.read_text()) for path in manifests]
        self.assertEqual({row["started_at"] for row in rows}, {1234.0})
        self.assertEqual(len({row["run_id"] for row in rows}), 2)
        self.assertTrue(all(row["status"] == "finished" and row["exit_code"] == 0 for row in rows))
        for path, row in zip(manifests, rows):
            index = json.loads(path.with_name("index.json").read_text())
            self.assertEqual(index["run_id"], row["run_id"])
            self.assertTrue(any(edge["target"]["qualname"] == "actual" for edge in index["edges"]))

    def test_child_module_help_and_version_flags_are_not_consumed(self):
        self.write("probe.py", "import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        process = self.invoke([sys.executable, "-m", "probe", "-h", "--version", "--root", "literal"])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), ["-h", "--version", "--root", "literal"])
        process = self.invoke([sys.executable, "-h"])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("usage:", process.stdout)
        self.assertNotIn("Quick capture", process.stdout)
        process = self.invoke([sys.executable, "--version"])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(process.stdout.startswith("Python "), process.stdout)
        self.assertEqual(len(self.histories()), 3)

    def test_explicit_recorder_options_and_separator_preserve_child_options(self):
        extra = self.root.parent / "dependency"
        extra.mkdir()
        output = self.root.parent / "external-recording"
        script = self.write("run.py", "import sys\nassert sys.argv[1:]==['--no-ray','-h']\n")
        process = self.invoke(["--root", str(self.root), "--include-root", str(extra),
            "--output", str(output), "--no-ray", "--", sys.executable, str(script), "--no-ray", "-h"],
            cwd=self.root.parent)
        self.assertEqual(process.returncode, 0, process.stderr)
        manifest = json.loads((output / "run.json").read_text())
        self.assertFalse(manifest["ray"])
        self.assertEqual(manifest["roots"], [str(self.root), str(extra)])
        self.assertEqual(manifest["command"][-2:], ["--no-ray", "-h"])
        catalog = list((self.root / ".callnav/recordings").glob("*.json"))
        self.assertEqual(len(catalog), 1)
        self.assertEqual(json.loads(catalog[0].read_text())["run_dir"], str(output))
        # An older recording can be registered by merge without rerunning it.
        catalog[0].unlink()
        process = self.invoke(["merge", str(output)])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(catalog[0].is_file())
        self.assertEqual(json.loads(catalog[0].read_text())["index_path"], str(output / "index.json"))

    def test_new_capture_keeps_first_history_manifest_index_and_catalog_unchanged(self):
        script = self.write("run.py", "def actual(): return 1\nactual()\n")
        first = self.invoke([sys.executable, str(script)])
        self.assertEqual(first.returncode, 0, first.stderr)
        manifest = self.histories()[0]
        catalog = next((self.root / ".callnav/recordings").glob("*.json"))
        snapshots = {path: path.read_bytes() for path in (manifest, manifest.with_name("index.json"), catalog)}
        second = self.invoke([sys.executable, str(script)])
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.histories()), 2)
        self.assertEqual(len(list((self.root / ".callnav/recordings").glob("*.json"))), 2)
        for path, original in snapshots.items():
            self.assertEqual(path.read_bytes(), original)

    def test_catalog_hashes_foreign_run_ids_instead_of_treating_them_as_paths(self):
        output = self.root.parent / "old-recording"
        output.mkdir()
        run_id = "../../untrusted/run"
        (output / "run.json").write_text(json.dumps({
            "schema_version": 1, "run_id": run_id, "root": str(self.root),
            "command": [], "status": "finished", "exit_code": 0,
        }))
        process = self.invoke(["merge", str(output)])
        self.assertEqual(process.returncode, 0, process.stderr)
        catalog = list((self.root / ".callnav/recordings").iterdir())
        self.assertEqual([path.name for path in catalog], [hashlib.sha256(run_id.encode()).hexdigest() + ".json"])
        self.assertEqual(json.loads(catalog[0].read_text())["run_id"], run_id)

    def test_history_write_failure_warns_without_changing_command_exit_or_index(self):
        output = self.root.parent / "external-output"
        launcher = textwrap.dedent("""
            import callnav.cli as cli
            original = cli._write_json
            def deny_history(path, value):
                if path.parent.name == 'recordings' or path.name == 'latest.json':
                    raise PermissionError('history is read-only')
                original(path, value)
            cli._write_json = deny_history
            raise SystemExit(cli.main())
        """)
        process = subprocess.run([sys.executable, "-c", launcher, "--output", str(output),
            "bash", "-c", "exit 7"], env=self.environment, cwd=self.root,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 7, process.stderr)
        self.assertIn("cannot register recording history", process.stderr)
        self.assertIn("cannot update latest recording", process.stderr)
        self.assertEqual(json.loads((output / "run.json").read_text())["exit_code"], 7)
        self.assertTrue((output / "index.json").is_file())

    def test_nonzero_and_missing_program_exit_statuses_still_have_history(self):
        process = self.invoke(["bash", "-c", "exit 7"])
        self.assertEqual(process.returncode, 7, process.stderr)
        process = self.invoke([str(self.root / "missing-program")])
        self.assertEqual(process.returncode, 127, process.stderr)
        self.assertEqual({json.loads(path.read_text())["exit_code"] for path in self.histories()}, {7, 127})

    def test_legacy_record_merge_and_top_level_flags_remain_compatible(self):
        output = self.root / "legacy"
        script = self.write("run.py", "def actual(): return 1\nactual()\n")
        process = self.invoke(["record", "--root", str(self.root), "--output", str(output),
            "--", sys.executable, str(script)])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertFalse(json.loads((output / "run.json").read_text())["ray"])
        before = json.loads((output / "index.json").read_text())
        process = self.invoke(["merge", str(output)])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads((output / "index.json").read_text())["edges"], before["edges"])
        process = self.invoke(["--version"])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "callnav " + __version__)
        process = self.invoke(["--root", str(self.root), "--version"])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "callnav " + __version__)
        process = self.invoke(["--help"])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("Quick capture: callnav bash launch.sh", process.stdout)


if __name__ == "__main__":
    unittest.main()

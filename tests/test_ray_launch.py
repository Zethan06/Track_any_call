"""Exercise shell Ray submission propagation without a Ray installation."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src"


@unittest.skipUnless(os.name == "posix", "the PATH shim supports POSIX shell launchers")
class RayLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="callnav shell with spaces ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "user project"
        self.root.mkdir()
        self.bin = self.base / "fake bin"
        self.bin.mkdir()
        self.dependency = self.base / "user modules"
        self.dependency.mkdir()
        (self.dependency / "helper.py").write_text("VALUE = 7\n", encoding="utf-8")
        self.probe = self.base / "ray probe.json"
        self.fake_ray = self.base / "ray_cli.py"
        self.fake_ray.write_text(textwrap.dedent("""\
            import json
            import os
            from pathlib import Path
            import subprocess
            import sys

            args = sys.argv[1:]
            if args[:1] == ["start"]:
                Path(os.environ["RAY_PROBE"]).write_text(json.dumps({
                    "recording_variables": [key for key in os.environ if key.startswith("CALLNAV_")],
                    "pythonpath": os.environ.get("PYTHONPATH", ""),
                    "path": os.environ["PATH"],
                }))
            elif args[:2] == ["job", "submit"]:
                separator = args.index("--")
                option = next(value for value in args[2:separator] if value.startswith("--runtime-env-json="))
                runtime = json.loads(option.split("=", 1)[1])
                # Emulate the separately started daemon: the submitted job only
                # receives recording variables supplied in its runtime env.
                env = {key: value for key, value in os.environ.items() if not key.startswith("CALLNAV_")}
                env.pop("PYTHONPATH", None)
                env.update(runtime["env_vars"])
                raise SystemExit(subprocess.run(args[separator + 1:], env=env).returncode)
            else:
                raise SystemExit("unexpected fake Ray command")
            """), encoding="utf-8")
        ray = self.bin / "ray"
        ray.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
            + shlex.quote(str(self.fake_ray)) + ' "$@"\n',
            encoding="utf-8",
        )
        ray.chmod(0o755)
        self.app = self.root / "app.py"
        self.app.write_text(textwrap.dedent("""\
            import os
            import sys
            from helper import VALUE

            def implementation(value):
                return value + VALUE

            assert os.environ["CUSTOM"] == "retained"
            assert sys.argv[1] == "space and $(literal)"
            selected = implementation
            assert selected(3) == 10
            print("USER_APP_OK")
            """), encoding="utf-8")
        self.launcher = self.root / "launch.sh"
        runtime = json.dumps({"env_vars": {"PYTHONPATH": str(self.dependency), "CUSTOM": "retained"}})
        self.launcher.write_text(
            "set -eu\nray start --head\nray job submit "
            + shlex.quote("--runtime-env-json=" + runtime) + " -- "
            + " ".join(map(shlex.quote, [sys.executable, str(self.app), "space and $(literal)"]))
            + "\n",
            encoding="utf-8",
        )

    def record(self, extra=()):
        env = dict(os.environ, PYTHONPATH=str(SOURCE), RAY_PROBE=str(self.probe))
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            [sys.executable, "-m", "callnav", "record", "--root", str(self.root),
             "--output", str(self.base / "recording"), "--ray", *extra,
             "--", "bash", str(self.launcher)],
            env=env, cwd=self.base, text=True, capture_output=True, timeout=30,
        )

    def test_generic_ray_jobs_keep_recording_and_daemons_do_not(self):
        process = self.record()
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn("USER_APP_OK", process.stdout)
        probe = json.loads(self.probe.read_text())
        self.assertEqual(probe["recording_variables"], [])
        self.assertNotIn(".bootstrap", probe["pythonpath"])
        self.assertNotIn(".bootstrap", probe["path"])
        index = json.loads((self.base / "recording" / "index.json").read_text())
        self.assertTrue(index["processes"])
        self.assertTrue(all(not process["errors"] for process in index["processes"]))
        calls = [edge for edge in index["edges"]
                 if edge["target"]["path"] == str(self.app)
                 and edge["target"]["qualname"] == "implementation"]
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["caller"].get("ambiguous"))
        self.assertEqual(index["ray"]["matched_tasks"], 0)

    def test_detached_job_is_refused_before_application_execution(self):
        text = self.launcher.read_text()
        self.launcher.write_text(text.replace("ray job submit ", "ray job submit --no-wait ", 1))
        process = self.record()
        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
        self.assertIn("--no-wait is unsupported", process.stderr)
        self.assertNotIn("USER_APP_OK", process.stdout)


if __name__ == "__main__":
    unittest.main()

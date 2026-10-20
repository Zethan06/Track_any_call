from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from callnav.adapters.ray_cli import RayCLIError, daemon_environment, main, merge_submit_args


ENV = {
    "CALLNAV_RUN_ID": "run-one",
    "CALLNAV_REAL_RAY": "/original/bin/ray",
    "PYTHONPATH": "/recording/.bootstrap:/callnav/src:/original/module",
}


class RayCLITests(unittest.TestCase):
    def test_preserves_non_submission_commands(self) -> None:
        original = ["start", "--head", "--num-gpus", "8"]
        self.assertEqual(merge_submit_args(original, ENV), original)

    def test_merges_equals_form_and_retains_application_argv(self) -> None:
        runtime = {"env_vars": {"PYTHONPATH": "/Megatron-LM", "NCCL": "retained"}, "working_dir": "/project"}
        suffix = ["--", "python3", "train.py", "--custom-option", "space and $(literal)", "--no-wait"]
        original = ["job", "submit", "--address=http://127.0.0.1:8265", "--runtime-env-json=" + json.dumps(runtime), *suffix]
        merged = merge_submit_args(original, ENV)
        value = json.loads(merged[3].split("=", 1)[1])
        self.assertEqual(merged[4:], suffix)
        self.assertEqual(value["working_dir"], "/project")
        self.assertEqual(value["env_vars"]["NCCL"], "retained")
        self.assertEqual(value["env_vars"]["CALLNAV_RUN_ID"], "run-one")
        self.assertEqual(value["env_vars"]["PYTHONPATH"], ENV["PYTHONPATH"] + ":/Megatron-LM")
        self.assertEqual(runtime["env_vars"]["PYTHONPATH"], "/Megatron-LM")

    def test_merges_separate_form(self) -> None:
        args = ["job", "submit", "--runtime-env-json", '{"env_vars":{"CUSTOM":"ok"}}', "--", "python", "train.py"]
        merged = merge_submit_args(args, ENV)
        self.assertEqual(merged[:3], args[:3])
        self.assertEqual(merged[4:], args[4:])
        self.assertEqual(json.loads(merged[3])["env_vars"]["CUSTOM"], "ok")

    def test_adds_runtime_env_when_missing(self) -> None:
        merged = merge_submit_args(["job", "submit", "--", "python", "train.py"], ENV)
        self.assertEqual(merged[:3], ["job", "submit", "--runtime-env-json"])
        self.assertEqual(merged[4:], ["--", "python", "train.py"])
        self.assertEqual(json.loads(merged[3])["env_vars"]["CALLNAV_RUN_ID"], "run-one")

    def test_unsupported_or_invalid_submissions_are_refused(self) -> None:
        examples = [
            ["--no-wait"], ["--runtime-env", "env.yaml"], ["--runtime-env=env.yaml"],
            ["--runtime-env-json", "not JSON"], ["--runtime-env-json=[]"],
            ["--runtime-env-json", '{"env_vars":{"BAD":1}}'],
            ["--runtime-env-json", "{}", "--runtime-env-json={}"],
        ]
        for options in examples:
            with self.subTest(options=options), self.assertRaises(RayCLIError):
                merge_submit_args(["job", "submit", *options, "--", "python", "train.py"], ENV)
        with self.assertRaises(RayCLIError):
            merge_submit_args(["job", "submit", "python", "train.py"], ENV)

    def test_executes_original_program_without_a_shell(self) -> None:
        with patch.dict(os.environ, ENV, clear=True), patch("os.execv") as execute:
            main(["stop", "--force"])
        execute.assert_called_once_with("/original/bin/ray", ["/original/bin/ray", "stop", "--force"])

    def test_daemon_cleanup_handles_launcher_prepended_paths(self) -> None:
        source = str(Path(__file__).resolve().parents[1] / "src")
        env = {
            **ENV,
            "CALLNAV_BOOTSTRAP_PATH": "/recording/.bootstrap",
            "PYTHONPATH": f"/user/prepended:/recording/.bootstrap:{source}:/user/modules:{source}",
            "PATH": "/user/tools:/recording/.bootstrap/bin:/user/bin:/usr/bin",
        }
        clean = daemon_environment(env)
        self.assertFalse(any(name.startswith("CALLNAV_") for name in clean))
        self.assertEqual(clean["PYTHONPATH"], f"/user/prepended:/user/modules:{source}")
        self.assertEqual(clean["PATH"], "/user/tools:/user/bin:/usr/bin")

    def test_ray_start_does_not_instrument_persistent_daemons(self) -> None:
        source = str(Path(__file__).resolve().parents[1] / "src")
        env = {
            **ENV,
            "CALLNAV_BOOTSTRAP_PATH": "/recording/.bootstrap",
            "PYTHONPATH": f"/recording/.bootstrap:{source}:/user/modules:{source}",
            "PATH": "/recording/.bootstrap/bin:/user/bin:/usr/bin",
            "CUDA_VISIBLE_DEVICES": "0,1",
        }
        clean = daemon_environment(env)
        self.assertFalse(any(name.startswith("CALLNAV_") for name in clean))
        self.assertEqual(clean["PYTHONPATH"], f"/user/modules:{source}")
        self.assertEqual(clean["PATH"], "/user/bin:/usr/bin")
        self.assertEqual(clean["CUDA_VISIBLE_DEVICES"], "0,1")
        with patch.dict(os.environ, env, clear=True), patch("os.execve") as execute:
            main(["start", "--head"])
        execute.assert_called_once_with("/original/bin/ray", ["/original/bin/ray", "start", "--head"], clean)


if __name__ == "__main__":
    unittest.main()

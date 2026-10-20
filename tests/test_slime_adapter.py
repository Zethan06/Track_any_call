"""Exercise the Slime integration without importing Ray or GPU libraries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src"


class SlimeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write("slime/__init__.py", "")
        self.write("slime/ray/__init__.py", "")
        self.write(
            "slime/ray/utils.py",
            """
            def add_default_ray_env_vars(env_vars=None):
                return {"SLIME_DEFAULT": "yes", **(env_vars or {})}
            """,
        )
        self.write(
            "slime/ray/actor_group.py",
            "from .utils import add_default_ray_env_vars\n",
        )
        self.write(
            "slime/ray/placement_group.py",
            """
            from .actor_group import add_default_ray_env_vars

            def create_training_models(args, pgs, rollout_manager, actor_cls=None):
                args.initialized_with_num_rollout = args.num_rollout
                if args.start_rollout_id is None:
                    args.start_rollout_id = args.checkpoint_rollout_id
                return "actor", "critic"
            """,
        )

    def write(self, name: str, source: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path

    def run_script(self, source: str, *, name: str = "adapter_probe.py", **env_vars: str) -> subprocess.CompletedProcess[str]:
        path = self.write(name, source)
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": os.pathsep.join([str(SOURCE), str(self.root)]),
                "CALLNAV_ADAPTER": "slime",
                "CALLNAV_ITERATIONS": "1",
                "CALLNAV_RUN_DIR": str(self.root / "recording"),
            }
        )
        env.update(env_vars)
        return subprocess.run(
            [sys.executable, str(path)], capture_output=True, text=True, env=env, cwd=self.root, timeout=15
        )

    def test_complete_iteration_after_checkpoint_resume(self) -> None:
        result = self.run_script(
            """
            import json
            from types import SimpleNamespace
            from callnav.adapters.slime import install
            install()
            from slime.ray.placement_group import create_training_models

            def train(args):
                events = ["rollout_manager_init"]
                actors = create_training_models(args, {}, object())
                events.extend(["train_model_init", "initial_weight_sync"])
                for rollout_id in range(args.start_rollout_id, args.num_rollout):
                    events.extend([f"rollout:{rollout_id}", f"reward:{rollout_id}"])
                    for _ in range(args.num_steps_per_rollout):
                        events.append(f"train:{rollout_id}")
                    events.append(f"weight_sync:{rollout_id}")
                events.extend(["dispose", "finish_tracking"])
                print(json.dumps({"args": vars(args), "events": events, "actors": actors}))

            args = SimpleNamespace(
                num_rollout=100, start_rollout_id=None,
                checkpoint_rollout_id=8, num_steps_per_rollout=3,
            )
            train(args)
            """,
            name="train.py",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["args"]["num_rollout"], 9)
        self.assertEqual(output["args"]["initialized_with_num_rollout"], 100)
        self.assertEqual(output["args"]["num_steps_per_rollout"], 3)
        self.assertEqual(output["actors"], ["actor", "critic"])
        markers = list((self.root / "recording" / "adapter").glob("*.json"))
        self.assertEqual(len(markers), 1)
        marker = json.loads(markers[0].read_text())
        self.assertEqual(marker["status"], "configured")
        self.assertEqual(marker["original_num_rollout"], 100)
        self.assertEqual(marker["start_rollout_id"], 8)
        self.assertEqual(marker["stop_rollout_id"], 9)
        self.assertEqual(
            output["events"],
            [
                "rollout_manager_init", "train_model_init", "initial_weight_sync",
                "rollout:8", "reward:8", "train:8", "train:8", "train:8",
                "weight_sync:8", "dispose", "finish_tracking",
            ],
        )

    def test_propagates_recording_to_actor_environment_without_losing_values(self) -> None:
        result = self.run_script(
            """
            import json
            from callnav.adapters.slime import install
            install()
            install()
            from slime.ray.actor_group import add_default_ray_env_vars
            supplied = {"PYTHONPATH": "/actor/modules", "CUSTOM": "retained"}
            print(json.dumps({"merged": add_default_ray_env_vars(supplied), "supplied": supplied}))
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        merged = output["merged"]
        self.assertEqual(merged["CALLNAV_RUN_DIR"], str(self.root / "recording"))
        self.assertEqual(merged["CALLNAV_ADAPTER"], "slime")
        self.assertEqual(merged["SLIME_DEFAULT"], "yes")
        self.assertEqual(merged["CUSTOM"], "retained")
        self.assertEqual(merged["PYTHONPATH"].split(os.pathsep), [str(SOURCE), str(self.root), "/actor/modules"])
        self.assertEqual(output["supplied"], {"PYTHONPATH": "/actor/modules", "CUSTOM": "retained"})

    def test_explicit_start_and_iteration_count(self) -> None:
        result = self.run_script(
            """
            from types import SimpleNamespace
            from callnav.adapters.slime import install
            install()
            from slime.ray.placement_group import create_training_models
            args = SimpleNamespace(num_rollout=200, start_rollout_id=11, checkpoint_rollout_id=99)
            create_training_models(args, {}, object())
            print(args.num_rollout)
            """,
            CALLNAV_ITERATIONS="2",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "13")

    def test_async_entrypoint_is_refused_before_application_code(self) -> None:
        result = self.run_script(
            """
            from callnav.adapters.slime import install
            install()
            print("APPLICATION EXECUTED")
            """,
            name="train_async.py",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("train_async.py is not supported", result.stderr)
        self.assertNotIn("APPLICATION EXECUTED", result.stdout)

    def test_incompatible_slime_signature_is_refused(self) -> None:
        self.write("slime/ray/placement_group.py", "def create_training_models(new_api): return None\n")
        result = self.run_script(
            """
            from callnav.adapters.slime import install
            install()
            import slime.ray.placement_group
            """
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported Slime create_training_models signature", result.stderr)

    def test_changed_native_driver_is_refused_before_application_imports(self) -> None:
        result = self.run_script(
            """
            from callnav.adapters.slime import install
            install()
            print("APPLICATION EXECUTED")
            """,
            name="train.py",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported train.py", result.stderr)
        self.assertNotIn("APPLICATION EXECUTED", result.stdout)

    def test_evaluation_only_mode_is_refused_before_initialization(self) -> None:
        result = self.run_script(
            """
            from types import SimpleNamespace
            from callnav.adapters.slime import install
            install()
            from slime.ray.placement_group import create_training_models
            args = SimpleNamespace(num_rollout=0, start_rollout_id=0)
            create_training_models(args, {}, object())
            """
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("evaluation-only mode", result.stderr)


if __name__ == "__main__":
    unittest.main()

"""Ray receipt joins plus an opt-in real, CPU-only Ray integration test."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest import mock
import textwrap
import unittest

from callnav.adapters.ray import merge_events
from callnav.adapters import ray as ray_adapter
from callnav.collector import Source

SOURCE = Path(__file__).resolve().parents[1] / "src"


class RayMergeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "ray").mkdir()
        self.index = {"run_id": "run", "sources": {}, "edges": [], "warnings": []}
        self.site = {"path": "/project/main.py", "line": 4, "column": 10, "end_line": 4,
                     "end_column": 20, "qualname": "submit", "offset": 12, "precision": "instruction"}
        self.target = {"path": "/project/actor.py", "line": 8, "column": 0, "qualname": "Worker.run"}

    def tearDown(self):
        self.directory.cleanup()

    def write(self, *events, trailing=""):
        (self.root / "ray" / "host-10.jsonl").write_text(
            "".join(json.dumps(dict(event, run_id="run")) + "\n" for event in events) + trailing
        )

    def submission(self, task="task"):
        return {"type": "submission", "task_id": task, "actor_id": "actor", "method": "run",
                "pid": 10, "caller": self.site, "sources": {"/project/main.py": {"sha256": "caller"}}}

    def execution(self, task="task", pid=20):
        return {"type": "execution", "task_id": task, "actor_id": "actor", "method": "run",
                "pid": pid, "target": self.target, "sources": {"/project/actor.py": {"sha256": "target"}}}

    def test_submission_without_actual_entry_has_no_navigation_edge(self):
        self.write(self.submission())
        merge_events(self.index, self.root)
        self.assertEqual(self.index["edges"], [])
        self.assertEqual(self.index["ray"]["unmatched_submissions"], 1)

    def test_join_uses_task_and_actor_ids_and_preserves_observed_hashes(self):
        wrong = self.execution()
        wrong["actor_id"] = "different"
        self.write(self.submission(), wrong)
        merge_events(self.index, self.root)
        self.assertEqual(self.index["edges"], [])
        self.index["warnings"].clear()
        self.write(self.submission(), self.execution())
        merge_events(self.index, self.root)
        edge = self.index["edges"][0]
        self.assertEqual(edge["kind"], "ray_actor")
        self.assertEqual(edge["target"], self.target)
        self.assertEqual(edge["process_ids"], [10, 20])
        self.assertEqual(edge["ray"]["task_ids"], ["task"])
        self.assertTrue(edge["ray"]["worker_confirmed"])
        self.assertEqual(self.index["sources"]["/project/actor.py"]["sha256"], "target")

    def test_constructor_join_requires_creation_task_and_actor_identity(self):
        submitted = dict(self.submission("creation-task"), method="__init__")
        wrong = dict(self.execution("method-task"), method="__init__")
        self.write(submitted, wrong)
        merge_events(self.index, self.root)
        self.assertEqual(self.index["edges"], [])
        correct = dict(self.execution("creation-task"), method="__init__",
                       target=dict(self.target, qualname="Worker.__init__"))
        self.write(submitted, correct)
        merge_events(self.index, self.root)
        self.assertEqual(self.index["edges"][0]["target"]["qualname"], "Worker.__init__")
        self.assertTrue(self.index["edges"][0]["ray"]["worker_confirmed"])

    def test_distinct_actual_wrapper_chains_keep_their_fingerprints(self):
        first = dict(self.execution(), via=[
            {"path": "/external/wrapper_a.py", "line": 2, "column": 0,
             "qualname": "wrapper", "sha256": "wrapper-a"}],
            sources={**self.execution()["sources"], "/external/wrapper_a.py": {"sha256": "wrapper-a"}})
        second = dict(self.execution(pid=30), via=[
            {"path": "/external/wrapper_b.py", "line": 7, "column": 0,
             "qualname": "wrapper", "sha256": "wrapper-b"}],
            sources={**self.execution()["sources"], "/external/wrapper_b.py": {"sha256": "wrapper-b"}})
        self.write(self.submission(), first, first, second)
        merge_events(self.index, self.root)
        self.assertEqual(len(self.index["edges"]), 2)
        self.assertEqual({edge["via"][0]["sha256"] for edge in self.index["edges"]},
                         {"wrapper-a", "wrapper-b"})
        self.assertTrue(all(edge["count"] == 1 for edge in self.index["edges"]))
        self.assertEqual(self.index["ray"]["matched_tasks"], 1)
        self.assertEqual(self.index["sources"]["/external/wrapper_a.py"]["sha256"], "wrapper-a")

    def test_invalid_wrapper_evidence_has_no_navigation_edge(self):
        for via in (None, {}, [None], [{"path": "/wrapper.py", "line": 1}]):
            with self.subTest(via=via):
                index = {"run_id": "run", "sources": {}, "edges": [], "warnings": []}
                self.write(self.submission(), dict(self.execution(), via=via))
                merge_events(index, self.root)
                self.assertEqual(index["edges"], [])
                self.assertEqual(index["ray"]["malformed_event_lines"], 1)

    def test_retries_deduplicate_logical_calls_and_partial_line_is_ignored(self):
        self.write(self.submission(), self.execution(), self.execution(pid=30), trailing='{"partial":')
        merge_events(self.index, self.root)
        self.assertEqual(self.index["edges"][0]["count"], 1)
        self.assertEqual(self.index["edges"][0]["ray"]["worker_process_ids"], [20, 30])
        self.assertEqual(self.index["ray"]["malformed_event_lines"], 1)

    def test_conflicting_source_versions_are_marked_stale(self):
        self.index["sources"]["/project/actor.py"] = {"sha256": "older"}
        self.write(self.submission(), self.execution())
        merge_events(self.index, self.root)
        self.assertTrue(self.index["sources"]["/project/actor.py"]["conflicting"])
        self.assertEqual(self.index["sources"]["/project/actor.py"]["sha256"], "")

    def test_invalid_json_objects_and_nested_metadata_do_not_break_merge(self):
        invalid_sources = dict(self.submission(), sources=[])
        invalid_hash = dict(self.submission(), sources={"/project/main.py": None})
        invalid_location = dict(self.execution(), target=None)
        self.write(invalid_sources, invalid_hash, invalid_location, trailing="null\n[]\n")
        merge_events(self.index, self.root)
        self.assertEqual(self.index["edges"], [])
        self.assertEqual(self.index["ray"]["malformed_event_lines"], 5)


@unittest.skipUnless(sys.version_info >= (3, 11), "requires instruction positions from Python 3.11+")
class RaySubmissionLocationTests(unittest.TestCase):
    def capture(self, method="remote", mutate=None):
        with tempfile.TemporaryDirectory(prefix="callnav-ray-site-") as temporary:
            path = Path(temporary) / "multiline.py"
            path.write_text(
                "result = klass.options(\n"
                "    scheduling_strategy=make_options(\n"
                "        token=1,\n"
                "    ),\n"
                f").{method}(1)\n"
            )
            source = Source(str(path))
            if mutate is not None:
                mutate(source)
            collector = SimpleNamespace(
                lock=threading.RLock(), instructions={},
                path=lambda filename: str(path) if filename == str(path) else None,
                source=lambda filename: source,
            )

            class Proxy:
                def options(self, **kwargs):
                    return self

                def remote(self, *args):
                    return ray_adapter._submission_site(sys._getframe(1), "__init__")[0]

                finish = remote

            namespace = {"klass": Proxy(), "make_options": lambda **kwargs: kwargs}
            with mock.patch.object(ray_adapter, "_collector", collector):
                exec(compile(path.read_text(), str(path), "exec"), namespace)
            caller = namespace["result"]
            line = path.read_text().splitlines()[caller["line"] - 1]
            span = line[caller["column"]:caller["end_column"]]
            return caller, span

    def test_multiline_options_remote_uses_instruction_and_only_remote_token(self):
        caller, span = self.capture()
        self.assertEqual(span, "remote")
        self.assertEqual(caller["line"], 5)
        self.assertEqual(caller["precision"], "instruction")
        self.assertFalse(caller.get("ambiguous"))

    def test_non_remote_chained_method_cannot_use_ray_recovery(self):
        caller, _ = self.capture(method="finish")
        self.assertTrue(caller.get("ambiguous"))
        self.assertEqual(caller["precision"], "line")

    def test_duplicate_ast_remote_anchor_is_rejected(self):
        def duplicate(source):
            call = next(node for node in source.calls
                        if getattr(node.func, "attr", None) == "remote")
            source.calls.append(call)

        caller, _ = self.capture(mutate=duplicate)
        self.assertTrue(caller.get("ambiguous"))
        self.assertEqual(caller["precision"], "line")

    def test_remote_token_with_wrong_call_end_is_rejected(self):
        def alter_end(source):
            call = next(node for node in source.calls
                        if getattr(node.func, "attr", None) == "remote")
            call.end_col_offset += 1

        caller, _ = self.capture(mutate=alter_end)
        self.assertTrue(caller.get("ambiguous"))
        self.assertEqual(caller["precision"], "line")


@unittest.skipUnless(os.environ.get("CALLNAV_TEST_RAY") == "1", "set CALLNAV_TEST_RAY=1 for a real CPU-only Ray cluster")
class RealRayTests(unittest.TestCase):
    def test_dynamic_classes_async_actor_options_and_nested_submission(self):
        temporary = tempfile.TemporaryDirectory(prefix="callnav-ray-test-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        script = root / "actors.py"
        script.write_text(textwrap.dedent('''\
            import asyncio
            import functools
            import json
            import os
            import tempfile
            import ray
            from ray.util.placement_group import placement_group, remove_placement_group
            from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy

            def traced(fn):
                @functools.wraps(fn)
                def wrapper(*args, **kwargs):
                    return fn(*args, **kwargs)
                return wrapper

            def deferred(fn):
                def deferred_func():
                    return None

                @functools.wraps(fn)
                def wrapper(*args, **kwargs):
                    try:
                        return fn(*args, **kwargs)
                    finally:
                        deferred_func()
                return wrapper

            def traced_async(fn):
                @functools.wraps(fn)
                async def async_wrapper(*args, **kwargs):
                    return await fn(*args, **kwargs)
                return async_wrapper

            def skipped(fn):
                @functools.wraps(fn)
                def skip_wrapper(*args, **kwargs):
                    return "skipped"
                return skip_wrapper

            class Base:
                @traced
                def __init__(self):
                    self.initialized = True

            @ray.remote(num_cpus=0)
            class First(Base):
                @traced
                def run(self, value):
                    assert self.initialized
                    return value + 1

            @ray.remote(num_cpus=0)
            class Second:
                @deferred
                def __init__(self):
                    self.initialized = True

                def run(self, value):
                    assert self.initialized
                    return value + 2

                @skipped
                def unused(self):
                    raise AssertionError("this body must never be recorded")

            @ray.remote(num_cpus=0)
            class Async:
                def __init__(self):
                    self.initialized = True

                @traced_async
                async def run(self, value):
                    assert self.initialized
                    await asyncio.sleep(0.01)
                    return value + 3

            def submit(runner):
                return ray.get(runner.run.remote(10))

            ray.init(num_cpus=2, num_gpus=0, include_dashboard=False,
                     object_store_memory=80 * 1024 * 1024,
                     _temp_dir=tempfile.mkdtemp(prefix="cn-ray-"),
                     runtime_env={"env_vars": {"CALLNAV_USER_VALUE": "preserved"}})
            pg = placement_group([{"CPU": 1}])
            ray.get(pg.ready())
            actors = [
                First.options(
                    name="callnav-constructor",
                    get_if_exists=True,
                    scheduling_strategy=PlacementGroupSchedulingStrategy(
                        placement_group=pg,
                        placement_group_bundle_index=0,
                    ),
                ).remote(),
                Second.remote(),
                Async.options(max_concurrency=2).remote(),
            ]
            existing = First.options(name="callnav-constructor", get_if_exists=True).remote()
            assert existing._ray_actor_id == actors[0]._ray_actor_id
            try:
                assert [submit(actor) for actor in actors[:2]] == [11, 12]
                assert ray.get(actors[0].run.options(name="option-test").remote(20)) == 21
                assert ray.get(actors[2].run.remote(30)) == 33
                assert ray.get(actors[1].unused.remote()) == "skipped"
                print("REAL_RAY_SMOKE_OK", flush=True)
            finally:
                for actor in actors:
                    ray.kill(actor)
                remove_placement_group(pg)
                ray.shutdown()
        '''), encoding="utf-8")
        output = root / "recording"
        env = os.environ.copy()
        env.update(PYTHONPATH=str(SOURCE), CUDA_VISIBLE_DEVICES="", RAY_ADDRESS="local",
                   OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
        env.pop("RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES", None)
        process = subprocess.run(
            [sys.executable, "-m", "callnav", "record", "--ray", "--root", str(root),
             "--output", str(output), "--", sys.executable, str(script)],
            env=env, cwd=root, text=True, capture_output=True, timeout=240,
        )
        self.assertEqual(process.returncode, 0, process.stdout + "\n" + process.stderr)
        self.assertIn("REAL_RAY_SMOKE_OK", process.stdout)
        index = json.loads((output / "index.json").read_text())
        edges = [edge for edge in index["edges"] if edge["kind"] == "ray_actor"]
        names = {edge["target"]["qualname"] for edge in edges}
        self.assertTrue({"First.run", "Second.run", "Async.run"}.issubset(names), index.get("ray"))
        nested = [edge for edge in edges if edge["caller"]["qualname"] == "submit"]
        self.assertEqual({edge["target"]["qualname"] for edge in nested}, {"First.run", "Second.run"})
        self.assertEqual(len({edge["caller"]["line"] for edge in nested}), 1)
        for edge in edges:
            self.assertTrue(edge["ray"]["worker_confirmed"])
            self.assertTrue(edge["ray"]["task_ids"])
            self.assertTrue(edge["ray"]["worker_process_ids"])
            caller = edge["caller"]
            if caller["end_line"] == caller["line"]:
                line = script.read_text().splitlines()[caller["line"] - 1]
                span = line[caller["column"]:caller["end_column"]]
                if edge["target"]["qualname"].endswith(".__init__"):
                    self.assertTrue(span.startswith(("First", "Second", "Async", "remote")), span)
                    if span == "remote":
                        self.assertEqual(caller["precision"], "instruction")
                        self.assertFalse(caller.get("ambiguous"))
                else:
                    self.assertTrue(span.startswith(("run", "unused")), span)
                self.assertTrue(span.endswith("remote"), span)
        self.assertEqual(index["sources"][str(script)]["sha256"], hashlib.sha256(script.read_bytes()).hexdigest())
        constructors = [edge for edge in edges if edge["target"]["qualname"].endswith(".__init__")]
        self.assertEqual({edge["target"]["qualname"] for edge in constructors},
                         {"Base.__init__", "Second.__init__", "Async.__init__"})
        self.assertEqual(sum(edge["count"] for edge in constructors), 3)
        inherited = next(edge for edge in constructors
                         if edge["target"]["qualname"] == "Base.__init__")
        self.assertEqual(inherited["caller"]["precision"], "instruction")
        self.assertFalse(inherited["caller"].get("ambiguous"))
        self.assertEqual(inherited["caller"]["line"], inherited["caller"]["end_line"])
        remote_line = script.read_text().splitlines()[inherited["caller"]["line"] - 1]
        self.assertEqual(remote_line[inherited["caller"]["column"]:inherited["caller"]["end_column"]], "remote")
        existing_line = next(i for i, line in enumerate(script.read_text().splitlines(), 1)
                             if line.startswith("existing = "))
        self.assertFalse(any(edge["caller"]["line"] == existing_line for edge in constructors))
        self.assertEqual(index["ray"]["matched_tasks"], 8, index["ray"])
        decorated = [edge for edge in edges if edge["target"]["qualname"] in
                     {"First.run", "Async.run", "Base.__init__", "Second.__init__"}]
        self.assertTrue(all(edge.get("via") for edge in decorated))
        self.assertTrue(any(edge["target"]["qualname"] == "skipped.<locals>.skip_wrapper"
                            for edge in edges))
        self.assertFalse(any(edge["target"]["qualname"] == "Second.unused" for edge in edges))
        for edge in decorated:
            for wrapper in edge["via"]:
                self.assertEqual(wrapper["sha256"], index["sources"][wrapper["path"]]["sha256"])
        self.assertEqual(index["ray"]["unmatched_submissions"], 0, index["ray"])


if __name__ == "__main__":
    unittest.main()

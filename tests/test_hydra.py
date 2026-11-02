"""Factory resolution needs an actual constructor entry, not a config string."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"

_FIXTURE = """
import functools
import importlib

def _call_target(_target_, _partial_, args, kwargs, full_key):
    if kwargs.pop("skip_entry", False):
        return "skipped"
    if _partial_:
        return functools.partial(_target_, *args, **kwargs)
    return _target_(*args, **kwargs)

def _node(config, args=(), overrides=None, full_key=""):
    if isinstance(config, list):
        return [_node(item, full_key=str(i)) for i, item in enumerate(config)]
    if not isinstance(config, dict):
        return config
    recursive = config.get("_recursive_", True)
    values = {key: value for key, value in config.items() if not key.startswith("_")}
    values.update(overrides or {})
    if recursive:
        values = {key: _node(value, full_key=full_key + "." + key)
                  for key, value in values.items()}
    target = config.get("_target_")
    if target is None:
        return values
    if isinstance(target, str):
        module, name = target.rsplit(".", 1)
        target = getattr(importlib.import_module(module), name)
    return _call_target(target, config.get("_partial_", False), args, values, full_key)

def instantiate(config, *args, **kwargs):
    if config is None:
        return None
    return _node(config, args, kwargs)
"""

_TARGETS = """
import functools

class Base:
    def __init__(self, value=1, child=None):
        self.value = value
        self.child = child

class Inherited(Base):
    pass

class Child:
    def __init__(self, value=2):
        self.value = value

class Failing:
    def __init__(self):
        raise ValueError("unchanged failure")

class WrongSignature:
    def __init__(self, required):
        self.value = required

class SkipMeta(type):
    def __call__(cls, *args, **kwargs):
        return "metaclass skipped initializer"

class NeverEntered(metaclass=SkipMeta):
    def __init__(self):
        raise AssertionError("must not enter")

class Wrapped:
    def decorator(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            return function(*args, **kwargs)
        return wrapper

    @decorator
    def __init__(self, value=3):
        self.value = value

def factory(value=4):
    return value

class Callable:
    def __call__(self, value=5):
        return value
"""

_MAIN_FIXTURE = """
import functools
from hydra._internal.main_runner import run
def main(config_path=None, config_name=None, version_base=None):
    def main_decorator(task_function):
        @functools.wraps(task_function)
        def decorated_main(cfg_passthrough=None):
            if cfg_passthrough is not None:
                return task_function(cfg_passthrough)
            return run(task_function)
        return decorated_main
    return main_decorator
"""

_MAIN_RUNNER = """
mode = "run"
helper = None
impostor = None
barrier = None
def run_job(task_function, config):
    if barrier is not None:
        barrier.wait()
    if mode == "impostor":
        return impostor(config)
    return task_function(config)
def run(task_function):
    if mode == "skip":
        return "not executed"
    if mode == "error":
        raise ValueError("before task body")
    if mode == "helper":
        return helper(task_function)
    if mode == "repeat":
        return [run_job(task_function, {"value": value}) for value in (1, 2)]
    return run_job(task_function, {"value": 7})
"""


class HydraTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "project"
        self.library = self.directory / "external"
        self.write(self.root / "targets.py", _TARGETS)
        for name in ("hydra/__init__.py", "hydra/_internal/__init__.py",
                     "hydra/_internal/instantiate/__init__.py"):
            self.write(self.library / name, "")
        self.write(self.library / "hydra/_internal/instantiate/_instantiate2.py", _FIXTURE)
        self.write(self.library / "hydra/main.py", _MAIN_FIXTURE)
        self.write(self.library / "hydra/_internal/main_runner.py", _MAIN_RUNNER)
        self.write(self.library / "hydra/__init__.py", "from .main import main\n")
        self.write(self.library / "hydra/utils.py", """
            from hydra._internal.instantiate._instantiate2 import instantiate
            call = instantiate
        """)

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")

    def run_case(self, body, *, before="", real=False):
        path = self.root / "driver.py"
        self.write(path, textwrap.dedent("""
            import json
            import sys
            import threading
            from callnav.collector import Collector
            from callnav.adapters import hydra as adapter
        """) + textwrap.dedent(before) + "\n" + textwrap.dedent("""
            collector = Collector()
            adapter.install(collector)
            adapter.install(collector)
            threading.setprofile(collector.profile)
            sys.setprofile(collector.profile)
            import hydra.utils
            from hydra.utils import instantiate, call
            import targets
        """) + textwrap.dedent(body) + "\n" + textwrap.dedent("""
            collector.stop()
            print(json.dumps({
                "result": result,
                "errors": collector.errors,
                "edges": list(collector.edges.values()),
                "contexts_released": adapter._origin.get() is None and adapter._pending.get() is None and adapter._main_pending.get() is None,
                "bound_functions": len(collector.frame_functions),
                "hydra_loaded": "hydra.utils" in sys.modules,
            }))
        """))
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("CALLNAV_")}
        environment.update({
            "CALLNAV_ROOT": str(self.root), "CALLNAV_RUN_DIR": str(self.directory / "recording"),
            "CALLNAV_RUN_ID": "hydra-unit", "PYTHONPATH": os.pathsep.join(
                [str(SOURCE), str(self.root)] + ([] if real else [str(self.library)])
            ),
        })
        process = subprocess.run(
            [sys.executable, str(path)], cwd=self.root, env=environment,
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        payload = json.loads(process.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["errors"], 0, payload)
        self.assertTrue(payload["contexts_released"])
        self.assertEqual(payload["bound_functions"], 0)
        return payload

    def factories(self, payload):
        return [edge for edge in payload["edges"] if edge["kind"] == "factory"]

    def test_import_hook_aliases_arguments_results_and_external_fingerprints(self):
        payload = self.run_case("""
            config = {"_target_": "targets.Inherited", "value": 7}
            first = instantiate(config, value=9)
            second = call({"_target_": "targets.Child"}, 11)
            result = [first.value, second.value, config]
        """)
        self.assertEqual(payload["result"], [9, 11, {"_target_": "targets.Inherited", "value": 7}])
        edges = self.factories(payload)
        self.assertEqual([edge["target"]["qualname"] for edge in edges],
                         ["Base.__init__", "Child.__init__"])
        for edge in edges:
            self.assertEqual(edge["caller"]["precision"], "instruction")
            self.assertNotIn("ambiguous", edge["caller"])
            self.assertTrue(edge["via"])
            self.assertTrue(all(item["path"].startswith(str(self.library)) for item in edge["via"]))
            self.assertTrue(all(len(item["sha256"]) == 64 for item in edge["via"]))
            for item in edge["via"]:
                self.assertEqual(item["sha256"], hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest())

    def test_install_does_not_import_hydra_or_require_it_as_a_dependency(self):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("CALLNAV_")}
        environment["PYTHONPATH"] = str(SOURCE)
        process = subprocess.run([
            sys.executable, "-c",
            "import sys\nfrom types import SimpleNamespace\n"
            "from callnav.adapters import hydra\n"
            "hydra.install(SimpleNamespace(add_entry_observer=lambda callback: None))\n"
            "assert not any(name == 'hydra' or name.startswith('hydra.') for name in sys.modules)\n",
        ], env=environment, text=True, capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def test_install_after_hydra_import_and_function_callable_targets(self):
        payload = self.run_case("""
            result = [instantiate({"_target_": targets.factory}, value=8),
                      call({"_target_": targets.Callable()}, value=9)]
        """, before="import hydra.utils\n")
        self.assertEqual(payload["result"], [8, 9])
        self.assertEqual([edge["target"]["qualname"] for edge in self.factories(payload)],
                         ["factory", "Callable.__call__"])

    def test_recursive_children_do_not_replace_the_root_constructor(self):
        payload = self.run_case("""
            config = {"_target_": "targets.Base", "child": {"_target_": "targets.Child"}}
            obj = instantiate(config)
            raw = instantiate(dict(config, _recursive_=False))
            siblings = instantiate({"_target_": "targets.Base", "child": [
                {"_target_": "targets.Child"},
                {"_target_": "targets.Base", "child": {"_target_": "targets.Child"}},
            ]})
            result = [obj.child.value, raw.child, config,
                      [siblings.child[0].value, siblings.child[1].child.value]]
        """)
        self.assertEqual(payload["result"][0], 2)
        self.assertEqual(payload["result"][1], {"_target_": "targets.Child"})
        self.assertEqual(payload["result"][3], [2, 2])
        self.assertEqual([edge["target"]["qualname"] for edge in self.factories(payload)],
                         ["Base.__init__", "Base.__init__", "Base.__init__"])

    def test_partial_missing_arguments_skipped_body_and_metaclass_do_not_infer(self):
        payload = self.run_case("""
            partial = instantiate({"_target_": "targets.Child", "_partial_": True})
            result = [partial().value]
            result.append(instantiate({"_target_": "targets.NeverEntered"}))
            result.append(instantiate({"_target_": "targets.Child", "skip_entry": True}))
            try:
                instantiate({"_target_": "targets.WrongSignature"})
            except TypeError:
                result.append("signature rejected before entry")
            result.append(instantiate(None))
            result.append([obj.value for obj in instantiate([
                {"_target_": "targets.Child"}, {"_target_": "targets.Child"},
            ])])
        """)
        self.assertEqual(payload["result"], [2, "metaclass skipped initializer", "skipped",
                                              "signature rejected before entry", None, [2, 2]])
        self.assertEqual(self.factories(payload), [])

    def test_entered_constructor_exception_is_evidence_and_context_is_reset(self):
        payload = self.run_case("""
            result = []
            try:
                instantiate({"_target_": "targets.Failing"})
            except ValueError as error:
                result.append(str(error))
            result.append(instantiate({"_target_": "targets.Child"}).value)
        """)
        self.assertEqual(payload["result"], ["unchanged failure", 2])
        self.assertEqual([edge["target"]["qualname"] for edge in self.factories(payload)],
                         ["Failing.__init__", "Child.__init__"])

    def test_wrapped_constructor_projects_only_after_the_wrapped_body_enters(self):
        payload = self.run_case("""
            result = instantiate({"_target_": "targets.Wrapped"}).value
        """)
        self.assertEqual(payload["result"], 3)
        factories = self.factories(payload)
        self.assertEqual(len(factories), 1)
        projected = [edge for edge in payload["edges"]
                     if edge["kind"] == "wrapped" and edge["caller"] == factories[0]["caller"]]
        self.assertEqual([edge["target"]["qualname"] for edge in projected], ["Wrapped.__init__"])

    def test_nested_reentrant_factories_have_distinct_source_origins(self):
        self.write(self.root / "nested.py", """
            from hydra.utils import instantiate
            class Outer:
                def __init__(self, depth=1):
                    self.child = instantiate({"_target_": "nested.Outer", "depth": depth - 1}) if depth else None
        """)
        payload = self.run_case("""
            result = instantiate({"_target_": "nested.Outer", "depth": 3}).child.child.child.child
        """)
        self.assertIsNone(payload["result"])
        edges = self.factories(payload)
        self.assertEqual(len(edges), 2)
        self.assertEqual(sorted(edge["count"] for edge in edges), [1, 3])
        self.assertEqual({Path(edge["caller"]["path"]).name for edge in edges},
                         {"driver.py", "nested.py"})

    def test_concurrent_threads_keep_distinct_callers_and_constructor_targets(self):
        payload = self.run_case("""
            from concurrent.futures import ThreadPoolExecutor
            barrier = threading.Barrier(2)
            def first():
                barrier.wait()
                return instantiate({"_target_": "targets.Base"}).value
            def second():
                barrier.wait()
                return instantiate({"_target_": "targets.Child"}).value
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(first), pool.submit(second)]
                result = [future.result() for future in futures]
        """)
        self.assertEqual(payload["result"], [1, 2])
        edges = self.factories(payload)
        self.assertEqual({(edge["caller"]["qualname"], edge["target"]["qualname"]) for edge in edges},
                         {("first", "Base.__init__"), ("second", "Child.__init__")})

    def test_private_hook_reentry_cannot_be_attributed_to_the_outer_factory(self):
        self.write(self.root / "private_reentry.py", """
            from hydra._internal.instantiate._instantiate2 import _call_target
            import targets
            class Outer:
                def __init__(self):
                    self.child = _call_target(targets.Child, False, (), {}, "")
        """)
        payload = self.run_case("""
            result = instantiate({"_target_": "private_reentry.Outer"}).child.value
        """)
        self.assertEqual(payload["result"], 2)
        self.assertEqual([edge["target"]["qualname"] for edge in self.factories(payload)],
                         ["Outer.__init__"])

    def test_deferred_async_task_context_cannot_reuse_a_completed_factory(self):
        self.write(self.root / "deferred.py", """
            import asyncio
            import targets
            pending = []
            async def later():
                await asyncio.sleep(0)
                # Invoke the private boundary after its originating factory has
                # returned. Copied ContextVars must not resurrect that origin.
                from hydra._internal.instantiate._instantiate2 import _call_target
                return _call_target(targets.Child, False, (), {}, "").value
            class Outer:
                def __init__(self):
                    pending.append(asyncio.create_task(later()))
        """)
        payload = self.run_case("""
            import asyncio
            import deferred
            async def main():
                instantiate({"_target_": "deferred.Outer"})
                return await asyncio.gather(*deferred.pending)
            result = asyncio.run(main())
        """)
        self.assertEqual(payload["result"], [2])
        self.assertEqual([edge["target"]["qualname"] for edge in self.factories(payload)],
                         ["Outer.__init__"])

    def test_main_actual_indirect_entry_repeated_runs_and_passthrough(self):
        payload = self.run_case("""
            import hydra
            from hydra._internal import main_runner
            @hydra.main(config_path=None, config_name=None, version_base="1.3")
            def task(config):
                return config["value"]
            assert task.__wrapped__.__name__ == "task"
            result = [task(), task({"value": 8})]
            main_runner.mode = "repeat"
            result.append(task())
        """)
        self.assertEqual(payload["result"], [7, 8, [1, 2]])
        edges = self.factories(payload)
        self.assertEqual([edge["target"]["qualname"] for edge in edges], ["task"] * 3)
        self.assertEqual(sorted(edge["count"] for edge in edges), [1, 1, 2])
        self.assertTrue(all(edge["via"] and all(item["path"].startswith(str(self.library))
            for item in edge["via"]) for edge in edges))

    def test_main_does_not_infer_skipped_failed_or_foreign_helper_invocations(self):
        payload = self.run_case("""
            import hydra
            from hydra._internal import main_runner
            @hydra.main()
            def task(config):
                return config["value"]
            def helper(task_function):
                return task_function({"value": 9})
            result = []
            main_runner.mode = "skip"
            result.append(task())
            main_runner.mode = "error"
            try:
                task()
            except ValueError as error:
                result.append(str(error))
            main_runner.mode = "helper"
            main_runner.helper = helper
            result.append(task())
        """)
        self.assertEqual(payload["result"], ["not executed", "before task body", 9])
        self.assertEqual(self.factories(payload), [])

    def test_main_same_code_impostor_is_not_the_captured_task_object(self):
        payload = self.run_case("""
            import hydra
            from hydra._internal import main_runner
            def make(value):
                def task(config):
                    return value
                return task
            original = make(1)
            impostor = make(2)
            assert original.__code__ is impostor.__code__
            entry = hydra.main()(original)
            main_runner.mode = "impostor"
            main_runner.impostor = impostor
            result = entry()
        """)
        self.assertEqual(payload["result"], 2)
        self.assertEqual(self.factories(payload), [])

    def test_main_nested_contexts_exceptions_and_threads_keep_actual_origins(self):
        payload = self.run_case("""
            import hydra
            from hydra._internal import main_runner
            from concurrent.futures import ThreadPoolExecutor
            @hydra.main()
            def inner(config):
                return config["value"]
            @hydra.main()
            def outer(config):
                return inner({"value": config["value"] + 1})
            result = [outer()]
            @hydra.main()
            def failing(config):
                raise ValueError("entered task body")
            try:
                failing()
            except ValueError as error:
                result.append(str(error))
            main_runner.barrier = threading.Barrier(2)
            def first(): return inner()
            def second(): return outer()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(first), pool.submit(second)]
                result.extend(future.result() for future in futures)
        """)
        self.assertEqual(payload["result"], [8, "entered task body", 7, 8])
        edges = self.factories(payload)
        self.assertEqual({(edge["caller"]["qualname"], edge["target"]["qualname"])
            for edge in edges}, {("<module>", "outer"), ("outer", "inner"),
                ("<module>", "failing"), ("first", "inner"), ("second", "outer")})

    def test_main_recursion_and_copied_async_context_do_not_reuse_old_origins(self):
        payload = self.run_case("""
            import hydra
            import asyncio
            from hydra._internal import main_runner
            @hydra.main()
            def recursive(config):
                if config["depth"]:
                    return recursive({"depth": config["depth"] - 1})
                return 7
            result = [recursive({"depth": 3})]
            pending = []
            async def later():
                await asyncio.sleep(0)
                return main_runner.run_job(spawner.__wrapped__, {"value": 9})
            @hydra.main()
            def spawner(config):
                if config.get("schedule"):
                    pending.append(asyncio.create_task(later()))
                return config["value"]
            async def main():
                value = spawner({"schedule": True, "value": 8})
                return [value, await asyncio.gather(*pending)]
            result.append(asyncio.run(main()))
        """)
        self.assertEqual(payload["result"], [7, [8, [9]]])
        edges = self.factories(payload)
        self.assertEqual({(edge["caller"]["qualname"], edge["target"]["qualname"], edge["count"])
            for edge in edges}, {("<module>", "recursive", 1), ("recursive", "recursive", 3),
                ("main", "spawner", 1)})

    @unittest.skipUnless(os.environ.get("CALLNAV_TEST_HYDRA") == "1", "optional real Hydra integration")
    def test_real_main_cli_route_and_passthrough_confirm_the_actual_task_body(self):
        payload = self.run_case("""
            import hydra
            observed = []
            @hydra.main(config_path=None, config_name=None, version_base="1.3")
            def task(config):
                observed.append("executed")
                return 9
            task()
            result = [observed, task({"value": 1})]
        """, before="import hydra\n", real=True)
        self.assertEqual(payload["result"], [["executed", "executed"], 9])
        edges = self.factories(payload)
        self.assertEqual([edge["target"]["qualname"] for edge in edges], ["task", "task"])
        self.assertTrue(any(len(edge["via"]) >= 4 for edge in edges))

    @unittest.skipUnless(os.environ.get("CALLNAV_TEST_HYDRA") == "1", "optional real Hydra integration")
    def test_real_hydra_recursive_partial_inheritance_exception_and_actual_entry(self):
        payload = self.run_case("""
            from hydra.errors import InstantiationException
            config = {"_target_": "targets.Inherited", "child": {"_target_": "targets.Child"}}
            constructed = instantiate(config)
            deferred = instantiate({"_target_": "targets.Child", "_partial_": True})
            result = [constructed.child.value, deferred().value]
            try:
                instantiate({"_target_": "targets.Failing"})
            except InstantiationException as error:
                result.append("unchanged failure" in str(error))
            try:
                instantiate({"_target_": "targets.WrongSignature"})
            except InstantiationException:
                result.append("signature rejected before entry")
        """, real=True)
        self.assertEqual(payload["result"], [2, 2, True, "signature rejected before entry"])
        self.assertEqual([edge["target"]["qualname"] for edge in self.factories(payload)],
                         ["Base.__init__", "Failing.__init__"])


if __name__ == "__main__":
    unittest.main()

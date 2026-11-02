"""Immediate factory results need exact return and actual-entry evidence."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"


class ReturnedCallableTests(unittest.TestCase):
    def record(self, content):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        root = directory / "project"
        root.mkdir()
        path = root / "driver.py"
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        output = directory / "recording"
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("CALLNAV_")}
        environment["PYTHONPATH"] = str(SOURCE)
        process = subprocess.run([
            sys.executable, "-m", "callnav", "record", "--root", str(root),
            "--output", str(output), "--", sys.executable, str(path),
        ], cwd=root, env=environment, text=True, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        index = json.loads((output / "index.json").read_text())
        self.assertTrue(index["processes"])
        self.assertTrue(all(item["errors"] == 0 for item in index["processes"]), index["processes"])
        self.assertTrue(all(item["status"] == "complete" for item in index["processes"]))
        return index["edges"]

    def target(self, edges, qualname):
        return [edge for edge in edges if edge["target"]["qualname"] == qualname]

    def assert_precise(self, edges):
        self.assertTrue(edges)
        self.assertTrue(all(edge["kind"] == "python" for edge in edges), edges)
        self.assertTrue(all(edge["caller"]["precision"] == "instruction"
                            and not edge["caller"].get("ambiguous") for edge in edges), edges)

    def test_classmethod_registry_result_constructs_the_actual_selected_class(self):
        edges = self.record("""
            class First:
                def __init__(self, value, *, scale=1):
                    self.value = value * scale
            class Second:
                def __init__(self, value, *, scale=1):
                    self.value = value + scale
            class Registry:
                implementations = {"first": First, "second": Second}
                @classmethod
                def get(cls, backend):
                    return cls.implementations[backend]
            registry = Registry
            args = (3,)
            kwargs = {"scale": 2}
            for backend in ("first", "second"):
                obj = registry.get(backend)(*args, **kwargs)
                assert obj.value in (5, 6)
        """)
        targets = self.target(edges, "First.__init__") + self.target(edges, "Second.__init__")
        self.assertEqual(len(targets), 2)
        self.assert_precise(targets)
        self.assertEqual({edge["caller"]["column"] for edge in targets}, {10})
        self.assertEqual({edge["caller"]["end_column"] for edge in targets}, {31})
        self.assertEqual(len({edge["caller"]["offset"] for edge in targets}), 1)

    def test_returned_function_bound_method_partial_and_callable_enter_precisely(self):
        edges = self.record("""
            from functools import partial
            def work(value=1): return value
            class Worker:
                def method(self, value=2): return value
            class Callable:
                def __call__(self, value=3): return value
            worker = Worker()
            choices = [work, worker.method, partial(work, 4), Callable()]
            def choose(index): return choices[index]
            assert choose(0)() == 1
            assert choose(1)() == 2
            assert choose(2)() == 4
            assert choose(3)() == 3
        """)
        targets = (self.target(edges, "work") + self.target(edges, "Worker.method")
                   + self.target(edges, "Callable.__call__"))
        self.assertEqual(len(targets), 4)
        self.assert_precise(targets)

    def test_intervening_argument_function_or_c_call_invalidates_return_binding(self):
        edges = self.record("""
            def work(value): return value
            def choose(): return work
            def argument(): return 7
            assert choose()(argument()) == 7
            assert choose()(len([1, 2])) == 2
        """)
        work = self.target(edges, "work")
        self.assertEqual(len(work), 2)
        self.assertTrue(all(edge["caller"].get("ambiguous") for edge in work), work)
        self.assertTrue(all(edge["caller"]["reason"] == "unresolved_callee" for edge in work), work)

    def test_factory_errors_and_argument_rejection_do_not_invent_body_entries(self):
        edges = self.record("""
            class Never:
                def __init__(self, required):
                    raise AssertionError("not entered")
            def never(required):
                raise AssertionError("not entered")
            def failed_factory():
                raise ValueError("factory failed")
            def choose_class(): return Never
            def choose_function(): return never
            try:
                failed_factory()()
            except ValueError as error:
                assert str(error) == "factory failed"
            for factory in (choose_class, choose_function):
                try:
                    factory()()
                except TypeError:
                    pass
                else:
                    raise AssertionError("missing argument accepted")
        """)
        self.assertEqual(self.target(edges, "Never.__init__"), [])
        self.assertEqual(self.target(edges, "never"), [])
        self.assertEqual(len(self.target(edges, "failed_factory")), 1)

    def test_custom_metaclass_without_initializer_entry_never_claims_initializer(self):
        edges = self.record("""
            class Meta(type):
                def __call__(cls):
                    return 23
            class Never(metaclass=Meta):
                def __init__(self):
                    raise AssertionError("initializer must not execute")
            def choose(): return Never
            assert choose()() == 23
        """)
        self.assertEqual(self.target(edges, "Never.__init__"), [])
        self.assert_precise(self.target(edges, "Meta.__call__"))

    def test_entered_constructor_failure_keeps_actual_destination(self):
        edges = self.record("""
            class Failing:
                def __init__(self):
                    raise ValueError("body executed")
            def choose(): return Failing
            try:
                choose()()
            except ValueError as error:
                assert str(error) == "body executed"
        """)
        self.assert_precise(self.target(edges, "Failing.__init__"))

    def test_returned_native_callable_does_not_turn_callback_into_direct_entry(self):
        edges = self.record("""
            def key(value): return -value
            def choose(): return sorted
            assert choose()([1, 2], key=key) == [2, 1]
        """)
        callbacks = self.target(edges, "key")
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0]["count"], 2)
        self.assertEqual(callbacks[0]["kind"], "callback")
        self.assertTrue(callbacks[0]["caller"].get("ambiguous"))
        self.assertEqual(callbacks[0]["caller"]["reason"], "native_callback")


if __name__ == "__main__":
    unittest.main()

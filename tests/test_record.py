import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / "src"


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "project"
        self.root.mkdir()
        self.output = self.directory / "recording"

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, content):
        path = self.root / name
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return path

    def record(self, command, extra_env=None, flags=()):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SOURCE)
        env.update(extra_env or {})
        process = subprocess.run(
            [sys.executable, "-m", "callnav", "record", "--root", str(self.root),
             "--output", str(self.output), *flags, "--", *command],
            cwd=self.root, env=env, text=True, capture_output=True,
        )
        index_path = self.output / "index.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else None
        return process, index

    def test_registry_observes_multiple_targets_counts_and_hash(self):
        path = self.write("run.py", """
            def first(x):
                return x + 1
            def second(x):
                return x * 2
            registry = {"a": first, "b": second}
            for key in ["a", "b", "a"]:
                fn = registry[key]
                fn(3)
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual({edge["target"]["qualname"] for edge in index["edges"]}, {"first", "second"})
        self.assertEqual({edge["target"]["qualname"]: edge["count"] for edge in index["edges"]},
                         {"first": 2, "second": 1})
        self.assertEqual(index["sources"][str(path)]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(index["processes"][0]["status"], "complete")
        path.write_text(path.read_text() + "# changed\n")
        self.assertNotEqual(index["sources"][str(path)]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_same_line_calls_are_precise_or_truthfully_ambiguous(self):
        path = self.write("run.py", """
            def first(): return 1
            def second(): return 2
            first(); second()
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(index["edges"]), 2)
        if sys.version_info >= (3, 11):
            self.assertEqual({edge["caller"]["column"] for edge in index["edges"]}, {0, 9})
            self.assertTrue(all(edge["caller"]["precision"] == "instruction" for edge in index["edges"]))
        else:
            self.assertTrue(all(edge["caller"].get("ambiguous") for edge in index["edges"]))

    def test_nested_calls_distinguish_callee_expressions(self):
        path = self.write("run.py", """
            def inner(): return 1
            def outer(x): return x
            outer(inner())
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        if sys.version_info >= (3, 11):
            locations = {edge["target"]["qualname"]: edge["caller"]["column"] for edge in index["edges"]}
            self.assertEqual(locations, {"inner": 6, "outer": 0})

    def test_thread_and_child_process_via_shell(self):
        self.write("worker.py", """
            def child(): pass
            child()
        """)
        self.write("driver.py", """
            import subprocess
            import sys
            import threading
            def target(): pass
            def launch():
                target()
            thread = threading.Thread(target=launch)
            thread.start()
            thread.join()
            subprocess.run([sys.executable, "worker.py"], check=True)
        """)
        script = self.write("run.sh", "python driver.py\n")
        process, index = self.record(["bash", str(script)])
        self.assertEqual(process.returncode, 0, process.stderr)
        names = {edge["target"]["qualname"] for edge in index["edges"]}
        self.assertTrue({"target", "child"}.issubset(names), names)
        self.assertGreaterEqual(len(index["processes"]), 2)

    def test_multiprocessing_spawn_final_flush(self):
        path = self.write("run.py", """
            import multiprocessing
            def inner(): pass
            def child():
                inner()
            if __name__ == "__main__":
                process = multiprocessing.get_context("spawn").Process(target=child)
                process.start()
                process.join()
                assert process.exitcode == 0
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        inner = next(edge for edge in index["edges"] if edge["target"]["qualname"] == "inner")
        # The auxiliary resource_tracker may outlive the submitted driver.
        children = [p for p in index["processes"] if p["pid"] in inner["process_ids"]]
        self.assertTrue(children)
        self.assertTrue(all(p["status"] == "complete" for p in children), children)

    def test_exception_still_flushes_and_returns_status(self):
        path = self.write("run.py", """
            def failure():
                raise RuntimeError("expected")
            failure()
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(index["edges"][0]["target"]["qualname"], "failure")
        self.assertEqual(index["processes"][0]["status"], "complete")

    def test_deferred_coroutines_resumes_and_generators_are_excluded(self):
        path = self.write("run.py", """
            import asyncio
            def sync(): pass
            async def task():
                sync()
                await asyncio.sleep(0)
                sync()
            def gen():
                yield 1
            async def main():
                pending = task()
                await pending
            asyncio.run(main())
            for _ in gen(): pass
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        targets = {edge["target"]["qualname"] for edge in index["edges"]}
        self.assertEqual(targets, {"sync"})
        self.assertGreater(index["processes"][0]["excluded_resume_events"], 0)

    def test_immediate_await_maps_only_initial_entry(self):
        path = self.write("run.py", """
            import asyncio
            async def custom():
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            async def main():
                fn = custom
                for _ in range(2):
                    await fn()
            asyncio.run(main())
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        edges = [edge for edge in index["edges"] if edge["target"]["qualname"].endswith("custom")]
        self.assertEqual(len(edges), 1, index["edges"])
        edge = edges[0]
        self.assertEqual(edge["kind"], "await")
        self.assertEqual(edge["count"], 2)
        self.assertEqual(edge["caller"]["line"], 8)
        self.assertEqual(edge["caller"]["column"], 14)
        self.assertFalse(edge["caller"].get("ambiguous"))

    def test_same_line_deferred_await_does_not_borrow_immediate_call(self):
        path = self.write("run.py", """
            import asyncio
            async def custom(): await asyncio.sleep(0)
            async def main():
                pending = custom()
                await pending; await custom()
            asyncio.run(main())
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        targets = [edge for edge in index["edges"] if edge["target"]["qualname"].endswith("custom")]
        if sys.version_info >= (3, 11):
            self.assertEqual(len(targets), 1, targets)
            self.assertEqual(targets[0]["count"], 1)
            self.assertEqual(targets[0]["caller"]["column"], 25)
        else:
            self.assertEqual(targets, [])

    def test_await_cache_does_not_trust_a_later_coroutine_factory(self):
        path = self.write("run.py", """
            import asyncio
            async def custom(): await asyncio.sleep(0)
            def factory(): return custom()
            async def main():
                for fn in [custom, factory]:
                    await fn()
            asyncio.run(main())
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        direct = [edge for edge in index["edges"] if edge["kind"] == "await"]
        self.assertEqual(len(direct), 1, direct)
        self.assertEqual(direct[0]["count"], 1)

    def test_existing_sitecustomize_is_executed(self):
        custom = self.directory / "custom"
        custom.mkdir()
        additional = self.directory / "additional"
        additional.mkdir()
        (additional / "user_extra.py").write_text("VALUE = 42\n")
        (custom / "sitecustomize.py").write_text(
            'import os, sys\nos.environ["USER_CUSTOM_RAN"] = "yes"\n'
            + "sys.path.insert(0, " + repr(str(additional)) + ")\n"
        )
        path = self.write("run.py", 'import os, user_extra\nassert user_extra.VALUE == 42\n'
                          'assert os.environ.get("USER_CUSTOM_RAN") == "yes"\n')
        process, index = self.record([sys.executable, str(path)],
                                     {"PYTHONPATH": str(SOURCE) + os.pathsep + str(custom)})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(index["processes"]), 1)

    def test_module_launch_and_decorated_definition(self):
        self.write("worker.py", """
            def decorate(fn):
                return fn
            @decorate
            def implementation(): pass
            implementation()
        """)
        process, index = self.record([sys.executable, "-m", "worker"])
        self.assertEqual(process.returncode, 0, process.stderr)
        edge = next(edge for edge in index["edges"] if edge["target"]["qualname"] == "implementation")
        self.assertEqual(edge["target"]["line"], 4)

    def test_utf16_columns(self):
        path = self.write("run.py", """
            def work(): pass
            label = "😀"; work()
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        if sys.version_info >= (3, 11):
            self.assertEqual(index["edges"][0]["caller"]["column"], 14)

    def test_native_callbacks_do_not_override_outer_callee(self):
        path = self.write("run.py", """
            def score(x): return x
            sorted([3, 1, 2], key=score)
            list(map(score, [3, 1, 2]))
            values = [3, 1, 2]
            values.sort(key=score)
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        scores = [edge for edge in index["edges"] if edge["target"]["qualname"] == "score"]
        self.assertEqual(sum(edge["count"] for edge in scores), 9)
        if sys.version_info >= (3, 11):
            self.assertTrue(all(edge["kind"] == "callback" for edge in scores), scores)
        self.assertTrue(all(edge["caller"].get("ambiguous") for edge in scores), scores)

    def test_python_method_class_callable_and_registry_remain_navigable(self):
        path = self.write("run.py", """
            class Worker:
                def __init__(self): pass
                def method(self): pass
                def __call__(self): pass
            def work(): pass
            worker = Worker()
            worker.method()
            worker()
            registry = {"a": work}
            registry["a"]()
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        direct = [edge for edge in index["edges"] if edge["kind"] == "python"]
        self.assertEqual(len(direct), 4, index["edges"])
        self.assertTrue(all(not edge["caller"].get("ambiguous") for edge in direct), direct)

    def test_unresolved_callee_is_observed_but_not_falsely_navigable(self):
        path = self.write("run.py", """
            from functools import partial
            def work(): pass
            def choose(): return work
            choose()()
            indirect = partial(work)
            indirect()
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        work = [edge for edge in index["edges"] if edge["target"]["qualname"] == "work"]
        self.assertEqual(len(work), 2)
        unknown = next(edge for edge in work if edge["caller"]["line"] == 4)
        partial_edge = next(edge for edge in work if edge["caller"]["line"] == 6)
        self.assertTrue(unknown["caller"].get("ambiguous"), unknown)
        self.assertFalse(partial_edge["caller"].get("ambiguous"), partial_edge)
        choose = next(edge for edge in index["edges"] if edge["target"]["qualname"] == "choose")
        if sys.version_info >= (3, 11):
            self.assertFalse(choose["caller"].get("ambiguous"), choose)

    def test_exact_partials_support_bound_methods_but_subclass_overrides_remain_real(self):
        path = self.write("run.py", """
            from functools import partial
            class Worker:
                def method(self, value): return value
            def work(): return 1
            def overridden(): return 2
            class CustomPartial(partial):
                def __call__(self):
                    return overridden()
            callback = partial(partial(Worker().method, 1))
            callback()
            CustomPartial(work)()
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        method = next(e for e in index["edges"] if e["target"]["qualname"] == "Worker.method")
        self.assertFalse(method["caller"].get("ambiguous"), method)
        self.assertNotIn("work", {e["target"]["qualname"] for e in index["edges"]})
        self.assertIn("CustomPartial.__call__", {e["target"]["qualname"] for e in index["edges"]})

    def test_partial_immediate_await_records_real_coroutine_entry(self):
        path = self.write("run.py", """
            import asyncio
            from functools import partial
            async def actual(value): return value
            async def main():
                fn = partial(actual, 1)
                await fn()
            asyncio.run(main())
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        edge = next(e for e in index["edges"] if e["target"]["qualname"] == "actual")
        self.assertEqual(edge["kind"], "await")
        self.assertFalse(edge["caller"].get("ambiguous"), edge)

    def test_decorator_projection_requires_actual_body_and_retains_unforwarded_calls(self):
        path = self.write("run.py", """
            from functools import wraps
            def cleanup(): pass
            def decorate(fn):
                @wraps(fn)
                def wrapper(active):
                    try:
                        if active:
                            return fn(active)
                    finally:
                        cleanup()
                return wrapper
            @decorate
            def actual(active): pass
            for active in [True, False, True]:
                actual(active)
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        projected = [e for e in index["edges"] if e["kind"] == "wrapped"]
        self.assertEqual(len(projected), 1, projected)
        self.assertEqual(projected[0]["target"]["qualname"], "actual")
        self.assertEqual(projected[0]["count"], 2)
        self.assertEqual(projected[0]["caller"]["line"], 15)
        via = projected[0]["via"]
        self.assertEqual(len(via), 1)
        self.assertEqual(via[0]["sha256"], index["sources"][str(path)]["sha256"])
        raw = next(e for e in index["edges"] if e["caller"]["line"] == 15 and e["kind"] == "python")
        self.assertEqual(raw["count"], 3)
        self.assertEqual(raw["wrapper_forwarded_count"], 2)
        self.assertFalse(any(e["target"]["qualname"] == "cleanup" for e in projected))

    def test_decorator_never_infers_unexecuted_wrapped_body(self):
        path = self.write("run.py", """
            from functools import wraps
            def helper(): pass
            def decorate(fn):
                @wraps(fn)
                def wrapper(*args, **kwargs):
                    helper()
                return wrapper
            @decorate
            def actual(): raise AssertionError("must not execute")
            actual()
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([e for e in index["edges"] if e["kind"] == "wrapped"], [])
        self.assertNotIn("actual", {e["target"]["qualname"] for e in index["edges"]})

    def test_same_wrapper_code_preserves_actual_per_instance_targets(self):
        path = self.write("run.py", """
            from functools import wraps
            def decorate(fn):
                @wraps(fn)
                def wrapper(*args, **kwargs):
                    return fn(*args, **kwargs)
                return wrapper
            @decorate
            def first(): pass
            @decorate
            def second(): pass
            for fn in [first, second, first]:
                fn()
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        projected = [e for e in index["edges"] if e["kind"] == "wrapped"]
        self.assertEqual({e["target"]["qualname"]: e["count"] for e in projected}, {"first": 2, "second": 1})
        raw = next(e for e in index["edges"] if e["caller"]["line"] == 12 and e["kind"] == "python")
        self.assertEqual(raw["wrapper_forwarded_count"], 3)
        self.assertEqual(raw["count"], 3)

    def test_external_wrappers_bridge_without_collecting_unrelated_library_callbacks(self):
        external = self.directory / "library"
        external.mkdir()
        library = external / "decorators.py"
        library.write_text(textwrap.dedent("""
            def decorate(fn):
                def wrapper(*args, **kwargs):
                    return fn(*args, **kwargs)
                return wrapper
            def ordinary_callback(fn):
                fn()
        """).lstrip())
        path = self.write("run.py", """
            from decorators import decorate, ordinary_callback
            @decorate
            @decorate
            def actual(): pass
            def unrelated(): pass
            actual()
            ordinary_callback(unrelated)
        """)
        process, index = self.record(
            [sys.executable, str(path)],
            {"PYTHONPATH": str(SOURCE) + os.pathsep + str(external)},
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        projected = [e for e in index["edges"] if e["kind"] == "wrapped"]
        self.assertEqual(len(projected), 1, projected)
        self.assertEqual(projected[0]["target"]["qualname"], "actual")
        self.assertEqual(len(projected[0]["via"]), 2)
        self.assertEqual(projected[0]["caller"]["line"], 6)
        self.assertIn(str(library), index["sources"])
        self.assertNotIn("unrelated", {e["target"]["qualname"] for e in index["edges"]})
        self.assertTrue(all(e["target"]["path"] == str(path) for e in index["edges"]))

    def test_multiple_unmarked_closure_callables_are_not_guessed(self):
        path = self.write("run.py", """
            def first(): pass
            def second(): pass
            def decorate(fn, helper):
                def wrapper(*args, **kwargs):
                    fn(*args, **kwargs)
                    helper(*args, **kwargs)
                return wrapper
            decorated = decorate(first, second)
            decorated()
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([e for e in index["edges"] if e["kind"] == "wrapped"], [])

    def test_async_wrapper_projection_survives_suspension_and_does_not_count_resumes(self):
        path = self.write("run.py", """
            import asyncio
            from functools import wraps
            def decorate(fn):
                @wraps(fn)
                async def wrapper(*args, **kwargs):
                    await asyncio.sleep(0)
                    return await fn(*args, **kwargs)
                return wrapper
            @decorate
            async def actual():
                await asyncio.sleep(0)
            async def main():
                await actual()
            asyncio.run(main())
            from callnav.collector import _collector
            assert not getattr(_collector, "relays", {})
            assert not getattr(_collector, "frame_functions", {})
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        projected = [e for e in index["edges"] if e["kind"] == "wrapped"]
        self.assertEqual(len(projected), 1, projected)
        self.assertEqual(projected[0]["target"]["qualname"], "actual")
        self.assertEqual(projected[0]["count"], 1)
        raw = next(e for e in index["edges"] if e["caller"]["line"] == 13 and e["kind"] == "await")
        self.assertEqual(raw["wrapper_forwarded_count"], 1)

    def test_external_wrapper_projection_stops_at_eight_hops(self):
        external = self.directory / "library"
        external.mkdir()
        (external / "deep.py").write_text(
            "def decorate(fn):\n"
            "    def wrapper(*args, **kwargs):\n"
            "        return fn(*args, **kwargs)\n"
            "    return wrapper\n"
        )
        path = self.write("run.py", """
            from deep import decorate
            def actual(): pass
            fn = actual
            for _ in range(9):
                fn = decorate(fn)
            fn()
        """)
        process, index = self.record(
            [sys.executable, str(path)],
            {"PYTHONPATH": str(SOURCE) + os.pathsep + str(external)},
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([e for e in index["edges"] if e["kind"] == "wrapped"], [])
        self.assertEqual(index["processes"][0]["errors"], 0)

    def test_callable_type_detection_does_not_read_user_class_property(self):
        path = self.write("run.py", """
            from functools import partial
            from callnav.collector import _function
            events = []
            class Meta(type):
                def __eq__(cls, other):
                    events.append("metaclass_eq")
                    return cls is other
                __hash__ = type.__hash__
            class Callback(metaclass=Meta):
                @property
                def __class__(self):
                    events.append("__class__")
                    return type(self)
                def __call__(self): pass
            callback = Callback()
            assert _function(callback) is None
            assert _function(partial(callback)) is None
            callback()
            class Holder:
                method = callback
            holder = Holder()
            holder.method()
            assert events == [], events
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        actual = [e for e in index["edges"] if e["target"]["qualname"] == "Callback.__call__"]
        self.assertEqual(sum(e["count"] for e in actual), 2)
        self.assertTrue(all(not e["caller"].get("ambiguous") for e in actual), actual)

    def test_static_callee_descriptor_does_not_gain_extra_property_reads(self):
        path = self.write("run.py", """
            events = []
            def actual(): pass
            class CallbackDescriptor:
                @property
                def __class__(self):
                    events.append("__class__")
                    return type(self)
                def __get__(self, instance, owner):
                    events.append("__get__")
                    return actual
            class Holder:
                method = CallbackDescriptor()
            holder = Holder()
            holder.method()
            assert events == ["__get__"], events
        """)
        process, index = self.record([sys.executable, str(path)])
        self.assertEqual(process.returncode, 0, process.stderr)
        actual = next(e for e in index["edges"] if e["target"]["qualname"] == "actual")
        self.assertTrue(actual["caller"].get("ambiguous"), actual)

    def test_iterations_without_adapter_rejected_before_launch(self):
        path = self.write("run.py", 'raise RuntimeError("must not launch")\n')
        process, index = self.record([sys.executable, str(path)], flags=["--iterations", "1"])
        self.assertEqual(process.returncode, 2)
        self.assertIsNone(index)
        self.assertIn("requires a framework adapter", process.stderr)

    def test_latest_index_tracks_newest_completed_recording(self):
        path = self.write("run.py", "def work(): pass\nwork()\n")
        first_process, first = self.record([sys.executable, str(path)])
        self.assertEqual(first_process.returncode, 0, first_process.stderr)
        latest = self.root / ".callnav" / "latest.json"
        self.assertEqual(json.loads(latest.read_text())["run_id"], first["run_id"])
        self.output = self.directory / "second"
        second_process, second = self.record([sys.executable, str(path)])
        self.assertEqual(second_process.returncode, 0, second_process.stderr)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(json.loads(latest.read_text())["run_id"], second["run_id"])


if __name__ == "__main__":
    unittest.main()

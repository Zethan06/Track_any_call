"""Coroutine-creation provenance must survive adversarial binding changes."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


class ScheduledTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "project"
        self.root.mkdir()
        self.output = self.directory / "recording"

    def tearDown(self):
        self.temporary.cleanup()

    def record(self, content):
        path = self.root / "run.py"
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT / "src")
        process = subprocess.run(
            [sys.executable, "-m", "callnav", "record", "--root", str(self.root),
             "--output", str(self.output), "--", sys.executable, str(path)],
            cwd=self.root, env=env, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        index = json.loads((self.output / "index.json").read_text())
        self.assertTrue(all(item["errors"] == 0 for item in index["processes"]), index["processes"])
        return path, index

    @staticmethod
    def scheduled(index):
        return [edge for edge in index["edges"] if edge["kind"] == "scheduled"]

    def test_decorated_native_body_multiline_utf16_and_alias(self):
        path, index = self.record('''
            import asyncio
            import functools
            from asyncio import create_task as launch
            def decorate(function):
                @functools.wraps(function)
                async def wrapped(*args, **kwargs):
                    return await function(*args, **kwargs)
                return wrapped
            @decorate
            async def actual(options):
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                return options
            async def main():
                options = {"value": 1}
                # The caller columns use UTF16, including the astral character.
                text = "😀"; task = launch(
                    actual(
                        options=options.copy(),
                    ),
                    name="capture",
                )
                assert await task == options
            asyncio.run(main())
        ''')
        scheduled = self.scheduled(index)
        self.assertEqual(len(scheduled), 1, scheduled)
        edge = scheduled[0]
        self.assertEqual(edge["target"]["qualname"], "decorate.<locals>.wrapped")
        self.assertEqual(edge["count"], 1)
        self.assertEqual(edge["wrapper_forwarded_count"], 1)
        self.assertEqual(edge["caller"]["precision"], "instruction")
        self.assertEqual(edge["caller"]["column"], 8)
        projected = [item for item in index["edges"] if item["kind"] == "wrapped"
                     and item["caller"] == edge["caller"]]
        self.assertEqual([item["target"]["qualname"] for item in projected], ["actual"])
        self.assertEqual(projected[0]["target"]["path"], str(path))

    def test_factories_rebinding_and_precreated_coroutines_have_no_edges(self):
        _, index = self.record('''
            import asyncio
            async def actual():
                await asyncio.sleep(0)
            def factory():
                return actual()
            def rebinding_factory():
                global dynamic
                dynamic = actual
                return actual()
            def rebinding_existing_factory():
                global dynamic
                dynamic = actual
                return existing
            async def main():
                global dynamic, existing
                await asyncio.create_task(factory())
                dynamic = rebinding_factory
                await asyncio.create_task(dynamic())
                existing = actual()
                dynamic = rebinding_existing_factory
                await asyncio.create_task(dynamic())
                previous = actual()
                await asyncio.create_task(previous)
            asyncio.run(main())
        ''')
        self.assertEqual(self.scheduled(index), [])

    def test_global_load_ignores_injected_local_bindings(self):
        _, index = self.record('''
            import asyncio
            async def actual():
                await asyncio.sleep(0)
            def dynamic():
                return existing
            async def main():
                global existing
                existing = actual()
                # LOAD_GLOBAL reads globals even when locals() has a spoofed
                # async binding. Snapshotting a source expression gets it wrong.
                locals()["dynamic"] = actual
                await asyncio.create_task(dynamic())
            asyncio.run(main())
        ''')
        self.assertEqual(self.scheduled(index), [])

    def test_native_code_changed_during_arguments_tracks_actual_new_body(self):
        _, index = self.record('''
            import asyncio
            async def actual(value=None):
                raise AssertionError("old code must not execute")
            async def alternate(value=None):
                await asyncio.sleep(0)
            def change_code():
                actual.__code__ = alternate.__code__
            async def main():
                await asyncio.create_task(actual(change_code()))
            asyncio.run(main())
        ''')
        edge, = self.scheduled(index)
        self.assertEqual(edge["target"]["qualname"], "alternate")

    def test_arguments_rebind_loaded_name_and_mutate_function_code(self):
        _, index = self.record('''
            import asyncio
            async def actual(value=None):
                await asyncio.sleep(0)
            async def alternate(value=None):
                await asyncio.sleep(0)
            def change_binding():
                global dynamic
                dynamic = alternate
            def factory(value=None):
                return existing
            def change_code():
                actual.__code__ = factory.__code__
            async def main():
                global dynamic, existing
                dynamic = actual
                await asyncio.create_task(dynamic(change_binding()))
                existing = alternate()
                await asyncio.create_task(actual(change_code()))
            asyncio.run(main())
        ''')
        scheduled = self.scheduled(index)
        self.assertEqual(len(scheduled), 1, scheduled)
        self.assertEqual(scheduled[0]["target"]["qualname"], "actual")
        self.assertEqual(scheduled[0]["caller"]["column"], 30)

    def test_callable_load_survives_suspended_argument_and_name_rebinding(self):
        _, index = self.record('''
            import asyncio
            async def actual(value):
                await asyncio.sleep(0)
            async def alternate(value):
                raise AssertionError("callee was loaded before argument evaluation")
            async def argument():
                global dynamic
                await asyncio.sleep(0)
                dynamic = alternate
                return 7
            async def main():
                global dynamic
                dynamic = actual
                await asyncio.create_task(dynamic(await argument()))
            asyncio.run(main())
        ''')
        edge, = self.scheduled(index)
        self.assertEqual(edge["target"]["qualname"], "actual")
        self.assertEqual(edge["count"], 1)

    def test_close_throw_and_reused_frame_ids_release_creation_state(self):
        _, index = self.record('''
            import asyncio
            import gc
            from callnav.collector import _collector
            class Pause:
                def __await__(self):
                    yield None
                    return 7
            async def child(value):
                await asyncio.sleep(0)
            async def first():
                await asyncio.create_task(child(await Pause()))
            async def second():
                await asyncio.create_task(child(await Pause()))
            def generator():
                asyncio.create_task(child((yield None)))
            for function in (first, generator):
                for mode in ("close", "throw"):
                    for _ in range(100):
                        call = function()
                        assert call.send(None) is None
                        if mode == "close":
                            call.close()
                        else:
                            try:
                                call.throw(RuntimeError("terminate"))
                            except RuntimeError:
                                pass
                        del call
                        gc.collect()
                        assert not _collector.scheduled.active
                        assert not _collector.scheduled.pending
            # Both async functions have identical instruction shapes. Reused
            # closed frame IDs must never carry first's caller into second.
            for _ in range(10):
                asyncio.run(second())
            assert not _collector.scheduled.active
            assert not _collector.scheduled.pending
        ''')
        scheduled = self.scheduled(index)
        self.assertEqual(len(scheduled), 1, scheduled)
        self.assertEqual(scheduled[0]["caller"]["qualname"], "second")
        self.assertEqual(scheduled[0]["target"]["qualname"], "child")
        self.assertEqual(scheduled[0]["count"], 10)

    def test_handled_throw_keeps_a_caller_that_suspends_again(self):
        _, index = self.record('''
            import asyncio
            import threading
            from callnav.collector import _collector
            class Pause:
                def __await__(self):
                    yield None
            async def child():
                pass
            async def planned():
                try:
                    await Pause()
                except ValueError:
                    await Pause()
                await asyncio.create_task(child())
            call = planned()
            key = (threading.get_ident(), id(call.cr_frame))
            assert call.send(None) is None
            assert key in _collector.scheduled.active
            assert call.throw(ValueError("handled")) is None
            assert key in _collector.scheduled.active
            call.close()
            assert key not in _collector.scheduled.active
            assert not _collector.scheduled.pending
        ''')
        self.assertEqual(self.scheduled(index), [])

    def test_cancelled_closed_never_run_and_fake_scheduler_have_no_edges(self):
        _, index = self.record('''
            import asyncio
            async def actual():
                raise AssertionError("body should not run")
            def create_task(coroutine):
                coroutine.close()
            async def main():
                task = asyncio.create_task(actual())
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                closed = actual()
                closed.close()
                create_task(actual())
            asyncio.run(main())
        ''')
        self.assertEqual(self.scheduled(index), [])

    def test_nested_tasks_repeated_site_and_suspend_counts(self):
        _, index = self.record('''
            import asyncio
            async def leaf():
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            async def parent():
                await asyncio.create_task(leaf())
            async def main():
                launch = asyncio.create_task
                for _ in range(3):
                    await launch(parent())
            asyncio.run(main())
        ''')
        observed = {edge["target"]["qualname"]: edge["count"] for edge in self.scheduled(index)}
        self.assertEqual(observed, {"parent": 3, "leaf": 3})

    def test_local_closure_bound_method_and_partial_name_loads(self):
        _, index = self.record('''
            import asyncio
            import functools
            class Worker:
                async def run(self, value):
                    await asyncio.sleep(0)
            def build():
                closed = functools.partial(Worker().run, 1)
                async def main():
                    local = closed
                    await asyncio.create_task(local())
                    await asyncio.create_task(closed())
                return main
            asyncio.run(build()())
        ''')
        scheduled = self.scheduled(index)
        self.assertEqual(len(scheduled), 2, scheduled)
        self.assertEqual([edge["target"]["qualname"] for edge in scheduled], ["Worker.run"] * 2)

    @unittest.skipIf(sys.version_info < (3, 12), "eager tasks require Python 3.12+")
    def test_eager_scheduler_observes_entry_before_create_task_returns(self):
        _, index = self.record('''
            import asyncio
            async def actual():
                return 7
            async def main():
                asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
                task = asyncio.create_task(actual())
                assert task.done()
                assert await task == 7
            asyncio.run(main())
        ''')
        edge, = self.scheduled(index)
        self.assertEqual(edge["target"]["qualname"], "actual")
        self.assertEqual(edge["count"], 1)

    def test_same_line_utf16_column_is_inner_callee(self):
        path, index = self.record('''
            import asyncio
            async def actual():
                await asyncio.sleep(0)
            async def main():
                text = "😀"; await asyncio.create_task(actual())
            asyncio.run(main())
        ''')
        edge, = self.scheduled(index)
        line = path.read_text().splitlines()[edge["caller"]["line"] - 1]
        column = len(line[:line.index("actual()")].encode("utf-16-le")) // 2
        self.assertEqual(edge["caller"]["column"], column)
        self.assertEqual(edge["caller"]["end_column"], column + len("actual"))

    def test_threaded_scheduling_and_post_start_debugger_conflict(self):
        _, index = self.record('''
            import asyncio
            import sys
            import threading
            async def actual():
                await asyncio.sleep(0)
            async def main():
                await asyncio.create_task(actual())
            def work():
                asyncio.run(main())
            threads = [threading.Thread(target=work) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            def debugger(frame, event, arg):
                return debugger
            sys.settrace(debugger)
            asyncio.run(main())
            assert sys.gettrace() is debugger
        ''')
        edge, = self.scheduled(index)
        self.assertEqual(edge["count"], 2)
        self.assertEqual(index["processes"][0]["scheduled_coroutines_status"], "trace_conflict")

    def test_existing_debugger_hook_is_preserved(self):
        path = self.root / "manual.py"
        path.write_text(textwrap.dedent('''
            import asyncio
            import sys
            from callnav.collector import start
            def debugger(frame, event, arg):
                return debugger
            sys.settrace(debugger)
            collector = start()
            assert sys.gettrace() is debugger
            assert collector.scheduled.status == "trace_conflict"
            async def actual():
                pass
            async def main():
                await asyncio.create_task(actual())
            asyncio.run(main())
            collector.stop()
            assert sys.gettrace() is debugger
        '''), encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), CALLNAV_ROOT=str(self.root),
                   CALLNAV_RUN_DIR=str(self.output), CALLNAV_RUN_ID="existing-trace")
        process = subprocess.run([sys.executable, str(path)], env=env, text=True,
                                 capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("scheduled coroutine capture disabled", process.stderr)
        shard, = (self.output / "shards").glob("*.json")
        payload = json.loads(shard.read_text())
        self.assertEqual(self.scheduled(payload), [])
        self.assertEqual(payload["process"]["scheduled_coroutines_status"], "trace_conflict")

    def test_closed_loop_drops_pending_coroutine_closure_cycle(self):
        _, index = self.record('''
            import asyncio
            import gc
            import warnings
            import weakref
            from callnav.collector import _collector
            warnings.simplefilter('ignore', RuntimeWarning)
            references = []
            def build():
                holder = {}
                async def child():
                    raise AssertionError(holder)
                task = asyncio.create_task(child())
                holder['function'] = child
                holder['task'] = task
                references.extend((weakref.ref(child), weakref.ref(task.get_coro())))
                loop.stop()
            loop = asyncio.new_event_loop()
            loop.set_exception_handler(lambda *args: None)
            loop.call_soon(build)
            loop.run_forever()
            loop.close()
            gc.collect()
            assert all(reference() is None for reference in references)
            assert not _collector.scheduled.pending
        ''')
        self.assertEqual(self.scheduled(index), [])


if __name__ == "__main__":
    unittest.main()

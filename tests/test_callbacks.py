"""Callback navigation requires registration and an exact stdlib dispatch."""

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class CallbackTests(unittest.TestCase):
    def record(self, content):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "run.py"
            script.write_text(textwrap.dedent(content).lstrip())
            env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
            process = subprocess.run(
                [sys.executable, "-m", "callnav", "record", "--root", str(root),
                 "--output", str(root / "trace"), "--", sys.executable, str(script)],
                env=env, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            data = json.loads((root / "trace/index.json").read_text())
            self.assertTrue(all(p["errors"] == 0 for p in data["processes"]), data["processes"])
            return [e for e in data["edges"] if e["kind"] == "registered"]

    def test_thread_target_executed_and_never_started(self):
        edges = self.record('''
            import threading
            class Worker:
                def run(self):
                    pass
            def unused():
                raise AssertionError()
            worker = Worker()
            thread = threading.Thread(target=worker.run)
            thread.start()
            thread.join()
            pending = threading.Thread(target=unused)
        ''')
        self.assertEqual([(e["caller"]["line"], e["target"]["qualname"], e["count"])
                          for e in edges], [(8, "Worker.run", 1)])

    def test_thread_changed_target_is_not_attributed_to_registration(self):
        edges = self.record('''
            import threading
            def original():
                raise AssertionError()
            def replacement():
                pass
            thread = threading.Thread(target=original)
            thread._target = replacement
            thread.start()
            thread.join()
        ''')
        self.assertEqual(edges, [])

    def test_thread_reinitialization_uses_only_latest_completed_registration(self):
        edges = self.record('''
            import threading
            def actual():
                pass
            thread = threading.Thread(target=actual)
            thread.__init__(target=actual)
            thread.start()
            thread.join()
        ''')
        self.assertEqual([(e["caller"]["line"], e["count"]) for e in edges], [(5, 1)])

    def test_failed_thread_reinitialization_does_not_prove_a_registration(self):
        edges = self.record('''
            import threading
            def actual():
                pass
            thread = threading.Thread(target=actual)
            try:
                thread.__init__(group=object(), target=actual)
            except AssertionError:
                pass
            thread.start()
            thread.join()
        ''')
        self.assertEqual(edges, [])

    def test_nested_thread_initialization_uses_last_completed_target_assignment(self):
        edges = self.record('''
            import threading
            def actual():
                pass
            thread = threading.Thread(target=actual)
            class Name:
                def __str__(self):
                    thread.__init__(target=actual)
                    return "worker"
            thread.__init__(name=Name(), target=actual)
            thread.start()
            thread.join()
        ''')
        self.assertEqual([(e["caller"]["line"], e["count"]) for e in edges], [(9, 1)])

    def test_task_factory_callbacks_removed_and_not_executed(self):
        edges = self.record('''
            import asyncio
            def make_callback(label):
                def done(task):
                    assert task.done()
                return done
            async def actual():
                await asyncio.sleep(0)
            async def main():
                task = asyncio.create_task(actual())
                task.add_done_callback(make_callback("executed"))
                removed = make_callback("removed")
                task.add_done_callback(removed)
                task.remove_done_callback(removed)
                await task
                await asyncio.sleep(0)
            asyncio.run(main())
        ''')
        self.assertEqual([(e["caller"]["line"], e["target"]["qualname"], e["count"])
                          for e in edges], [(10, "make_callback.<locals>.done", 1)])

    def test_callback_entered_directly_does_not_prove_registration_dispatch(self):
        edges = self.record('''
            import asyncio
            def done(task):
                pass
            async def actual():
                pass
            async def main():
                task = asyncio.create_task(actual())
                task.add_done_callback(done)
                task.remove_done_callback(done)
                done(task)
                await task
            asyncio.run(main())
        ''')
        self.assertEqual(edges, [])

    def test_manual_handle_with_same_callback_and_task_is_not_a_receipt(self):
        edges = self.record('''
            import asyncio
            def done(task):
                pass
            async def actual():
                pass
            async def main():
                task = asyncio.create_task(actual())
                task.add_done_callback(done)
                task.remove_done_callback(done)
                await task
                asyncio.get_running_loop().call_soon(done, task)
                await asyncio.sleep(0)
            asyncio.run(main())
        ''')
        self.assertEqual(edges, [])

    def test_native_callable_subclass_does_not_run_metaclass_attribute_hooks(self):
        edges = self.record('''
            import asyncio
            import functools
            events = []
            class Meta(type):
                def __getattribute__(cls, name):
                    if name == "__name__":
                        events.append(name)
                    return type.__getattribute__(cls, name)
            class Forward(functools.partial, metaclass=Meta):
                pass
            def done(task):
                pass
            async def actual():
                await asyncio.sleep(0)
            async def main():
                task = asyncio.create_task(actual())
                task.add_done_callback(done)
                loop = asyncio.get_running_loop()
                loop.call_soon(Forward(loop.call_soon, done, task))
                await task
                await asyncio.sleep(0)
                assert events == [], events
            asyncio.run(main())
        ''')
        self.assertEqual(sum(edge["count"] for edge in edges), 1)

    def test_native_callable_structural_metadata_bypasses_data_descriptors(self):
        edges = self.record('''
            import asyncio
            import functools
            events = []
            class Meta(type):
                @property
                def __dict__(cls):
                    events.append("__dict__")
                    return vars(type)["__dict__"].__get__(cls)
                @property
                def __name__(cls):
                    events.append("__name__")
                    return vars(type)["__name__"].__get__(cls)
            class Forward(functools.partial, metaclass=Meta):
                @property
                def __class__(self):
                    events.append("__class__")
                    return type(self)
            def done(task):
                pass
            async def actual():
                await asyncio.sleep(0)
            async def main():
                task = asyncio.create_task(actual())
                task.add_done_callback(done)
                loop = asyncio.get_running_loop()
                loop.call_soon(Forward(loop.call_soon, done, task))
                await task
                await asyncio.sleep(0)
                assert events == [], events
            asyncio.run(main())
        ''')
        self.assertEqual(sum(edge["count"] for edge in edges), 1)

    def test_same_callback_registered_twice_gets_one_receipt_each(self):
        edges = self.record('''
            import asyncio
            def done(task):
                pass
            async def actual():
                await asyncio.sleep(0)
            async def main():
                task = asyncio.create_task(actual())
                task.add_done_callback(done)
                task.add_done_callback(done)
                await task
                await asyncio.sleep(0)
            asyncio.run(main())
        ''')
        self.assertEqual(sorted((e["caller"]["line"], e["count"]) for e in edges),
                         [(8, 1), (9, 1)])

    def test_removed_callback_reregistered_uses_new_site(self):
        edges = self.record('''
            import asyncio
            def done(task):
                pass
            async def actual():
                pass
            async def main():
                task = asyncio.create_task(actual())
                task.add_done_callback(done)
                task.remove_done_callback(done)
                task.add_done_callback(done)
                await task
                await asyncio.sleep(0)
            asyncio.run(main())
        ''')
        self.assertEqual([(e["caller"]["line"], e["count"]) for e in edges], [(10, 1)])

    def test_callback_receipts_do_not_retain_application_objects(self):
        edges = self.record('''
            import asyncio
            import gc
            import threading
            import weakref
            class Payload:
                pass
            def make_callback(payload):
                def done(*args):
                    assert payload is not None
                return done
            payload = Payload()
            callback = make_callback(payload)
            payload_reference = weakref.ref(payload)
            callback_reference = weakref.ref(callback)
            thread = threading.Thread(target=callback)
            thread_reference = weakref.ref(thread)
            thread.start()
            thread.join()
            del callback, payload, thread
            gc.collect()
            assert payload_reference() is None
            assert callback_reference() is None
            assert thread_reference() is None
            async def actual():
                await asyncio.sleep(0)
            async def main():
                payload = Payload()
                callback = make_callback(payload)
                task = asyncio.create_task(actual())
                payload_reference = weakref.ref(payload)
                callback_reference = weakref.ref(callback)
                task_reference = weakref.ref(task)
                task.add_done_callback(callback)
                await task
                await asyncio.sleep(0)
                del callback, payload, task
                gc.collect()
                assert payload_reference() is None
                assert callback_reference() is None
                assert task_reference() is None
            asyncio.run(main())
        ''')
        self.assertEqual(sum(edge["count"] for edge in edges), 2)


@unittest.skipUnless(importlib.util.find_spec("uvloop"), "optional uvloop not installed")
class UvloopCallbackTests(unittest.TestCase):
    record = CallbackTests.record
    def test_uvloop_task_callbacks_from_two_sites(self):
        edges = self.record('''
            import asyncio
            import uvloop
            def done(task):
                assert task.done()
            async def actual():
                await asyncio.sleep(0)
            async def main():
                first = asyncio.create_task(actual())
                first.add_done_callback(done)
                second = asyncio.create_task(actual())
                second.add_done_callback(done)
                await asyncio.gather(first, second)
                await asyncio.sleep(0)
            uvloop.run(main())
        ''')
        self.assertEqual(sorted((edge["caller"]["line"], edge["count"]) for edge in edges), [(9, 1), (11, 1)])
        self.assertTrue(all(edge.get("callback_dispatch") == "uvloop" for edge in edges), edges)

    def test_uvloop_manual_scheduling_and_removed_registration_are_not_receipts(self):
        edges = self.record('''
            import asyncio
            import uvloop
            def done(task):
                pass
            async def actual():
                await asyncio.sleep(0)
            async def main():
                task = asyncio.create_task(actual())
                task.add_done_callback(done)
                asyncio.get_running_loop().call_soon(done, task)
                task.remove_done_callback(done)
                await task
                asyncio.get_running_loop().call_soon(done, task)
                await asyncio.sleep(0)
            uvloop.run(main())
        ''')
        self.assertEqual(edges, [])

    def test_uvloop_factory_callbacks_preserve_context_cancellation_and_exception(self):
        edges = self.record('''
            import asyncio
            import contextvars
            import uvloop
            label = contextvars.ContextVar("label", default="outside")
            errors, calls = [], []
            def make_callback(expected):
                def done(task):
                    assert task.done()
                    assert label.get() == "callback"
                    calls.append((expected, task.cancelled()))
                    if expected == "cancelled":
                        raise ValueError("callback failure")
                return done
            async def actual():
                await asyncio.sleep(0)
            async def main():
                loop = asyncio.get_running_loop()
                loop.set_exception_handler(lambda loop, info: errors.append(info["exception"]))
                context = contextvars.copy_context()
                context.run(label.set, "callback")
                first = asyncio.create_task(actual())
                first.add_done_callback(make_callback("normal"), context=context)
                second = asyncio.create_task(actual())
                second.add_done_callback(make_callback("cancelled"), context=context)
                second.cancel()
                await asyncio.gather(first, second, return_exceptions=True)
                await asyncio.sleep(0)
                assert sorted(calls) == [("cancelled", True), ("normal", False)], calls
                assert len(errors) == 1 and str(errors[0]) == "callback failure", errors
                assert label.get() == "outside"
            uvloop.run(main())
        ''')
        self.assertEqual(sum(edge["count"] for edge in edges), 2)

    def test_uvloop_duplicate_and_reregistered_callback_use_actual_sites(self):
        edges = self.record('''
            import asyncio
            import uvloop
            def done(task):
                pass
            async def actual():
                await asyncio.sleep(0)
            async def main():
                task = asyncio.create_task(actual())
                task.add_done_callback(done)
                task.add_done_callback(done)
                task.remove_done_callback(done)
                task.add_done_callback(done)
                task.add_done_callback(done)
                await task
                await asyncio.sleep(0)
            uvloop.run(main())
        ''')
        self.assertEqual(sorted((edge["caller"]["line"], edge["count"]) for edge in edges), [(12, 1), (13, 1)])

    def test_uvloop_callbacks_do_not_retain_payload_or_task(self):
        edges = self.record('''
            import asyncio
            import gc
            import uvloop
            import weakref
            class Payload:
                pass
            def make_callback(payload):
                def done(task):
                    assert payload is not None
                return done
            async def actual():
                await asyncio.sleep(0)
            async def schedule():
                payload = Payload()
                callback = make_callback(payload)
                task = asyncio.create_task(actual())
                references = [weakref.ref(value) for value in (payload, callback, task)]
                task.add_done_callback(callback)
                await task
                await asyncio.sleep(0)
                return references
            async def main():
                references = await schedule()
                await asyncio.sleep(0)
                gc.collect()
                assert all(reference() is None for reference in references)
            uvloop.run(main())
        ''')
        self.assertEqual(sum(edge["count"] for edge in edges), 1)

    def test_uvloop_future_set_result_while_coroutine_active_cannot_mint_receipt(self):
        edges = self.record('''
            import asyncio
            import uvloop
            def done(task):
                pass
            async def actual():
                task = asyncio.current_task()
                task.add_done_callback(done)
                asyncio.Future.set_result(task, None)
                asyncio.get_running_loop().call_soon(done, task)
            async def main():
                loop = asyncio.get_running_loop()
                loop.set_exception_handler(lambda *args: None)
                task = asyncio.create_task(actual())
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            uvloop.run(main())
        ''')
        self.assertEqual(edges, [])

    def test_uvloop_loop_and_task_subclasses_fail_closed(self):
        edges = self.record('''
            import asyncio
            import uvloop
            class CustomLoop(uvloop.Loop):
                pass
            class CustomTask(asyncio.Task):
                pass
            def done(task):
                pass
            async def actual():
                await asyncio.sleep(0)
            async def main(custom_task=False):
                loop = asyncio.get_running_loop()
                if custom_task:
                    loop.set_task_factory(lambda loop, coro, **kwargs: CustomTask(coro, loop=loop, **kwargs))
                task = asyncio.create_task(actual())
                task.add_done_callback(done)
                await task
                await asyncio.sleep(0)
            with asyncio.Runner(loop_factory=CustomLoop) as runner:
                runner.run(main())
            uvloop.run(main(custom_task=True))
        ''')
        self.assertEqual(edges, [])

    def test_uvloop_policy_in_background_thread_preserves_original_registration(self):
        edges = self.record('''
            import asyncio
            import threading
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            results = []
            def make_callback(label):
                def done(task):
                    results.append((label, task.result()))
                return done
            async def actual():
                await asyncio.sleep(0)
                return 4
            async def main():
                task = asyncio.create_task(actual())
                task.add_done_callback(make_callback("worker"))
                await task
                await asyncio.sleep(0)
            thread = threading.Thread(target=asyncio.run, args=(main(),))
            thread.start()
            thread.join()
            assert results == [("worker", 4)], results
        ''')
        uvloop_edges = [edge for edge in edges if edge.get("callback_dispatch") == "uvloop"]
        self.assertEqual(sum(edge["count"] for edge in uvloop_edges), 1, edges)

    def test_uvloop_patch_is_restored_on_stop(self):
        edges = self.record('''
            import uvloop
            from callnav.collector import _collector
            assert uvloop.Loop.__dict__.get("call_soon") is _collector.callbacks.uvloop_patch
            _collector.callbacks.stop()
            assert "call_soon" not in uvloop.Loop.__dict__
        ''')
        self.assertEqual(edges, [])

    def test_uvloop_stop_preserves_an_application_replacement(self):
        edges = self.record('''
            import uvloop
            from callnav.collector import _collector
            original = uvloop.Loop.__bases__[0].call_soon
            def application_call_soon(loop, callback, *args, context=None):
                return original(loop, callback, *args, context=context)
            uvloop.Loop.call_soon = application_call_soon
            _collector.callbacks.stop()
            assert uvloop.Loop.call_soon is application_call_soon
            del uvloop.Loop.call_soon
        ''')
        self.assertEqual(edges, [])

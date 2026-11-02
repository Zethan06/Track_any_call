"""Instruction delivery must shrink without weakening activation proofs."""

import dis
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from callnav.collector import Collector
from callnav.scheduled import ScheduledCalls


class MonitoringFailuresTests(unittest.TestCase):
    def scheduled(self):
        scheduled = ScheduledCalls(types.SimpleNamespace(errors=0))
        scheduled.monitoring.api = mock.Mock(
            events=types.SimpleNamespace(INSTRUCTION=1), DISABLE=object(),
        )
        scheduled.monitoring.api.get_tool.return_value = scheduled.monitoring.name
        return scheduled

    def test_occupied_tool_slots_leave_full_tracing_available(self):
        scheduled = self.scheduled()
        api = scheduled.monitoring.api
        api.use_tool_id.side_effect = ValueError("occupied")
        scheduled.monitoring.install()
        self.assertIsNone(scheduled.monitoring.tool_id)
        self.assertEqual([call.args[0] for call in api.use_tool_id.call_args_list], [3, 4])
        api.register_callback.assert_not_called()
        api.free_tool_id.assert_not_called()

    def test_registration_failure_releases_the_reserved_tool(self):
        scheduled = self.scheduled()
        api = scheduled.monitoring.api
        api.register_callback.side_effect = [RuntimeError("audit refused"), None]
        scheduled.monitoring.install()
        self.assertIsNone(scheduled.monitoring.tool_id)
        api.free_tool_id.assert_called_once_with(3)
        self.assertFalse(scheduled.monitoring.offsets)

    def test_one_code_instrumentation_failure_preserves_other_live_codes(self):
        scheduled = self.scheduled()
        scheduled._plans = lambda code: ()
        monitoring = scheduled.monitoring
        monitoring.install()
        first = compile("value = 1", "/first.py", "exec")
        second = compile("value = 2", "/second.py", "exec")
        self.assertTrue(monitoring.watch(first, tuple(dis.get_instructions(first)), None))
        monitoring.api.set_local_events.side_effect = [RuntimeError("unavailable"), None]
        self.assertFalse(monitoring.watch(second, tuple(dis.get_instructions(second)), None))
        self.assertIn(first, monitoring.offsets)
        self.assertNotIn(second, monitoring.offsets)
        self.assertEqual(monitoring.tool_id, 3)
        monitoring.api.set_local_events.assert_called_with(3, second, 0)
        monitoring.api.set_local_events.side_effect = None
        monitoring.stop()

    def test_frame_access_failure_never_raises_into_the_application(self):
        scheduled = self.scheduled()
        scheduled.status = "recording"
        code = (lambda: None).__code__
        scheduled.monitoring.tool_id = 3
        scheduled.monitoring.offsets[code] = frozenset({2})
        with mock.patch("sys._getframe", side_effect=RuntimeError("audit refused")):
            scheduled.monitoring._instruction(code, 2)
        self.assertEqual(scheduled.collector.errors, 1)
        self.assertEqual(scheduled.status, "complete")
        self.assertFalse(scheduled.monitoring.offsets)

    def test_secondary_cleanup_failure_is_contained(self):
        scheduled = self.scheduled()
        code = (lambda: None).__code__
        scheduled.monitoring.tool_id = 3
        scheduled.monitoring.offsets[code] = frozenset({2})
        scheduled.status = "recording"
        scheduled.stop = mock.Mock(side_effect=RuntimeError("cleanup refused"))
        with mock.patch("sys._getframe", side_effect=RuntimeError("audit refused")):
            scheduled.monitoring._instruction(code, 2)
        self.assertEqual(scheduled.collector.errors, 2)

    def test_slot_takeover_never_changes_the_foreign_tools_events_or_callback(self):
        for operation in ("watch", "reset", "stop"):
            with self.subTest(operation=operation):
                scheduled = self.scheduled()
                monitoring = scheduled.monitoring
                monitoring.tool_id = 3
                code = (lambda: None).__code__
                monitoring.offsets[code] = frozenset({2})
                monitoring.api.get_tool.return_value = "foreign.owner"
                if operation == "watch":
                    monitoring.watch(code, (), None)
                else:
                    getattr(monitoring, operation)()
                self.assertIsNone(monitoring.tool_id)
                self.assertFalse(monitoring.offsets)
                self.assertEqual(scheduled.collector.errors, 1)
                monitoring.api.set_local_events.assert_not_called()
                monitoring.api.register_callback.assert_not_called()
                monitoring.api.free_tool_id.assert_not_called()

    def test_stop_rejects_new_codes_before_reentrant_cleanup(self):
        scheduled = self.scheduled()
        scheduled._plans = lambda code: ()
        scheduled.status = "recording"
        monitoring = scheduled.monitoring
        monitoring.install()
        codes = [compile(f"value = {index}", f"/module{index}.py", "exec") for index in range(3)]
        for code in codes[:2]:
            monitoring.watch(code, tuple(dis.get_instructions(code)), None)
        attempted = []

        def interleaved_watch(tool_id, code, events):
            if events == 0:
                self.assertEqual(scheduled.status, "complete")
                attempted.append(monitoring.watch(codes[2], tuple(dis.get_instructions(codes[2])), None))

        monitoring.api.set_local_events.side_effect = interleaved_watch
        scheduled.stop()
        self.assertEqual(attempted, [False, False])
        self.assertFalse(monitoring.offsets)
        self.assertIsNone(monitoring.tool_id)
        self.assertEqual(scheduled.collector.errors, 0)


class MonitoringRuntimeTests(unittest.TestCase):
    def collector(self, content):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        path = root / "application.py"
        path.write_text(content, encoding="utf-8")
        namespace = {"sys": sys}
        exec(compile(content, str(path), "exec"), namespace)
        with mock.patch.dict(os.environ, CALLNAV_ROOT=str(root), CALLNAV_RUN_DIR=str(root / "run"),
                             CALLNAV_RUN_ID="monitoring-test", CALLNAV_INCLUDE_ROOTS="[]"):
            collector = Collector()
        self.addCleanup(collector.scheduled.stop)
        return collector, namespace

    def test_legacy_python_uses_the_owned_trace_hook(self):
        collector, namespace = self.collector(
            "def leaf():\n    return 7\n"
            "def work():\n    value = leaf()\n    return sys._getframe().f_trace is not None\n"
        )
        collector.scheduled.monitoring.api = None
        collector.scheduled.install()
        self.assertTrue(namespace["work"]())
        self.assertEqual(collector.errors, 0)

    @unittest.skipUnless(hasattr(sys, "monitoring"), "requires Python 3.12 monitoring")
    def test_real_occupied_slots_fall_back_to_opcode_tracing(self):
        collector, namespace = self.collector(
            "def leaf():\n    return 7\n"
            "def work():\n    value = leaf()\n    return sys._getframe().f_trace_opcodes\n"
        )
        reserved = []
        try:
            for tool_id in (3, 4):
                if sys.monitoring.get_tool(tool_id) is None:
                    sys.monitoring.use_tool_id(tool_id, "callnav.test.occupied")
                    reserved.append(tool_id)
            collector.scheduled.install()
            self.assertIsNone(collector.scheduled.monitoring.tool_id)
            self.assertTrue(namespace["work"]())
            self.assertEqual(collector.errors, 0)
        finally:
            collector.scheduled.stop()
            for tool_id in reserved:
                sys.monitoring.free_tool_id(tool_id)

    @unittest.skipUnless(sys.version_info[:2] == (3, 12), "tests the Python 3.12 fallback")
    def test_fresh_legacy_fallback_proves_construction_on_current_and_new_threads(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        path = root / "application.py"
        path.write_text(textwrap.dedent('''
            import threading
            def stream():
                yield 7
            def run():
                iterator = stream()
                assert next(iterator) == 7
                iterator.close()
            def monitored():
                iterator = stream()
                assert next(iterator) == 7
                iterator.close()
            run()
            monitored()
            worker = threading.Thread(target=run)
            worker.start()
            worker.join()
        ''').lstrip(), encoding="utf-8")
        program = textwrap.dedent('''
            import json, os, sys, threading
            from pathlib import Path
            from callnav.collector import start
            root = Path(sys.argv[1])
            mode = sys.argv[2]
            os.environ.update(CALLNAV_ROOT=str(root), CALLNAV_RUN_DIR=str(root / "recording"),
                              CALLNAV_RUN_ID="fresh-legacy-fallback", CALLNAV_INCLUDE_ROOTS="[]")
            if mode == "occupied":
                for tool_id in (3, 4):
                    sys.monitoring.use_tool_id(tool_id, "callnav.test.occupied")
            else:
                original_events = sys.monitoring.set_local_events
                def fail_one_code(tool_id, code, events):
                    if code.co_name == "run" and events:
                        raise RuntimeError("one code cannot be instrumented")
                    return original_events(tool_id, code, events)
                sys.monitoring.set_local_events = fail_one_code
            original_trace = sys.settrace
            trace_installs = 0
            initial_thread = threading.get_ident()
            def counted_trace(hook):
                global trace_installs
                if threading.get_ident() == initial_thread:
                    trace_installs += 1
                return original_trace(hook)
            sys.settrace = counted_trace
            collector = start()
            monitoring = collector.scheduled.monitoring
            assert (monitoring.tool_id is None) == (mode == "occupied")
            path = root / "application.py"
            namespace = {"__name__": "__main__"}
            exec(compile(path.read_text(), str(path), "exec"), namespace)
            if mode == "watch_failure":
                code = namespace["monitored"].__code__
                assert sys.monitoring.get_local_events(monitoring.tool_id, code)
            assert trace_installs == (1 if mode == "occupied" else 2), trace_installs
            if collector.scheduled.direct_monitoring:
                assert sys.gettrace() is None and threading.gettrace() is None
                assert collector.profile_monitoring.instruction_lease
                assert sys.monitoring.get_events(monitoring.tool_id) & sys.monitoring.events.INSTRUCTION
            else:
                assert collector.scheduled.legacy_opcodes_ready
            collector.stop()
            print(json.dumps({"errors": collector.errors, "edges": list(collector.edges.values())}))
            if mode == "occupied":
                for tool_id in (3, 4):
                    sys.monitoring.free_tool_id(tool_id)
        ''')
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        for mode in ("occupied", "watch_failure"):
            with self.subTest(mode=mode):
                process = subprocess.run([sys.executable, "-c", program, str(root), mode],
                                         env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(process.returncode, 0, process.stderr)
                result = json.loads(process.stdout)
                edges = [edge for edge in result["edges"]
                         if edge["kind"] == "deferred" and edge["target"]["qualname"] == "stream"]
                self.assertEqual(result["errors"], 0)
                self.assertEqual(sorted((edge["caller"]["qualname"], edge["count"]) for edge in edges),
                                 [("monitored", 1), ("run", 2)])

    @unittest.skipUnless(hasattr(sys, "monitoring"), "requires Python 3.12 monitoring")
    def test_hot_arithmetic_loop_stops_dispatching_unrelated_instructions(self):
        collector, namespace = self.collector(
            "def leaf():\n    return 7\n"
            "def work(count):\n"
            "    value = leaf()\n"
            "    for item in range(count):\n"
            "        value += item\n"
            "        value ^= item\n"
            "        value += item * 2\n"
            "    return value\n"
        )
        monitoring = collector.scheduled.monitoring
        events = []
        lookup = monitoring.offsets.get
        code = namespace["work"].__code__

        def counted_lookup(candidate, default=None):
            if candidate is code:
                events.append(candidate)
            return lookup(candidate, default)

        monitoring.offsets.get = counted_lookup
        collector.scheduled.install()
        self.assertIsNotNone(monitoring.tool_id)
        namespace["work"](1)
        events.clear()
        namespace["work"](100)
        # The loop executes well over 1,000 instructions, but only its
        # back-edge destination and fixed proof sites still enter Python.
        self.assertLess(len(events), 150)
        self.assertGreater(len(events), 90)
        self.assertEqual(collector.errors, 0)

    @unittest.skipUnless(hasattr(sys, "monitoring"), "requires Python 3.12 monitoring")
    def test_binding_is_consumed_at_its_first_unrelated_instruction(self):
        collector, namespace = self.collector(
            "def stream():\n    yield 7\n"
            "def work():\n"
            "    iterator = stream()\n"
            "    marker = 9\n"
            "    return iterator\n"
        )
        deferred = collector.scheduled.deferred
        operation = deferred.opcode
        checks = []

        def record_binding(frame, state=None):
            if state is not None and state["binding"] is not None:
                checks.append(frame.f_lasti)
            return operation(frame, state)

        deferred.opcode = record_binding
        collector.scheduled.install()
        for _ in range(2):
            iterator = namespace["work"]()
            iterator.close()
        instructions = dict((op.offset, op) for op in dis.get_instructions(namespace["work"]))
        self.assertEqual(len(checks), 2)
        self.assertTrue(all(instructions[offset].opname == "LOAD_CONST" for offset in checks))
        self.assertTrue(all(instructions[offset].argval == 9 for offset in checks))
        self.assertEqual(collector.errors, 0)

    @unittest.skipUnless(hasattr(sys, "monitoring"), "requires Python 3.12 monitoring")
    def test_untraced_thread_cannot_disable_a_later_activations_proof_sites(self):
        collector, namespace = self.collector(
            "def leaf():\n    return 7\n"
            "def work():\n    return leaf()\n"
        )
        ready, run, finished = threading.Event(), threading.Event(), threading.Event()

        def untraced_worker():
            ready.set()
            run.wait(5)
            namespace["work"]()
            finished.set()

        worker = threading.Thread(target=untraced_worker)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(run.set)
        self.assertTrue(ready.wait(5))
        scheduled = collector.scheduled
        observed = []
        operation = scheduled.deferred.opcode

        def record_proof(frame, state=None):
            if frame.f_code is namespace["work"].__code__:
                observed.append(frame.f_lasti)
            return operation(frame, state)

        scheduled.deferred.opcode = record_proof
        scheduled.install()
        namespace["work"]()
        first = list(observed)
        observed.clear()
        run.set()
        self.assertTrue(finished.wait(5))
        self.assertEqual(observed, [])
        namespace["work"]()
        self.assertEqual(observed, first)
        self.assertTrue(first)
        self.assertEqual(collector.errors, 0)

    @unittest.skipUnless(hasattr(sys, "monitoring"), "requires Python 3.12 monitoring")
    def test_stop_unregisters_callback_local_events_and_tool_id(self):
        collector, namespace = self.collector(
            "def leaf():\n    return 7\n"
            "def work():\n    return leaf()\n"
        )
        scheduled = collector.scheduled
        scheduled.install()
        namespace["work"]()
        tool_id = scheduled.monitoring.tool_id
        scheduled.stop()
        self.assertIsNone(sys.monitoring.get_tool(tool_id))
        self.assertEqual(sys.monitoring.get_local_events(tool_id, namespace["work"].__code__), 0)
        # A newly owned slot must not inherit a callback retaining Collector.
        sys.monitoring.use_tool_id(tool_id, "callnav.test.reuse")
        try:
            previous = sys.monitoring.register_callback(tool_id, sys.monitoring.events.INSTRUCTION, None)
            self.assertIsNone(previous)
        finally:
            sys.monitoring.free_tool_id(tool_id)

    @unittest.skipUnless(hasattr(sys, "monitoring"), "requires Python 3.12 monitoring")
    def test_real_slot_takeover_preserves_the_new_callback(self):
        collector, namespace = self.collector(
            "def leaf():\n    return 7\n"
            "def work():\n    return leaf()\n"
        )
        scheduled = collector.scheduled
        scheduled.install()
        namespace["work"]()
        tool_id = scheduled.monitoring.tool_id
        sys.monitoring.free_tool_id(tool_id)
        sys.monitoring.use_tool_id(tool_id, "callnav.test.foreign")

        def foreign_callback(code, offset):
            return None

        sys.monitoring.register_callback(tool_id, sys.monitoring.events.INSTRUCTION, foreign_callback)
        try:
            scheduled.stop()
            self.assertEqual(sys.monitoring.get_tool(tool_id), "callnav.test.foreign")
            previous = sys.monitoring.register_callback(tool_id, sys.monitoring.events.INSTRUCTION, None)
            self.assertIs(previous, foreign_callback)
            self.assertEqual(collector.errors, 1)
        finally:
            for code in (namespace["work"].__code__, namespace["leaf"].__code__):
                sys.monitoring.set_local_events(tool_id, code, 0)
            sys.monitoring.register_callback(tool_id, sys.monitoring.events.INSTRUCTION, None)
            sys.monitoring.free_tool_id(tool_id)


if __name__ == "__main__":
    unittest.main()

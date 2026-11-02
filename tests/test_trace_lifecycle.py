"""Trace hooks preserve deferred calls and release application objects safely."""

import ast
import dis
import os
import sys
from pathlib import Path
import tempfile
import threading
import types
import unittest
import weakref
from unittest import mock

import test_scheduled as scheduled_helpers

from callnav.callbacks import RegisteredCallbacks
from callnav.collector import Source
from callnav.deferred import DeferredCalls
from callnav.scheduled import ScheduledCalls


class TraceLifecycleTests(unittest.TestCase):
    def _record(self, content):
        harness = scheduled_helpers.ScheduledTests()
        harness.setUp()
        self.addCleanup(harness.tearDown)
        return harness.record(content)

    def test_irrelevant_callback_events_skip_keys_module_lookups_and_user_hooks(self):
        with mock.patch.object(RegisteredCallbacks, '_install_uvloop'):
            callbacks = RegisteredCallbacks(types.SimpleNamespace())

        class Modules(dict):
            def get(self, name, default=None):
                if name == 'uvloop':
                    raise AssertionError('irrelevant callback event checked uvloop')
                return super().get(name, default)

        class Argument:
            @property
            def __class__(self):
                raise AssertionError('irrelevant callback argument ran a user hook')

        code = (lambda: None).__code__
        # Empty registries do not require materializing the caller frame.
        frame = types.SimpleNamespace(f_code=code)
        with mock.patch.object(sys, 'modules', Modules(sys.modules)):
            with mock.patch('threading.get_ident', side_effect=AssertionError('irrelevant event made a key')):
                for event in ('return', 'call', 'c_call', 'c_return', 'c_exception'):
                    callbacks.observe(frame, event, Argument())
                # Another frame's pending receipt does not make this one a
                # callback return, including when dispatch bookkeeping is busy.
                callbacks.thread_initializing[(1, 2)] = object()
                callbacks.scheduling[(1, 2)] = object()
                callbacks.observe(frame, 'return', None)

    def test_uvloop_module_return_is_checked_without_a_callback_registration(self):
        with mock.patch.object(RegisteredCallbacks, '_install_uvloop'):
            callbacks = RegisteredCallbacks(types.SimpleNamespace())
        module = types.ModuleType('uvloop')
        frame = types.SimpleNamespace(f_code=compile('pass', '/uvloop/__init__.py', 'exec'),
                                      f_globals=vars(module))
        callbacks._install_uvloop = mock.Mock()
        with mock.patch.dict(sys.modules, uvloop=module):
            with mock.patch('threading.get_ident', side_effect=AssertionError('module return made a key')):
                callbacks.observe(frame, 'return', None)
        callbacks._install_uvloop.assert_called_once_with()

    def test_promoting_a_wrapper_invalidates_its_cached_negative_plan(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'external.py'
        path.write_text('def wrapper():\n    return original()\n', encoding='utf-8')
        namespace = {'original': lambda: None}
        exec(compile(path.read_text(), str(path), 'exec'), namespace)
        wrapper = namespace['wrapper']
        wrapper.__wrapped__ = namespace['original']
        collector = types.SimpleNamespace(
            path=mock.Mock(return_value=None), source=mock.Mock(return_value=Source(str(path))),
            _instructions=lambda code: (None, tuple(dis.get_instructions(code))),
        )
        deferred = DeferredCalls(types.SimpleNamespace(collector=collector))
        self.assertFalse(deferred._plans(wrapper.__code__))
        caller = compile('wrapper()', '/included/caller.py', 'exec')
        frame = types.SimpleNamespace(f_code=caller, f_lasti=2)
        plan = (None, None, ast.parse('wrapper()').body[0].value, {})
        state = {'iteration': None, 'binding': None, 'tuple_loops': {},
                 'comprehensions': {}, 'load_offsets': {}, 'plans': {2: plan},
                 'loads': {2: (wrapper, True)}, 'calls': {}, 'creations': {},
                 'opcode_offsets': frozenset({2}), 'prefix_offsets': frozenset(),
                 'list_comps': ({}, {}),
                 'tuple_builds': {}, 'assignments': {}, 'iterations': {},
                 'namespaces': {}, 'fast_names': {}}
        deferred.active[(threading.get_ident(), id(frame))] = state
        deferred.list_comps[caller] = {}, {}
        deferred.tuple_loops[caller] = {}
        deferred.assignments[caller] = {}
        deferred.opcode(frame)
        self.assertNotIn(wrapper.__code__, deferred.plans)
        self.assertTrue(deferred._plans(wrapper.__code__))
        collector.source.assert_called_once_with(str(path))

    def test_transient_binding_and_iteration_consume_arbitrary_next_opcodes(self):
        deferred = DeferredCalls(types.SimpleNamespace(collector=types.SimpleNamespace()))
        deferred._remember = mock.Mock()
        creation, value, token = object(), object(), object()
        state = {'opcode_offsets': frozenset(), 'binding': ('created', creation),
                 'iteration': None, 'token': token, 'plans': {}, 'load_offsets': {},
                 'list_comps': ({}, {}), 'tuple_builds': {}, 'tuple_loops': {},
                 'assignments': {}, 'iterations': {}}
        frame = types.SimpleNamespace(f_lasti=100, f_locals={'created': value})
        deferred.opcode(frame, state)
        self.assertIsNone(state['binding'])
        deferred._remember.assert_called_once_with(value, frame, creation, token)
        state['iteration'] = [(100, 102), 0, object()]
        deferred.opcode(frame, state)
        self.assertEqual(state['iteration'][1], 1)
        # An intervening opcode must invalidate the protocol, even when no
        # static call/load/store plan refers to that instruction.
        frame.f_lasti = 104
        deferred.opcode(frame, state)
        self.assertIsNone(state['iteration'])

    def test_trace_dispatch_skips_handlers_but_preserves_yields_and_transient_checks(self):
        for scheduled_active in (False, True):
            with self.subTest(scheduled_active=scheduled_active):
                scheduled = ScheduledCalls(types.SimpleNamespace(errors=0))
                scheduled.status = 'recording'
                scheduled.deferred.opcode = mock.Mock()
                frame = types.SimpleNamespace(f_lasti=100)
                key = threading.get_ident(), id(frame)
                state = {'yield_offsets': frozenset({100}), 'opcode_offsets': frozenset(),
                         'binding': None, 'iteration': None, 'exception_pending': True}
                scheduled.deferred.active[key] = state
                if scheduled_active:
                    yield_state = {'yield_offsets': frozenset({100}),
                                   'opcode_offsets': frozenset(), 'exception_pending': True}
                    scheduled.active[key] = yield_state
                else:
                    yield_state = state
                self.assertIs(scheduled._trace(frame, 'opcode', None), scheduled.hook)
                self.assertFalse(yield_state['exception_pending'])
                scheduled.deferred.opcode.assert_not_called()
                state['binding'] = object()
                scheduled._trace(frame, 'opcode', None)
                scheduled.deferred.opcode.assert_called_once_with(frame, state)
                scheduled.deferred.opcode.reset_mock()
                state['binding'] = None
                state['iteration'] = object()
                scheduled._trace(frame, 'opcode', None)
                scheduled.deferred.opcode.assert_called_once_with(frame, state)
                self.assertEqual(scheduled.collector.errors, 0)

    def test_local_hook_drops_snapshots_at_return_and_has_no_self_cycle(self):
        class Snapshot:
            pass

        scheduled = ScheduledCalls(types.SimpleNamespace(errors=0))
        scheduled.status = 'recording'
        snapshot = Snapshot()
        snapshot_reference = weakref.ref(snapshot)
        state = {'snapshot': snapshot}
        hook = scheduled._frame_trace((1, 2), state, None)
        hook_reference = weakref.ref(hook)
        self.assertIs(scheduled.frame_traces[2][0](), hook)
        del snapshot, state
        with mock.patch.object(scheduled, '_trace', return_value=scheduled.hook):
            self.assertIs(hook(types.SimpleNamespace(), 'return', None), hook)
        self.assertIsNone(snapshot_reference())
        del hook
        self.assertIsNone(hook_reference())
        self.assertEqual(scheduled.frame_traces, {})

    def test_fork_stop_and_conflict_retire_local_opcode_hooks(self):
        class Snapshot:
            pass

        for operation in ('after_fork', 'stop', '_conflict'):
            with self.subTest(operation=operation):
                scheduled = ScheduledCalls(types.SimpleNamespace(errors=0))
                scheduled.status = 'recording'
                scheduled.deferred.opcode = mock.Mock()
                state = {'yield_offsets': frozenset(), 'opcode_offsets': frozenset({100}),
                         'binding': None, 'iteration': None}
                snapshot = Snapshot()
                reference = weakref.ref(snapshot)
                state['snapshot'] = snapshot
                hook = scheduled._frame_trace((1, 2), None, state)
                del snapshot, state
                getattr(scheduled, operation)()
                # Retirement must release snapshots even when sys.settrace
                # disables all further callbacks in this still-live frame.
                self.assertIsNone(reference())
                self.assertIsNone(hook(types.SimpleNamespace(f_lasti=100), 'opcode', None))
                scheduled.deferred.opcode.assert_not_called()
                self.assertEqual(scheduled.collector.errors, 0)

    def test_resuming_an_owned_frame_on_another_thread_keeps_capture_complete(self):
        _, index = self._record('''
            import threading
            def value():
                return 7
            def sequence():
                yield value()
                yield value()
            iterator = sequence()
            assert next(iterator) == 7
            def finish():
                assert next(iterator) == 7
            worker = threading.Thread(target=finish)
            worker.start()
            worker.join()
            iterator.close()
        ''')
        self.assertTrue(all(process['scheduled_coroutines_status'] == 'complete'
                            for process in index['processes']), index['processes'])
        calls = [edge for edge in index['edges'] if edge['target']['qualname'] == 'value']
        self.assertEqual(sum(edge['count'] for edge in calls), 2)

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires os.fork')
    def test_resuming_an_owned_frame_after_fork_keeps_capture_complete(self):
        _, index = self._record('''
            import os
            import sys
            def value():
                return 7
            def sequence():
                yield value()
                yield value()
            iterator = sequence()
            assert next(iterator) == 7
            child = os.fork()
            if child == 0:
                assert next(iterator) == 7
                iterator.close()
                sys.exit(0)
            assert os.waitpid(child, 0)[1] == 0
            iterator.close()
        ''')
        self.assertEqual(len(index['processes']), 2)
        self.assertTrue(all(process['scheduled_coroutines_status'] == 'complete'
                            for process in index['processes']), index['processes'])
        calls = [edge for edge in index['edges'] if edge['target']['qualname'] == 'value']
        self.assertEqual(sum(edge['count'] for edge in calls), 2)

    def test_stop_releases_callable_snapshots_before_the_application_frame_returns(self):
        self._record('''
            import gc
            import weakref
            from callnav.collector import start
            def target():
                return 7
            reference = weakref.ref(target)
            collector = start()
            def application():
                global target
                assert target() == 7
                del target
                collector.stop()
                gc.collect()
                assert reference() is None, 'local trace retained a callable after stop'
            application()
        ''')

    def test_foreign_frame_hook_is_rejected_even_when_an_owned_hook_was_registered(self):
        scheduled = ScheduledCalls(types.SimpleNamespace(errors=0))
        scheduled.status = 'recording'

        def foreign(*args):
            return None

        frame = types.SimpleNamespace(f_trace=foreign)
        own = scheduled._frame_trace((threading.get_ident(), id(frame)), {}, None)
        self.assertIsNot(own, foreign)
        self.assertIsNone(scheduled._trace(frame, 'call', None))
        self.assertEqual(scheduled.status, 'trace_conflict')
        self.assertIs(frame.f_trace, foreign)
        self.assertEqual(scheduled.collector.errors, 0)

    def test_excluded_trace_call_still_plans_a_promoted_wrapper(self):
        collector = types.SimpleNamespace(errors=0, path=mock.Mock(return_value=None))
        scheduled = ScheduledCalls(collector)
        scheduled.status = 'recording'
        scheduled._select = mock.Mock(return_value=())
        scheduled.deferred.begin = mock.Mock(return_value=False)
        code = (lambda: None).__code__
        scheduled.deferred.wrapper_codes.add(code)
        frame = types.SimpleNamespace(f_trace=None, f_code=code)
        self.assertIsNone(scheduled._trace(frame, 'call', None))
        scheduled._select.assert_called_once_with(frame)
        scheduled.deferred.begin.assert_called_once_with(frame)
        self.assertEqual(scheduled.collector.errors, 0)

    def test_idle_observer_skips_bookkeeping_but_pending_proofs_are_observed(self):
        scheduled = ScheduledCalls(types.SimpleNamespace())
        scheduled.status = 'recording'
        scheduled.deferred.observe = mock.Mock()
        frame = types.SimpleNamespace()
        with mock.patch('sys.gettrace', return_value=scheduled.hook):
            scheduled.observe(frame, 'c_call', None)
            scheduled.deferred.observe.assert_not_called()
            scheduled.deferred.pending[123] = object()
            scheduled.observe(frame, 'c_call', None)
            scheduled.deferred.observe.assert_called_once_with(frame, 'c_call', None)

if __name__ == '__main__':
    unittest.main()

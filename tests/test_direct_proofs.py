"""Direct proof monitoring preserves exception safety and working fallbacks."""

import os
import sys
import unittest

import test_scheduled as helpers
import test_profile_lifecycle as lifecycle_helpers


@unittest.skipUnless(sys.version_info[:2] in ((3, 12), (3, 13))
                     and os.environ.get('CALLNAV_PROFILE_BACKEND') == 'monitoring'
                     and os.environ.get('CALLNAV_DIRECT_PROOFS') == '1',
                     'requires selected direct proof monitoring')
class DirectProofTests(unittest.TestCase):
    setUp = helpers.ScheduledTests.setUp
    tearDown = helpers.ScheduledTests.tearDown
    run_script = lifecycle_helpers.ProfileLifecycleTests.run_script

    def test_exception_traceback_access_never_runs_user_hooks(self):
        result, shard = self.run_script('''
            import json
            from pathlib import Path
            import sys
            import threading
            from callnav.collector import start
            collector = start()
            assert collector.profile_monitoring.active
            assert collector.scheduled.direct_monitoring
            assert sys.gettrace() is None and threading.gettrace() is None
            reads = []
            class Marker(Exception):
                def __getattribute__(self, name):
                    if name == '__traceback__':
                        reads.append(name)
                        raise AssertionError('user traceback hook executed')
                    return super().__getattribute__(name)
            def stream():
                try:
                    yield 7
                except Marker:
                    yield 11
            def consume():
                iterator = stream()
                assert next(iterator) == 7
                assert iterator.throw(Marker('injected')) == 11
                iterator.close()
            consume()
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(reads))
        ''')
        self.assertEqual(result, [])
        self.assertEqual(shard['process']['errors'], 0)
        self.assertTrue(shard['process']['direct_proof_monitoring'])
        self.assertEqual(shard['process']['scheduled_coroutines_status'], 'complete')
        self.assertTrue(any(edge['target']['qualname'] == 'stream' for edge in shard['edges']))

    def test_instruction_mask_failure_preserves_global_proofs_without_trace(self):
        result, shard = self.run_script('''
            import json
            from pathlib import Path
            import sys
            from callnav.collector import start
            collector = start()
            assert collector.scheduled.direct_monitoring
            api = sys.monitoring
            original = api.set_local_events
            fired = False
            def stream():
                yield 7
            def application():
                assert list(stream()) == [7]
                iterator = stream()
                return list(iterator)
            def fail_after_instruction_mask(tool, code, mask):
                global fired
                original(tool, code, mask)
                if code is application.__code__ and mask & api.events.INSTRUCTION and not fired:
                    fired = True
                    raise RuntimeError('injected partially applied instruction mask')
            api.set_local_events = fail_after_instruction_mask
            assert application() == [7]
            assert application() == [7]
            api.set_local_events = original
            backend = collector.profile_monitoring
            result = dict(fired=fired, lease=backend.instruction_lease,
                          no_trace=sys.gettrace() is None,
                          global_instructions=bool(api.get_events(backend.monitoring.tool_id)
                                                   & api.events.INSTRUCTION))
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(fired=True, lease=True, no_trace=True,
                                     global_instructions=True))
        self.assertEqual(shard['process']['errors'], 0)
        origins = [edge for edge in shard['edges']
                   if edge['kind'] == 'deferred' and edge['target']['qualname'] == 'stream']
        self.assertEqual(sum(edge['count'] for edge in origins), 2, origins)
        creation = next(number for number, line in enumerate(
            (self.root / 'run.py').read_text().splitlines(), 1)
                        if line.strip() == 'iterator = stream()')
        self.assertEqual({edge['caller']['line'] for edge in origins}, {creation})

    def test_activation_failure_restores_legacy_proofs(self):
        result, shard = self.run_script('''
            import json
            from pathlib import Path
            import sys
            import threading
            from callnav.collector import start
            original = sys.settrace
            fired = False
            def fail_after_removal(hook):
                global fired
                original(hook)
                if hook is None and not fired:
                    fired = True
                    raise RuntimeError('injected partially completed trace removal')
            sys.settrace = fail_after_removal
            collector = start()
            sys.settrace = original
            result = dict(fired=fired, direct=collector.scheduled.direct_monitoring,
                          active=collector.profile_monitoring.active,
                          restored=sys.gettrace() is collector.scheduled.hook,
                          future_restored=threading.gettrace() is collector.scheduled.hook)
            def stream():
                yield 7
            def application():
                iterator = stream()
                return list(iterator)
            assert application() == [7]
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(fired=True, direct=False, active=True,
                                     restored=True, future_restored=True))
        self.assertEqual(shard['process']['errors'], 0)
        self.assertFalse(shard['process']['direct_proof_monitoring'])
        origins = [edge for edge in shard['edges']
                   if edge['kind'] == 'deferred' and edge['target']['qualname'] == 'stream']
        self.assertEqual(sum(edge['count'] for edge in origins), 1, origins)


if __name__ == '__main__':
    unittest.main()

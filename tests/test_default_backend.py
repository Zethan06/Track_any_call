"""The normal CLI selects efficient capture without extra configuration."""

from contextlib import contextmanager
import json
import os
import sys
import unittest
from unittest import mock

import test_profile_lifecycle as lifecycle_helpers
import test_scheduled as helpers


@contextmanager
def default_environment():
    with mock.patch.dict(os.environ):
        os.environ.pop('CALLNAV_PROFILE_BACKEND', None)
        os.environ.pop('CALLNAV_DIRECT_PROOFS', None)
        yield


class DefaultBackendTests(unittest.TestCase):
    setUp = helpers.ScheduledTests.setUp
    tearDown = helpers.ScheduledTests.tearDown
    record = helpers.ScheduledTests.record

    def test_normal_cli_captures_without_backend_environment(self):
        with default_environment():
            path, index = self.record('''
                import json
                from pathlib import Path
                import sys
                import threading
                from callnav.collector import _collector
                def stream():
                    yield 7
                def application():
                    iterator = stream()
                    return list(iterator)
                assert application() == [7]
                Path(__file__).with_name('backend.json').write_text(json.dumps(dict(
                    trace=sys.gettrace() is not None,
                    future_trace=threading.gettrace() is not None)))
            ''')
        selected = sys.version_info[:2] in ((3, 12), (3, 13))
        state = json.loads(path.with_name('backend.json').read_text())
        self.assertEqual(state, dict(trace=not selected, future_trace=not selected))
        process = index['processes'][0]
        self.assertEqual(process['profile_backend'], 'local' if selected else 'legacy')
        self.assertEqual(process['direct_proof_monitoring'], selected)
        self.assertEqual(process['errors'], 0)
        self.assertTrue(any(edge['kind'] == 'deferred'
                            and edge['target']['qualname'] == 'stream'
                            for edge in index['edges']))

    @unittest.skipUnless(hasattr(sys, 'monitoring'), 'requires monitoring slots')
    def test_occupied_user_slots_keep_default_cli_proofs_via_legacy(self):
        with default_environment():
            result, shard = lifecycle_helpers.ProfileLifecycleTests.run_script(self, '''
                import json
                from pathlib import Path
                import sys
                api = sys.monitoring
                for tool in (3, 4):
                    api.use_tool_id(tool, 'foreign.occupied')
                from callnav.collector import start
                collector = start()
                def stream():
                    yield 7
                def application():
                    iterator = stream()
                    return list(iterator)
                assert application() == [7]
                result = dict(profile=sys.getprofile() is not None,
                              trace=sys.gettrace() is collector.scheduled.hook,
                              direct=collector.scheduled.direct_monitoring)
                collector.stop()
                assert all(api.get_tool(tool) == 'foreign.occupied' for tool in (3, 4))
                for tool in (3, 4):
                    api.free_tool_id(tool)
                Path(__file__).with_name('result.json').write_text(json.dumps(result))
            ''')
        self.assertEqual(result, dict(profile=True, trace=True, direct=False))
        self.assertEqual(shard['process']['profile_backend'], 'legacy')
        self.assertEqual(shard['process']['errors'], 0)
        self.assertTrue(any(edge['kind'] == 'deferred'
                            and edge['target']['qualname'] == 'stream'
                            for edge in shard['edges']))

    @unittest.skipUnless(sys.version_info[:2] in ((3, 12), (3, 13)),
                         'requires efficient backend selection')
    def test_audit_hook_refusal_falls_back_to_hybrid_with_proofs(self):
        with default_environment():
            result, shard = lifecycle_helpers.ProfileLifecycleTests.run_script(self, '''
                import json
                from pathlib import Path
                import sys
                def deny_new_audit_hooks(event, arguments):
                    if event == 'sys.addaudithook':
                        raise RuntimeError('foreign audit policy')
                sys.addaudithook(deny_new_audit_hooks)
                from callnav.collector import start
                collector = start()
                def stream():
                    yield 7
                def application():
                    values = stream()
                    return list(values)
                assert application() == [7]
                result = dict(backend=collector.profile_monitoring.backend_name,
                              direct=collector.scheduled.direct_monitoring)
                collector.stop()
                Path(__file__).with_name('result.json').write_text(json.dumps(result))
            ''')
        self.assertEqual(result, dict(backend='hybrid', direct=True))
        self.assertEqual(shard['process']['profile_backend'], 'hybrid')
        self.assertEqual(shard['process']['errors'], 0)
        self.assertTrue(any(edge['kind'] == 'deferred'
                            and edge['target']['qualname'] == 'stream'
                            for edge in shard['edges']))

    def test_explicit_legacy_diagnostic_retains_capture(self):
        with default_environment(), mock.patch.dict(os.environ, CALLNAV_PROFILE_BACKEND='legacy'):
            _, index = self.record('''
                def stream():
                    yield 7
                def application():
                    iterator = stream()
                    return list(iterator)
                assert application() == [7]
            ''')
        process = index['processes'][0]
        self.assertEqual(process['profile_backend'], 'legacy')
        self.assertFalse(process['direct_proof_monitoring'])
        self.assertTrue(any(edge['kind'] == 'deferred'
                            and edge['target']['qualname'] == 'stream'
                            for edge in index['edges']))


if __name__ == '__main__':
    unittest.main()

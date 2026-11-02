"""Failure and thread boundaries of the experimental profile backend."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

import test_scheduled as helpers


@unittest.skipUnless(sys.version_info[:2] in ((3, 12), (3, 13))
                     and os.environ.get('CALLNAV_PROFILE_BACKEND') == 'monitoring',
                     'requires selected experimental monitoring backend')
class ProfileLifecycleTests(unittest.TestCase):
    setUp = helpers.ScheduledTests.setUp
    tearDown = helpers.ScheduledTests.tearDown

    def run_script(self, content, shard_count=1):
        script = self.root / 'run.py'
        script.write_text(textwrap.dedent(content).lstrip('\n'), encoding='utf-8')
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'),
                   CALLNAV_ROOT=str(self.root), CALLNAV_RUN_DIR=str(self.output),
                   CALLNAV_RUN_ID='profile-lifecycle', CALLNAV_INCLUDE_ROOTS='[]')
        process = subprocess.run([sys.executable, str(script)], env=env, cwd=self.root,
                                 text=True, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        shards = list((self.output / 'shards').glob('*.json'))
        self.assertEqual(len(shards), shard_count)
        payloads = [json.loads(shard.read_text()) for shard in shards]
        return (json.loads(script.with_name('result.json').read_text()),
                payloads[0] if shard_count == 1 else payloads)

    def test_callback_failure_retires_proofs_and_releases_callables(self):
        result, shard = self.run_script('''
            import gc
            import json
            from pathlib import Path
            import weakref
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            assert backend.active
            def target():
                return 7
            reference = weakref.ref(target)
            result = {}
            def application():
                global target
                def fail_receive(*args):
                    raise RuntimeError('injected profile delivery failure')
                backend._receive = fail_receive
                assert target() == 7
                del target
                gc.collect()
                result.update(active=backend.active, released=reference() is None,
                              deferred=len(collector.scheduled.deferred.active),
                              scheduled=len(collector.scheduled.active),
                              status=collector.status, failure=backend.failure)
            application()
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertFalse(result['active'])
        self.assertTrue(result['released'])
        self.assertEqual(result['deferred'], 0)
        self.assertEqual(result['scheduled'], 0)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['failure'], 'Python entry callback failed')
        self.assertGreater(shard['process']['errors'], 0)

    def test_partial_registration_failure_keeps_working_legacy_proofs(self):
        result, shard = self.run_script('''
            import json
            from pathlib import Path
            import sys
            from callnav.collector import start
            api = sys.monitoring
            register = api.register_callback
            def fail_after_register(tool, event, callback):
                previous = register(tool, event, callback)
                if event == api.events.PY_START and callback is not None:
                    raise RuntimeError('injected partial registration')
                return previous
            api.register_callback = fail_after_register
            collector = start()
            api.register_callback = register
            result = dict(active=collector.profile_monitoring.active,
                          started=collector.profile_monitoring.started,
                          legacy=sys.getprofile() is not None)
            def stream():
                yield 7
            def consume(values):
                return list(values)
            def application():
                return consume(stream())
            assert application() == [7]
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(active=False, started=False, legacy=True))
        self.assertEqual(shard['process']['errors'], 0)
        origins = [edge for edge in shard['edges']
                   if edge['kind'] == 'deferred' and edge['target']['qualname'] == 'stream']
        self.assertEqual(sum(edge['count'] for edge in origins), 1)

    def test_native_mask_failure_keeps_instruction_proofs_and_global_coverage(self):
        result, shard = self.run_script('''
            import json
            from pathlib import Path
            import sys
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            assert backend.active
            api = sys.monitoring
            local_events = api.set_local_events
            def stream():
                yield 7
            def consume(values):
                return list(values)
            def application():
                return consume(stream())
            def fail_after_native_mask(tool, code, mask):
                local_events(tool, code, mask)
                if code is application.__code__ and mask & api.events.CALL:
                    raise RuntimeError('injected partial native mask update')
            api.set_local_events = fail_after_native_mask
            assert application() == [7]
            api.set_local_events = local_events
            tool = collector.scheduled.monitoring.tool_id
            result = dict(active=backend.active, lease=backend.native_lease,
                          instructions=bool(api.get_local_events(tool, application.__code__)
                                            & api.events.INSTRUCTION))
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(active=True, lease=True, instructions=True))
        self.assertEqual(shard['process']['errors'], 0)
        origins = [edge for edge in shard['edges']
                   if edge['kind'] == 'deferred' and edge['target']['qualname'] == 'stream']
        self.assertEqual(sum(edge['count'] for edge in origins), 1)

    def test_only_main_and_future_threading_threads_are_enrolled(self):
        result, shard = self.run_script('''
            import _thread
            import json
            from pathlib import Path
            import threading
            from callnav.collector import start
            ready = threading.Event()
            proceed = threading.Event()
            raw_done = threading.Event()
            states = {}
            collector = None
            def shared(label):
                states[label] = bool(getattr(collector.profile_monitoring.local, 'enabled', False))
            def old_worker():
                ready.set()
                assert proceed.wait(5)
                shared('old')
            old = threading.Thread(target=old_worker)
            old.start()
            assert ready.wait(5)
            collector = start()
            assert collector.profile_monitoring.active
            shared('main')
            proceed.set()
            old.join(5)
            assert not old.is_alive()
            def raw_worker():
                shared('raw')
                raw_done.set()
            _thread.start_new_thread(raw_worker, ())
            assert raw_done.wait(5)
            def new_worker():
                shared('new')
            new = threading.Thread(target=new_worker)
            new.start()
            new.join(5)
            assert not new.is_alive()
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(states))
        ''')
        self.assertEqual(result, dict(main=True, old=False, raw=False, new=True))
        self.assertEqual(shard['process']['errors'], 0)
        edges = [edge for edge in shard['edges'] if edge['target']['qualname'] == 'shared']
        self.assertEqual(sum(edge['count'] for edge in edges), 2, edges)
        self.assertEqual({edge['caller']['qualname'] for edge in edges}, {'<module>', 'new_worker'})

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires fork')
    def test_fork_preserves_surviving_threads_enrollment(self):
        result, shards = self.run_script('''
            import json
            import os
            from pathlib import Path
            import threading
            from callnav.collector import start
            ready = threading.Event()
            proceed = threading.Event()
            collector = None
            def leaf():
                return 7
            def worker(label):
                if label == 'old':
                    ready.set()
                    assert proceed.wait(5)
                child = os.fork()
                if child == 0:
                    assert leaf() == 7
                    backend = collector.profile_monitoring
                    enabled = bool(getattr(backend.local, 'enabled', False))
                    collector.stop()
                    Path(__file__).with_name(label + '.json').write_text(json.dumps(enabled))
                    os._exit(0)
                assert os.waitpid(child, 0)[1] == 0
            old = threading.Thread(target=worker, args=('old',))
            old.start()
            assert ready.wait(5)
            collector = start()
            assert collector.profile_monitoring.active
            proceed.set()
            old.join(10)
            assert not old.is_alive()
            new = threading.Thread(target=worker, args=('new',))
            new.start()
            new.join(10)
            assert not new.is_alive()
            collector.stop()
            states = {label: json.loads(Path(__file__).with_name(label + '.json').read_text())
                      for label in ('old', 'new')}
            Path(__file__).with_name('result.json').write_text(json.dumps(states))
        ''', shard_count=3)
        self.assertEqual(result, dict(old=False, new=True))
        self.assertTrue(all(shard['process']['errors'] == 0 for shard in shards))
        leaves = [edge for shard in shards for edge in shard['edges']
                  if edge['target']['qualname'] == 'leaf']
        self.assertEqual(sum(edge['count'] for edge in leaves), 1, leaves)


@unittest.skipUnless(sys.version_info[:2] in ((3, 12), (3, 13))
                     and os.environ.get('CALLNAV_PROFILE_BACKEND', 'auto') in ('auto', 'local'),
                     'requires selected local profile backend')
class LocalProfileLifecycleTests(unittest.TestCase):
    setUp = helpers.ScheduledTests.setUp
    tearDown = helpers.ScheduledTests.tearDown
    run_script = ProfileLifecycleTests.run_script
    test_callback_failure_retires_proofs_and_releases_callables = (
        ProfileLifecycleTests.test_callback_failure_retires_proofs_and_releases_callables)
    test_native_mask_failure_keeps_instruction_proofs_and_global_coverage = (
        ProfileLifecycleTests.test_native_mask_failure_keeps_instruction_proofs_and_global_coverage)
    test_only_main_and_future_threading_threads_are_enrolled = (
        ProfileLifecycleTests.test_only_main_and_future_threading_threads_are_enrolled)
    test_fork_preserves_surviving_threads_enrollment = (
        ProfileLifecycleTests.test_fork_preserves_surviving_threads_enrollment)


if __name__ == '__main__':
    unittest.main()

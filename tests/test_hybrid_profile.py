"""Behavioral checks for native profile dispatch with direct proof delivery."""

import os
import sys
import textwrap
import unittest

import test_direct_proofs as direct_helpers
import test_profile_lifecycle as lifecycle_helpers
import test_scheduled as helpers


SELECTED = (sys.version_info[:2] in ((3, 12), (3, 13))
            and os.environ.get('CALLNAV_PROFILE_BACKEND') == 'hybrid'
            and os.environ.get('CALLNAV_DIRECT_PROOFS', '1') == '1')


@unittest.skipUnless(SELECTED, 'requires hybrid direct proof backend')
class HybridDirectProofTests(direct_helpers.DirectProofTests):
    # Inherit the same user-hook, partially applied mask and trace removal
    # failure cases; class-level skip of the monitoring-only parent differs.
    __unittest_skip__ = not SELECTED


@unittest.skipUnless(SELECTED, 'requires hybrid direct proof backend')
class HybridProfileTests(unittest.TestCase):
    tearDown = helpers.ScheduledTests.tearDown
    def run_script(self, content, **kwargs):
        prefix = "import sys\nsys.path.insert(0, " + repr(str(self.directory)) + ")\n"
        return lifecycle_helpers.ProfileLifecycleTests.run_script(
            self, prefix + textwrap.dedent(content), **kwargs)

    def test_original_native_offsets_and_task_removal_without_monitor_call(self):
        result, shard = self.run_script('''
            import asyncio
            import json
            from pathlib import Path
            import sys
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            assert backend.active and collector.scheduled.direct_monitoring
            seen = []
            def callback(task):
                seen.append('done')
            async def worker():
                await asyncio.sleep(0)
                return 7
            async def main():
                task = asyncio.create_task(worker())
                task.add_done_callback(callback)
                import remove_external
                assert remove_external.remove(task, callback) == 1
                remove_external.add(task, callback)
                assert await task == 7
                await asyncio.sleep(0)
            def application(function):
                return function()
            def stream():
                yield 4
            assert application(lambda: 3) == 3
            assert list(stream()) == [4]
            asyncio.run(main())
            mask = sys.monitoring.get_events(backend.monitoring.tool_id)
            assert not mask & sys.monitoring.events.CALL
            result = dict(seen=seen, native_pending=not not collector.native_calls,
                          direct=collector.scheduled.direct_monitoring)
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(seen=['done'], native_pending=False, direct=True))
        self.assertEqual(shard['process']['errors'], 0)
        self.assertEqual(shard['process']['profile_backend'], 'hybrid')
        self.assertFalse(any(edge['kind'] == 'registered' for edge in shard['edges']))
        self.assertTrue(any(edge['kind'] == 'scheduled' and
                            edge['target']['qualname'] == 'worker' for edge in shard['edges']))

    def setUp(self):
        helpers.ScheduledTests.setUp(self)
        # Importable but deliberately outside selected source roots.
        (self.directory / 'remove_external.py').write_text(
            'def remove(task, callback):\n    return task.remove_done_callback(callback)\n'
            'def add(task, callback):\n    task.add_done_callback(callback)\n')
    def test_future_threads_only_and_profile_hook_restored_on_stop(self):
        result, shard = self.run_script('''
            import _thread
            import json
            from pathlib import Path
            import threading
            import sys
            from callnav.collector import start
            ready = threading.Event()
            proceed = threading.Event()
            raw_done = threading.Event()
            def old_target():
                return 1
            def raw_target():
                return 2
            def future_target():
                return 3
            def main_target():
                return 4
            def old_worker():
                ready.set()
                assert proceed.wait(5)
                old_target()
            old = threading.Thread(target=old_worker)
            old.start()
            assert ready.wait(5)
            collector = start()
            main_target()
            proceed.set()
            old.join(5)
            def raw_worker():
                raw_target()
                raw_done.set()
            _thread.start_new_thread(raw_worker, ())
            assert raw_done.wait(5)
            future = threading.Thread(target=future_target)
            future.start()
            future.join(5)
            collector.stop()
            result = dict(profile=sys.getprofile() is None,
                          future=threading.getprofile() is None,
                          trace=sys.gettrace() is None)
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(profile=True, future=True, trace=True))
        self.assertEqual(shard['process']['errors'], 0)
        names = {edge['target']['qualname'] for edge in shard['edges']}
        self.assertIn('main_target', names)
        self.assertIn('future_target', names)
        self.assertNotIn('old_target', names)
        self.assertNotIn('raw_target', names)

    def test_delivery_failure_stops_and_releases_snapshots(self):
        result, shard = self.run_script('''
            import gc
            import json
            from pathlib import Path
            import sys
            import weakref
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            def stream():
                yield 7
            reference = weakref.ref(stream)
            def fail(*args):
                raise RuntimeError('injected proof delivery failure')
            def application():
                iterator = stream()
                backend._proof_exception = fail
                raise ValueError('trigger')
            try:
                application()
            except ValueError:
                pass
            del stream
            gc.collect()
            result = dict(active=backend.active, status=collector.status,
                          released=reference() is None,
                          restored=sys.getprofile() is None,
                          deferred=len(collector.scheduled.deferred.active))
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(active=False, status='complete', released=True,
                                     restored=True, deferred=0))
        self.assertGreater(shard['process']['errors'], 0)
        self.assertEqual(shard['process']['profile_backend_failure'],
                         'proof exception callback failed')


    @unittest.skipUnless(hasattr(os, 'fork'), 'requires fork')
    def test_fork_from_old_and_future_threads_preserves_enrollment(self):
        result, shards = self.run_script('''
            import json
            import os
            from pathlib import Path
            import sys
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
                    enabled = sys.getprofile() is collector.profile_monitoring.hook
                    collector.stop()
                    Path(__file__).with_name(label + '.json').write_text(json.dumps(enabled))
                    os._exit(0)
                assert os.waitpid(child, 0)[1] == 0
            old = threading.Thread(target=worker, args=('old',))
            old.start()
            assert ready.wait(5)
            collector = start()
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


    def test_excluded_c_events_skip_consumer_but_python_entries_are_kept(self):
        (self.directory / 'native_external.py').write_text(
            'def work(callback):\n    assert abs(-7) == 7\n'
            '    assert len([1]) == 1\n    return callback()\n')
        result, shard = self.run_script('''
            import json
            from pathlib import Path
            import native_external
            from callnav.collector import start
            collector = start()
            original = collector.profile
            seen = []
            def observe(frame, event, arg):
                if frame.f_code is native_external.work.__code__:
                    seen.append(event)
                return original(frame, event, arg)
            collector.profile = observe
            def leaf():
                return 7
            assert native_external.work(leaf) == 7
            assert native_external.work(leaf) == 7
            collector.profile = original
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(seen))
        ''')
        self.assertEqual(result, ['call', 'return', 'call', 'return'])
        self.assertEqual(shard['process']['errors'], 0)


    def test_excluded_tool_takeover_is_detected_before_next_source_event(self):
        (self.directory / 'tool_external.py').write_text(
            'def steal(api, tool):\n    api.free_tool_id(tool)\n'
            '    api.use_tool_id(tool, "foreign.test.owner")\n'
            '    api.set_events(tool, api.events.RAISE)\n')
        result, shard = self.run_script('''
            import json
            from pathlib import Path
            import sys
            import tool_external
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            tool = backend.monitoring.tool_id
            tool_external.steal(sys.monitoring, tool)
            result = dict(status=collector.status, active=backend.active,
                          restored=sys.getprofile() is None,
                          owner=sys.monitoring.get_tool(tool),
                          mask=sys.monitoring.get_events(tool) == sys.monitoring.events.RAISE)
            sys.monitoring.free_tool_id(tool)
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(status='complete', active=False, restored=True,
                                     owner='foreign.test.owner', mask=True))
        self.assertGreater(shard['process']['errors'], 0)


    def test_unwatched_exceptions_do_not_enter_proof_bridge(self):
        (self.directory / 'fault_external.py').write_text(
            'def faults():\n    for _ in range(100):\n        try:\n'
            '            {}["missing"]\n        except KeyError:\n            pass\n')
        result, shard = self.run_script('''
            import fault_external
            import json
            from pathlib import Path
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            original = backend._proof_exception
            seen = []
            def inspect(frame, code, exception):
                if code is fault_external.faults.__code__:
                    seen.append('irrelevant')
                return original(frame, code, exception)
            backend._proof_exception = inspect
            def stream():
                yield 7
            def application():
                iterator = stream()
                fault_external.faults()
                return list(iterator)
            assert application() == [7]
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(seen))
        ''')
        self.assertEqual(result, [])
        self.assertEqual(shard['process']['errors'], 0)
        self.assertTrue(any(edge['target']['qualname'] == 'stream' and
                            edge['kind'] == 'deferred' for edge in shard['edges']))


if __name__ == '__main__':
    unittest.main()

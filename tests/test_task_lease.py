"""Task-only global CALL may retire without losing source C pairs or renewals."""

import os
import sys
import textwrap
import unittest

import test_profile_lifecycle as lifecycle_helpers
import test_scheduled as helpers


@unittest.skipUnless(sys.version_info[:2] in ((3, 12), (3, 13))
                     and os.environ.get('CALLNAV_PROFILE_BACKEND', 'auto') in ('auto', 'local', 'monitoring'),
                     'requires selected profile monitoring')
class TaskLeaseTests(unittest.TestCase):
    setUp = helpers.ScheduledTests.setUp
    tearDown = helpers.ScheduledTests.tearDown
    run_script = lifecycle_helpers.ProfileLifecycleTests.run_script

    def outside(self):
        (self.directory / 'outside.py').write_text(textwrap.dedent('''
            def remove(task, callback):
                return task.remove_done_callback(callback)
            def replace(task, callback):
                assert task.remove_done_callback(callback) == 1
                task.add_done_callback(callback)
        ''').lstrip(), encoding='utf-8')

    def test_release_inside_source_c_pair_and_later_reacquisition(self):
        self.outside()
        result, shard = self.run_script('''
            import asyncio
            import json
            from pathlib import Path
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))
            import outside
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            states = []
            called = []
            task = None
            first = True
            pending = -1
            original_profile = collector.profile
            def capture(frame, event, arg):
                global pending
                original_profile(frame, event, arg)
                if event == 'return' and frame.f_code is work.__code__:
                    # Read while recorder reentry is suppressed, rather than
                    # counting this observation's own len() C call.
                    pending = len(collector.native_calls)
            collector.profile = capture
            def done(value):
                called.append('done')
            def kept(value):
                called.append('kept')
            def stream():
                global first
                if first:
                    first = False
                    assert outside.remove(task, done) == 1
                    states.append(not backend.native_lease)
                yield 7
            def direct(values):
                return list(values)
            def work():
                for function in (list, direct):
                    assert function(stream()) == [7]
            async def main():
                global task
                gate = asyncio.Event()
                async def wait():
                    await gate.wait()
                task = asyncio.create_task(wait())
                task.add_done_callback(done)
                assert backend.native_lease
                work()
                task.add_done_callback(done)
                assert backend.native_lease
                outside.replace(task, done)
                assert not backend.native_lease
                task.add_done_callback(kept)
                assert backend.native_lease
                gate.set()
                await task
                await asyncio.sleep(0)
                assert not backend.native_lease
                assert not backend.api.get_events(backend.monitoring.tool_id) & backend.api.events.CALL
            asyncio.run(main())
            result = dict(states=states, called=sorted(called),
                          origins=collector.callbacks.task_origins,
                          pending=pending,
                          acquired=backend.leases_acquired, released=backend.leases_released)
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result['states'], [True])
        self.assertEqual(result['called'], ['done', 'kept'])
        self.assertEqual(result['origins'], 0)
        self.assertEqual(result['pending'], 0)
        self.assertGreaterEqual(result['acquired'], 3)
        self.assertEqual(result['acquired'], result['released'])
        direct = [edge for edge in shard['edges'] if edge['target']['qualname'] == 'direct']
        self.assertEqual(sum(edge['count'] for edge in direct), 1)
        self.assertTrue(all(edge['kind'] == 'python' and not edge['caller'].get('ambiguous')
                            for edge in direct))
        self.assertFalse(any(edge['kind'] == 'registered' and edge['target']['qualname'] == 'done'
                             for edge in shard['edges']))
        self.assertEqual(sum(edge['count'] for edge in shard['edges']
                             if edge['kind'] == 'registered' and edge['target']['qualname'] == 'kept'), 1)
        self.assertEqual(shard['process']['errors'], 0)

    def test_local_native_mask_refusal_keeps_global_fallback(self):
        result, shard = self.run_script('''
            import asyncio
            import json
            from pathlib import Path
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            api = backend.api
            original = api.set_local_events
            fired = False
            def stream():
                yield 7
            def work():
                iterator = stream()
                return list(iterator)
            def fail(tool, code, mask):
                global fired
                original(tool, code, mask)
                if code is work.__code__ and mask & api.events.CALL:
                    fired = True
                    raise RuntimeError('local native mask refused')
            def done(task):
                pass
            async def main():
                task = asyncio.create_task(asyncio.sleep(0))
                task.add_done_callback(done)
                assert backend.native_lease
                api.set_local_events = fail
                assert work() == [7]
                api.set_local_events = original
                await task
                await asyncio.sleep(0)
            asyncio.run(main())
            result = dict(fired=fired, fallback=backend.native_fallback,
                          lease=backend.native_lease, origins=collector.callbacks.task_origins,
                          actual=bool(api.get_events(backend.monitoring.tool_id) & api.events.CALL))
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(fired=True, fallback=True, lease=True, origins=0, actual=True))
        self.assertEqual(shard['process']['errors'], 0)
        self.assertEqual(sum(edge['count'] for edge in shard['edges']
                             if edge['kind'] == 'deferred' and edge['target']['qualname'] == 'stream'), 1)

    def test_partial_release_failure_restores_global_capture(self):
        result, shard = self.run_script('''
            import asyncio
            import json
            from pathlib import Path
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            api = backend.api
            original = api.set_events
            fired = False
            def fail(tool, mask):
                global fired
                original(tool, mask)
                if not mask & api.events.CALL and not fired:
                    fired = True
                    raise RuntimeError('global mask changed before failure')
            def done(task):
                pass
            async def main():
                task = asyncio.create_task(asyncio.sleep(0))
                task.add_done_callback(done)
                api.set_events = fail
                await task
                await asyncio.sleep(0)
                api.set_events = original
            asyncio.run(main())
            result = dict(fired=fired, fallback=backend.native_fallback,
                          lease=backend.native_lease, origins=collector.callbacks.task_origins,
                          actual=bool(api.get_events(backend.monitoring.tool_id) & api.events.CALL))
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(fired=True, fallback=True, lease=True, origins=0, actual=True))
        self.assertEqual(shard['process']['errors'], 0)
        self.assertEqual(sum(edge['count'] for edge in shard['edges']
                             if edge['kind'] == 'registered' and edge['target']['qualname'] == 'done'), 1)

    def test_concurrent_registration_reacquires_before_native_body(self):
        self.outside()
        result, shard = self.run_script('''
            import asyncio
            import json
            from pathlib import Path
            import sys
            import threading
            sys.path.insert(0, str(Path(__file__).parent.parent))
            import outside
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            api = backend.api
            original_events = api.set_events
            original_observe = collector.callbacks.observe
            prepared = threading.Event()
            proceed = threading.Event()
            registration = threading.Event()
            values = []
            shared = {}
            fired = False
            def first_done(task):
                values.append('first')
            def removed(task):
                values.append('removed')
            def kept(task):
                values.append('kept')
            def observe(frame, event, arg):
                needed = original_observe(frame, event, arg)
                if (event == 'c_call' and getattr(arg, '__name__', '') == 'add_done_callback'
                        and getattr(arg, '__self__', None) is shared.get('task')):
                    registration.set()
                return needed
            collector.callbacks.observe = observe
            def worker():
                async def run():
                    gate = asyncio.Event()
                    async def wait():
                        await gate.wait()
                    task = asyncio.create_task(wait())
                    shared['task'] = task
                    prepared.set()
                    assert proceed.wait(10)
                    task.add_done_callback(removed)
                    assert backend.native_lease
                    outside.replace(task, removed)
                    task.add_done_callback(kept)
                    gate.set()
                    await task
                    await asyncio.sleep(0)
                asyncio.run(run())
            thread = threading.Thread(target=worker)
            thread.start()
            assert prepared.wait(10)
            def overlap(tool, mask):
                global fired
                previous_mask = api.get_events(tool)
                original_events(tool, mask)
                # Interleave at an actual CALL removal, rather than a proof
                # mask change that leaves an already-disabled CALL unchanged.
                if (previous_mask & api.events.CALL
                        and not mask & api.events.CALL and not fired):
                    fired = True
                    proceed.set()
                    assert registration.wait(10)
            api.set_events = overlap
            async def main():
                task = asyncio.create_task(asyncio.sleep(0))
                task.add_done_callback(first_done)
                await task
                await asyncio.sleep(0)
            asyncio.run(main())
            thread.join(10)
            assert not thread.is_alive()
            api.set_events = original_events
            collector.callbacks.observe = original_observe
            result = dict(fired=fired, registered=registration.is_set(), values=sorted(values),
                          origins=collector.callbacks.task_origins, fallback=backend.native_fallback)
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(fired=True, registered=True,
                                     values=['first', 'kept', 'removed'], origins=0, fallback=False))
        self.assertEqual(shard['process']['errors'], 0)
        registered = {edge['target']['qualname']: edge['count'] for edge in shard['edges']
                      if edge['kind'] == 'registered'}
        self.assertEqual(registered.get('first_done'), 1)
        self.assertEqual(registered.get('kept'), 1)
        self.assertNotIn('removed', registered)

    def test_manual_legacy_delivery_can_renew_callback_interest(self):
        self.outside()
        result, shard = self.run_script('''
            import asyncio
            import json
            from pathlib import Path
            import sys
            import threading
            sys.path.insert(0, str(Path(__file__).parent.parent))
            import outside
            from callnav.collector import start
            collector = start()
            backend = collector.profile_monitoring
            called = []
            def warm(task):
                called.append('warm')
            def removed(task):
                called.append('removed')
            async def main():
                first = asyncio.create_task(asyncio.sleep(0))
                first.add_done_callback(warm)
                await first
                await asyncio.sleep(0)
                assert not backend.native_lease
                sys.setprofile(collector.profile)
                second = asyncio.create_task(asyncio.sleep(0))
                second.add_done_callback(removed)
                assert backend.native_lease
                assert backend.api.get_events(backend.monitoring.tool_id) & backend.api.events.CALL
                worker = threading.Thread(target=outside.replace, args=(second, removed))
                worker.start()
                worker.join(5)
                assert not worker.is_alive()
                sys.setprofile(None)
                await second
                await asyncio.sleep(0)
            asyncio.run(main())
            result = dict(called=sorted(called), origins=collector.callbacks.task_origins)
            collector.stop()
            Path(__file__).with_name('result.json').write_text(json.dumps(result))
        ''')
        self.assertEqual(result, dict(called=['removed', 'warm'], origins=0))
        self.assertEqual(shard['process']['errors'], 0)
        self.assertFalse(any(edge['kind'] == 'registered' and edge['target']['qualname'] == 'removed'
                             for edge in shard['edges']))


if __name__ == '__main__':
    unittest.main()

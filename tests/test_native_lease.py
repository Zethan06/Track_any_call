"""A Task-only global lease must retain later source and wrapper native proofs."""

import os
import sys
import textwrap
import unittest

import test_scheduled as helpers


@unittest.skipUnless(sys.version_info[:2] in ((3, 12), (3, 13))
                     and os.environ.get('CALLNAV_PROFILE_BACKEND', 'auto') in ('auto', 'monitoring', 'hybrid', 'local'),
                     'requires selected profile monitoring')
class NativeLeaseTests(unittest.TestCase):
    setUp = helpers.ScheduledTests.setUp
    tearDown = helpers.ScheduledTests.tearDown
    record = helpers.ScheduledTests.record

    def test_later_source_and_external_wrapper_proofs_survive_excluded_removal(self):
        (self.directory / 'outside.py').write_text(textwrap.dedent('''
            import functools
            def decorate(function):
                @functools.wraps(function)
                async def wrapper(*args, **kwargs):
                    if kwargs.pop('forward', True):
                        return await function(*args, **kwargs)
                    return 'skipped'
                return wrapper
            def replace(task, callback):
                assert task.remove_done_callback(callback) == 1
                task.add_done_callback(callback)
        ''').lstrip(), encoding='utf-8')
        _, index = self.record('''
            import asyncio
            from pathlib import Path
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))
            import outside
            from callnav.collector import _collector
            called = []
            class Result:
                def method(self):
                    return 11
            def stream():
                yield 7
            def consume():
                iterator = stream()
                return list(iterator)
            def work():
                assert consume() == [7]
                receiver = Result()
                assert getattr(receiver, 'method')() == 11
                assert getattr(Result(), 'method')() == 11
            def removed(task):
                called.append('removed')
            def kept(task):
                called.append('kept')
            class Worker:
                @outside.decorate
                async def actual(self):
                    await asyncio.sleep(0)
                    return 'actual'
                @outside.decorate
                async def skipped(self):
                    return 'body'
            async def main():
                gate = asyncio.Event()
                async def wait():
                    await gate.wait()
                task = asyncio.create_task(wait())
                task.add_done_callback(removed)
                import os
                assert _collector.profile_monitoring.native_lease == (
                    os.environ.get('CALLNAV_PROFILE_BACKEND', 'auto') in ('auto', 'local', 'monitoring'))
                outside.replace(task, removed)
                task.add_done_callback(kept)
                work()
                worker = Worker()
                assert await worker.actual() == 'actual'
                assert await worker.skipped(forward=False) == 'skipped'
                gate.set()
                await task
                await asyncio.sleep(0)
            asyncio.run(main())
            assert sorted(called) == ['kept', 'removed']
        ''')
        def target(name, kind):
            return [edge for edge in index['edges']
                    if edge['target']['qualname'] == name and edge['kind'] == kind]
        for name, kind in [('stream', 'deferred'), ('Worker.actual', 'wrapped'),
                           ('kept', 'registered')]:
            edges = target(name, kind)
            self.assertEqual(sum(edge['count'] for edge in edges), 1, (name, edges))
            self.assertTrue(all(edge['caller']['precision'] == 'instruction'
                                and not edge['caller'].get('ambiguous') for edge in edges),
                            (name, edges))
        methods = target('Result.method', 'python')
        self.assertEqual(sum(edge['count'] for edge in methods), 2)
        self.assertEqual(sum(edge['count'] for edge in methods
                             if not edge['caller'].get('ambiguous')), 1)
        # The anonymous constructor has no initializer entry proving its
        # returned object. Its unresolved call must remain conservative.
        self.assertEqual(sum(edge['count'] for edge in methods
                             if edge['caller'].get('reason') == 'unresolved_callee'), 1)
        self.assertEqual(target('removed', 'registered'), [])
        self.assertFalse(any(edge['target']['qualname'] == 'Worker.skipped'
                             for edge in index['edges']))


if __name__ == '__main__':
    unittest.main()

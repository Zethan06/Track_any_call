"""Native suspended objects retain their real creation sites across consumers."""

import dis
import json
import shutil
import subprocess
import sys
import types
import unittest

import test_scheduled as scheduled_helpers

from callnav.deferred import DeferredCalls


class DeferredTests(unittest.TestCase):
    setUp = scheduled_helpers.ScheduledTests.setUp
    tearDown = scheduled_helpers.ScheduledTests.tearDown
    record = scheduled_helpers.ScheduledTests.record
    # Keep the subprocess harness without inheriting the scheduled test cases.
    @staticmethod
    def deferred(index):
        return [edge for edge in index['edges'] if edge['kind'] == 'deferred']

    def test_untracked_library_returns_do_not_disassemble_external_functions(self):
        class Collector:
            def __init__(self):
                self.instructions = {}

            def _suspended(self, frame):
                self.instructions[frame.f_code] = tuple(dis.get_instructions(frame.f_code))
                return False

        collector = Collector()
        observer = DeferredCalls(types.SimpleNamespace(collector=collector))
        namespace = {}
        exec(compile('\n'.join(f'def external_{i}(): return {i}' for i in range(100)),
                     '/external/library.py', 'exec'), namespace)
        for function in namespace.values():
            if type(function) is types.FunctionType:
                observer.observe(types.SimpleNamespace(f_code=function.__code__), 'return', 0)
        self.assertEqual(collector.instructions, {})

    def test_create_task_uses_one_canonical_proof_and_one_forwarding_count(self):
        path, index = self.record('''
            import asyncio
            import functools
            def decorate(function):
                @functools.wraps(function)
                async def wrapped(*args, **kwargs):
                    return await function(*args, **kwargs)
                return wrapped
            @decorate
            async def leaf():
                await asyncio.sleep(0)
            class Worker:
                @decorate
                async def run(self):
                    await asyncio.sleep(0)
            async def main():
                worker = Worker()
                for _ in range(3):
                    await asyncio.create_task(leaf())
                    await asyncio.create_task(worker.run())
            asyncio.run(main())
        ''')
        lines = path.read_text().splitlines()
        points = []
        for token, expected, kind in [('leaf()', 'leaf', 'scheduled'),
                                      ('worker.run()', 'Worker.run', 'deferred')]:
            number = next(i for i, line in enumerate(lines, 1)
                          if f'create_task({token})' in line)
            edges = [edge for edge in index['edges'] if edge['caller']['line'] == number]
            direct = [edge for edge in edges if edge['kind'] != 'wrapped']
            self.assertEqual(len(direct), 1, edges)
            self.assertEqual(direct[0]['kind'], kind)
            self.assertEqual(direct[0]['count'], 3)
            self.assertEqual(direct[0]['wrapper_forwarded_count'], 3)
            projected = [edge for edge in edges if edge['kind'] == 'wrapped']
            self.assertEqual(len(projected), 1, edges)
            self.assertEqual(projected[0]['target']['qualname'], expected)
            self.assertEqual(projected[0]['count'], 3)
            points.append({'line': number - 1, 'character': direct[0]['caller']['column'],
                           'expected': expected})
        node = shutil.which('node')
        if node is not None:
            program = '''
                const fs = require('node:fs');
                const { RuntimeIndex } = require(process.argv[1]);
                const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
                const file = process.argv[3];
                const source = fs.readFileSync(file, 'utf8');
                const points = JSON.parse(process.argv[4]);
                const document = { uri: { fsPath: file }, isDirty: false, getText: () => source };
                (async () => {
                    const index = new RuntimeIndex(data);
                    const results = [];
                    for (const point of points) {
                        const result = await index.lookup(document, point, [document]);
                        results.push({ issue: result.issue,
                            targets: result.edges.map(edge => edge.target.qualname) });
                    }
                    process.stdout.write(JSON.stringify(results));
                })().catch(error => { console.error(error); process.exitCode = 1; });
            '''
            process = subprocess.run(
                [node, '-e', program, str(scheduled_helpers.PROJECT / 'vscode/src/index.js'),
                 str(self.output / 'index.json'), str(path), json.dumps(points)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout), [
                {'issue': None, 'targets': ['leaf']},
                {'issue': None, 'targets': ['Worker.run']},
            ])

    def test_native_generator_tuple_loop_records_each_actual_object_once(self):
        _, index = self.record('''
            class Worker:
                def non_expert(self):
                    yield 1
                    yield 2
                def expert(self):
                    yield 3
                def run(self):
                    output = []
                    for chunk_iter in (self.non_expert(), self.expert()):
                        for value in chunk_iter:
                            output.append(value)
                    return output
            assert Worker().run() == [1, 2, 3]
        ''')
        origins = [edge for edge in self.deferred(index) if edge['caller']['qualname'] == 'Worker.run']
        self.assertEqual({edge['target']['qualname'] for edge in origins},
                         {'Worker.non_expert', 'Worker.expert'})
        self.assertEqual([edge['count'] for edge in origins], [1, 1])
        self.assertEqual(len({edge['caller']['column'] for edge in origins}), 2)

    def test_generator_arguments_forwarded_to_native_chain_keep_creation_sites(self):
        _, index = self.record('''
            import itertools
            def first():
                yield 1
            def second():
                yield 2
            def chain(first_iterator, second_iterator):
                return itertools.chain(first_iterator, second_iterator)
            assert list(chain(first(), second())) == [1, 2]
        ''')
        origins = self.deferred(index)
        self.assertEqual({edge['target']['qualname'] for edge in origins}, {'first', 'second'})
        self.assertEqual([edge['count'] for edge in origins], [1, 1])

    def test_simple_listcomp_gather_keeps_per_iteration_native_binding_and_lookup(self):
        path, index = self.record('''
            import asyncio
            class First:
                async def init(self):
                    await asyncio.sleep(0)
                    return 1
            class Second:
                async def init(self):
                    await asyncio.sleep(0)
                    return 2
            async def main():
                servers = [First(), Second()]
                assert await asyncio.gather(*[server.init() for server in servers]) == [1, 2]
            asyncio.run(main())
        ''')
        line = next(i for i, value in enumerate(path.read_text().splitlines(), 1)
                    if 'gather(*[server.init()' in value)
        origins = [edge for edge in self.deferred(index) if edge['caller']['line'] == line]
        self.assertEqual({edge['target']['qualname'] for edge in origins}, {'First.init', 'Second.init'})
        self.assertEqual([edge['count'] for edge in origins], [1, 1])
        node = shutil.which('node')
        if node is not None:
            program = '''
                const fs = require('node:fs');
                const { RuntimeIndex } = require(process.argv[1]);
                const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
                const file = process.argv[3];
                const source = fs.readFileSync(file, 'utf8');
                const document = { uri: { fsPath: file }, isDirty: false, getText: () => source };
                new RuntimeIndex(data).lookup(document, JSON.parse(process.argv[4]), [document])
                    .then(result => process.stdout.write(JSON.stringify({ issue: result.issue,
                        targets: result.edges.map(edge => edge.target.qualname).sort() })))
                    .catch(error => { console.error(error); process.exitCode = 1; });
            '''
            process = subprocess.run(
                [node, '-e', program, str(scheduled_helpers.PROJECT / 'vscode/src/index.js'),
                 str(self.output / 'index.json'), str(path),
                 json.dumps({'line': line - 1, 'character': origins[0]['caller']['column']})],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout),
                             {'issue': None, 'targets': ['First.init', 'Second.init']})

    def test_listcomp_factory_previous_same_code_coroutines_are_not_creation_proofs(self):
        _, index = self.record('''
            import asyncio
            class Worker:
                async def init(self):
                    await asyncio.sleep(0)
                    return 7
            class Factory:
                def init(self):
                    return previous
            async def main():
                global previous
                worker = Worker()
                previous = worker.init()
                assert await asyncio.gather(*[server.init() for server in [Factory(), worker]]) == [7, 7]
            asyncio.run(main())
        ''')
        origins = [edge for edge in self.deferred(index) if edge['target']['qualname'] == 'Worker.init']
        self.assertEqual(len(origins), 1, origins)
        self.assertEqual(origins[0]['count'], 1)

    def test_listcomp_native_callable_load_survives_receiver_rebinding_in_argument(self):
        _, index = self.record('''
            import asyncio
            class Worker:
                async def init(self, value):
                    await asyncio.sleep(0)
                    return value
            async def replacement(value):
                raise AssertionError('replacement must not execute')
            def mutate(server):
                server.init = replacement
                return 7
            async def main():
                servers = [Worker(), Worker()]
                assert await asyncio.gather(*[server.init(mutate(server)) for server in servers]) == [7, 7]
            asyncio.run(main())
        ''')
        origins = [edge for edge in self.deferred(index) if edge['target']['qualname'] == 'Worker.init']
        self.assertEqual(len(origins), 1, origins)
        # The second argument callback invalidates the earlier list entry;
        # this final native construction still has an uninterrupted receipt.
        self.assertEqual(origins[0]['count'], 1)

    def test_listcomp_iterator_cannot_substitute_an_older_same_code_coroutine(self):
        _, index = self.record('''
            import asyncio
            import gc
            import types
            class Worker:
                async def init(self):
                    await asyncio.sleep(0)
                    return 7
            previous = Worker().init()
            mutated = False
            class Sources:
                def __init__(self):
                    self.done = False
                def __iter__(self):
                    return self
                def __next__(self):
                    global mutated
                    if not self.done:
                        self.done = True
                        return Worker()
                    fresh = None
                    for candidate in gc.get_objects():
                        if (type(candidate) is types.CoroutineType and candidate is not previous
                                and candidate.cr_code is Worker.init.__code__):
                            fresh = candidate
                            break
                    assert fresh is not None
                    for container in gc.get_referrers(fresh):
                        if type(container) is list and len(container) == 1 and container[0] is fresh:
                            container[0] = previous
                            mutated = True
                    fresh.close()
                    raise StopIteration
            async def main():
                assert await asyncio.gather(*[server.init() for server in Sources()]) == [7]
                assert mutated
            asyncio.run(main())
        ''')
        self.assertFalse([edge for edge in self.deferred(index)
                          if edge['target']['qualname'] == 'Worker.init'])

    def test_listcomp_argument_cannot_swap_earlier_entry_for_old_same_code_coroutine(self):
        _, index = self.record('''
            import asyncio
            import gc
            import types
            async def actual(value):
                return value
            old = actual(9)
            def mutate(index):
                if index:
                    changed = 0
                    for value in gc.get_objects():
                        if (type(value) is types.CoroutineType and value.cr_code is actual.__code__
                                and value is not old):
                            for container in gc.get_referrers(value):
                                if type(container) is list and len(container) == 1 and container[0] is value:
                                    container[0] = old
                                    changed += 1
                            value.close()
                    assert changed == 1, changed
                return index
            async def main():
                assert await asyncio.gather(*[actual(mutate(i)) for i in range(2)]) == [9, 1]
            asyncio.run(main())
        ''')
        origins = [edge for edge in self.deferred(index) if edge['target']['qualname'] == 'actual']
        self.assertEqual(len(origins), 1, origins)
        self.assertEqual(origins[0]['count'], 1)

    def test_assigned_native_generators_use_actual_stored_objects(self):
        _, index = self.record('''
            import asyncio
            def stream():
                yield 1
                yield 2
            async def async_stream():
                yield 3
                await asyncio.sleep(0)
                yield 4
            def drive():
                iterator = stream()
                return list(iterator)
            async def main():
                iterator = async_stream()
                assert [value async for value in iterator] == [3, 4]
            assert drive() == [1, 2]
            asyncio.run(main())
        ''')
        origins = {(edge['caller']['qualname'], edge['target']['qualname']): edge['count']
                   for edge in self.deferred(index)}
        self.assertEqual(origins[('drive', 'stream')], 1)
        self.assertEqual(origins[('main', 'async_stream')], 1)

    @unittest.skipIf(sys.version_info < (3, 13), 'writable frame locals require Python 3.13')
    def test_store_destructor_cannot_replace_native_generator_receipt(self):
        _, index = self.record('''
            import sys
            old = None
            class Rebind:
                def __del__(self):
                    sys._getframe(1).f_locals['item'] = old
            def actual():
                yield 4
            def run():
                global old
                old = actual()
                item = Rebind()
                item = actual()
                assert item is old
                assert list(item) == [4]
                old = None
            run()
        ''')
        self.assertFalse([edge for edge in self.deferred(index)
                          if edge['target']['qualname'] == 'actual'])

    @unittest.skipIf(sys.version_info < (3, 13), 'writable frame locals require Python 3.13')
    def test_tuple_loop_destructor_cannot_replace_native_generator_receipt(self):
        _, index = self.record('''
            import sys
            old = None
            class Rebind:
                def __del__(self):
                    sys._getframe(1).f_locals['item'] = old
            def actual():
                yield 4
            def run():
                global old
                old = actual()
                item = Rebind()
                for item in (actual(),):
                    assert item is old
                    assert list(item) == [4]
                old = None
            run()
        ''')
        self.assertFalse([edge for edge in self.deferred(index)
                          if edge['target']['qualname'] == 'actual'])

    def test_direct_asyncfor_vm_receipt_and_nested_native_generator_arguments(self):
        _, index = self.record('''
            import asyncio
            async def chunks():
                yield 1
                await asyncio.sleep(0)
                yield 2
            async def merge(chunks, size):
                async for value in chunks:
                    yield value + size
            async def ensure_async_iterator(values):
                async for value in values:
                    yield value
            async def main():
                output = []
                async for value in merge(chunks(), 10):
                    output.append(value)
                assert output == [11, 12]
                output = []
                async for value in ensure_async_iterator(chunks()):
                    output.append(value)
                assert output == [1, 2]
            asyncio.run(main())
        ''')
        origins = [edge for edge in self.deferred(index) if edge['caller']['qualname'] == 'main']
        by_target = {}
        for edge in origins:
            by_target.setdefault(edge['target']['qualname'], []).append(edge['count'])
        self.assertEqual(by_target.get('merge'), [1])
        self.assertEqual(by_target.get('chunks'), [1, 1])
        self.assertEqual(by_target.get('ensure_async_iterator'), [1])

    def test_asyncfor_factory_old_generator_and_custom_protocol_do_not_gain_creation(self):
        _, index = self.record('''
            import asyncio
            async def actual():
                yield 7
            def factory():
                return existing
            class Proxy:
                def __aiter__(self):
                    return existing
            async def main():
                global existing
                existing = actual()
                async for value in factory():
                    assert value == 7
                existing = actual()
                async for value in Proxy():
                    assert value == 7
            asyncio.run(main())
        ''')
        self.assertFalse([edge for edge in self.deferred(index)
                          if edge['target']['qualname'] == 'actual'])

    def test_direct_asyncfor_loaded_native_binding_survives_argument_rebinding(self):
        _, index = self.record('''
            import asyncio
            class Worker:
                async def stream(self, value):
                    yield value
                    await asyncio.sleep(0)
                    yield value + 1
            async def replacement(value):
                raise AssertionError('replacement should not execute')
                yield value
            def mutate(worker):
                worker.stream = replacement
                return 7
            async def main():
                worker = Worker()
                output = []
                async for value in worker.stream(mutate(worker)):
                    output.append(value)
                assert output == [7, 8]
            asyncio.run(main())
        ''')
        origins = [edge for edge in self.deferred(index)
                   if edge['target']['qualname'] == 'Worker.stream']
        self.assertEqual(len(origins), 1, origins)
        self.assertEqual(origins[0]['count'], 1)

    def test_tuple_generator_factories_old_objects_and_unconsumed_do_not_borrow(self):
        _, index = self.record('''
            def make(value):
                def generated():
                    yield value
                return generated
            first, second = make(1), make(2)
            previous = second()
            def factory():
                return previous
            def run():
                output = []
                for iterator in (first(), factory(), second()):
                    if iterator.gi_frame.f_locals['value'] == 1:
                        iterator.close()
                        continue
                    output.extend(iterator)
                return output
            assert run() == [2, 2]
        ''')
        lines = self.root.joinpath('run.py').read_text().splitlines()
        number = next(i for i, line in enumerate(lines, 1) if 'for iterator in (first(),' in line)
        origins = [edge for edge in self.deferred(index) if edge['caller']['line'] == number]
        self.assertEqual(len(origins), 1, origins)
        self.assertEqual(origins[0]['caller']['column'], lines[number - 1].index('second()'))
        self.assertEqual(origins[0]['count'], 1)

    def test_bash_style_asyncio_run_method_and_threadsafe_handoff(self):
        _, index = self.record('''
            import asyncio
            import threading
            class Worker:
                async def loop(self):
                    await asyncio.sleep(0)
            async def child():
                await asyncio.sleep(0)
            def handoff(coro):
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever)
                thread.start()
                asyncio.run_coroutine_threadsafe(coro, loop).result()
                loop.call_soon_threadsafe(loop.stop)
                thread.join()
                loop.close()
            asyncio.run(Worker().loop())
            worker = Worker()
            asyncio.run(worker.loop())
            handoff(child())
        ''')
        observed = {(edge['caller']['qualname'], edge['target']['qualname']) for edge in self.deferred(index)}
        self.assertIn(('<module>', 'Worker.loop'), observed)
        self.assertIn(('<module>', 'child'), observed)

    def test_sync_wrapped_coroutine_and_async_generator_return_exact_objects(self):
        _, index = self.record('''
            import asyncio
            import functools
            from contextlib import nullcontext
            def decorate(function):
                @functools.wraps(function)
                def wrapped(*args, **kwargs):
                    with nullcontext():
                        return function(*args, **kwargs)
                return wrapped
            class Worker:
                @decorate
                async def send(self):
                    await asyncio.sleep(0)
                @decorate
                async def receive(self):
                    yield 7
            async def main():
                worker = Worker()
                await worker.send()
                async for value in worker.receive():
                    assert value == 7
            asyncio.run(main())
        ''')
        origins = {(edge['caller']['qualname'], edge['target']['qualname']) for edge in self.deferred(index)}
        self.assertIn(('main', 'Worker.send'), origins)
        self.assertIn(('main', 'Worker.receive'), origins)

    def test_external_context_decorator_proves_in_root_original_body(self):
        self.directory.joinpath('external_decorator.py').write_text('''
import functools
from contextlib import nullcontext
def decorate(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with nullcontext():
            return function(*args, **kwargs)
    return wrapped
''')
        _, index = self.record('''
            import asyncio
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from external_decorator import decorate
            @decorate
            async def send():
                await asyncio.sleep(0)
            @decorate
            async def receive():
                yield 7
            async def main():
                await send()
                await asyncio.sleep(0)
                await send()
                async for value in receive():
                    assert value == 7
            asyncio.run(main())
        ''')
        targets = [(edge['caller']['qualname'], edge['target']['qualname'], edge['count'])
                   for edge in self.deferred(index)]
        self.assertEqual(len([item for item in targets if item[:2] == ('main', 'send')]), 2)
        self.assertIn(('main', 'receive', 1), targets)

    def test_stolen_precreated_object_and_cancelled_body_do_not_project(self):
        _, index = self.record('''
            import asyncio
            import functools
            async def actual():
                await asyncio.sleep(0)
            @functools.wraps(actual)
            def stolen():
                return existing
            async def main():
                global existing
                existing = actual()
                await stolen()
                cancelled = asyncio.create_task(actual())
                cancelled.cancel()
                try:
                    await cancelled
                except asyncio.CancelledError:
                    pass
            asyncio.run(main())
        ''')
        lines = self.root.joinpath('run.py').read_text().splitlines()
        forbidden = {number for number, line in enumerate(lines, 1) if 'await stolen()' in line or 'create_task(actual())' in line}
        self.assertFalse([edge for edge in self.deferred(index) if edge['caller']['line'] in forbidden])

    def test_finally_return_replaces_created_object(self):
        _, index = self.record('''
            import asyncio
            import functools
            import warnings
            warnings.simplefilter('ignore', RuntimeWarning)
            async def actual():
                await asyncio.sleep(0)
            @functools.wraps(actual)
            def replaced():
                try:
                    return actual()
                finally:
                    return existing
            async def main():
                global existing
                existing = actual()
                await replaced()
            asyncio.run(main())
        ''')
        self.assertFalse([edge for edge in self.deferred(index)
                          if edge['caller']['qualname'] == 'replaced'])
        lines = self.root.joinpath('run.py').read_text().splitlines()
        outer = next(number for number, line in enumerate(lines, 1) if 'await replaced()' in line)
        self.assertFalse([edge for edge in self.deferred(index) if edge['caller']['line'] == outer])

    def test_attribute_global_load_ignores_injected_locals(self):
        _, index = self.record('''
            import asyncio
            class AsyncWorker:
                async def run(self):
                    await asyncio.sleep(0)
            class Factory:
                def run(self):
                    return existing
            worker = Factory()
            def launch():
                global existing
                spoof = AsyncWorker()
                existing = spoof.run()
                locals()['worker'] = spoof
                asyncio.run(worker.run())
            launch()
        ''')
        lines = self.root.joinpath('run.py').read_text().splitlines()
        outer = next(number for number, line in enumerate(lines, 1) if 'asyncio.run(worker.run())' in line)
        self.assertFalse([edge for edge in self.deferred(index) if edge['caller']['line'] == outer])

    def test_same_code_different_closure_is_not_a_declared_wrapper_forward(self):
        _, index = self.record('''
            import asyncio
            import functools
            def make(value):
                async def body():
                    await asyncio.sleep(0)
                    return value
                return body
            first, second = make(1), make(2)
            @functools.wraps(first)
            def wrapper():
                return second()
            async def main():
                assert await wrapper() == 2
            asyncio.run(main())
        ''')
        self.assertFalse([edge for edge in self.deferred(index) if edge['caller']['qualname'] == 'main'])

    def test_partial_receiver_does_not_shift_creation_to_older_argument(self):
        _, index = self.record('''
            import asyncio
            import functools
            async def actual():
                await asyncio.sleep(0)
            def consume(previous, current):
                current.close()
                asyncio.run(previous)
            previous = actual()
            receiver = functools.partial(consume, previous)
            receiver(actual())
        ''')
        lines = self.root.joinpath('run.py').read_text().splitlines()
        origin = next(number for number, line in enumerate(lines, 1) if 'receiver(actual())' in line)
        self.assertFalse([edge for edge in self.deferred(index) if edge['caller']['line'] == origin])

    def test_unconsumed_closure_cycle_is_not_retained_by_pending_origin(self):
        _, index = self.record('''
            import gc
            import warnings
            import weakref
            from callnav.collector import _collector
            warnings.simplefilter('ignore', RuntimeWarning)
            class Payload:
                pass
            def accept(coro):
                return coro
            def build():
                payload = Payload()
                holder = {'payload': payload}
                async def child():
                    return holder
                coroutine = accept(child())
                holder['function'] = child
                holder['coroutine'] = coroutine
                return weakref.ref(payload), weakref.ref(child), weakref.ref(coroutine)
            references = build()
            gc.collect()
            assert all(reference() is None for reference in references)
            assert not _collector.scheduled.deferred.pending
        ''')
        self.assertEqual(self.deferred(index), [])

    def test_later_wrapper_invocation_cannot_reuse_old_frame_creation(self):
        _, index = self.record('''
            import asyncio
            import functools
            async def actual():
                await asyncio.sleep(0)
            existing = None
            @functools.wraps(actual)
            def wrapper():
                if existing is None:
                    return actual()
                return existing
            existing = wrapper()
            asyncio.run(wrapper())
        ''')
        lines = self.root.joinpath('run.py').read_text().splitlines()
        later = next(number for number, line in enumerate(lines, 1) if 'asyncio.run(wrapper())' in line)
        self.assertFalse([edge for edge in self.deferred(index) if edge['caller']['line'] == later])

"""A framework wrapper outside the roots needs actual forwarding evidence."""

import json
import shutil
import subprocess
import sys
import textwrap
import unittest

import test_scheduled as helpers


class ExternalDeferredTests(unittest.TestCase):
    setUp = helpers.ScheduledTests.setUp
    tearDown = helpers.ScheduledTests.tearDown
    record = helpers.ScheduledTests.record

    def test_external_async_wrapper_projects_only_actual_forwarding(self):
        external = self.directory / "outside_wrapper.py"
        external.write_text(textwrap.dedent('''
            import functools
            def decorate(function):
                @functools.wraps(function)
                async def resume(*args, **kwargs):
                    if kwargs.pop("forward", True):
                        return await function(*args, **kwargs)
                    return "skipped"
                return resume
            def misleading(function):
                @functools.wraps(function)
                async def resume(*args, **kwargs):
                    return await unrelated()
                return resume
            async def unrelated():
                return "other"
        ''').lstrip(), encoding="utf-8")
        path, index = self.record('''
            import asyncio
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from outside_wrapper import decorate, misleading
            def safe_create_task(coro, name):
                return asyncio.create_task(coro, name=name)
            @decorate
            async def plain():
                await asyncio.sleep(0)
                return "plain"
            class Worker:
                @decorate
                async def actual(self):
                    await asyncio.sleep(0)
                    return "actual"
                @decorate
                async def skipped(self):
                    return "body"
                @misleading
                async def claimed(self):
                    return "claimed"
            async def main():
                worker = Worker()
                for _ in range(2):
                    assert await safe_create_task(worker.actual(), name="method") == "actual"
                    assert await asyncio.create_task(plain()) == "plain"
                assert await safe_create_task(worker.skipped(forward=False), name="skip") == "skipped"
                assert await safe_create_task(worker.claimed(), name="false") == "other"
                assert await Worker.skipped.__wrapped__(worker) == "body"
            asyncio.run(main())
        ''')
        lines = path.read_text().splitlines()
        points = []
        for expression, target in [("worker.actual()", "Worker.actual"), ("plain()", "plain")]:
            line = next(number for number, text in enumerate(lines, 1)
                        if expression in text and "assert await" in text)
            edges = [edge for edge in index["edges"] if edge["caller"]["line"] == line]
            projected = [edge for edge in edges if edge["kind"] == "wrapped"]
            self.assertEqual(len(projected), 1, edges)
            edge = projected[0]
            self.assertEqual(edge["target"]["qualname"], target)
            self.assertEqual(edge["count"], 2)
            self.assertEqual(edge["via"][0]["path"], str(external))
            self.assertIn("sha256", edge["via"][0])
            points.append({"line": line - 1, "character": edge["caller"]["column"],
                           "expected": target})
        for expression in ("worker.skipped(forward=False)", "worker.claimed()"):
            line = next(number for number, text in enumerate(lines, 1) if expression in text)
            self.assertFalse([edge for edge in index["edges"]
                              if edge["caller"]["line"] == line and edge["kind"] == "wrapped"])
        self.assertTrue(all(edge["target"]["path"] == str(path) for edge in index["edges"]))
        node = shutil.which("node")
        if node is not None:
            program = '''
                const fs = require('node:fs');
                const {RuntimeIndex} = require(process.argv[1]);
                (async () => {
                  const index = new RuntimeIndex(JSON.parse(fs.readFileSync(process.argv[2])));
                  for (const point of JSON.parse(process.argv[4])) {
                    const result = await index.lookup({uri:{fsPath:process.argv[3]},isDirty:false}, point);
                    if (result.issue || result.edges.length !== 1 ||
                        result.edges[0].target.qualname !== point.expected) throw Error(JSON.stringify(result));
                  }
                  fs.appendFileSync(process.argv[5], '\\n# changed external forwarding source\\n');
                  const result = await index.lookup({uri:{fsPath:process.argv[3]},isDirty:false},
                      JSON.parse(process.argv[4])[0]);
                  if (result.edges.length || !result.issue) throw Error('Changed wrapper was accepted');
                })().catch(error => { console.error(error); process.exitCode = 1; });
            '''
            process = subprocess.run([node, "-e", program,
                                      str(helpers.PROJECT / "vscode/src/index.js"),
                                      str(self.output / "index.json"), str(path),
                                      json.dumps(points), str(external)],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)


    @unittest.skipIf(sys.version_info < (3, 13), 'writable frame locals require Python 3.13')
    def test_external_destructor_cannot_replace_native_creation_receipt(self):
        external = self.directory / "outside_destructor.py"
        external.write_text(textwrap.dedent("""
            import sys
            old = None
            class Rebind:
                def __del__(self):
                    sys._getframe(1).f_locals['item'] = old
        """).lstrip(), encoding="utf-8")
        _, index = self.record("""
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).parent.parent))
            import outside_destructor
            def actual():
                yield 4
            def run():
                outside_destructor.old = actual()
                item = outside_destructor.Rebind()
                item = actual()
                assert item is outside_destructor.old
                assert list(item) == [4]
                outside_destructor.old = None
            run()
        """)
        self.assertFalse([edge for edge in index['edges']
                          if edge['kind'] == 'deferred' and edge['target']['qualname'] == 'actual'])

    @unittest.skipIf(sys.version_info < (3, 13), 'writable frame locals require Python 3.13')
    def test_external_destructor_cannot_replace_tuple_loop_creation_receipt(self):
        external = self.directory / "outside_destructor.py"
        external.write_text(textwrap.dedent("""
            import sys
            old = None
            class Rebind:
                def __del__(self):
                    sys._getframe(1).f_locals['item'] = old
        """).lstrip(), encoding="utf-8")
        _, index = self.record("""
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).parent.parent))
            import outside_destructor
            def actual():
                yield 4
            def run():
                outside_destructor.old = actual()
                item = outside_destructor.Rebind()
                for item in (actual(),):
                    assert item is outside_destructor.old
                    assert list(item) == [4]
                outside_destructor.old = None
            run()
        """)
        self.assertFalse([edge for edge in index['edges']
                          if edge['kind'] == 'deferred' and edge['target']['qualname'] == 'actual'])


    def test_threaded_native_creation_receipts_survive_frequent_switches(self):
        _, index = self.record("""
            import sys
            import threading
            sys.setswitchinterval(0.000001)
            barrier = threading.Barrier(4)
            def stream(value):
                yield value
            def invoke(value):
                item = stream(value)
                return list(item)[0]
            def worker():
                barrier.wait()
                for value in range(100):
                    assert invoke(value) == value
            assert invoke(-1) == -1
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        """)
        origins = [edge for edge in index['edges']
                   if edge['kind'] == 'deferred' and edge['target']['qualname'] == 'stream']
        self.assertEqual(len(origins), 1, origins)
        self.assertEqual(origins[0]['count'], 401)
        self.assertTrue(all(process['errors'] == 0 for process in index['processes']))

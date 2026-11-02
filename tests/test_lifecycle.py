"""Closed decorated coroutines must not retain payloads or stale origins."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"


class LifecycleTests(unittest.TestCase):
    def record(self, content):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        script = root / "probe.py"
        script.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        output = root / "trace"
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith("CALLNAV_")}
        environment["PYTHONPATH"] = str(SOURCE)
        process = subprocess.run([
            sys.executable, "-m", "callnav", "record", "--root", str(root), "--output", str(output),
            "--", sys.executable, str(script),
        ], env=environment, cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        index = json.loads((output / "index.json").read_text())
        self.assertTrue(all(item["errors"] == 0 for item in index["processes"]), index["processes"])
        return index

    def test_closed_wrapper_releases_closure_payload_before_collector_stop(self):
        index = self.record("""
            import functools
            import gc
            import weakref
            from callnav.collector import _collector as collector
            class Payload: pass
            class Pause:
                def __await__(self):
                    yield None
                    return 1
            async def body(): return 7
            def decorate(function, payload):
                @functools.wraps(function)
                async def wrapper(*args, **kwargs):
                    if payload is None: return None
                    await Pause()
                    return await function(*args, **kwargs)
                return wrapper
            payload = Payload()
            reference = weakref.ref(payload)
            entry = decorate(body, payload)
            async def origin():
                return await entry()
            item = origin()
            item.send(None)
            assert collector.frame_functions and collector.relays
            item.close()
            del item, entry, payload
            gc.collect()
            assert reference() is None, "collector retained the closed wrapper's payload"
            assert not collector.frame_functions
            assert not collector.relays
            assert not collector.scheduled.active
            assert not collector.scheduled.pending
        """)
        self.assertEqual([edge for edge in index["edges"] if edge["kind"] == "wrapped"], [])

    def test_fresh_real_frame_discards_a_recycled_slot_before_it_can_project(self):
        index = self.record("""
            import functools
            import gc
            import sys
            from callnav.collector import _collector as collector
            class Pause:
                def __await__(self):
                    yield None
            async def body(): return 7
            checking = False
            def decorate(function):
                @functools.wraps(function)
                async def wrapper(*args, **kwargs):
                    if checking:
                        key = id(sys._getframe())
                        assert key not in collector.frame_functions
                        assert key not in collector.relays
                    await Pause()
                    return await function(*args, **kwargs)
                return wrapper
            old_entry = decorate(body)
            async def old_origin():
                return await old_entry()
            original = old_origin()
            original.send(None)
            old_key = id(original.cr_await.cr_frame)
            # These are actual recorded states, obtained from a live observed
            # wrapper. Frames cannot be weak-referenced on supported CPython.
            old_origin_state = collector.relays[old_key]
            original.close()
            del original
            gc.collect()
            new_entry = decorate(body)
            checking = True
            for _ in range(20):
                candidate = new_entry()
                # Model allocator reuse deterministically using the next real
                # coroutine frame and the previous real origin's saved state.
                frame = candidate.cr_frame
                assert collector.bind_function(frame, old_entry)
                collector.relays[id(frame)] = old_origin_state
                del frame
                assert candidate.send(None) is None
                try:
                    candidate.send(None)
                except StopIteration as result:
                    assert result.value == 7
                else:
                    raise AssertionError("coroutine did not finish")
                del candidate
            del old_entry, new_entry, old_origin_state
            gc.collect()
            assert not collector.frame_functions
            assert not collector.relays
        """)
        stale = [edge for edge in index["edges"]
                 if edge["kind"] == "wrapped" and edge["caller"]["qualname"] == "old_origin"]
        self.assertEqual(stale, [])

    def test_live_wrapper_preserves_its_origin_across_multiple_suspensions(self):
        index = self.record("""
            import asyncio
            import functools
            from callnav.collector import _collector as collector
            def decorate(function):
                @functools.wraps(function)
                async def wrapper(*args, **kwargs):
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    return await function(*args, **kwargs)
                return wrapper
            @decorate
            async def body():
                await asyncio.sleep(0)
                return 7
            async def origin():
                return await body()
            assert asyncio.run(origin()) == 7
            assert not collector.frame_functions
            assert not collector.relays
        """)
        wrapped = [edge for edge in index["edges"] if edge["kind"] == "wrapped"]
        self.assertEqual(len(wrapped), 1, wrapped)
        self.assertEqual(wrapped[0]["target"]["qualname"], "body")
        self.assertEqual(wrapped[0]["caller"]["qualname"], "origin")
        self.assertEqual(wrapped[0]["count"], 1)


if __name__ == "__main__":
    unittest.main()

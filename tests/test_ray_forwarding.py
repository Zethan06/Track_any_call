"""Ray entry receipts must follow actual generated dispatch, not guess targets."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src"


class DynamicForwardingTests(unittest.TestCase):
    def test_generated_getattr_dispatch_reaches_sync_async_and_decorated_body(self):
        with tempfile.TemporaryDirectory(prefix="callnav-forward-receipt-") as folder:
            root = Path(folder)
            script = root / "driver.py"
            script.write_text(textwrap.dedent('''\
                import asyncio
                import functools
                import json
                from callnav import collector as runtime
                from callnav.adapters import ray as adapter

                def decorate(function):
                    @functools.wraps(function)
                    def wrapper(*args, **kwargs):
                        return function(*args, **kwargs)
                    return wrapper

                class Worker:
                    def plain(self, value):
                        return value
                    @decorate
                    def decorated(self, value):
                        return value
                    async def asynchronous(self, value):
                        await asyncio.sleep(0)
                        return value

                class OtherWorker:
                    def plain(self, value):
                        return value

                getter_calls = []
                class HookWorker(Worker):
                    def __getattribute__(self, name):
                        getter_calls.append(name)
                        return object.__getattribute__(self, name)

                def generate(name):
                    def func(self, *args, **kwargs):
                        return getattr(self.worker_dict["actor"], name)(*args, **kwargs)
                    return func

                def generate_async(name):
                    async def func(self, *args, **kwargs):
                        return await getattr(self.worker_dict["actor"], name)(*args, **kwargs)
                    return func

                def no_forward(name):
                    def func(self, *args, **kwargs):
                        return getattr(self.worker_dict["actor"], name)(3)
                    return func

                def effectful(name):
                    def func(self, *args, **kwargs):
                        def argument():
                            self.worker_dict["actor"] = OtherWorker()
                            return args
                        return getattr(self.worker_dict["actor"], name)(*argument(), **kwargs)
                    return func

                class WorkerDict:
                    plain = generate("plain")
                    decorated = generate("decorated")
                    asynchronous = generate_async("asynchronous")
                    nonforwarding = no_forward("plain")
                    changed_argument = effectful("plain")
                    misleading = generate("plain")
                    misleading.__wrapped__ = Worker.asynchronous
                    def __init__(self):
                        self.worker_dict = {"actor": Worker()}

                instance = runtime._collector
                adapter._collector = instance
                instance.entry_observer = adapter.observe_entry
                worker = WorkerDict()

                def context(method):
                    return dict(task_id="task", actor_id="actor", method="dispatch",
                                codes={method.__func__.__code__: method.__func__},
                                root_frame=None, receipts={})

                def finish(pending, token):
                    pending["root_frame"] = None
                    adapter._pending.reset(token)
                    assert not instance.call_bindings, "Completed activation retained a call binding"
                    return sorted({value["target"]["qualname"]
                                   for value in pending["receipts"].values()})

                def invoke(method):
                    pending = context(method)
                    token = adapter._pending.set(pending)
                    assert method(3) == 3
                    return finish(pending, token)

                async def invoke_async(method):
                    pending = context(method)
                    token = adapter._pending.set(pending)
                    assert await method(3) == 3
                    return finish(pending, token)

                results = dict(plain=invoke(worker.plain),
                               decorated=invoke(worker.decorated),
                               asynchronous=asyncio.run(invoke_async(worker.asynchronous)),
                               nonforwarding=invoke(worker.nonforwarding),
                               misleading=invoke(worker.misleading),
                               changed_argument=invoke(worker.changed_argument))
                results["mutated"] = invoke(worker.plain)
                worker.worker_dict["actor"] = HookWorker()
                results["custom_getter"] = invoke(worker.plain)
                assert getter_calls == ["plain"], getter_calls
                print(json.dumps(results))
                '''), encoding="utf-8")
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("CALLNAV_")}
            environment.update(PYTHONPATH=str(SOURCE), PYTHONDONTWRITEBYTECODE="1")
            output = root / "recording"
            process = subprocess.run([
                sys.executable, "-m", "callnav", "record", "--root", str(root),
                "--output", str(output), "--no-ray", "--", sys.executable, str(script),
            ], env=environment, cwd=root, text=True, capture_output=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            index = json.loads((output / "index.json").read_text())
            self.assertTrue(all(row["errors"] == 0 for row in index["processes"]))
            actual = json.loads(process.stdout.splitlines()[-1])
            for name in ("plain", "decorated", "asynchronous"):
                with self.subTest(name=name):
                    self.assertEqual(actual[name], ["Worker." + name])
            self.assertEqual(actual["mutated"], ["OtherWorker.plain"])
            self.assertEqual(actual["nonforwarding"], ["no_forward.<locals>.func"])
            self.assertEqual(actual["changed_argument"], ["effectful.<locals>.func"])
            self.assertEqual(actual["misleading"], ["generate.<locals>.func"])
            self.assertEqual(actual["custom_getter"], ["generate.<locals>.func"])


if __name__ == "__main__":
    unittest.main()

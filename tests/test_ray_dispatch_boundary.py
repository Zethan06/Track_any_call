"""An API's explicit dispatch argument is not a transparent wrapper binding."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src"


class DispatchBoundaryTests(unittest.TestCase):
    def test_selector_consuming_worker_api_is_not_skipped(self):
        with tempfile.TemporaryDirectory(prefix="callnav-api-boundary-") as folder:
            root = Path(folder)
            script = root / "driver.py"
            script.write_text(textwrap.dedent('''\
                import json
                from callnav import collector as runtime
                from callnav.adapters import ray as adapter

                class Engine:
                    def prepare(self, value):
                        return value

                engine = Engine()
                def module_selector(method, *args, **kwargs):
                    return getattr(engine, method)(*args, **kwargs)

                class Worker:
                    def __init__(self):
                        self.engine = Engine()
                    def execute(self, method, *args, **kwargs):
                        return getattr(self.engine, method)(*args, **kwargs)
                    def execute_variadic(self, *args, **kwargs):
                        return getattr(self.engine, args[0])(*args[1:], **kwargs)
                    def execute_keyword(self, *args, method="prepare", **kwargs):
                        return getattr(self.engine, method)(*args, **kwargs)
                    def execute_from_kwargs(self, *args, **kwargs):
                        method = kwargs.pop("method")
                        return getattr(self.engine, method)(*args, **kwargs)
                    execute_static = staticmethod(module_selector)

                def generate(name):
                    def func(self, *args, **kwargs):
                        return getattr(self.workers["actor"], name)(*args, **kwargs)
                    return func

                class WorkerDict:
                    execute = generate("execute")
                    execute_variadic = generate("execute_variadic")
                    execute_keyword = generate("execute_keyword")
                    execute_from_kwargs = generate("execute_from_kwargs")
                    execute_static = generate("execute_static")
                    def __init__(self):
                        self.workers = {"actor": Worker()}

                instance = runtime._collector
                adapter._collector = instance
                instance.entry_observer = adapter.observe_entry
                worker = WorkerDict()
                results = {}
                for name in ("execute", "execute_variadic", "execute_keyword",
                             "execute_from_kwargs", "execute_static"):
                    method = getattr(worker, name)
                    pending = dict(task_id="task", actor_id="actor", method=name,
                                   codes={method.__func__.__code__: method.__func__},
                                   root_frame=None, receipts={})
                    token = adapter._pending.set(pending)
                    if name == "execute_keyword":
                        value = method(3, method="prepare")
                    elif name == "execute_from_kwargs":
                        value = method(3, method="prepare")
                    else:
                        value = method("prepare", 3)
                    assert value == 3
                    pending["root_frame"] = None
                    adapter._pending.reset(token)
                    assert not instance.call_bindings
                    results[name] = sorted({row["target"]["qualname"]
                                            for row in pending["receipts"].values()})
                print(json.dumps(results))
                '''), encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if not key.startswith("CALLNAV_")}
            env.update(PYTHONPATH=str(SOURCE), PYTHONDONTWRITEBYTECODE="1")
            output = root / "recording"
            done = subprocess.run([
                sys.executable, "-m", "callnav", "record", "--root", str(root),
                "--output", str(output), "--no-ray", "--", sys.executable, str(script),
            ], env=env, cwd=root, text=True, capture_output=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            index = json.loads((output / "index.json").read_text())
            self.assertTrue(all(row["errors"] == 0 for row in index["processes"]))
            actual = json.loads(done.stdout.splitlines()[-1])
            for name in ("execute", "execute_variadic", "execute_keyword", "execute_from_kwargs"):
                with self.subTest(name=name):
                    self.assertEqual(actual[name], ["Worker." + name])
            self.assertEqual(actual["execute_static"], ["module_selector"])


if __name__ == "__main__":
    unittest.main()

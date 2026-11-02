"""Safe dynamic dispatch resolution must preserve actual callable identities."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src"


class ResolutionTests(unittest.TestCase):
    def record(self, content):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "project"
            root.mkdir()
            path = root / "run.py"
            path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
            output = directory / "recording"
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("CALLNAV_")}
            environment["PYTHONPATH"] = str(SOURCE)
            process = subprocess.run([
                sys.executable, "-m", "callnav", "record", "--root", str(root),
                "--output", str(output), "--", sys.executable, str(path),
            ], cwd=root, env=environment, text=True, capture_output=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            index = json.loads((output / "index.json").read_text())
            self.assertTrue(all(item["errors"] == 0 for item in index["processes"]), index["processes"])
            return index["edges"]

    def precise(self, edges, qualname):
        result = [edge for edge in edges if edge["target"]["qualname"] == qualname]
        self.assertTrue(result, (qualname, edges))
        self.assertTrue(all(not edge["caller"].get("ambiguous") for edge in result), result)
        self.assertTrue(all(edge["caller"]["precision"] == "instruction" for edge in result), result)
        return result

    def test_super_respects_actual_mro_and_classmethod_receiver(self):
        edges = self.record('''
            from abc import ABC
            class Base(ABC):
                def __init__(self, value): self.value = value
                def method(self): return self.value
                @classmethod
                def name(cls): return cls.__name__
            class Left(Base):
                def method(self): return super().method()
            class Right(Base):
                def method(self): return 7 + super(Right, self).method()
            class Child(Left, Right):
                def __init__(self): super().__init__(3)
                @classmethod
                def name(cls): return super().name()
            child = Child()
            assert child.method() == 10
            assert Child.name() == "Child"
        ''')
        for name in ("Base.__init__", "Right.method", "Base.method", "Base.name"):
            self.precise(edges, name)

    def test_getattr_dict_dispatch_projects_sync_and_async_wrappers(self):
        edges = self.record('''
            import asyncio
            import functools
            def decorate(function):
                @functools.wraps(function)
                def wrapped(*args, **kwargs): return function(*args, **kwargs)
                return wrapped
            def adecorate(function):
                @functools.wraps(function)
                async def wrapped(*args, **kwargs): return await function(*args, **kwargs)
                return wrapped
            class Worker:
                @decorate
                def work(self, value): return value
                @adecorate
                async def update(self, value): return value
            class Group:
                def __init__(self): self.workers = {"actor": Worker()}
                def dispatch(self, key, name, *args, **kwargs):
                    return getattr(self.workers[key], name)(*args, **kwargs)
                async def adispatch(self, key, name, *args, **kwargs):
                    return await getattr(self.workers[key], name)(*args, **kwargs)
            async def main():
                group = Group()
                assert group.dispatch("actor", "work", 4) == 4
                assert await group.adispatch("actor", "update", 6) == 6
            asyncio.run(main())
        ''')
        for name in ("decorate.<locals>.wrapped", "adecorate.<locals>.wrapped"):
            self.precise(edges, name)
        for name in ("Worker.work", "Worker.update"):
            projected = [edge for edge in self.precise(edges, name) if edge["kind"] == "wrapped"]
            self.assertEqual(len(projected), 1, (name, projected))

    def test_returned_object_method_uses_single_real_factory_return(self):
        edges = self.record('''
            calls = []
            class Worker:
                def run(self, value): return value
            worker = Worker()
            def get_worker():
                calls.append("factory")
                return worker
            assert get_worker().run(4) == 4
            assert calls == ["factory"], calls
        ''')
        self.precise(edges, "Worker.run")

    def test_shadowed_builtins_and_descriptors_stay_unresolved_without_extra_reads(self):
        edges = self.record('''
            events = []
            def actual(): return 4
            class Descriptor:
                def __get__(self, obj, owner):
                    events.append("descriptor")
                    return actual
            class Holder:
                target = Descriptor()
            holder = Holder()
            import builtins
            assert builtins.getattr(holder, "target")() == 4
            def getattr(obj, name): return actual
            assert getattr(holder, "missing")() == 4
            class Base:
                def method(self): return 8
            class Child(Base):
                def method(self):
                    super = lambda: holder
                    return super().target()
            assert Child().method() == 4
            assert events == ["descriptor", "descriptor"], events
        ''')
        actual = [edge for edge in edges if edge["target"]["qualname"] == "actual"]
        self.assertEqual(len(actual), 3)
        # A shadowed Python factory has its own real return proof. It must not
        # be interpreted as builtin getattr, but that return remains usable.
        self.assertEqual(sum(bool(edge["caller"].get("ambiguous")) for edge in actual), 2, actual)

    def test_custom_getattribute_and_dict_keys_are_never_reexecuted(self):
        self.record('''
            events = []
            def actual(): return 3
            class Holder:
                def __getattribute__(self, name):
                    events.append(name)
                    return actual
            holder = Holder()
            assert getattr(holder, "work")() == 3
            assert events == ["work"], events
            class Key(str):
                def __hash__(self): return hash("work")
                def __eq__(self, other):
                    events.append("eq")
                    return str.__eq__(self, other)
            table = {Key("work"): actual}
            assert table["work"]() == 3
            assert events == ["work", "eq"], events
        ''')

    def test_mutating_arguments_do_not_borrow_replacement_wrapper_binding(self):
        edges = self.record('''
            import functools
            def decorate(function):
                @functools.wraps(function)
                def wrapper(*args): return function(*args)
                return wrapper
            @decorate
            def first(value): return value
            @decorate
            def second(value): return value
            class Holder: pass
            holder = Holder()
            holder.work = first
            def replace():
                holder.work = second
                return 4
            assert getattr(holder, "work")(replace()) == 4
        ''')
        wrappers = [edge for edge in edges if edge["target"]["qualname"] == "decorate.<locals>.wrapper"]
        self.assertEqual(len(wrappers), 1, wrappers)
        self.assertTrue(wrappers[0]["caller"].get("ambiguous"), wrappers)
        self.assertFalse(any(edge["kind"] == "wrapped" for edge in edges), edges)

    def test_property_arguments_invalidate_getattr_proof_before_outer_entry(self):
        edges = self.record('''
            import functools
            def decorate(function):
                @functools.wraps(function)
                def wrapper(*args): return function(*args)
                return wrapper
            @decorate
            def first(value): return value
            @decorate
            def second(value): return value
            class Holder:
                @property
                def argument(self):
                    self.work = second
                    return 4
            holder = Holder()
            holder.work = first
            assert getattr(holder, "work")(holder.argument) == 4
        ''')
        wrappers = [edge for edge in edges if edge["target"]["qualname"] == "decorate.<locals>.wrapper"]
        self.assertEqual(len(wrappers), 1, wrappers)
        self.assertTrue(wrappers[0]["caller"].get("ambiguous"), wrappers)
        self.assertFalse(any(edge["kind"] == "wrapped" for edge in edges), edges)

    def test_nested_comprehension_arguments_do_not_borrow_outer_method_token(self):
        edges = self.record('''
            class Remote:
                def remote(self, **kwargs): return kwargs
            class Engine:
                def __init__(self): self.update = Remote()
            def element(value): return value
            values = [1, 2]
            engines = [Engine()]
            results = [
                engine.update.remote(
                    names=[element(value) for value in values],
                    dtypes={element(value) for value in values},
                    shapes={value: element(value) for value in values},
                )
                for engine in engines
            ]
            assert results[0]["names"] == [1, 2]
        ''')
        calls = self.precise(edges, "Remote.remote")
        self.assertEqual(len(calls), 1, calls)
        remote = calls[0]["caller"]
        self.assertEqual((remote["line"], remote["column"], remote["end_column"]), (9, 18, 24))
        implicit = [edge for edge in edges if edge["target"]["qualname"].endswith(
            ("<listcomp>", "<setcomp>", "<dictcomp>"))]
        for edge in implicit:
            caller = edge["caller"]
            # The outer result comprehension contains this real invocation;
            # each argument comprehension belongs to its own later line.
            if edge["target"]["line"] in (10, 11, 12):
                self.assertEqual(caller["line"], edge["target"]["line"], edge)
                self.assertNotEqual(caller["line"], remote["line"], edge)
        element_calls = self.precise(edges, "element")
        self.assertEqual(len(element_calls), 3)
        self.assertEqual({edge["caller"]["line"] for edge in element_calls}, {10, 11, 12})

    def test_metaclass_data_descriptor_cannot_prove_shadowed_native_async_generator(self):
        edges = self.record('''
            import asyncio
            events = []
            class Meta(type):
                @property
                def generate(cls):
                    events.append("descriptor")
                    return lambda: old
            class Worker(metaclass=Meta):
                @staticmethod
                async def generate():
                    yield 4
            native = vars(Worker)["generate"].__func__
            old = native()
            async def main():
                values = []
                async for value in Worker.generate():
                    values.append(value)
                assert values == [4]
                assert events == ["descriptor"], events
            asyncio.run(main())
        ''')
        self.assertFalse(any(edge["kind"] == "deferred" and edge["target"]["qualname"] == "Worker.generate"
                             for edge in edges), edges)

    def test_metaclass_data_descriptor_cannot_prove_shadowed_native_coroutine_argument(self):
        edges = self.record('''
            import asyncio
            events = []
            class Meta(type):
                @property
                def work(cls):
                    events.append("descriptor")
                    return lambda: old
            class Worker(metaclass=Meta):
                @staticmethod
                async def work():
                    return 4
            native = vars(Worker)["work"].__func__
            old = native()
            def consume(coro):
                return asyncio.create_task(coro)
            async def main():
                assert await consume(Worker.work()) == 4
                assert events == ["descriptor"], events
            asyncio.run(main())
        ''')
        self.assertFalse(any(edge["kind"] in ("deferred", "scheduled")
                             and edge["target"]["qualname"] == "Worker.work" for edge in edges), edges)

    def test_ordinary_metaclass_preserves_static_and_class_native_bindings(self):
        edges = self.record('''
            import asyncio
            class Meta(type):
                pass
            class Worker(metaclass=Meta):
                @staticmethod
                async def static():
                    return 4
                @classmethod
                async def bound(cls):
                    return cls.__name__
            def consume(coro):
                return asyncio.create_task(coro)
            async def main():
                assert await consume(Worker.static()) == 4
                assert await consume(Worker.bound()) == "Worker"
            asyncio.run(main())
        ''')
        for name in ("Worker.static", "Worker.bound"):
            self.precise(edges, name)

    def test_structural_mro_and_namespace_reads_bypass_metaclass_properties(self):
        edges = self.record('''
            events = []
            mro_descriptor = vars(type)["__mro__"]
            dict_descriptor = vars(type)["__dict__"]
            class MetaMeta(type):
                @property
                def __mro__(cls):
                    events.append("meta_mro")
                    return mro_descriptor.__get__(cls)
                @property
                def __dict__(cls):
                    events.append("meta_dict")
                    return dict_descriptor.__get__(cls)
            class Meta(type, metaclass=MetaMeta):
                @property
                def __mro__(cls):
                    events.append("class_mro")
                    return mro_descriptor.__get__(cls)
                @property
                def __dict__(cls):
                    events.append("class_dict")
                    return dict_descriptor.__get__(cls)
            class Base(metaclass=Meta):
                def method(self): return 4
            class Worker(Base):
                def method(self): return super().method()
            events.clear()
            assert Worker().method() == 4
            assert events == [], events
        ''')
        self.precise(edges, "Base.method")


if __name__ == "__main__":
    unittest.main()

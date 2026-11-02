"""Profile boundaries preserve live bindings and require actual execution proofs."""

import ast
import functools
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from callnav.collector import _UNKNOWN, _attribute_value, _function, _static_value


SOURCE = Path(__file__).resolve().parents[1] / 'src'


class ProfileBoundaryTests(unittest.TestCase):
    def test_name_resolution_rereads_live_frame_bindings(self):
        expression = ast.Name(id='binding')
        binding = object()
        frame = inspect.currentframe()
        try:
            self.assertIs(_static_value(expression, frame), binding)
            binding = object()
            self.assertIs(_static_value(expression, frame), binding)
            binding = _UNKNOWN
            self.assertIs(_static_value(expression, frame), _UNKNOWN)
        finally:
            del frame

    def test_function_inspection_preserves_methods_partials_and_cycles(self):
        def target():
            pass

        class Partial(functools.partial):
            @property
            def func(self):
                raise AssertionError('partial subclass hook executed')

        method = types.MethodType(target, object())
        self.assertIs(_function(target), target)
        self.assertIs(_function(method), target)
        self.assertIs(_function(functools.partial(method)), target)
        self.assertIsNone(_function(Partial(target)))
        self.assertIsNone(_function(len))
        cycle = functools.partial(target)
        cycle.__setstate__((cycle, (), {}, None))
        self.assertIsNone(_function(cycle))

    def test_module_lookup_rereads_globals_without_hooks_or_descriptor_shadowing(self):
        module = types.ModuleType('example')
        def first():
            return 1
        def second():
            return 2
        namespace = vars(module)
        namespace['entry'] = first
        namespace['__getattr__'] = lambda name: self.fail('module hook executed')
        self.assertIs(_attribute_value(module, 'entry'), first)
        namespace['entry'] = second
        self.assertIs(_attribute_value(module, 'entry'), second)
        self.assertIs(_attribute_value(module, 'missing'), _UNKNOWN)
        namespace['__dict__'] = first
        self.assertIs(_attribute_value(module, '__dict__'), _UNKNOWN)

    def test_module_values_preserve_static_resolution_and_descriptor_rejection(self):
        import inspect

        module = types.ModuleType('example')
        def target():
            return 1
        class Descriptor:
            def __get__(self, instance, owner):
                raise AssertionError('descriptor executed')
        class Meta(type):
            def __getattribute__(self, name):
                raise AssertionError('metaclass hook executed')
        class Kind(metaclass=Meta):
            pass
        values = [len, Kind, 3, 'text', [], {}, (), set(), None, object()]
        for value in values:
            vars(module)['entry'] = value
            self.assertIs(_attribute_value(module, 'entry'), value)
        for value in (Descriptor(), property(target)):
            vars(module)['entry'] = value
            self.assertIs(_attribute_value(module, 'entry'), _UNKNOWN)
        for value in (staticmethod(target), classmethod(target)):
            vars(module)['entry'] = value
            self.assertIs(_attribute_value(module, 'entry'), target)
        vars(module)['entry'] = inspect._sentinel
        self.assertIs(_attribute_value(module, 'entry'), _UNKNOWN)

    def test_optional_framework_observers_start_at_compatible_boundaries(self):
        from callnav.adapters import hydra, ray

        def collector():
            value = types.SimpleNamespace(entry_observer=None, entry_observers=[])
            def add(callback):
                if callback not in value.entry_observers:
                    value.entry_observers.append(callback)
            value.add_entry_observer = add
            return value

        names = {*hydra._MODULES, 'ray', 'ray.actor', 'ray._private.function_manager'}
        with mock.patch.dict(sys.modules, {name: None for name in names}), \
                mock.patch.object(sys, 'meta_path', list(sys.meta_path)), \
                mock.patch.object(hydra, '_finder', None), \
                mock.patch.object(hydra, '_collector', None), \
                mock.patch.object(ray, '_finder', None), \
                mock.patch.object(ray, '_collector', None):
            first = collector()
            hydra.install(first)
            ray.install(first)
            self.assertIsNone(first.entry_observer)
            self.assertFalse(first.entry_observers)

            main_module = types.ModuleType('hydra.main')
            def main():
                pass
            main_module.main = main
            manager_module = types.ModuleType('ray._private.function_manager')
            class Manager:
                def _make_actor_method_executor(self, method_name, method):
                    return method
            manager_module.FunctionActorManager = Manager
            sys.modules['hydra.main'] = main_module
            sys.modules['ray._private.function_manager'] = manager_module
            hydra.install(first)
            ray.install(first)
            self.assertEqual(first.entry_observers, [hydra.observe_entry])
            self.assertIs(first.entry_observer, ray.observe_entry)
            # Already patched modules must register with a replacement recorder.
            second = collector()
            hydra.install(second)
            ray.install(second)
            self.assertEqual(second.entry_observers, [hydra.observe_entry])
            self.assertIs(second.entry_observer, ray.observe_entry)

    def test_external_factory_return_proves_method_without_inventing_callback_edges(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'project'
            root.mkdir()
            external = directory / 'external'
            external.mkdir()
            (external / 'library.py').write_text(
                'def make(kind):\n    return kind()\n'
                'def invoke(callback):\n    return callback()\n', encoding='utf-8')
            script = root / 'run.py'
            script.write_text(
                'import sys\n'
                f'sys.path.insert(0, {str(external)!r})\n'
                'import library\n'
                'class Worker:\n    def work(self):\n        return 7\n'
                'def callback():\n    return 9\n'
                'assert library.make(Worker).work() == 7\n'
                'assert library.invoke(callback) == 9\n', encoding='utf-8')
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith('CALLNAV_')}
            environment['PYTHONPATH'] = str(SOURCE)
            output = directory / 'trace'
            result = subprocess.run(
                [sys.executable, '-m', 'callnav', '--root', str(root), '--output', str(output),
                 '--', sys.executable, str(script)], cwd=root, env=environment,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            index = json.loads((output / 'index.json').read_text())
            self.assertTrue(all(process['errors'] == 0 for process in index['processes']))
            methods = [edge for edge in index['edges']
                       if edge['target']['qualname'] == 'Worker.work']
            self.assertEqual(len(methods), 1, index['edges'])
            self.assertFalse(methods[0]['caller'].get('ambiguous'))
            self.assertEqual(methods[0]['caller']['precision'], 'instruction')
            self.assertEqual(methods[0]['count'], 1)
            self.assertFalse(any(edge['target']['qualname'] == 'callback'
                                 for edge in index['edges']))


if __name__ == '__main__':
    unittest.main()

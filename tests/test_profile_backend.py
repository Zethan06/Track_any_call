"""Experimental profile delivery must preserve native and Python method calls."""

import json
import os
import sys
import unittest

import test_scheduled as helpers


class ProfileBackendTests(unittest.TestCase):
    setUp = helpers.ScheduledTests.setUp
    tearDown = helpers.ScheduledTests.tearDown
    record = helpers.ScheduledTests.record

    def test_builtin_backed_methods_do_not_break_capture(self):
        path, index = self.record('''
            import functools
            import json
            from pathlib import Path
            import sys
            import types
            from callnav.collector import _collector

            backend = _collector.profile_monitoring
            Path(__file__).with_name('backend.json').write_text(json.dumps({
                'selected': bool(backend and backend.active),
                'legacy_hook': sys.getprofile() is not None,
                'direct_proofs': _collector.scheduled.direct_monitoring,
                'trace_hook': sys.gettrace() is not None,
            }))
            native = types.MethodType(len, [1])
            partial_native = functools.partial(native)
            def target(receiver):
                return receiver[0]
            python_method = types.MethodType(target, [7])
            for iteration in range(20):
                assert native() == 1
                assert partial_native() == 1
                assert python_method() == 7
        ''')
        backend = json.loads(path.with_name('backend.json').read_text())
        mode = os.environ.get('CALLNAV_PROFILE_BACKEND', 'auto')
        selected = (mode in ('auto', 'monitoring', 'hybrid', 'local')
                    and sys.version_info[:2] in ((3, 12), (3, 13)))
        self.assertEqual(backend['selected'], selected)
        self.assertEqual(backend['legacy_hook'],
                         not selected or mode == 'hybrid')
        direct = selected and os.environ.get(
            'CALLNAV_DIRECT_PROOFS', '1' if mode in ('auto', 'hybrid', 'local') else '0') == '1'
        self.assertEqual(backend['direct_proofs'], direct)
        self.assertEqual(backend['trace_hook'], not direct)
        self.assertEqual(index['processes'][0]['direct_proof_monitoring'], direct)
        self.assertEqual(index['processes'][0]['profile_backend'],
                         ('local' if mode == 'auto' else mode) if selected else 'legacy')
        self.assertIsNone(index['processes'][0]['profile_backend_failure'])
        edges = [edge for edge in index['edges'] if edge['target']['qualname'] == 'target']
        self.assertEqual(len(edges), 1, edges)
        self.assertEqual(edges[0]['count'], 20)
        self.assertEqual(edges[0]['caller']['precision'], 'instruction')
        self.assertFalse(edges[0]['caller'].get('ambiguous'))

    def test_callable_cannot_spoof_a_native_function_through_class_property(self):
        _, index = self.record('''
            import types
            events = []
            class Callback:
                @property
                def __class__(self):
                    events.append('class_read')
                    return types.BuiltinFunctionType
                def __call__(self):
                    return 4
            callback = Callback()
            for iteration in range(20):
                assert callback() == 4
            assert events == [], events
        ''')
        edges = [edge for edge in index['edges']
                 if edge['target']['qualname'] == 'Callback.__call__']
        self.assertEqual(len(edges), 1, edges)
        self.assertEqual(edges[0]['count'], 20)
        self.assertFalse(edges[0]['caller'].get('ambiguous'))

    def test_function_construction_preserves_implicit_source_property_entry(self):
        dynamic = self.root / 'constructed_source.py'
        dynamic.write_text('def leaf():\n    return 7\ndef getter(self):\n    return leaf()\n',
                           encoding='utf-8')
        _, index = self.record('''
            from pathlib import Path
            import types
            path = Path(__file__).with_name('constructed_source.py')
            module = compile(path.read_text(), str(path), 'exec')
            codes = {code.co_name: code for code in module.co_consts
                     if type(code) is types.CodeType}
            namespace = {}
            namespace['leaf'] = types.FunctionType(codes['leaf'], namespace)
            getter = types.FunctionType(codes['getter'], namespace)
            Receiver = type('Receiver', (), {'value': property(getter)})
            assert Receiver().value == 7
        ''')
        edges = [edge for edge in index['edges']
                 if edge['caller']['qualname'] == 'getter'
                 and edge['target']['qualname'] == 'leaf']
        self.assertEqual(len(edges), 1, edges)
        self.assertEqual(edges[0]['count'], 1)
        self.assertEqual(edges[0]['caller']['path'], str(dynamic))
        self.assertEqual(edges[0]['target']['path'], str(dynamic))

    def test_exec_audit_preserves_implicit_source_property_entry(self):
        dynamic = self.root / 'dynamic_source.py'
        dynamic.write_text('''def leaf():
    return 7
class Receiver:
    @property
    def value(self):
        return leaf()
assert Receiver().value == 7
''', encoding='utf-8')
        _, index = self.record('''
            from pathlib import Path
            path = Path(__file__).with_name('dynamic_source.py')
            exec(compile(path.read_text(), str(path), 'exec'), {})
        ''')
        edges = [edge for edge in index['edges']
                 if edge['caller']['qualname'] == 'Receiver.value'
                 and edge['target']['qualname'] == 'leaf']
        self.assertEqual(len(edges), 1, edges)
        self.assertEqual(edges[0]['count'], 1)
        self.assertEqual(edges[0]['target']['path'], str(dynamic))


if __name__ == '__main__':
    unittest.main()

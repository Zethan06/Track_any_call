"""Implicit call offsets identify instructions through cache specialization."""

import dis
import json
import unittest

from callnav.collector import Source

import test_scheduled as helpers


class InstructionOffsetTests(unittest.TestCase):
    setUp = helpers.ScheduledTests.setUp
    tearDown = helpers.ScheduledTests.tearDown
    record = helpers.ScheduledTests.record

    def test_warm_implicit_entries_merge_without_changing_explicit_calls(self):
        path, index = self.record('''
            import json
            import sys
            from pathlib import Path
            class Accessor:
                attr = 1
                def __getattribute__(self, name):
                    return object.__getattribute__(self, name)
                def __getitem__(self, key):
                    return 2
            explicit_offsets = set()
            def leaf():
                explicit_offsets.add(sys._getframe(1).f_lasti)
                return 7
            def planned(obj):
                value = leaf()
                attribute = obj.attr
                item = obj['key']
                return value + attribute + item
            def noplan(obj):
                attribute = obj.attr
                item = obj['key']
                return attribute + item
            def repeated(obj):
                return obj['left'] + obj['right']
            obj = Accessor()
            for iteration in range(50):
                assert planned(obj) == 10
                assert noplan(obj) == 3
                assert repeated(obj) == 4
            Path(__file__).with_name('expected.json').write_text(
                json.dumps(sorted(explicit_offsets)), encoding='utf-8')
        ''')
        compiled = compile(path.read_text(encoding="utf-8"), str(path), "exec")
        codes = {code.co_name: code for code in compiled.co_consts
                 if hasattr(code, "co_code")}
        for caller in ("planned", "noplan", "repeated"):
            operations = list(dis.get_instructions(codes[caller]))
            for opname, target in (("LOAD_ATTR", "Accessor.__getattribute__"),
                                   ("BINARY_SUBSCR", "Accessor.__getitem__")):
                with self.subTest(caller=caller, operation=opname):
                    expected = sorted(operation.offset for operation in operations
                                      if operation.opname == opname)
                    edges = [edge for edge in index["edges"]
                             if edge["caller"]["qualname"] == caller
                             and edge["target"]["qualname"] == target]
                    self.assertEqual(sorted(edge["caller"]["offset"] for edge in edges),
                                     expected, edges)
                    for edge in edges:
                        self.assertEqual(edge["count"], 50, edge)
                        self.assertEqual(edge["caller"]["precision"], "line", edge)
                        self.assertTrue(edge["caller"].get("ambiguous"), edge)
        explicit = [edge for edge in index["edges"]
                    if edge["caller"]["qualname"] == "planned"
                    and edge["target"]["qualname"] == "leaf"]
        raw_offsets = json.loads(path.with_name("expected.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw_offsets), 1)
        self.assertEqual(len(explicit), 1, explicit)
        self.assertEqual(explicit[0]["caller"]["offset"], raw_offsets[0])
        self.assertEqual(explicit[0]["caller"]["precision"], "instruction")
        self.assertEqual(explicit[0]["count"], 50)

    def test_source_cache_offsets_keep_line_precision_and_ambiguity(self):
        path = self.root / "source.py"
        path.write_text("def work(obj):\n    return obj.attr\n", encoding="utf-8")
        code = compile(path.read_text(encoding="utf-8"), str(path), "exec").co_consts[0]
        operations = list(dis.get_instructions(code))
        attribute = next(operation for operation in operations if operation.opname == "LOAD_ATTR")
        # Python 3.13 attaches cache information instead of yielding CACHE rows.
        following = operations[operations.index(attribute) + 1]
        cache_offset = following.offset - 2
        self.assertGreater(cache_offset, attribute.offset)
        source = Source(str(path))
        site, expression = source.callsite(
            code, cache_offset, attribute.positions.lineno,
            ([operation.offset for operation in operations], operations),
        )
        self.assertEqual(site["offset"], attribute.offset)
        self.assertEqual(site["precision"], "line")
        self.assertTrue(site.get("ambiguous"))
        self.assertIsNone(expression)

    def test_missing_instruction_and_negative_offsets_stay_conservative(self):
        path = self.root / "source.py"
        path.write_text("def work(obj):\n    return obj.attr\n", encoding="utf-8")
        code = compile(path.read_text(encoding="utf-8"), str(path), "exec").co_consts[0]
        operations = list(dis.get_instructions(code))
        source = Source(str(path))
        for offset, instructions in ((-1, ([op.offset for op in operations], operations)),
                                     (42, ([], []))):
            with self.subTest(offset=offset):
                site, expression = source.callsite(code, offset, 2, instructions)
                self.assertEqual(site["offset"], offset)
                self.assertEqual(site["precision"], "line")
                self.assertTrue(site.get("ambiguous"))
                self.assertIsNone(expression)


if __name__ == "__main__":
    unittest.main()

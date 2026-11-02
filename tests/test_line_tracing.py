"""Line windows retain native construction proofs across control flow."""

import sys
import unittest

import test_scheduled as helpers


class LineTracingTests(unittest.TestCase):
    setUp = helpers.ScheduledTests.setUp
    tearDown = helpers.ScheduledTests.tearDown
    record = helpers.ScheduledTests.record

    @unittest.skipUnless(sys.version_info < (3, 12), "tests the Python 3.11 line backend")
    def test_arithmetic_lines_run_without_opcode_callbacks(self):
        self.record('''
            import sys
            def run():
                frame = sys._getframe()
                active = 0
                for index in range(100):
                    if frame.f_trace_opcodes:
                        active += 1
                assert active == 0, active
            run()
        ''')

    def test_same_line_generator_resume_retains_construction_proof(self):
        _, index = self.record('''
            def stream():
                yield 7
            def run():
                for index in range(1): yield 0; value = stream(); yield next(value)
            generator = run()
            assert next(generator) == 0
            assert next(generator) == 7
        ''')
        edges = [edge for edge in index["edges"]
                 if edge["target"]["qualname"] == "stream"]
        self.assertEqual(len(edges), 1, index["edges"])
        self.assertEqual(edges[0]["kind"], "deferred")
        self.assertEqual(edges[0]["count"], 1)
        self.assertFalse(edges[0]["caller"].get("ambiguous"))

    def test_same_line_loop_rearms_each_native_construction(self):
        _, index = self.record('''
            def stream():
                yield 7
            def run():
                for index in range(4): value = stream(); assert next(value) == 7; value.close()
            run()
        ''')
        edges = [edge for edge in index["edges"]
                 if edge["target"]["qualname"] == "stream"]
        self.assertEqual(len(edges), 1, index["edges"])
        self.assertEqual(edges[0]["kind"], "deferred")
        self.assertEqual(edges[0]["count"], 4)

    def test_handler_and_multiline_calls_keep_distinct_bindings(self):
        _, index = self.record('''
            def first():
                yield 1
            def second():
                yield 2
            def run():
                function = first
                value = function(
                )
                assert next(value) == 1
                value.close()
                try:
                    raise ValueError("switch")
                except ValueError:
                    function = second
                    value = function()
                assert next(value) == 2
            run()
        ''')
        edges = [edge for edge in index["edges"] if edge["kind"] == "deferred"]
        self.assertEqual(sorted((edge["target"]["qualname"], edge["count"]) for edge in edges),
                         [("first", 1), ("second", 1)])


if __name__ == "__main__":
    unittest.main()

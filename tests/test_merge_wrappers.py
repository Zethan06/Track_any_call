"""Merging worker shards must preserve wrapper execution proofs."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from callnav.cli import merge


class WrapperMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.run = self.base / "run"
        (self.run / "shards").mkdir(parents=True)
        (self.run / "run.json").write_text(json.dumps({
            "run_id": "merge-proof", "root": str(self.base), "roots": [str(self.base)]}))
        self.edge = {
            "caller": {"path": "/project/run.py", "line": 2, "column": 0,
                       "end_line": 2, "end_column": 4},
            "target": {"path": "/project/work.py", "line": 5, "column": 0,
                       "qualname": "work"},
            "count": 1, "process_ids": [1], "kind": "wrapped",
            "via": [{"path": "/dependency/decorator.py", "line": 3, "column": 0,
                     "qualname": "wrap", "sha256": "a" * 64}],
        }

    def tearDown(self):
        self.temp.cleanup()

    def shard(self, pid, edges):
        (self.run / "shards" / (str(pid) + ".json")).write_text(json.dumps({
            "run_id": "merge-proof", "process": {"pid": pid, "status": "complete"},
            "sources": {}, "edges": edges,
        }))

    def test_distinct_observed_wrapper_paths_remain_separately_verifiable(self):
        different = copy.deepcopy(self.edge)
        different["via"][0]["path"] = "/dependency/other.py"
        different["process_ids"] = [2]
        self.shard(1, [self.edge])
        self.shard(2, [different])
        index = merge(self.run)
        self.assertEqual(len(index["edges"]), 2)
        self.assertEqual({e["via"][0]["path"] for e in index["edges"]},
                         {"/dependency/decorator.py", "/dependency/other.py"})
        # A refresh must not multiply either receipt.
        self.assertEqual(merge(self.run)["edges"], index["edges"])

    def test_forwarding_evidence_counts_are_combined_across_workers(self):
        raw = copy.deepcopy(self.edge)
        raw.pop("via")
        raw.update(kind="python", count=3, wrapper_forwarded_count=2)
        other = copy.deepcopy(raw)
        other.update(count=2, wrapper_forwarded_count=2, process_ids=[2])
        self.shard(1, [raw])
        self.shard(2, [other])
        combined = merge(self.run)["edges"][0]
        self.assertEqual(combined["count"], 5)
        self.assertEqual(combined["wrapper_forwarded_count"], 4)
        self.assertEqual(combined["process_ids"], [1, 2])
        # One invocation did not reach the body; its wrapper cannot be erased.
        self.assertLess(combined["wrapper_forwarded_count"], combined["count"])


if __name__ == "__main__":
    unittest.main()

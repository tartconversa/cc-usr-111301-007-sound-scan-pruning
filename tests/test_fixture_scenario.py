"""冻结的台架数据上的端到端断言（fixtures/ 只读，不覆盖）。"""

import json
import unittest
from pathlib import Path

from src.catalog import load_catalog
from src.planner import plan_scan
from src.predicate_ast import Column, normalize, parse
from src.pruning_reader import load_queries, run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def fixture_plan(query):
    catalog = load_catalog(FIXTURES / "catalog.json")
    columns = {
        name: Column(name=name, kind=column.kind)
        for name, column in catalog.columns.items()
    }
    node = normalize(parse(query, columns))
    return catalog, plan_scan(catalog, node)


class FixtureScenarioTests(unittest.TestCase):
    TRUSTED_MINMAX = {"op": "gt", "field": "x", "value": 10}
    TRUSTED_BOUNDARY = {"op": "ge", "field": "x", "value": 10}
    NOT_EQ = {"op": "not", "arg": {"op": "eq", "field": "x", "value": 1}}
    EMPTY_IN = {"op": "in", "field": "x", "values": []}
    IS_NULL = {"op": "is_null", "field": "x"}

    def decisions(self, query):
        _, plan = fixture_plan(query)
        return {item.file_id: item.decision for item in plan.files}

    def test_minmax_gt_and_ge(self):
        self.assertEqual(
            self.decisions(self.TRUSTED_MINMAX),
            {"p1": "skip", "p2": "skip", "p3": "read"},
        )
        self.assertEqual(
            self.decisions(self.TRUSTED_BOUNDARY),
            {"p1": "skip", "p2": "skip", "p3": "read"},
        )

    def test_gt_classification_matches_range_edges(self):
        """p1 的 x∈{1,2} ∪ {NULL}：x>10 不可能，但 x≥10 同样不可能。"""
        catalog, plan = fixture_plan(self.TRUSTED_BOUNDARY)
        entry = next(item for item in plan.files if item.file_id == "p1")
        self.assertEqual(entry.analysis.truth_names, ("FALSE", "UNKNOWN"))

    def test_not_eq_one(self):
        # p2 只有 NULL：x=1 是 UNKNOWN，NOT 后仍是 UNKNOWN，不得当成 TRUE。
        catalog, plan = fixture_plan(self.NOT_EQ)
        decisions = {item.file_id: item.decision for item in plan.files}
        self.assertEqual(decisions, {"p1": "read", "p2": "skip", "p3": "read"})
        p2 = next(item for item in plan.files if item.file_id == "p2")
        self.assertEqual(p2.analysis.truth_names, ("UNKNOWN",))

    def test_empty_in(self):
        self.assertEqual(
            self.decisions(self.EMPTY_IN),
            {"p1": "skip", "p2": "skip", "p3": "skip"},
        )

    def test_is_null_reads_all(self):
        self.assertEqual(
            self.decisions(self.IS_NULL),
            {"p1": "read", "p2": "read", "p3": "read"},
        )

    def test_full_report_verifies_against_fixtures(self):
        catalog = load_catalog(FIXTURES / "catalog.json")
        raw_queries, meta = load_queries(FIXTURES / "predicates.json")
        self.assertTrue(meta.get("where_true_only"))
        report = run(catalog, raw_queries, verify=True)
        self.assertTrue(report["verification"]["ok"], report["verification"]["problems"])
        for entry in report["queries"]:
            self.assertTrue(entry["verification"]["ok"])
            self.assertEqual(entry["verification"]["skipped_files_with_true_rows"], [])

    def test_untrusted_minmax_never_prunes(self):
        catalog, plan = fixture_plan({"op": "gt", "field": "x", "value": 0})
        p3 = next(item for item in plan.files if item.file_id == "p3")
        self.assertEqual(p3.decision, "read")
        # p3 的统计 trusted=false：其 min=0、max=0 不得被用来推断。
        self.assertEqual(p3.analysis.truth_names, ("TRUE", "FALSE", "UNKNOWN"))

    def test_json_report_serializable(self):
        catalog = load_catalog(FIXTURES / "catalog.json")
        raw_queries, _ = load_queries(FIXTURES / "predicates.json")
        report = run(catalog, raw_queries, verify=True)
        text = json.dumps(report, ensure_ascii=False)
        restored = json.loads(text)
        for entry in restored["queries"]:
            for file in entry["plan"]["files"]:
                self.assertIn(file["decision"], {"read", "skip"})
                self.assertIsInstance(file["derivation"], dict)


if __name__ == "__main__":
    unittest.main()

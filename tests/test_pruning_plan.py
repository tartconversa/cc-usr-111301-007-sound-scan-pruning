"""目录契约与端到端裁剪计划：临时合成湖文件，裁剪结果必须与全量扫描一致。"""

import json
import tempfile
import unittest
from pathlib import Path

from src.catalog import CatalogError, load_catalog
from src.planner import plan_scan
from src.predicate_ast import Column, normalize, parse, show
from src.pruning_reader import run
from src.scanner import DataError


def write_lake(root, catalog, files):
    """files: {file_id: {"path": 相对路径, "row_count": n, "stats": ..., "rows": [...]}}"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    layout = {"columns": catalog["columns"], "files": []}
    for file_id, spec in files.items():
        data_path = root / spec["path"]
        data_path.parent.mkdir(parents=True, exist_ok=True)
        with data_path.open("w", encoding="utf-8") as stream:
            for row in spec.get("rows", []):
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        entry = {
            "file_id": file_id,
            "path": spec["path"],
            "row_count": spec.get("row_count", len(spec.get("rows", []))),
            "stats": spec.get("stats", {}),
        }
        entry.update({k: v for k, v in spec.items() if k not in {"path", "row_count", "stats", "rows"}})
        layout["files"].append(entry)
    catalog_path = root / "catalog.json"
    catalog_path.write_text(json.dumps(layout, ensure_ascii=False), encoding="utf-8")
    return catalog_path


TWO_INT_COLUMNS = {
    "x": {"field_id": 1, "type": "integer"},
    "y": {"field_id": 2, "type": "integer"},
}


def load_and_plan(catalog_path, query):
    catalog = load_catalog(catalog_path)
    columns = {
        name: Column(name=name, kind=column.kind)
        for name, column in catalog.columns.items()
    }
    node = normalize(parse(query, columns))
    return catalog, node, plan_scan(catalog, node)


class CatalogTests(unittest.TestCase):
    def test_untrusted_stats_do_not_prune(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": TWO_INT_COLUMNS},
                {
                    "a": {
                        "path": "data/a.jsonl",
                        "stats": {"x": {"trusted": False, "null_count": 0, "min": 0, "max": 0}},
                        "rows": [{"id": 1, "x": 10, "y": 0}],
                    }
                },
            )
            catalog, node, plan = load_and_plan(path, {"op": "gt", "field": "x", "value": 10})
            file_plan = plan.files[0]
            self.assertEqual(file_plan.decision, "read")
            self.assertIn("不可信", file_plan.reason)

    def test_missing_trusted_flag_is_untrusted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": TWO_INT_COLUMNS},
                {
                    "a": {
                        "path": "data/a.jsonl",
                        "stats": {"x": {"null_count": 0, "min": 0, "max": 0}},
                        "rows": [{"id": 1, "x": 0, "y": 0}],
                    }
                },
            )
            catalog, node, plan = load_and_plan(path, {"op": "gt", "field": "x", "value": 10})
            self.assertEqual(plan.files[0].decision, "read")

    def test_contradictory_stats_become_untrusted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": TWO_INT_COLUMNS},
                {
                    "a": {
                        "path": "data/a.jsonl",
                        "stats": {"x": {"trusted": True, "null_count": 0, "min": 10, "max": 0}},
                        "rows": [{"id": 1, "x": 5, "y": 0}],
                    }
                },
            )
            catalog, node, plan = load_and_plan(path, {"op": "gt", "field": "x", "value": 100})
            self.assertEqual(plan.files[0].decision, "read")
            self.assertTrue(any("矛盾" in note for note in plan.files[0].notes))

    def test_null_count_exceeds_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": TWO_INT_COLUMNS},
                {
                    "a": {
                        "path": "data/a.jsonl",
                        "stats": {"x": {"trusted": True, "null_count": 5}},
                        "rows": [{"id": 1, "x": None, "y": 0}],
                    }
                },
            )
            catalog, node, plan = load_and_plan(path, {"op": "is_null", "field": "x"})
            self.assertEqual(plan.files[0].decision, "read")
            self.assertIn("TRUE", plan.files[0].analysis.truth_names)

    def test_empty_file_skipped_but_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": TWO_INT_COLUMNS},
                {
                    "a": {"path": "data/a.jsonl", "row_count": 0, "rows": []},
                    "b": {"path": "data/b.jsonl", "rows": [{"id": 1, "x": 1, "y": 0}]},
                },
            )
            catalog, node, plan = load_and_plan(path, {"op": "is_null", "field": "x"})
            self.assertEqual(plan.files[0].decision, "skip")
            self.assertEqual(plan.files[1].decision, "read")
            # 目录必须仍能读到空文件本身。
            self.assertEqual(catalog.files[0].row_count, 0)
            self.assertTrue(catalog.files[0].path.exists())

    def test_all_null_file_survives_user_scenario(self):
        """值域 {NULL}：NOT(x=1) 不能入选，但 IS NULL 必须入选。"""
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": TWO_INT_COLUMNS},
                {
                    "a": {
                        "path": "data/a.jsonl",
                        "stats": {"x": {"trusted": True, "null_count": 2}},
                        "rows": [{"id": 1, "x": None, "y": 0}, {"id": 2, "x": None, "y": 1}],
                    }
                },
            )
            catalog, node, plan = load_and_plan(
                path, {"op": "not", "arg": {"op": "eq", "field": "x", "value": 1}}
            )
            self.assertEqual(plan.files[0].decision, "skip")
            self.assertEqual(plan.files[0].analysis.truth_names, ("UNKNOWN",))

            _, node2, plan2 = load_and_plan(path, {"op": "is_null", "field": "x"})
            self.assertEqual(plan2.files[0].decision, "read")
            self.assertEqual(plan2.files[0].analysis.truth_names, ("TRUE",))

    def test_exact_dictionary_minmax_cross_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": TWO_INT_COLUMNS},
                {
                    "a": {
                        "path": "data/a.jsonl",
                        "stats": {"x": {"trusted": True, "null_count": 0, "min": 1, "max": 5, "exact_values": [2, 3]}},
                        "rows": [{"id": 1, "x": 2, "y": 0}, {"id": 2, "x": 3, "y": 0}],
                    }
                },
            )
            catalog, node, plan = load_and_plan(path, {"op": "gt", "field": "x", "value": 100})
            # min/max 与精确字典不一致：整块作废，不能借精确字典跳过。
            self.assertEqual(plan.files[0].decision, "read")

    def test_bloom_metadata_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": TWO_INT_COLUMNS},
                {
                    "a": {
                        "path": "data/a.jsonl",
                        "bloom": {"x": {"bits": 64, "hashes": 2}},
                        "stats": {"x": {"trusted": True, "null_count": 0}},
                        "rows": [{"id": 1, "x": 5, "y": 0}],
                    }
                },
            )
            catalog, node, plan = load_and_plan(path, {"op": "eq", "field": "x", "value": 99})
            entry = catalog.files[0]
            self.assertEqual(entry.ignored_bloom_keys, ("bloom",))
            self.assertEqual(plan.files[0].decision, "read")
            self.assertTrue(any("Bloom" in note for note in plan.files[0].notes))

    def test_unsupported_column_type_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": {"z": {"field_id": 1, "type": "float"}}},
                {"a": {"path": "data/a.jsonl", "rows": [{"z": 1.5}]}},
            )
            with self.assertRaises(CatalogError):
                load_catalog(path)

    def test_missing_file_is_reported_not_assumed_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(directory, {"columns": TWO_INT_COLUMNS}, {})
            layout = json.loads(Path(path).read_text(encoding="utf-8"))
            layout["files"] = [{"file_id": "gone", "path": "data/gone.jsonl", "row_count": 1, "stats": {}}]
            Path(path).write_text(json.dumps(layout), encoding="utf-8")
            catalog, node, _ = load_and_plan(path, {"op": "is_null", "field": "x"})
            with self.assertRaises(FileNotFoundError):
                run(catalog, [{"op": "is_null", "field": "x"}], verify=True)


class EndToEndDifferentialTests(unittest.TestCase):
    def test_pruned_matches_full_scan_for_bench_matrix(self):
        """一组混合文件（含全空、坏统计、空文件、边界值）上逐一比对。"""
        with tempfile.TemporaryDirectory() as directory:
            files = {
                "f_empty": {"path": "data/f_empty.jsonl", "row_count": 0, "rows": []},
                "f_all_null": {
                    "path": "data/f_all_null.jsonl",
                    "stats": {"x": {"trusted": True, "null_count": 3}},
                    "rows": [{"id": 30, "x": None, "y": 0}] * 3,
                },
                "f_minmax": {
                    "path": "data/f_minmax.jsonl",
                    "stats": {"x": {"trusted": True, "null_count": 0, "min": 0, "max": 10}},
                    "rows": [
                        {"id": 40, "x": 0, "y": 0},
                        {"id": 41, "x": 10, "y": 10},
                        {"id": 42, "x": 5, "y": 3},
                    ],
                },
                "f_untrusted": {
                    "path": "data/f_untrusted.jsonl",
                    "stats": {"x": {"trusted": False, "null_count": 0, "min": 100, "max": 100}},
                    "rows": [{"id": 50, "x": 0, "y": 0}],
                },
                "f_exact": {
                    "path": "data/f_exact.jsonl",
                    "stats": {"x": {"trusted": True, "null_count": 1, "exact_values": [1, 2]}},
                    "rows": [{"id": 60, "x": 1, "y": 2}, {"id": 61, "x": 2, "y": 1}, {"id": 62, "x": None, "y": 0}],
                },
                "f_single": {
                    "path": "data/f_single.jsonl",
                    "stats": {"x": {"trusted": True, "null_count": 0, "min": 7, "max": 7}},
                    "rows": [{"id": 70, "x": 7, "y": 0}, {"id": 71, "x": 7, "y": 0}],
                },
            }
            path = write_lake(directory, {"columns": TWO_INT_COLUMNS}, files)
            queries = [
                {"op": "not", "arg": {"op": "eq", "field": "x", "value": 1}},
                {"op": "gt", "field": "x", "value": 10},
                {"op": "ge", "field": "x", "value": 10},
                {"op": "is_null", "field": "x"},
                {"op": "in", "field": "x", "values": []},
                {"op": "in", "field": "x", "values": [1, 3]},
                {"op": "in", "field": "x", "values": [1, None]},
                {"op": "ne", "field": "x", "value": 5},
                {"op": "not", "arg": {"op": "in", "field": "x", "values": []}},
                {"op": "and", "args": [
                    {"op": "ge", "field": "x", "value": 0},
                    {"op": "le", "field": "x", "value": 10},
                ]},
                {"op": "not", "arg": {"op": "or", "args": [
                    {"op": "lt", "field": "x", "value": 1},
                    {"op": "gt", "field": "x", "value": 10},
                ]}},
                {"op": "eq", "field": "x", "other_field": "y"},
            ]
            catalog = load_catalog(path)
            report = run(catalog, queries, verify=True)
            self.assertTrue(report["verification"]["ok"], report["verification"]["problems"])
            for entry in report["queries"]:
                self.assertTrue(entry["verification"]["ok"], entry["verification"])
                for file in entry["plan"]["files"]:
                    if file["decision"] == "skip" and "TRUE" in file["possible_truths"]:
                        # 空文件可以跳过：没有行即没有满足条件的行，与真值集合无关。
                        self.assertEqual(file["row_count"], 0, file)

    def test_string_and_integer_domains_differ(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": {"s": {"field_id": 1, "type": "string"}}},
                {
                    "a": {
                        "path": "data/a.jsonl",
                        "stats": {"s": {"trusted": True, "null_count": 0, "min": "aaa", "max": "bbb"}},
                        "rows": [{"id": 1, "s": "aaa"}],
                    },
                    "b": {
                        "path": "data/b.jsonl",
                        "stats": {"s": {"trusted": True, "null_count": 0, "min": "zzz", "max": "zzz"}},
                        "rows": [{"id": 2, "s": "zzz"}],
                    },
                },
            )
            catalog, node, plan = load_and_plan(path, {"op": "lt", "field": "s", "value": "bbb"})
            self.assertEqual(plan.files[0].decision, "read")
            # min=max=zzz 的文件对 s < "bbb" 确定不可能入选。
            self.assertEqual(plan.files[1].decision, "skip")

    def test_bad_row_type_fails_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": TWO_INT_COLUMNS},
                {"a": {"path": "data/a.jsonl", "stats": {"x": {"trusted": True, "null_count": 0, "min": 1, "max": 1}},
                      "rows": [{"id": 1, "x": "one", "y": 0}]}},
            )
            catalog = load_catalog(path)
            with self.assertRaises(DataError):
                run(catalog, [{"op": "eq", "field": "x", "value": 1}], verify=True)

    def test_declared_row_count_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_lake(
                directory,
                {"columns": TWO_INT_COLUMNS},
                {"a": {"path": "data/a.jsonl", "row_count": 2, "rows": [{"id": 1, "x": 1, "y": 0}]}},
            )
            catalog = load_catalog(path)
            with self.assertRaises(DataError):
                run(catalog, [{"op": "eq", "field": "x", "value": 1}], verify=True)


if __name__ == "__main__":
    unittest.main()

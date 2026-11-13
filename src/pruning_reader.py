"""端到端 CLI：规划、扫描、逐行真实谓词校验，以及与全量扫描的差分。

用法::

    python -m src.pruning_reader fixtures/catalog.json
    python -m src.pruning_reader fixtures/catalog.json --all-queries
    python -m src.pruning_reader fixtures/catalog.json --verify --json
"""

import argparse
import csv
import json
import sys
from pathlib import Path

from .abstract_domain import Limits
from .catalog import CatalogError, load_catalog
from .planner import plan_scan
from .predicate_ast import Column, PredicateError, normalize, parse, show
from .scanner import DataError, scan_file


def load(path):
    """通用读取入口（保留原契约）：按后缀读取 csv / jsonl / json。

    裁剪逻辑本身只经 ``load_catalog`` 读取目录；本函数服务于既有
    输入契约测试与小型工具调用。
    """
    source = Path(path)
    if source.suffix == ".csv":
        with source.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    if source.suffix == ".jsonl":
        with source.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]
    if source.suffix == ".json":
        return json.loads(source.read_text(encoding="utf-8"))
    raise ValueError("不支持的输入文件格式")


def load_queries(path):
    """queries.json 既接受单个 AST，也接受 {"queries": [...], "where_true_only": ...}。"""
    source = Path(path)
    if source.suffix == ".jsonl":
        queries = []
        with source.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    queries.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise PredicateError(f"{source}: 第 {number} 行不是合法 JSON: {error}") from None
        return queries, {}
    raw = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw, {}
    if not isinstance(raw, dict):
        raise PredicateError(f"{source}: 查询文件顶层必须是对象或数组")
    if "queries" in raw:
        queries = raw["queries"]
        if not isinstance(queries, list) or not queries:
            raise PredicateError(f"{source}: queries 必须是非空数组")
        return queries, raw
    return [raw], raw


def run(catalog, raw_queries, limits=Limits(), verify=True):
    """返回可直接 json.dumps 的报告（逐行数据不放进报告）。"""
    columns = {name: Column(name=name, kind=column.kind) for name, column in catalog.columns.items()}
    report = {
        "catalog": str(catalog.source),
        "catalog_notes": list(catalog.notes),
        "queries": [],
        "verification": {"performed": bool(verify), "ok": True, "problems": []},
    }

    for index, raw_query in enumerate(raw_queries):
        node = normalize(parse(raw_query, columns, f"queries[{index}]"))
        plan = plan_scan(catalog, node, limits)
        entry = {
            "index": index,
            "predicate": show(node),
            "plan": plan.to_dict(),
        }

        if verify:
            verification = _verify(catalog, plan, node, columns)
            entry["scan"] = verification["pruned"]
            entry["baseline"] = verification["baseline"]
            entry["verification"] = verification["checks"]
            if not verification["checks"]["ok"]:
                report["verification"]["ok"] = False
                report["verification"]["problems"].append(
                    {
                        "query_index": index,
                        "predicate": show(node),
                        "problems": verification["checks"]["problems"],
                    }
                )
        report["queries"].append(entry)
    return report


def _verify(catalog, plan, node, columns):
    """裁剪扫描 vs 不裁剪扫描：逐项比对，差一即视为不一致。

    基线总是读取全部文件；被裁剪计划跳过的文件不应由它贡献任何入选行。
    """
    pruned_rows = []
    pruned_scan = {}
    for file_plan in plan.files:
        file_entry = catalog.by_id[file_plan.file_id]
        if file_plan.decision == "skip":
            pruned_scan[file_plan.file_id] = {"skipped": True}
            continue
        result = scan_file(file_entry, node, columns)
        pruned_scan[file_plan.file_id] = result.to_dict()
        pruned_rows.extend(result.true_rows)

    baseline_rows = []
    baseline_scan = {}
    skipped_contained_true = []
    for file_entry in catalog.files:
        result = scan_file(file_entry, node, columns)
        baseline_scan[file_entry.file_id] = result.to_dict()
        baseline_rows.extend(result.true_rows)
        if file_entry.file_id in {item.file_id for item in plan.skip_files}:
            if result.true_rows:
                skipped_contained_true.append(
                    {
                        "file_id": file_entry.file_id,
                        "true_rows": len(result.true_rows),
                    }
                )

    problems = []
    if skipped_contained_true:
        problems.append(
            {
                "kind": "漏读",
                "detail": "被跳过的文件实际含满足条件的行",
                "files": skipped_contained_true,
            }
        )
    if len(pruned_rows) != len(baseline_rows):
        problems.append(
            {
                "kind": "行数不一致",
                "detail": f"裁剪扫描 {len(pruned_rows)} 行，全量扫描 {len(baseline_rows)} 行",
            }
        )
    if pruned_rows != baseline_rows:
        mismatched_ids = _row_identity_mismatch(pruned_rows, baseline_rows)
        if mismatched_ids:
            problems.append(
                {
                    "kind": "行内容不一致",
                    "detail": "逐行比对发现差异",
                    "ids": mismatched_ids,
                }
            )
    checks = {
        "ok": not problems,
        "skipped_files_checked": [
            item.file_id for item in plan.skip_files
        ],
        "skipped_files_with_true_rows": [
            item["file_id"] for item in skipped_contained_true
        ],
        "problems": problems,
        "pruned_selected": len(pruned_rows),
        "baseline_selected": len(baseline_rows),
        "row_identity_compared": "按 id 字段（缺 id 时按行内容）比较",
    }
    return {"pruned": pruned_scan, "baseline": baseline_scan, "checks": checks}


def _row_identity(row):
    if isinstance(row, dict) and "id" in row:
        return ("id", str(row["id"]))
    return ("row", json.dumps(row, sort_keys=True, ensure_ascii=False))


def _row_identity_mismatch(pruned_rows, baseline_rows):
    """裁剪结果应是全量结果的子序列；返回两侧第一处差异的数量与标识。"""
    pruned = [_row_identity(row) for row in pruned_rows]
    baseline = [_row_identity(row) for row in baseline_rows]
    pruned_only = [item for item in pruned if item not in baseline]
    baseline_only = [item for item in baseline if item not in pruned]
    return {
        "pruned_count": len(pruned),
        "baseline_count": len(baseline),
        "pruned_only": pruned_only[:10],
        "baseline_only": baseline_only[:10],
    }


__all__ = ["load_queries", "run"]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="湖文件三值逻辑保守裁剪规划器：规划、扫描、逐行校验"
    )
    parser.add_argument("catalog", help="目录 JSON（只读；不读目录外的统计来源）")
    parser.add_argument(
        "query",
        nargs="?",
        help="查询文件（AST 数组或 {\"queries\": [...]}）；省略时使用同目录 predicates.json",
    )
    parser.add_argument("--verify", action="store_true", help="扫描后与全量扫描逐行比对")
    parser.add_argument("--no-verify", action="store_true", help="只输出裁剪计划")
    parser.add_argument("--all-queries", action="store_true", help="输出全部查询的报告")
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认人类可读）")
    parser.add_argument(
        "--enumeration-limit", type=int, default=256,
        help="精确字典展开为逐值枚举的上限；超过则退化为边界域（更保守、树更小）",
    )
    args = parser.parse_args(argv)

    if args.verify and args.no_verify:
        parser.error("--verify 与 --no-verify 互斥")

    query_path = Path(args.query) if args.query else Path(args.catalog).parent / "predicates.json"
    try:
        catalog = load_catalog(args.catalog)
        raw_queries, query_meta = load_queries(query_path)
    except (CatalogError, PredicateError, json.JSONDecodeError, FileNotFoundError) as error:
        print(f"输入错误: {error}", file=sys.stderr)
        return 2

    limits = Limits(enumeration_limit=max(0, args.enumeration_limit))
    verify = not args.no_verify
    try:
        report = run(catalog, raw_queries, limits=limits, verify=verify)
    except PredicateError as error:
        print(f"谓词错误: {error}", file=sys.stderr)
        return 2
    except DataError as error:
        print(f"数据错误: {error}", file=sys.stderr)
        return 1
    report["queries_file"] = str(query_path)
    report["where_true_only"] = query_meta.get("where_true_only", True)

    if args.json:
        selected = report
        if not args.all_queries and len(report["queries"]) > 1:
            selected = dict(report)
            selected["queries"] = report["queries"][:1]
            selected["note"] = "默认每个查询各列一次报告会被截断，用 --all-queries 查看全部"
        print(json.dumps(selected, ensure_ascii=False, indent=2))
    else:
        _print_human(report, args.all_queries)
    return 0 if report["verification"]["ok"] else 1


def _print_human(report, all_queries):
    for entry in report["queries"][: None if all_queries else 1]:
        plan = entry["plan"]
        print(f"# 查询 {entry['index']}: {entry['predicate']}")
        for file in plan["files"]:
            truths = "/".join(file["possible_truths"]) or "∅"
            print(
                f"  {file['decision']:<4} {file['file_id']:<6} rows={file['row_count']} "
                f"possible={truths}  理由: {file['reason']}"
            )
        print(
            f"  汇总: 读 {plan['summary']['read']} 个文件 / "
            f"跳 {plan['summary']['skip']} 个，估计读取行数 "
            f"{plan['summary']['rows_read_estimate']}（跳过 "
            f"{plan['summary']['rows_skipped_estimate']}）"
        )
        if "verification" in entry:
            checks = entry["verification"]
            print(
                f"  校验: {'通过' if checks['ok'] else '失败'} "
                f"(裁剪入选 {checks['pruned_selected']} 行，全量 {checks['baseline_selected']} 行)"
            )
            for problem in checks["problems"]:
                print(f"    - {problem['kind']}: {problem['detail']}")
        print()


if __name__ == "__main__":
    sys.exit(main())

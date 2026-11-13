"""数据读取：严格的 JSONL 读取与逐行三值求值。

数据只在真正被读取时校验：任何 JSON 语法错误、非对象行、目录声明的
类型不符、目录行数与实际行数不符都直接报错，而不是悄悄跳过行。裁剪
的正确性依赖“被跳过的文件不含入选行”，所以被读取的文件必须让校验
看得见每一行。

注意：目录统计与真实数据之间的矛盾**不**在这里作为错误抛出——统计
的职责只是保守上界，若扫描发现某行违反上界，说明目录撒谎，结果与
不裁剪扫描比对时由调用方作为不一致报告。
"""

import json
from dataclasses import dataclass, field

from .predicate_ast import evaluate
from .three_valued import TRUE, UNKNOWN, value_matches_kind


class DataError(ValueError):
    """数据不可读或不符合目录契约。"""


@dataclass
class ScanResult:
    file_id: str
    row_count_declared: int
    row_count_actual: int
    rows_seen: int
    true_rows: list
    false_rows: int = 0
    unknown_rows: int = 0
    truth_counts: dict = field(default_factory=dict)

    def to_dict(self, include_rows=False):
        result = {
            "file_id": self.file_id,
            "row_count_declared": self.row_count_declared,
            "row_count_actual": self.row_count_actual,
            "rows_seen": self.rows_seen,
            "selected": len(self.true_rows),
            "truth_counts": dict(self.truth_counts),
        }
        if include_rows:
            result["rows"] = self.true_rows
        return result


def read_rows(path):
    """按 UTF-8 读取 JSONL 的每一行；坏行带行号报错。"""
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise DataError(f"{path}: 第 {number} 行不是合法 JSON: {error}") from None
            if not isinstance(row, dict):
                raise DataError(f"{path}: 第 {number} 行不是 JSON 对象")
            rows.append(row)
    return rows


def validate_rows(file_entry, row_count_actual, columns, strict_row_count=True):
    if strict_row_count and row_count_actual != file_entry.row_count:
        raise DataError(
            f"{file_entry.rel_path}: 目录 row_count={file_entry.row_count}，"
            f"实际读取 {row_count_actual} 行"
        )
    if file_entry.row_count == 0 and row_count_actual != 0:
        raise DataError(f"{file_entry.rel_path}: 目录声明空文件但实际有行")


def check_row_values(row, columns, where):
    """目录声明的字段类型必须与行内值一致；缺列按 NULL。"""
    for name, column in columns.items():
        if name not in row:
            continue
        value = row[name]
        if value is None:
            continue
        if not value_matches_kind(value, column.kind):
            raise DataError(
                f"{where}: 字段 {name!r} 声明为 {column.kind}，实际值 {value!r} 类型不符"
            )


def scan_file(file_entry, node, columns):
    """完整读取并逐行求值。返回 ScanResult，真实入选行一并带回。"""
    rows = read_rows(file_entry.path)
    validate_rows(file_entry, len(rows), columns)
    true_rows = []
    false_count = 0
    unknown_count = 0
    for number, row in enumerate(rows, start=1):
        check_row_values(row, columns, f"{file_entry.rel_path}:{number}")
        truth = evaluate(node, row)
        if truth is TRUE:
            true_rows.append(row)
        elif truth is UNKNOWN:
            unknown_count += 1
        else:
            false_count += 1
    truth_counts = {name: count for name, count in (
        ("TRUE", len(true_rows)), ("FALSE", false_count), ("UNKNOWN", unknown_count)
    ) if count}
    return ScanResult(
        file_id=file_entry.file_id,
        row_count_declared=file_entry.row_count,
        row_count_actual=len(rows),
        rows_seen=len(rows),
        true_rows=true_rows,
        false_rows=false_count,
        unknown_rows=unknown_count,
        truth_counts=truth_counts,
    )


__all__ = ["DataError", "ScanResult", "read_rows", "validate_rows", "check_row_values", "scan_file"]

"""谓词 AST 的校验、规范化与具体求值。

谓词只描述数据，不执行任意 Python 表达式。支持的节点：

- 叶子：``{"op": "eq"|"ne"|"lt"|"le"|"gt"|"ge", "field": 名, "value": 常量}``
- 叶子：``{"op": "in", "field": 名, "values": [常量...]}``
- 叶子：``{"op": "is_null"|"is_not_null", "field": 名}``
- 组合：``{"op": "not", "arg": ...}``、``{"op": "and"|"or", "args": [...]}``

常量可以是 JSON 整数/字符串/null。``is_null``/``is_not_null`` 之外的叶子
遇到 NULL 常量时按 SQL 恒为 UNKNOWN（``x IN (1, NULL)`` 不是恒 UNKNOWN，
只有候选全为 NULL 才是），规范化阶段会据此折叠。
"""

from dataclasses import dataclass

from .three_valued import (
    COMPARISON_OPS,
    FALSE,
    TRUE,
    UNKNOWN,
    and3,
    compare,
    in_set,
    is_not_null3,
    is_null3,
    NEGATED_COMPARISON,
    not3,
    or3,
    value_matches_kind,
)


class PredicateError(ValueError):
    """谓词结构或类型不合契约。"""


@dataclass(frozen=True)
class Column:
    name: str
    kind: str


@dataclass(frozen=True)
class Compare:
    op: str
    field: str
    value: object = None
    other_field: str | None = None

    @property
    def is_cross_column(self):
        return self.other_field is not None


@dataclass(frozen=True)
class InList:
    field: str
    values: tuple


@dataclass(frozen=True)
class IsNull:
    field: str


@dataclass(frozen=True)
class NotNull:
    field: str


@dataclass(frozen=True)
class Not:
    arg: object


@dataclass(frozen=True)
class And:
    args: tuple


@dataclass(frozen=True)
class Or:
    args: tuple


@dataclass(frozen=True)
class Constant:
    """语义完全确定的真值节点，仅由规范化产生。"""

    truth: object


@dataclass(frozen=True)
class NotIn:
    """``NOT (x IN (...))``：保留候选列表，以便抽象解释传播 NULL 候选。"""

    field: str
    values: tuple


LEAF_NODES = (Compare, InList, IsNull, NotNull, Constant, NotIn)

#: NOT 展开成 De Morgan 的节点数上限。超过时保留 Not 节点，抽象解释器
#: 也会直接处理它；这样规范化不会因嵌套查询把推导树膨胀成指数级。
NORMALIZE_TREE_BUDGET = 96


def size(node):
    if isinstance(node, (Compare, InList, NotIn)):
        if isinstance(node, Compare):
            return 1
        return 1 + len(node.values)
    if isinstance(node, (IsNull, NotNull, Constant)):
        return 1
    if isinstance(node, Not):
        return 1 + size(node.arg)
    if isinstance(node, (And, Or)):
        return 1 + sum(size(child) for child in node.args)
    raise PredicateError(f"未知 AST 节点: {node!r}")


def parse(node, columns, where="predicate"):
    """校验并构造不可变 AST。columns 为 {名: Column}。"""
    if not isinstance(node, dict):
        raise PredicateError(f"{where}: 谓词节点必须是对象")
    keys = set(node)
    op = node.get("op")
    if not isinstance(op, str):
        raise PredicateError(f"{where}: 缺少字符串 op")

    if op in COMPARISON_OPS:
        if "other_field" in node:
            _check_keys(keys, {"op", "field", "other_field"}, where, op)
            left = _column(node["field"], columns, where)
            right = _column(node["other_field"], columns, where)
            if left.kind != right.kind:
                raise PredicateError(
                    f"{where}: 列间比较要求同类型，{left.name!r} 是 {left.kind}，"
                    f"{right.name!r} 是 {right.kind}"
                )
            if left.name == right.name:
                raise PredicateError(f"{where}: 列间比较的两侧不能是同一列")
            return Compare(op, left.name, None, right.name)
        _check_keys(keys, {"op", "field", "value"}, where, op)
        column = _column(node["field"], columns, where)
        value = node["value"]
        _check_constant(value, column, where)
        return Compare(op, column.name, value)

    if op == "in":
        _check_keys(keys, {"op", "field", "values"}, where, op)
        column = _column(node["field"], columns, where)
        values = node["values"]
        if not isinstance(values, list):
            raise PredicateError(f"{where}: in 的 values 必须是数组")
        for value in values:
            _check_constant(value, column, where)
        return InList(column.name, tuple(values))

    if op in ("is_null", "is_not_null"):
        _check_keys(keys, {"op", "field"}, where, op)
        column = _column(node["field"], columns, where)
        return IsNull(column.name) if op == "is_null" else NotNull(column.name)

    if op == "not":
        _check_keys(keys, {"op", "arg"}, where, op)
        return Not(parse(node["arg"], columns, f"{where}.not"))

    if op in ("and", "or"):
        _check_keys(keys, {"op", "args"}, where, op)
        args = node["args"]
        if not isinstance(args, list) or not args:
            raise PredicateError(f"{where}: {op} 的 args 必须是非空数组")
        parsed = tuple(
            parse(child, columns, f"{where}.{op}[{index}]")
            for index, child in enumerate(args)
        )
        return And(parsed) if op == "and" else Or(parsed)

    raise PredicateError(f"{where}: 不支持的 op {op!r}")


def _check_keys(keys, allowed, where, op):
    missing = allowed - keys
    unexpected = keys - allowed
    if missing or unexpected:
        raise PredicateError(
            f"{where}: {op} 节点的键不符，缺少 {sorted(missing)}，多出 {sorted(unexpected)}"
        )


def _check_constant(value, column, where):
    if value is None:
        return
    if not value_matches_kind(value, column.kind):
        raise PredicateError(
            f"{where}: 列 {column.name!r} 声明为 {column.kind}，"
            f"但谓词常量 {value!r} 类型不符"
        )


def _column(name, columns, where):
    if not isinstance(name, str) or name not in columns:
        raise PredicateError(f"{where}: 未知列 {name!r}")
    return columns[name]


def normalize(node, budget=NORMALIZE_TREE_BUDGET):
    """在保持三值语义的前提下做语法级化简。

    只折叠语义已完全确定的节点：
    - 比较常量 NULL、候选全为 NULL 的非空 IN → 恒 UNKNOWN；
    - 空 IN → FALSE（SQL 空析取，哪怕 x 是 NULL）；
    - 双重否定、NOT 作用于上述常量。

    NOT 对含 NULL 候选的 IN 变为 ``NotIn``；对 AND/OR 在预算内做
    De Morgan，让推导树逐叶可读。任何化简都不把 UNKNOWN 折成 FALSE。
    """
    if isinstance(node, (IsNull, NotNull, Constant, NotIn)):
        return node
    if isinstance(node, Compare):
        if node.is_cross_column:
            return node
        return Constant(UNKNOWN) if node.value is None else node
    if isinstance(node, InList):
        if not node.values:
            return Constant(FALSE)
        if all(value is None for value in node.values):
            return Constant(UNKNOWN)
        return node
    if isinstance(node, Not):
        return _normalize_not(normalize(node.arg, budget), budget)
    if isinstance(node, And):
        args = tuple(normalize(child, budget) for child in node.args)
        return args[0] if len(args) == 1 else And(args)
    if isinstance(node, Or):
        args = tuple(normalize(child, budget) for child in node.args)
        return args[0] if len(args) == 1 else Or(args)
    raise PredicateError(f"未知 AST 节点: {node!r}")


def _normalize_not(arg, budget):
    if isinstance(arg, Constant):
        return Constant(not3(arg.truth))
    if isinstance(arg, Compare):
        if arg.is_cross_column:
            return Compare(NEGATED_COMPARISON[arg.op], arg.field, None, arg.other_field)
        return Compare(NEGATED_COMPARISON[arg.op], arg.field, arg.value)
    if isinstance(arg, InList):
        return NotIn(arg.field, arg.values)  # 空 IN 已在 normalize 折成 FALSE
    if isinstance(arg, NotIn):
        return InList(arg.field, arg.values)
    if isinstance(arg, IsNull):
        return NotNull(arg.field)
    if isinstance(arg, NotNull):
        return IsNull(arg.field)
    if isinstance(arg, Not):
        return arg.arg
    if isinstance(arg, And):
        expanded = Or(tuple(_normalize_not(child, budget) for child in arg.args))
        return expanded if size(expanded) <= budget else Not(arg)
    if isinstance(arg, Or):
        expanded = And(tuple(_normalize_not(child, budget) for child in arg.args))
        return expanded if size(expanded) <= budget else Not(arg)
    raise PredicateError(f"未知 AST 节点: {arg!r}")


def evaluate(node, row, missing_is_null=True):
    """对一行数据具体求值，返回 TRUE/FALSE/UNKNOWN。

    行里缺列时按 NULL 处理（合成湖文件的稀疏行语义）；严格模式由
    扫描器在读取阶段用目录校验承担。
    """
    if isinstance(node, Constant):
        return node.truth
    if isinstance(node, Compare):
        if node.other_field is not None:
            left = _field_value(row, node.field, missing_is_null)
            right = _field_value(row, node.other_field, missing_is_null)
            if left is None or right is None:
                return UNKNOWN
            return compare(node.op, left, right)
        value = _field_value(row, node.field, missing_is_null)
        if value is None or node.value is None:
            return UNKNOWN
        return compare(node.op, value, node.value)
    if isinstance(node, InList):
        return in_set(_field_value(row, node.field, missing_is_null), node.values)
    if isinstance(node, NotIn):
        return not3(in_set(_field_value(row, node.field, missing_is_null), node.values))
    if isinstance(node, IsNull):
        return is_null3(_field_value(row, node.field, missing_is_null))
    if isinstance(node, NotNull):
        return is_not_null3(_field_value(row, node.field, missing_is_null))
    if isinstance(node, Not):
        return not3(evaluate(node.arg, row, missing_is_null))
    if isinstance(node, And):
        # 谨慎求值：FALSE 可短路，UNKNOWN 必须继续传播。
        result = TRUE
        for child in node.args:
            result = and3(result, evaluate(child, row, missing_is_null))
            if result is FALSE:
                return FALSE
        return result
    if isinstance(node, Or):
        result = FALSE
        for child in node.args:
            result = or3(result, evaluate(child, row, missing_is_null))
            if result is TRUE:
                return TRUE
        return result
    raise PredicateError(f"未知 AST 节点 {node!r}")


def _field_value(row, name, missing_is_null):
    if not isinstance(row, dict):
        raise PredicateError("JSONL 行必须是对象")
    if name in row:
        return row[name]
    if missing_is_null:
        return None
    raise PredicateError(f"行缺少字段 {name!r}")


def show(node):
    """把 AST 渲染成紧凑中缀文本，供推导树展示。"""
    if isinstance(node, Constant):
        return "TRUE" if node.truth is TRUE else "FALSE" if node.truth is FALSE else "UNKNOWN"
    if isinstance(node, Compare):
        if node.is_cross_column:
            symbol = {"eq": "=", "ne": "≠", "lt": "<", "le": "≤", "gt": ">", "ge": "≥"}[node.op]
            return f"{node.field} {symbol} {node.other_field}"
        symbol = {"eq": "=", "ne": "≠", "lt": "<", "le": "≤", "gt": ">", "ge": "≥"}[node.op]
        return f"{node.field} {symbol} {_show_constant(node.value)}"
    if isinstance(node, InList):
        return f"{node.field} IN ({', '.join(_show_constant(v) for v in node.values)})"
    if isinstance(node, NotIn):
        return f"{node.field} NOT IN ({', '.join(_show_constant(v) for v in node.values)})"
    if isinstance(node, IsNull):
        return f"{node.field} IS NULL"
    if isinstance(node, NotNull):
        return f"{node.field} IS NOT NULL"
    if isinstance(node, Not):
        return f"NOT ({show(node.arg)})"
    if isinstance(node, And):
        return "(" + " AND ".join(show(child) for child in node.args) + ")"
    if isinstance(node, Or):
        return "(" + " OR ".join(show(child) for child in node.args) + ")"
    raise PredicateError(f"未知节点 {node!r}")


def referenced_fields(node):
    """谓词引用的列集合，用于把“无统计”归因到具体列。"""
    if isinstance(node, Compare):
        return {node.field} | ({node.other_field} if node.other_field else set())
    if isinstance(node, (InList, NotIn, IsNull, NotNull)):
        return {node.field}
    if isinstance(node, Not):
        return referenced_fields(node.arg)
    if isinstance(node, (And, Or)):
        fields = set()
        for child in node.args:
            fields.update(referenced_fields(child))
        return fields
    if isinstance(node, Constant):
        return set()
    raise PredicateError(f"未知 AST 节点 {node!r}")


def _show_constant(value):
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return repr(value)
    return str(value)


__all__ = [
    "Column",
    "PredicateError",
    "Compare",
    "InList",
    "IsNull",
    "NotNull",
    "Not",
    "And",
    "Or",
    "Constant",
    "NotIn",
    "LEAF_NODES",
    "parse",
    "normalize",
    "size",
    "show",
    "evaluate",
]

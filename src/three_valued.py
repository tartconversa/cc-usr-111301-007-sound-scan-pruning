"""SQL 三值逻辑的公共语义。

表达式求值（扫描校验用）与抽象解释（裁剪规划用）共用这里的常量与
比较规则，避免两条路径对 NULL 的理解出现漂移。

约定：
- Python 的 ``None`` 代表 SQL NULL。
- 真值为 ``True`` / ``False`` / ``None``（UNKNOWN）。
- WHERE 只接受 ``True``；``False`` 与 ``None`` 都不入选。

``IN`` 的空列表按 SQL 空析取处理：``x IN ()`` 恒为 FALSE，即使 x 是
NULL（等价于对空子查询做 IN）。这一点直接决定了 ``NOT (x IN ())``
对全 NULL 行仍为 TRUE，规划器与扫描器必须给出同一答案。
"""

TRUE = True
FALSE = False
UNKNOWN = None

TRUTHS = (TRUE, FALSE, UNKNOWN)

#: 统计只允许出现在这些受支持类型上。
SUPPORTED_TYPES = frozenset({"integer", "string"})

NEGATED_COMPARISON = {
    "eq": "ne",
    "ne": "eq",
    "lt": "ge",
    "le": "gt",
    "gt": "le",
    "ge": "lt",
}

COMPARISON_OPS = frozenset(NEGATED_COMPARISON)


def truth_name(value):
    if value is TRUE:
        return "TRUE"
    if value is FALSE:
        return "FALSE"
    if value is UNKNOWN:
        return "UNKNOWN"
    raise ValueError(f"不是三值逻辑真值: {value!r}")


def truth_names(values):
    order = {TRUE: 0, FALSE: 1, UNKNOWN: 2}
    return [truth_name(value) for value in sorted(values, key=order.__getitem__)]


def value_matches_kind(value, kind):
    """行值/统计值是否符合目录声明的明确类型。bool 不算 integer。"""
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "string":
        return isinstance(value, str)
    return False


def _scalar(value):
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, str))


def compare(op, left, right):
    """按 SQL 语义比较两个非 NULL 值。

    若任一侧类型不符（例如整数列里出现 JSON 字符串），结果统一为
    UNKNOWN：不做隐式转换，也不让一条坏行把扫描器抛进异常路径。扫描
    前置校验会先拒绝这种行，因此正常路径不会走到这里。
    """
    if op not in COMPARISON_OPS:
        raise ValueError(f"未知比较运算符: {op}")
    assert left is not None and right is not None
    if not _scalar(left) or not _scalar(right):
        return UNKNOWN
    if type(left) is not type(right):
        return UNKNOWN
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    raise AssertionError("不可达")


def in_set(value, candidates):
    """SQL ``x IN (values...)`` 语义。candidates 已按列类型校验，可为 None。"""
    if not candidates:
        # SQL 空析取：FALSE，包括 NULL IN ()。
        return FALSE
    if value is None:
        return UNKNOWN
    saw_null = False
    for candidate in candidates:
        if candidate is None:
            saw_null = True
            continue
        result = compare("eq", value, candidate)
        if result is TRUE:
            return TRUE
        if result is UNKNOWN:
            return UNKNOWN
    return UNKNOWN if saw_null else FALSE


def not3(value):
    if value is UNKNOWN:
        return UNKNOWN
    return not value


def and3(left, right):
    if left is FALSE or right is FALSE:
        return FALSE
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    return TRUE


def or3(left, right):
    if left is TRUE or right is TRUE:
        return TRUE
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    return FALSE


def is_null3(value):
    return TRUE if value is None else FALSE


def is_not_null3(value):
    return FALSE if value is None else TRUE


__all__ = [
    "TRUE",
    "FALSE",
    "UNKNOWN",
    "TRUTHS",
    "SUPPORTED_TYPES",
    "COMPARISON_OPS",
    "NEGATED_COMPARISON",
    "truth_name",
    "truth_names",
    "value_matches_kind",
    "compare",
    "in_set",
    "not3",
    "and3",
    "or3",
    "is_null3",
    "is_not_null3",
]

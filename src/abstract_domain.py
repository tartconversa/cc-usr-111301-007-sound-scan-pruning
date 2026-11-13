"""统计域上的保守抽象解释：推导单个文件内谓词可能为真的真值集合。

核心不变量：文件内任一行对谓词的取值都落在返回的真值集合里。因此
``TRUE`` 不在集合中 ⟹ 文件不可能含入选行 ⟹ 允许跳过。集合宁大勿小：
多读一行的代价可接受，漏读一行不可接受。

单列域 ``ColumnDomain`` 只描述“这一列可能有哪些值、是否可能为 NULL”，
不携带跨列相关性。AND/OR 直接按集合组合，绝不从两列各自的 min/max
推导 ``x = y`` 之类似是而非的结论；列间比较在无相关性信息时同时保留
TRUE 与 FALSE 两种可能。

NOT 按三值 ``not3`` 逐点传播，而不是对集合取补：UNKNOWN 取反仍是
UNKNOWN，全 NULL 文件不会因为 NOT 把 UNKNOWN 翻成 TRUE。
"""

from dataclasses import dataclass

from .predicate_ast import (
    And,
    Compare,
    Constant,
    InList,
    IsNull,
    Not,
    NotIn,
    NotNull,
    Or,
    show,
)
from .three_valued import (
    FALSE,
    TRUE,
    TRUTHS,
    UNKNOWN,
    and3,
    compare,
    in_set,
    not3,
    or3,
    truth_name,
    value_matches_kind,
)

#: 精确字典枚举抽象解释的额外上限。超过时退化为只保留 min/max 边界，
#: 推导树更小，但可能多保留一个真值（仍然保守）。
DEFAULT_ENUMERATION_LIMIT = 256


class AnalysisError(ValueError):
    """抽象解释输入不满足前置条件。"""


@dataclass(frozen=True)
class Limits:
    enumeration_limit: int = DEFAULT_ENUMERATION_LIMIT


@dataclass(frozen=True)
class Analysis:
    truths: frozenset
    tree: object

    @property
    def may_be_true(self):
        return TRUE in self.truths

    @property
    def truth_names(self):
        return _names(self.truths)

    def to_dict(self):
        return {
            "possible_truths": list(self.truth_names),
            "may_be_true": self.may_be_true,
            "derivation": self.tree.to_dict(),
        }


def analyze(node, domains, limits=Limits()):
    return _analyze(node, domains, limits)


def analyze_truths(node, domains, limits=Limits()):
    return _analyze(node, domains, limits).truths


def _analyze(node, domains, limits):
    if isinstance(node, Constant):
        truths = frozenset([node.truth])
        return Analysis(
            truths,
            LeafTree("常量", _names(truths), "规范化已确定真值", "—", "constant"),
        )
    if isinstance(node, Not):
        inner = _analyze(node.arg, domains, limits)
        truths = frozenset(not3(value) for value in inner.truths)
        return Analysis(
            truths,
            NotTree(
                "NOT",
                inner.tree,
                _names(truths),
                "逐点三值取反：UNKNOWN 取反仍是 UNKNOWN，不把它当 FALSE 再翻转",
            ),
        )
    if isinstance(node, And):
        results = [_analyze(child, domains, limits) for child in node.args]
        truths, steps = _combine_and_or(results, and3)
        return Analysis(
            truths,
            GroupTree(
                "AND",
                _names(truths),
                tuple(result.tree for result in results),
                "逐枝谨慎 AND：任一 FALSE 即 FALSE；否则任一 UNKNOWN 即 UNKNOWN",
                steps,
            ),
        )
    if isinstance(node, Or):
        results = [_analyze(child, domains, limits) for child in node.args]
        truths, steps = _combine_and_or(results, or3)
        return Analysis(
            truths,
            GroupTree(
                "OR",
                _names(truths),
                tuple(result.tree for result in results),
                "逐枝谨慎 OR：任一 TRUE 即 TRUE；否则任一 UNKNOWN 即 UNKNOWN",
                steps,
            ),
        )
    if isinstance(node, Compare):
        return _analyze_compare(node, domains, limits)
    if isinstance(node, InList):
        return _analyze_in(node, domains, limits)
    if isinstance(node, NotIn):
        inner = _analyze_in(InList(node.field, node.values), domains, limits)
        truths = frozenset(not3(value) for value in inner.truths)
        return Analysis(
            truths,
            NotTree("NOT IN", inner.tree, _names(truths), "NOT 在 IN 的结果上逐点取反"),
        )
    if isinstance(node, IsNull):
        return _analyze_is_null(node, domains)
    if isinstance(node, NotNull):
        return _analyze_not_null(node, domains)
    raise AnalysisError(f"未知 AST 节点: {node!r}")


def unused_domain_notes(node, domains):
    """谓词用不到的列：它们的统计状态不应影响任何 read/skip 理由。"""
    from .predicate_ast import referenced_fields

    used = referenced_fields(node)
    return {
        name: "该列未被谓词引用，其统计状态不参与本次裁决"
        for name in domains
        if name not in used
    }


def _combine_and_or(results, operation):
    """按谨慎三值运算从左到右折叠，保留每步累积集合方便审计。"""
    seed = frozenset([TRUE]) if operation is and3 else frozenset([FALSE])
    accumulated = seed
    steps = []
    for result in results:
        accumulated = frozenset(
            operation(left, right)
            for left in accumulated
            for right in result.truths
        )
        steps.append(
            {
                "branch_truths": list(_names(result.truths)),
                "accumulated": list(_names(accumulated)),
            }
        )
    return accumulated, tuple(steps)


def _names(truths):
    order = {TRUE: 0, FALSE: 1, UNKNOWN: 2}
    return tuple(truth_name(value) for value in sorted(truths, key=order.__getitem__))


def _domain(domains, name):
    try:
        return domains[name]
    except KeyError:
        raise AnalysisError(f"统计域缺少列 {name!r}") from None


def _null_outcomes(domain):
    return frozenset([UNKNOWN]) if domain.null_possible else frozenset()


def _non_null_representation(domain, limits, column_kind):
    """非 NULL 值的抽象表示：("enum", 值表) / ("bounded", lo, hi) / ("unknown",)。"""
    if domain.exact is not None:
        if not domain.exact:
            # 空精确字典与非空可能性自相矛盾；宁可不精确，不能当作“没有非空值”。
            return ("unknown",)
        if len(domain.exact) <= limits.enumeration_limit:
            return ("enum", tuple(sorted(domain.exact)))
        return ("bounded", min(domain.exact), max(domain.exact))
    if domain.lo is not None or domain.hi is not None:
        return ("bounded", domain.lo, domain.hi)
    return ("unknown",)


def _empty_domain_truths(domain):
    """两向都不可能只出现在 row_count=0 的域；没有行，空集合也是保守的。"""
    return frozenset()


def _analyze_compare(node, domains, limits):
    domain = _domain(domains, node.field)
    other = getattr(node, "other_field", None)
    if other is None and node.value is None:
        # 防御：未规范化的 AST 里 ``x = NULL`` 恒为 UNKNOWN，与 x 的域无关。
        truths = frozenset([UNKNOWN])
        return Analysis(truths, _leaf(node, domain, truths, "比较常量是 NULL：恒为 UNKNOWN"))
    if other is not None:
        return _analyze_cross_compare(node, domains, limits)
    if not domain.null_possible and not domain.non_null_possible:
        return Analysis(
            _empty_domain_truths(domain),
            _leaf(node, domain, (), "文件无行，真值集合为空"),
        )
    if not domain.non_null_possible:
        truths = _null_outcomes(domain)
        return Analysis(
            truths,
            _leaf(node, domain, truths, "无可信非空值：比较只可能得到 UNKNOWN"),
        )

    representation = _non_null_representation(domain, limits, domain.kind)
    if representation[0] == "enum":
        outcomes = frozenset(compare(node.op, value, node.value) for value in representation[1])
        reason = f"枚举可信精确字典的 {len(representation[1])} 个非空值逐个求值"
    elif representation[0] == "bounded":
        outcomes = _bounded_compare_outcomes(
            node.op, representation[1], representation[2], node.value
        )
        reason = (
            "按可信 min/max 判定 TRUE/FALSE 各自的可满足性；"
            "边界外无法否定任一真值"
        )
    else:
        outcomes = frozenset([TRUE, FALSE])
        reason = "无非空值边界可用，保留 TRUE 与 FALSE 两种可能"
    truths = frozenset(outcomes) | _null_outcomes(domain)
    return Analysis(truths, _leaf(node, domain, truths, reason, representation))


def _analyze_cross_compare(node, domains, limits):
    """列间比较：没有相关性信息时，两个非空值之间 TRUE/FALSE 都可能。

    即使两列都有 0..10 的边界，也不能断言 ``x = y`` 一定存在或一定不存在。
    """
    other = node.other_field
    left = _domain(domains, node.field)
    right = _domain(domains, other)
    outcomes = set()
    if left.null_possible or right.null_possible:
        outcomes.add(UNKNOWN)
    if left.non_null_possible and right.non_null_possible:
        outcomes.update((TRUE, FALSE))
    truths = frozenset(outcomes)
    tree = LeafTree(
        predicate=show(node),
        truths=_names(truths),
        reason="列间相关未知：不按单列范围推断 TRUE/FALSE 的存在与否，只保留可能真值",
        column=f"{left.kind}: {node.field} vs {right.kind}: {other}",
        provenance="cross-column",
        detail=None,
    )
    return Analysis(truths, tree)


def _bounded_compare_outcomes(op, lo, hi, constant):
    """给定非空值落在闭区间 [lo, hi]（None 表示该侧无界），判定真值可行性。

    这是存在性判断：只知道值落在区间内，因此逐个真值问“区间里是否存在
    一个值使它成立”。只要区间里找得到例子就保留该真值。区间本身没有
    相关性含义，只用于本列。
    """
    low_open = lo is None
    high_open = hi is None

    contains = (low_open or constant >= lo) and (high_open or constant <= hi)
    # 非空值区间是否可能取到 constant 以外的值。无界区间一定可能；
    # 有界区间只要不是单点，或单点不等于 constant，就可能。
    other_possible = low_open or high_open or lo != hi or lo != constant

    outcomes = set()
    if op == "eq":
        if contains:
            outcomes.add(TRUE)
        if other_possible:
            outcomes.add(FALSE)
    elif op == "ne":
        if other_possible:
            outcomes.add(TRUE)
        if contains:
            outcomes.add(FALSE)
    elif op == "lt":
        if low_open or lo < constant:
            outcomes.add(TRUE)
        if high_open or hi >= constant:
            outcomes.add(FALSE)
    elif op == "le":
        if low_open or lo <= constant:
            outcomes.add(TRUE)
        if high_open or hi > constant:
            outcomes.add(FALSE)
    elif op == "gt":
        if high_open or hi > constant:
            outcomes.add(TRUE)
        if low_open or lo <= constant:
            outcomes.add(FALSE)
    elif op == "ge":
        if high_open or hi >= constant:
            outcomes.add(TRUE)
        if low_open or lo < constant:
            outcomes.add(FALSE)
    else:
        raise AnalysisError(f"未知比较运算符 {op!r}")
    return frozenset(outcomes)


def _analyze_in(node, domains, limits):
    domain = _domain(domains, node.field)
    if not node.values:
        truth = frozenset([FALSE])
        return Analysis(truth, _leaf(node, domain, truth, "空 IN 是空析取，恒为 FALSE"))
    if all(value is None for value in node.values):
        truths = frozenset([UNKNOWN])
        return Analysis(truths, _leaf(node, domain, truths, "候选全为 NULL：IN 恒为 UNKNOWN"))
    if not domain.null_possible and not domain.non_null_possible:
        return Analysis(_empty_domain_truths(domain), _leaf(node, domain, (), "文件无行"))

    has_null_candidate = any(value is None for value in node.values)
    concrete = [value for value in node.values if value is not None]

    if not domain.non_null_possible:
        truths = _null_outcomes(domain) or frozenset([UNKNOWN])
        if not has_null_candidate:
            # 全 NULL 列 + 候选全非空：x = c 是 UNKNOWN，IN 也是 UNKNOWN。
            truths = frozenset([UNKNOWN])
        reason = "无可信非空值：IN 只可能得到 UNKNOWN"
        return Analysis(truths, _leaf(node, domain, truths, reason))

    representation = _non_null_representation(domain, limits, domain.kind)
    if representation[0] == "enum":
        outcomes = frozenset(in_set(value, node.values) for value in representation[1])
        reason = f"枚举可信精确字典的 {len(representation[1])} 个非空值逐个求值"
    else:
        hit, miss = _bounded_in_feasibility(
            concrete, representation, domain.kind
        )
        outcomes = set()
        if hit:
            outcomes.add(TRUE)
        if miss:
            outcomes.add(UNKNOWN if has_null_candidate else FALSE)
        if has_null_candidate and not hit:
            outcomes.add(UNKNOWN)
        if not outcomes:
            outcomes.add(FALSE)
        reason = "按可信边界判定命中/未命中的可能性；候选含 NULL 时未命中是 UNKNOWN"
    truths = frozenset(outcomes) | _null_outcomes(domain)
    return Analysis(truths, _leaf(node, domain, truths, reason, representation))


def _bounded_in_feasibility(concrete, representation, column_kind):
    """返回 (是否可能命中, 是否可能未命中)。

    精确字典过大退化成的 bounded 表示保留了集合的最小/最大值；此时
    “未命中”几乎总是可能（集合细节已丢失），因此按宽集合处理，宁可
    多读。只有明确的无界/单点情形才判不可能。
    """
    if representation[0] != "bounded":
        # 无任何边界信息：有限候选之外一定还有别的值（整数/字符串域无穷）。
        return (bool(concrete), True)
    _, lo, hi = representation
    low_open = lo is None
    high_open = hi is None
    hit = any(
        value_matches_kind(value, column_kind)
        and (low_open or value >= lo)
        and (high_open or value <= hi)
        for value in concrete
    )
    if not concrete:
        return (False, True)
    singleton = not low_open and not high_open and lo == hi
    if singleton:
        miss = not any(value == lo and value_matches_kind(value, column_kind) for value in concrete)
    else:
        miss = True
    return (hit, miss)


def _analyze_is_null(node, domains):
    domain = _domain(domains, node.field)
    outcomes = set()
    if domain.null_possible:
        outcomes.add(TRUE)
    if domain.non_null_possible:
        outcomes.add(FALSE)
    if not outcomes:
        return Analysis(outcomes, _leaf(node, domain, outcomes, "文件无行"))
    truths = frozenset(outcomes)
    return Analysis(
        truths,
        _leaf(node, domain, truths, "由可信 null_count 判定 NULL 是否可能出现"),
    )


def _analyze_not_null(node, domains):
    domain = _domain(domains, node.field)
    outcomes = set()
    if domain.null_possible:
        outcomes.add(FALSE)
    if domain.non_null_possible:
        outcomes.add(TRUE)
    if not outcomes:
        return Analysis(outcomes, _leaf(node, domain, outcomes, "文件无行"))
    truths = frozenset(outcomes)
    return Analysis(
        truths,
        _leaf(node, domain, truths, "由可信 null_count 判定非 NULL 是否可能出现"),
    )


# --- 推导树 -------------------------------------------------------------

@dataclass(frozen=True)
class LeafTree:
    predicate: str
    truths: tuple
    reason: str
    column: str
    provenance: str
    detail: object = None

    def to_dict(self):
        result = {
            "kind": "leaf",
            "predicate": self.predicate,
            "truths": list(self.truths),
            "reason": self.reason,
            "column": self.column,
            "domain": self.provenance,
        }
        if self.detail is not None:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class NotTree:
    label: str
    child: object
    truths: tuple
    rule: str

    def to_dict(self):
        return {
            "kind": "not",
            "label": self.label,
            "truths": list(self.truths),
            "rule": self.rule,
            "child": self.child.to_dict(),
        }


@dataclass(frozen=True)
class GroupTree:
    operator: str
    truths: tuple
    children: tuple
    rule: str
    steps: tuple

    def to_dict(self):
        return {
            "kind": "group",
            "operator": self.operator,
            "truths": list(self.truths),
            "rule": self.rule,
            "children": [child.to_dict() for child in self.children],
            "steps": [
                {
                    "branch_truths": list(step["branch_truths"]),
                    "accumulated": list(step["accumulated"]),
                }
                for step in self.steps
            ],
        }


def _leaf(node, domain, truths, reason, representation=None):
    detail = None
    if representation is not None:
        if representation[0] == "enum":
            detail = {"exact_values": list(representation[1])}
        elif representation[0] == "bounded":
            detail = {"min": representation[1], "max": representation[2]}
    return LeafTree(
        predicate=show(node),
        truths=_names(truths),
        reason=reason,
        column=node.field,
        provenance=domain.provenance,
        detail=detail,
    )


__all__ = [
    "AnalysisError",
    "Limits",
    "Analysis",
    "analyze",
    "analyze_truths",
    "LeafTree",
    "NotTree",
    "GroupTree",
]

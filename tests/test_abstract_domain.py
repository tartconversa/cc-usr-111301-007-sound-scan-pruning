"""小域穷举：抽象解释的保守性必须在不裁剪扫描上逐项成立。

对每个列域组合，构造真实行集合（包含 NULL、边界内/外的值），
要求：抽象解释给出的真值集合 ⊇ 所有真实行真值，且
“TRUE 不在集合中” 蕴含 “没有行取 TRUE”。
"""

import itertools
import unittest

from src.abstract_domain import analyze
from src.catalog import ColumnDomain
from src.predicate_ast import And, Column, Compare, InList, IsNull, Not, NotNull, Or, evaluate


INTEGER_COLUMNS = {"x": Column("x", "integer"), "y": Column("y", "integer")}


def int_domain(null_possible, non_null_possible, lo=None, hi=None, exact=None, provenance="test"):
    if exact is not None:
        exact = frozenset(exact)
    return ColumnDomain(
        "integer", null_possible, non_null_possible, lo, hi, exact, provenance
    )


def actual_truths_for_rows(node, rows):
    return {evaluate(node, row) for row in rows}


class ExhaustiveSmallDomainTests(unittest.TestCase):
    def assert_conservative(self, node, domains, rows):
        analysis = analyze(node, domains)
        actual = actual_truths_for_rows(node, rows)
        self.assertTrue(
            actual <= analysis.truths,
            f"抽象集合 {analysis.truth_names} 漏掉真实真值 "
            f"{sorted(str(v) for v in actual)}：谓词 {node}",
        )
        if True not in analysis.truths:
            self.assertNotIn(
                True, actual, f"跳过理由不成立：可能集合缺少 TRUE 却存在真实 TRUE"
            )

    def test_small_domain_exhaustive_all_truth_sets(self):
        """{NULL, 1, 2} 值域上穷举单列谓词，与真实行逐项比对。"""
        domain = int_domain(True, True, exact=[1, 2], provenance="exact")
        rows = [{"x": None}, {"x": 1}, {"x": 2}]
        predicates = [
            Compare("eq", "x", 1),
            Compare("ne", "x", 1),
            Compare("lt", "x", 1),
            Compare("le", "x", 1),
            Compare("gt", "x", 1),
            Compare("ge", "x", 1),
            InList("x", (1,)),
            InList("x", (1, 2)),
            InList("x", (0,)),
            InList("x", ()),
            InList("x", (1, None)),
            IsNull("x"),
            NotNull("x"),
            Not(Compare("eq", "x", 1)),
            Not(InList("x", (1,))),
            Not(IsNull("x")),
            Not(Not(Compare("eq", "x", 1))),
            And((Compare("ne", "x", 1), Compare("ne", "x", 2))),
            Or((Compare("eq", "x", 1), Compare("eq", "x", 2))),
            And((Not(Compare("eq", "x", 1)), IsNull("x"))),
            Or((Not(Compare("eq", "x", 1)), IsNull("x"))),
            Not(And((Compare("eq", "x", 1), Compare("eq", "x", 2)))),
        ]
        for node in predicates:
            with self.subTest(predicate=node):
                self.assert_conservative(node, {"x": domain}, rows)

    def test_not_eq_one_only_two_selects(self):
        analysis = analyze(Not(Compare("eq", "x", 1)), {"x": int_domain(True, True, exact=[1, 2])})
        self.assertEqual(analysis.truth_names, ("TRUE", "FALSE", "UNKNOWN"))
        # 值域 {NULL,1,2} 时 NOT(x=1) 对 NULL 是 UNKNOWN，故 TRUE 必须在集合中。
        self.assertIn(True, analysis.truths)

    def test_all_null_never_becomes_true_under_not(self):
        domain = int_domain(True, False, provenance="all-null")
        for predicate in (
            Not(Compare("eq", "x", 1)),
            Not(InList("x", (1,))),
            Not(Not(Compare("le", "x", 5))),
        ):
            analysis = analyze(predicate, {"x": domain})
            self.assertNotIn(True, analysis.truths, f"{predicate} 把 UNKNOWN 翻成了 TRUE")
            self.assertNotIn(False, analysis.truths)
        # IS NULL 在全空文件上是确定的 TRUE，NOT 后确定 FALSE——这不是把 UNKNOWN 翻转。
        self.assertEqual(
            analyze(Not(IsNull("x")), {"x": domain}).truth_names, ("FALSE",)
        )

    def test_minmax_boundary_gt_vs_ge(self):
        domain = int_domain(True, True, lo=0, hi=10, provenance="minmax")
        gt = analyze(Compare("gt", "x", 10), {"x": domain})
        ge = analyze(Compare("ge", "x", 10), {"x": domain})
        self.assertNotIn(True, gt.truths)
        self.assertIn(True, ge.truths)
        self.assertEqual(gt.truth_names, ("FALSE", "UNKNOWN"))
        self.assertEqual(ge.truth_names, ("TRUE", "FALSE", "UNKNOWN"))

    def test_cross_column_no_correlation(self):
        both = int_domain(True, True, lo=0, hi=10, provenance="minmax")
        domains = {"x": both, "y": both}
        for op in ("eq", "ne", "lt", "le", "gt", "ge"):
            node = Compare(op, "x", None, "y")
            analysis = analyze(node, domains)
            self.assertIn(True, analysis.truths, f"x {op} y 的 TRUE 被错误排除")
            self.assertIn(False, analysis.truths, f"x {op} y 的 FALSE 被错误排除")
        # 真实行验证：0..10 上 x=y 既有 TRUE 也有 FALSE。
        rows = [{"x": v, "y": v} for v in range(11)] + [{"x": 0, "y": 1}]
        self.assertIn(True, actual_truths_for_rows(Compare("eq", "x", None, "y"), rows))
        self.assertIn(False, actual_truths_for_rows(Compare("eq", "x", None, "y"), rows))

    def test_cross_column_nulls_produce_unknown(self):
        domains = {
            "x": int_domain(True, True, lo=0, hi=10),
            "y": int_domain(True, True, lo=0, hi=10),
        }
        analysis = analyze(Compare("eq", "x", None, "y"), domains)
        self.assertIn(None, analysis.truths)

    def test_string_bounds(self):
        domain = ColumnDomain("string", True, True, lo="a", hi="m", provenance="minmax")
        columns = {"s": domain}
        # (谓词, TRUE 是否可能)：min="a"、max="m"（字符串按码点序）。
        cases = [
            (Compare("lt", "s", "a"), False),
            (Compare("ge", "s", "a"), True),
            (Compare("gt", "s", "m"), False),
            (Compare("le", "s", "m"), True),
            (Compare("eq", "s", "z"), False),
            (Compare("eq", "s", "c"), True),
            (Compare("gt", "s", "c"), True),
            (Compare("ne", "s", "a"), True),
            (InList("s", ("c", "d")), True),
            (InList("s", ("z",)), False),
        ]
        for case in cases:
            node, expects_true = case[0], case[1]
            with self.subTest(predicate=node):
                analysis = analyze(node, columns)
                if expects_true:
                    self.assertIn(True, analysis.truths, f"{node} 应可能 TRUE")
                else:
                    self.assertNotIn(True, analysis.truths, f"{node} 应确定不可能 TRUE")
                rows = [{"s": "a"}, {"s": "c"}, {"s": "m"}, {"s": None}]
                self.assertTrue(actual_truths_for_rows(node, rows) <= analysis.truths)

    def test_string_compare_full_small_domain(self):
        domain = ColumnDomain("string", True, True, exact={"a", "b"}, provenance="exact")
        rows = [{"s": None}, {"s": "a"}, {"s": "b"}]
        predicates = [
            Compare("eq", "s", "a"),
            Compare("ne", "s", "a"),
            Compare("lt", "s", "b"),
            Compare("ge", "s", "b"),
            InList("s", ("b",)),
            InList("s", ()),
            Not(InList("s", ("a", "b"))),
            Not(Compare("eq", "s", "a")),
        ]
        for node in predicates:
            self.assert_conservative(node, {"s": domain}, rows)

    def test_exact_dict_tightens_not_loose(self):
        exact = int_domain(True, True, exact=[1, 2])
        minmax = int_domain(True, True, lo=1, hi=10)
        self.assertEqual(
            analyze(Compare("eq", "x", 3), {"x": exact}).truth_names,
            ("FALSE", "UNKNOWN"),
        )
        self.assertIn(True, analyze(Compare("eq", "x", 3), {"x": minmax}).truths)

    def test_empty_in_is_false_even_for_null(self):
        for domain in (
            int_domain(True, True, exact=[1, 2]),
            int_domain(True, False, provenance="all-null"),
            int_domain(True, True, lo=0, hi=10),
            int_domain(True, True),
        ):
            analysis = analyze(InList("x", ()), {"x": domain})
            self.assertEqual(analysis.truth_names, ("FALSE",))
        # NOT (x IN ()) 对全 NULL 也是 TRUE：空析取取反。
        analysis = analyze(Not(InList("x", ())), {"x": int_domain(True, False)})
        self.assertEqual(analysis.truth_names, ("TRUE",))

    def test_bad_trusted_flag_keeps_read_permissive(self):
        # 不可信/缺失统计在目录层会降级成顶域：无 null 计数、无边界。
        untrusted = int_domain(True, True, provenance="untrusted")
        analysis = analyze(Compare("gt", "x", 10), {"x": untrusted})
        self.assertIn(True, analysis.truths)
        self.assertEqual(analysis.truth_names, ("TRUE", "FALSE", "UNKNOWN"))

    def test_nested_not_and_or(self):
        domain = int_domain(True, True, exact=[1, 2])
        rows = [{"x": None}, {"x": 1}, {"x": 2}]
        predicates = [
            Not(Or((Compare("eq", "x", 1), Not(Compare("eq", "x", 2))))),
            And((Not(Compare("eq", "x", 1)), Not(Compare("eq", "x", 2)))),
            Not(Not(Not(Compare("eq", "x", 1)))),
        ]
        for node in predicates:
            self.assert_conservative(node, {"x": domain}, rows)


class ProductDomainTests(unittest.TestCase):
    def test_two_column_product_truth_set(self):
        """两列小域笛卡尔积：AND/OR/NOT 的集合组合对每行都保守。"""
        x = int_domain(True, True, exact=[1, 2])
        y = int_domain(True, True, lo=0, hi=10)
        domains = {"x": x, "y": y}
        rows = [
            {"x": None, "y": v} for v in (None, 0, 10)
        ] + [
            {"x": 1, "y": v} for v in (None, 0, 10)
        ] + [
            {"x": 2, "y": v} for v in (None, 0, 10)
        ]
        predicates = [
            And((Compare("eq", "x", 1), Compare("gt", "y", 5))),
            Or((Compare("eq", "x", 1), Compare("gt", "y", 5))),
            Not(And((Compare("eq", "x", 1), Compare("gt", "y", 5)))),
            And((Not(Compare("eq", "x", 1)), Not(Compare("gt", "y", 5)))),
            And((IsNull("x"), NotNull("y"))),
            And((Compare("lt", "x", 2), IsNull("y"))),
            Or((InList("x", ()), IsNull("y"))),
            Not(Or((IsNull("x"), IsNull("y")))),
            And((Compare("eq", "x", None, "y"), Compare("gt", "x", 0))),
            Or((Compare("eq", "x", None, "y"), Compare("eq", "x", 1))),
        ]
        for node in predicates:
            with self.subTest(predicate=node):
                analysis = analyze(node, domains)
                actual = actual_truths_for_rows(node, rows)
                self.assertTrue(actual <= analysis.truths, f"{node}: {analysis.truth_names} vs {actual}")


if __name__ == "__main__":
    unittest.main()

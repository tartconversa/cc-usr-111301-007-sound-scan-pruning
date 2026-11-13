"""小域枚举与随机穷举：抽象解释必须覆盖真实行的每一种真值。

除手工挑的边界情形外，这里在 {NULL, 1, 2} / {"a","b"} 这类有限域上
自动枚举谓词，并与全量求值逐项比对。为了让生成器覆盖面足够，枚举
策略是“运算符 × 常量 × 结构深度”，同时再跑一个带固定种子的随机生成器。
"""

import itertools
import random
import unittest

from src.abstract_domain import analyze
from src.catalog import ColumnDomain
from src.predicate_ast import (
    And,
    Column,
    Compare,
    Constant,
    InList,
    IsNull,
    Not,
    NotIn,
    NotNull,
    Or,
    evaluate,
)


def _int_domain(null, non_null, lo=None, hi=None, exact=None):
    return ColumnDomain(
        "integer", null, non_null, lo, hi,
        None if exact is None else frozenset(exact),
        "test",
    )


def _string_domain(null, non_null, lo=None, hi=None, exact=None):
    return ColumnDomain(
        "string", null, non_null, lo, hi,
        None if exact is None else frozenset(exact),
        "test",
    )


INT_DOMAINS = {
    "exact-1-2": _int_domain(True, True, exact=[1, 2]),
    "exact-no-null": _int_domain(False, True, exact=[1, 2]),
    "all-null": _int_domain(True, False),
    "minmax-0-10": _int_domain(True, True, lo=0, hi=10),
    "single-5": _int_domain(True, True, lo=5, hi=5),
}
INT_ROWS = {
    "exact-1-2": [{"x": None}, {"x": 1}, {"x": 2}],
    "exact-no-null": [{"x": 1}, {"x": 2}],
    "all-null": [{"x": None}],
    "minmax-0-10": [{"x": None}] + [{"x": v} for v in range(11)],
    "single-5": [{"x": None}, {"x": 5}],
}

STRING_DOMAINS = {
    "minmax-a-m": _string_domain(True, True, lo="a", hi="m"),
    "exact-empty-string": _string_domain(True, True, exact=[""]),
    "exact-a-b": _string_domain(True, True, exact=["a", "b"]),
    "all-null": _string_domain(True, False),
}
STRING_ROWS = {
    "minmax-a-m": [{"x": None}, {"x": "a"}, {"x": "b"}, {"x": "m"}],
    "exact-empty-string": [{"x": None}, {"x": ""}],
    "exact-a-b": [{"x": None}, {"x": "a"}, {"x": "b"}],
    "all-null": [{"x": None}],
}


def atom_generators(kind):
    constants = (None, 1, 2, 3, -1) if kind == "integer" else (None, "", "a", "b", "z")
    in_sets = [
        (),
        (constants[1],),
        (constants[2],),
        (None,),
        (constants[1], None),
        tuple(constants[1:]),
    ]
    for op in ("eq", "ne", "lt", "le", "gt", "ge"):
        for constant in constants:
            yield Compare(op, "x", constant)
    for values in in_sets:
        yield InList("x", values)
    yield IsNull("x")
    yield NotNull("x")
    yield NotIn("x", (1,))
    yield Constant(True)
    yield Constant(False)
    yield Constant(None)


def composed(atoms, depth):
    """有界深度的结构生成：and/or/not，最多两层组合。"""
    yield from atoms
    if depth <= 0:
        return
    subset = list(atoms)
    for first, second in itertools.combinations(subset[::3], 2):
        yield And((first, second))
        yield Or((first, second))
        yield Not(And((first, second)))
        yield Not(Or((first, second)))
    for atom in subset[::5]:
        yield Not(atom)


class EnumerativeTests(unittest.TestCase):
    def check_domain(self, predicate, domain, rows):
        analysis = analyze(predicate, {"x": domain})
        actual = {evaluate(predicate, row) for row in rows}
        self.assertTrue(
            actual <= analysis.truths,
            f"抽象集合 {analysis.truth_names} 漏掉真实真值：谓词 {predicate}，实际 {actual}",
        )
        if True not in analysis.truths:
            self.assertNotIn(True, actual, f"skip 理由不成立：谓词 {predicate}")

    def test_enumerate_integer_structures(self):
        atoms = list(atom_generators("integer"))
        for name, domain in INT_DOMAINS.items():
            rows = INT_ROWS[name]
            for predicate in composed(atoms, depth=1):
                self.check_domain(predicate, domain, rows)

    def test_enumerate_string_structures(self):
        atoms = list(atom_generators("string"))
        for name, domain in STRING_DOMAINS.items():
            rows = STRING_ROWS[name]
            for predicate in composed(atoms, depth=1):
                self.check_domain(predicate, domain, rows)

    def test_random_small_domains(self):
        """固定种子的随机生成器：两列混合域、含列间比较与嵌套 NOT。"""
        rng = random.Random(20261113)
        columns = {"x": "integer", "y": "integer", "s": "string"}

        def domain_for(kind):
            mode = rng.choice(["none", "minmax", "single", "exact", "all-null", "no-null"])
            null = mode != "no-null"
            non_null = mode != "all-null"
            lo = hi = exact = None
            if kind == "integer":
                if mode == "exact":
                    exact = rng.sample([0, 1, 2, 3], rng.randint(1, 4))
                elif mode in ("minmax", "single"):
                    picks = sorted(rng.sample([0, 1, 2, 3], 2 if mode == "minmax" else 1))
                    if mode == "single":
                        lo = hi = picks[0]
                    else:
                        lo, hi = picks
                return _int_domain(null, non_null, lo, hi, exact)
            if mode in ("minmax", "single"):
                picks = sorted(rng.sample(["a", "b", "m", "z"], 2 if mode == "minmax" else 1))
                if mode == "single":
                    lo = hi = picks[0]
                else:
                    lo, hi = picks
            return _string_domain(null, non_null, lo, hi, None)

        def make_predicate(subset, depth):
            options = ["cmp", "in", "null", "notnull", "const"]
            if depth > 0:
                options += ["not", "and", "or"]
            choice = rng.choice(options)
            if choice == "const":
                return Constant(rng.choice([True, False, None]))
            if choice == "null":
                return IsNull(rng.choice(list(subset)))
            if choice == "notnull":
                return NotNull(rng.choice(list(subset)))
            field = rng.choice(list(subset))
            kind = subset[field]
            if choice == "in":
                values = (1, 2, None) if kind == "integer" else ("a", "z", None)
                return InList(field, tuple(rng.choice(values) for _ in range(rng.randint(0, 3))))
            if choice == "cmp":
                same_kind = [name for name in subset if name != field and subset[name] == kind]
                if same_kind and rng.random() < 0.2:
                    return Compare(
                        rng.choice(["eq", "ne", "lt", "le", "gt", "ge"]),
                        field,
                        None,
                        rng.choice(same_kind),
                    )
                constant = rng.choice([1, 2, 3] if kind == "integer" else ["a", "b", "z"])
                return Compare(
                    rng.choice(["eq", "ne", "lt", "le", "gt", "ge"]), field, constant
                )
            if choice == "not":
                return Not(make_predicate(subset, depth - 1))
            parts = tuple(make_predicate(subset, depth - 1) for _ in range(rng.randint(2, 3)))
            return And(parts) if choice == "and" else Or(parts)

        def rows_for(domains):
            rows = []
            for _ in range(6):
                row = {}
                for name, domain in domains.items():
                    if not domain.non_null_possible and not domain.null_possible:
                        row[name] = None
                        continue
                    values = []
                    if domain.non_null_possible:
                        if domain.exact:
                            values.extend(domain.exact)
                        elif domain.kind == "integer":
                            values.extend(
                                v for v in range(4)
                                if (domain.lo is None or v >= domain.lo)
                                and (domain.hi is None or v <= domain.hi)
                            )
                        else:
                            values.extend(
                                v for v in ("a", "b", "m", "z")
                                if (domain.lo is None or v >= domain.lo)
                                and (domain.hi is None or v <= domain.hi)
                            )
                    if domain.null_possible:
                        values.append(None)
                    if not values:
                        return []
                    row[name] = rng.choice(values)
                rows.append(row)
            return rows

        for trial in range(600):
            subset = rng.sample(list(columns), rng.randint(1, 3))
            subset = {name: columns[name] for name in subset}
            predicate = make_predicate(subset, rng.randint(0, 3))
            domains = {name: domain_for(kind) for name, kind in subset.items()}
            rows = rows_for(domains)
            analysis = analyze(predicate, domains)
            actual = {evaluate(predicate, row) for row in rows}
            self.assertTrue(
                actual <= analysis.truths,
                f"trial {trial}: {analysis.truth_names} 漏掉 {actual}（谓词 {predicate}）",
            )
            if True not in analysis.truths:
                self.assertNotIn(True, actual, f"trial {trial}: skip 理由不成立")


if __name__ == "__main__":
    unittest.main()

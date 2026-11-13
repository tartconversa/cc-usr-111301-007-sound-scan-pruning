"""目录（catalog.json）的严格加载与单文件抽象域构造。

目录是裁剪计划的唯一事实来源；本模块只读目录，不读任何 JSONL 数据。
统计块的 ``trusted`` 标记是整个块的开关：``false`` 或缺失时整块不可用，
对应文件必须可读。统计彼此矛盾（如 null_count 大于 row_count、min 大于
max）时同样整块作废——宁可多读，不拿坏统计做证明。

目录里的 Bloom 元数据本版一律不参与裁剪：普通 Bloom 只有在哈希协议
经验证且构建完整时才可信，而输入没有给出这样的协议。检测到此类字段
只记录诊断信息。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .three_valued import SUPPORTED_TYPES, value_matches_kind

STAT_KEYS = frozenset(
    {"trusted", "null_count", "min", "max", "exact_values", "values", "distinct_count"}
)
EXACT_KEYS = ("exact_values", "values")


class CatalogError(ValueError):
    """目录结构或类型不合契约。"""


@dataclass(frozen=True)
class Column:
    name: str
    field_id: int
    kind: str


@dataclass(frozen=True)
class ColumnDomain:
    """单列可能取值的保守上界（用于抽象解释）。

    - ``null_possible`` / ``non_null_possible`` 说 NULL 与非 NULL 是否可能；
    - ``lo``/``hi`` 是可信 min/max，仅约束非 NULL 值，``None`` 表示该侧无界；
    - ``exact`` 是可信精确字典（非 NULL 值集合），优先于 min/max；
    - ``provenance`` 记录域来自哪种统计，便于推导树解释。
    """

    kind: str
    null_possible: bool
    non_null_possible: bool
    lo: object = None
    hi: object = None
    exact: frozenset | None = None
    provenance: str = "none"
    notes: tuple = ()


@dataclass(frozen=True)
class StatInfo:
    """已解析的可信统计块，保留原始声明值供数据一致性校验。"""

    column: str
    declared_trusted: bool
    usable: bool
    null_count: int | None = None
    lo: object = None
    hi: object = None
    exact: frozenset | None = None
    exact_had_null: bool = False
    notes: tuple = ()


@dataclass(frozen=True)
class FileEntry:
    file_id: str
    rel_path: str
    path: Path
    row_count: int
    domains: dict
    stats: dict
    notes: tuple = ()
    ignored_bloom_keys: tuple = ()


@dataclass
class Catalog:
    source: Path
    root: Path
    columns: dict
    files: list
    notes: tuple = ()

    @property
    def by_id(self):
        return {entry.file_id: entry for entry in self.files}


def load_catalog(path):
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CatalogError(f"目录不存在: {source}") from None
    except json.JSONDecodeError as error:
        raise CatalogError(f"目录不是合法 JSON: {source}: {error}") from None
    if not isinstance(raw, dict):
        raise CatalogError("目录顶层必须是 JSON 对象")
    root = source.parent

    columns = _parse_columns(raw.get("columns"))
    if "files" not in raw:
        raise CatalogError("目录缺少 files 数组")
    raw_files = raw["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise CatalogError("files 必须是非空数组")

    notes = []
    catalog_bloom = sorted(
        key for key in raw if isinstance(key, str) and "bloom" in key.lower()
    )
    if catalog_bloom:
        notes.append(
            "目录级 Bloom 元数据已被忽略（未验证哈希协议，本版不使用 Bloom）: "
            + ", ".join(catalog_bloom)
        )

    seen_ids = set()
    files = []
    for index, raw_file in enumerate(raw_files):
        entry = _parse_file(index, raw_file, columns, root)
        if entry.file_id in seen_ids:
            raise CatalogError(f"file_id 重复: {entry.file_id!r}")
        seen_ids.add(entry.file_id)
        files.append(entry)
    return Catalog(source=source, root=root, columns=columns, files=files, notes=tuple(notes))


def _parse_columns(raw):
    if not isinstance(raw, dict) or not raw:
        raise CatalogError("columns 必须是非空对象")
    columns = {}
    seen_ids = set()
    for name, spec in raw.items():
        if not isinstance(name, str) or not name:
            raise CatalogError(f"列名必须是非空字符串: {name!r}")
        if not isinstance(spec, dict):
            raise CatalogError(f"列 {name!r} 的登记必须是对象")
        kind = spec.get("type")
        if kind not in SUPPORTED_TYPES:
            raise CatalogError(
                f"列 {name!r} 的类型 {kind!r} 不受支持；仅支持 {sorted(SUPPORTED_TYPES)}"
            )
        field_id = spec.get("field_id")
        if not isinstance(field_id, int) or isinstance(field_id, bool):
            raise CatalogError(f"列 {name!r} 的 field_id 必须是整数")
        if field_id in seen_ids:
            raise CatalogError(f"field_id 重复: {field_id}")
        seen_ids.add(field_id)
        columns[name] = Column(name=name, field_id=field_id, kind=kind)
    return columns


def _parse_file(index, raw_file, columns, root):
    where = f"files[{index}]"
    if not isinstance(raw_file, dict):
        raise CatalogError(f"{where}: 必须是对象")
    file_id = raw_file.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise CatalogError(f"{where}: file_id 必须是非空字符串")
    rel = raw_file.get("path")
    if not isinstance(rel, str) or not rel:
        raise CatalogError(f"{where}: path 必须是非空字符串")
    row_count = raw_file.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
        raise CatalogError(f"{where}: row_count 必须是非负整数")
    path = (root / rel).resolve()

    ignored_bloom = sorted(
        key for key in raw_file if isinstance(key, str) and "bloom" in key.lower()
    )
    notes = []
    if ignored_bloom:
        notes.append(
            "已忽略 Bloom 元数据（未验证哈希协议）: " + ", ".join(ignored_bloom)
        )

    raw_stats = raw_file.get("stats", {})
    if raw_stats is None:
        raw_stats = {}
    if not isinstance(raw_stats, dict):
        raise CatalogError(f"{where}.stats 必须是对象")
    domains = {}
    stat_infos = {}
    for name, column in columns.items():
        raw_stat = raw_stats.get(name)
        if raw_stat is None:
            domains[name] = _top_domain(column.kind, row_count)
            stat_infos[name] = StatInfo(name, declared_trusted=False, usable=False)
            continue
        domain, info, stat_notes = _build_column_domain(column, raw_stat, row_count, where)
        domains[name] = domain
        stat_infos[name] = info
        notes.extend(stat_notes)

    unknown_stats = sorted(set(raw_stats) - set(columns))
    if unknown_stats:
        notes.append("目录含未登记列的统计，已忽略: " + ", ".join(unknown_stats))

    return FileEntry(
        file_id=file_id,
        rel_path=rel,
        path=path,
        row_count=row_count,
        domains=domains,
        stats=stat_infos,
        notes=tuple(notes),
        ignored_bloom_keys=tuple(ignored_bloom),
    )


def _top_domain(kind, row_count, provenance="none", notes=()):
    if row_count == 0:
        return ColumnDomain(kind, null_possible=False, non_null_possible=False,
                            provenance="empty", notes=tuple(notes))
    return ColumnDomain(kind, null_possible=True, non_null_possible=True,
                        provenance=provenance, notes=tuple(notes))


def _build_column_domain(column, raw_stat, row_count, where):
    """返回 (域, 统计信息, 诊断)。任何不可信/矛盾统计都降级为 top 域。"""
    context = f"{where}.stats[{column.name!r}]"
    if not isinstance(raw_stat, dict):
        return (
            _top_domain(column.kind, row_count, "invalid", (f"{context}: 统计必须是对象，已作废",)),
            StatInfo(column.name, False, False, notes=(f"{context}: 统计必须是对象",)),
            (f"{context}: 统计必须是对象，已作废并保留文件可读",),
        )

    notes = []
    declared_trusted = raw_stat.get("trusted")
    if not isinstance(declared_trusted, bool):
        notes.append(f"{context}: trusted 缺失或非布尔，按不可信处理")
        return (
            _top_domain(column.kind, row_count, "untrusted", tuple(notes)),
            StatInfo(column.name, False, False, notes=tuple(notes)),
            (f"{context}: 无可信标记，整块统计不可用，文件保留可读",),
        )
    if not declared_trusted:
        notes.append(f"{context}: trusted=false，整块统计不可用于裁剪")
        return (
            _top_domain(column.kind, row_count, "untrusted", tuple(notes)),
            StatInfo(column.name, False, False, notes=tuple(notes)),
            (f"{context}: 统计标记不可信，文件保留可读",),
        )

    for key in sorted(set(raw_stat) - STAT_KEYS):
        if "bloom" in key.lower():
            notes.append(f"{context}: Bloom 字段 {key!r} 已忽略")
        else:
            notes.append(f"{context}: 未知统计字段 {key!r} 已忽略")

    problems = []
    null_count = raw_stat.get("null_count", None)
    if "null_count" in raw_stat:
        if not isinstance(null_count, int) or isinstance(null_count, bool) or null_count < 0:
            problems.append("null_count 必须是非负整数")
        elif null_count > row_count:
            problems.append(f"null_count={null_count} 大于 row_count={row_count}")

    lo = raw_stat.get("min", None)
    hi = raw_stat.get("max", None)
    if "min" in raw_stat and lo is not None and not value_matches_kind(lo, column.kind):
        problems.append(f"min={lo!r} 与列类型 {column.kind} 不符")
    if "max" in raw_stat and hi is not None and not value_matches_kind(hi, column.kind):
        problems.append(f"max={hi!r} 与列类型 {column.kind} 不符")
    if lo is not None and hi is not None and "min" in raw_stat and "max" in raw_stat:
        if value_matches_kind(lo, column.kind) and value_matches_kind(hi, column.kind):
            if lo > hi:
                problems.append(f"min={lo!r} 大于 max={hi!r}")

    exact_raw = None
    exact_key = None
    present_exact_keys = [key for key in EXACT_KEYS if key in raw_stat]
    if len(present_exact_keys) > 1:
        problems.append(
            "同时声明了多个精确字典字段: " + ", ".join(present_exact_keys)
        )
    if present_exact_keys:
        exact_key = present_exact_keys[0]
        exact_raw = raw_stat[exact_key]
    exact_had_null = False
    exact = None
    if exact_key is not None:
        if not isinstance(exact_raw, list):
            problems.append(f"{exact_key} 必须是数组")
        else:
            values = []
            for value in exact_raw:
                if value is None:
                    exact_had_null = True
                elif value_matches_kind(value, column.kind):
                    values.append(value)
                else:
                    problems.append(f"{exact_key} 含类型不符的值 {value!r}")
            exact = frozenset(values)

    if problems:
        joined = "；".join(problems)
        notes.append(f"{context}: 统计矛盾或类型不符（{joined}），整块作废，保留文件可读")
        info = StatInfo(
            column.name, True, False,
            null_count=null_count if isinstance(null_count, int) and not isinstance(null_count, bool) else None,
            lo=lo, hi=hi, exact=exact, exact_had_null=exact_had_null, notes=tuple(notes),
        )
        return (
            _top_domain(column.kind, row_count, "invalid", tuple(notes)),
            info,
            (f"{context}: 统计矛盾或类型不符，已作废并保留文件可读",),
        )

    null_possible = True
    if "null_count" in raw_stat:
        null_possible = null_count > 0
    elif exact_had_null:
        null_possible = True

    if exact is not None:
        non_null_possible = bool(exact)
    else:
        if "null_count" in raw_stat:
            non_null_possible = (row_count - null_count) > 0
        else:
            non_null_possible = row_count > 0

    # 精确字典与 min/max/null_count 的交叉一致性。精确字典被解释为
    # 该列全部非 NULL 值的集合，因此必须与其它可信统计吻合。
    if exact is not None:
        if null_count is not None and len(exact) > row_count - null_count:
            problems.append(
                f"精确字典含 {len(exact)} 个值，超过非空行数 {row_count - null_count}"
            )
        if exact_had_null and "null_count" in raw_stat and null_count == 0:
            problems.append("精确字典含 NULL 但 null_count=0")
        if exact and "min" in raw_stat and lo is not None and value_matches_kind(lo, column.kind):
            if lo != min(exact):
                problems.append(f"min={lo!r} 与精确字典最小值 {min(exact)!r} 不一致")
        if exact and "max" in raw_stat and hi is not None and value_matches_kind(hi, column.kind):
            if hi != max(exact):
                problems.append(f"max={hi!r} 与精确字典最大值 {max(exact)!r} 不一致")
    if not non_null_possible and (("min" in raw_stat and lo is not None) or ("max" in raw_stat and hi is not None) or exact):
        problems.append("统计声称没有非 NULL 值，却给出了 min/max 或非空精确字典")
    if null_count is not None and row_count == 0 and null_count != 0:
        problems.append("空文件的 null_count 不为 0")

    distinct_count = raw_stat.get("distinct_count", None)
    if "distinct_count" in raw_stat:
        if (
            not isinstance(distinct_count, int)
            or isinstance(distinct_count, bool)
            or distinct_count < 0
        ):
            problems.append("distinct_count 必须是非负整数")
        else:
            non_null_rows = row_count - null_count if null_count is not None else row_count
            if distinct_count > non_null_rows:
                problems.append(
                    f"distinct_count={distinct_count} 超过非空行数上限 {non_null_rows}"
                )
            if exact is not None and distinct_count != len(exact):
                problems.append(
                    f"distinct_count={distinct_count} 与精确字典大小 {len(exact)} 不一致"
                )

    if problems:
        joined = "；".join(problems)
        notes.append(f"{context}: 统计交叉不一致（{joined}），整块作废，保留文件可读")
        info = StatInfo(
            column.name, True, False, null_count=null_count, lo=lo, hi=hi,
            exact=exact, exact_had_null=exact_had_null, notes=tuple(notes),
        )
        return (
            _top_domain(column.kind, row_count, "invalid", tuple(notes)),
            info,
            (f"{context}: 统计交叉不一致，已作废并保留文件可读",),
        )

    if not null_possible and not non_null_possible:
        domain = ColumnDomain(column.kind, False, False, provenance="empty", notes=tuple(notes))
    elif not non_null_possible:
        domain = ColumnDomain(
            column.kind, null_possible=True, non_null_possible=False,
            exact=frozenset(exact) if exact is not None else None,
            provenance="all-null", notes=tuple(notes),
        )
    elif exact is not None:
        domain = ColumnDomain(
            column.kind, null_possible, True, exact=frozenset(exact),
            provenance="exact", notes=tuple(notes),
        )
    elif "min" in raw_stat or "max" in raw_stat:
        domain = ColumnDomain(
            column.kind, null_possible, True,
            lo=lo if "min" in raw_stat else None,
            hi=hi if "max" in raw_stat else None,
            provenance="minmax", notes=tuple(notes),
        )
    else:
        domain = ColumnDomain(
            column.kind, null_possible, True,
            provenance="trusted-counts", notes=tuple(notes),
        )

    info = StatInfo(
        column.name, True, True, null_count=null_count, lo=lo, hi=hi,
        exact=frozenset(exact) if exact is not None else None,
        exact_had_null=exact_had_null, notes=tuple(notes),
    )
    return domain, info, ()


__all__ = [
    "CatalogError",
    "Column",
    "ColumnDomain",
    "StatInfo",
    "FileEntry",
    "Catalog",
    "load_catalog",
]

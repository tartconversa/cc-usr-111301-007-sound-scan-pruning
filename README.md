# 空值与边界下的湖文件保守裁剪器

本项目保存虚构算力设施的仓库源表、湖文件与只读裁剪规划器。所有设备、组织和金额均为合成；仓库不执行事实加工、目录提交、汇总或联接。

裁剪器要解决的是“统计缺失或不可信时，文件被 min/max 筛掉、查询少行却不报错”。它的唯一承诺是：

> 被跳过的文件**不可能**含满足 WHERE 的行；允许多读，不允许漏读。

实现是 SQL 三值逻辑（TRUE / FALSE / UNKNOWN）上的保守抽象解释：对每个文件推导谓词**可能取到的真值集合**，仅当 TRUE 不在集合中时跳过。WHERE 只取 TRUE，UNKNOWN 既不入选，也不许被当成 FALSE 再取反。

## 输入

- `fixtures/catalog.json`：列登记（`field_id`、`type`）与每文件 `row_count`、可选 `null_count`、可选 `min`/`max`、`trusted` 标记、可选精确字典（`exact_values` 或 `values`）。
- `fixtures/data/*.jsonl`：合成 JSONL 数据，仅支持**明确类型**的整数与 UTF-8 字符串；JSON `null` 是独立的 SQL NULL。
- `fixtures/predicates.json`：谓词 AST，只用 `eq`/`ne`/`lt`/`le`/`gt`/`ge`、`in`、`is_null`、`is_not_null`、`not`、`and`、`or`。不执行任意 Python 表达式。

统计契约：

| 情形 | 处理 |
| --- | --- |
| `trusted: false` 或缺失 `trusted` | 整块统计不可用，文件一律保留可读 |
| 统计自相矛盾（`null_count > row_count`、`min > max`、精确字典与 min/max/null_count 不一致等） | 整块作废并写诊断，保留可读 |
| 未登记统计的列 | 视为统计缺失 |
| Bloom 元数据 | 本版不使用：普通的 Bloom 未命中只有在已验证哈希协议、完整构建时才可裁剪；目录没有给出协议，故忽略并记录诊断 |

`min`/`max` 只约束非 NULL 值；全空文件的比较结果是 UNKNOWN，不会被 `NOT` 翻成 TRUE。空文件（`row_count=0`，无行）可以跳过：没有行就不可能有入选行，这与真值集合无关。

## 运行

Python 3.11 以上，标准库即可，无第三方依赖。

    # 人类可读计划 + 扫描后逐行校验（默认加载 fixtures/predicates.json）
    python -m src.pruning_reader fixtures/catalog.json --all-queries

    # 只输出计划，不扫描
    python -m src.pruning_reader fixtures/catalog.json --no-verify

    # JSON 报告：planner、scan、baseline、verification 全都在内
    python -m src.pruning_reader fixtures/catalog.json --json --all-queries

    python -m compileall -q src tests
    python -m unittest discover -s tests -v

退出码：`0` 校验通过；`1` 数据错误或差分校验失败；`2` 目录/谓词不合契约。

`--verify`（默认开启）会把裁剪结果与**不裁剪的全量扫描**逐项比对：被跳过文件的实际入选行数、裁剪入选行与全量入选行的行数及行标识（`id` 字段，缺省用行内容）。任何差异都会使报告 `verification.ok=false`。

## 模块

| 文件 | 职责 |
| --- | --- |
| `src/three_valued.py` | SQL 三值逻辑常量、比较、`IN`、`AND`/`OR`/`NOT` 的谨慎求值 |
| `src/predicate_ast.py` | AST 校验、语义保持规范化、具体逐行求值、渲染 |
| `src/catalog.py` | 目录严格加载；把可信统计解析为单列抽象域（含一致性交叉校验） |
| `src/abstract_domain.py` | 统计域上的保守抽象解释 + 可序列化推导树 |
| `src/planner.py` | 每文件 read/skip 裁决与理由 |
| `src/scanner.py` | 严格 JSONL 读取、目录行数/类型校验、逐行求值 |
| `src/pruning_reader.py` | CLI：加载目录、规划、扫描、差分校验、报告 |

## 设计取舍

**精确字典 vs 边界。** 可信且规模不大（默认 ≤256 个值，`--enumeration-limit` 可调）的精确字典逐值枚举：精度最高，能证明“值 3 不在字典里”这类结论。超过阈值退化为 `min`/`max` 边界：推导树更小，但可能多留一个真值。两种表示都只用在本列，绝不跨列。

**列间相关性未知。** `x` 与 `y` 各自 `0..10` 推不出 `x = y` 的任何一侧结论；`x = y` 在抽象解释里同时保留 TRUE、FALSE（两侧都可能有 NULL 时还有 UNKNOWN）。列间比较只允许同类型字段，且任何一侧可能为 NULL 就会产生 UNKNOWN。

**AND/OR 的集合组合。** 用谨慎三值运算逐枝组合：`AND` 只要有一枝 FALSE 就是 FALSE，否则有 UNKNOWN 就是 UNKNOWN；`OR` 对称。不会做集合取补，因此 `NOT (x = 1)` 对全 NULL 文件仍是 UNKNOWN 而非 TRUE。

**规范化的界限。** `NOT` 对 `AND`/`OR` 的 De Morgan 展开有节点预算（`NORMALIZE_TREE_BUDGET`），超过时保留 `Not` 节点，抽象解释直接处理 `Not`——推导树不会因嵌套查询爆炸，语义不变。空 `IN` 是 FALSE（空析取），所以 `NOT (x IN ())` 恒为 TRUE，即使全列是 NULL；候选全为 NULL 的 `IN` 是 UNKNOWN。

**扫描成本。** 抽象解释对一个文件只遍历谓词树并在小集合上组合；精确字典展开最多受 `--enumeration-limit` 约束。真正读取只发生在 read 文件上，读取时逐行校验目录声明的类型与行数（坏行、类型不符、行数不符直接报错），不会因为“跳过”吞掉数据问题。

## 测试覆盖

- 小域穷举：`{NULL,1,2}`、字符串 `{"a","b"}` 等值域上枚举比较、`IN`、`IS NULL`、嵌套 `NOT`/`AND`/`OR`，要求抽象真值集合覆盖每一真实行。
- 固定种子随机生成器：多列混合域、列间比较、大/退化精确字典。
- 目录契约：不可信/缺失/矛盾统计、`null_count` 越界、精确字典与 min/max 不一致、Bloom 忽略、空文件、缺文件、类型不符。
- 端到端差分：混合文件矩阵上裁剪扫描与全量扫描逐行一致；字符串边界；空 `IN`；全空文件；边界 `x>10` 跳、`x>=10` 读。
- 冻结台架：`fixtures/` 上的固定期望。

## 固定容器

    docker build -t sound-scan-pruning:local .
    docker run --rm sound-scan-pruning:local

镜像以完整 digest 固定；默认命令运行输入契约测试。新增分析功能全部使用标准库，未添加依赖。

`material-index.json` 记录全部输入内容哈希；`fixtures/` 为原始资料，不要覆盖，计算输出可放 `.workspace-output/`。

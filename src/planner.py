"""裁剪计划：为目录中每个文件决定 read / skip，并给出可审计的推导。

决策规则只有一条：

    当且仅当抽象解释得到的可能真值集合**不含 TRUE**时跳过。

其余情况（含统计缺失、不可信、矛盾、普通 Bloom）一律 read。空文件
（无行）可以被跳过，因为“没有行”本身就证明了没有满足条件的行。
"""

from dataclasses import dataclass, field

from .abstract_domain import Analysis, Limits, analyze, unused_domain_notes
from .catalog import Catalog, FileEntry
from .predicate_ast import referenced_fields, show
from .three_valued import TRUE


@dataclass(frozen=True)
class FilePlan:
    file_id: str
    rel_path: str
    decision: str  # "read" | "skip"
    reason: str
    analysis: Analysis
    row_count: int
    notes: tuple = ()

    def to_dict(self):
        result = {
            "file_id": self.file_id,
            "path": self.rel_path,
            "row_count": self.row_count,
            "decision": self.decision,
            "reason": self.reason,
            "possible_truths": list(self.analysis.truth_names),
            "derivation": self.analysis.tree.to_dict(),
        }
        if self.notes:
            result["notes"] = list(self.notes)
        return result


@dataclass
class Plan:
    predicate_text: str
    files: list
    limits: Limits = field(default_factory=Limits)

    @property
    def read_files(self):
        return [item for item in self.files if item.decision == "read"]

    @property
    def skip_files(self):
        return [item for item in self.files if item.decision == "skip"]

    def to_dict(self):
        return {
            "predicate": self.predicate_text,
            "summary": {
                "files": len(self.files),
                "read": len(self.read_files),
                "skip": len(self.skip_files),
                "rows_read_estimate": sum(item.row_count for item in self.read_files),
                "rows_skipped_estimate": sum(item.row_count for item in self.skip_files),
            },
            "files": [item.to_dict() for item in self.files],
            "notes": [
                "统计缺失、不可信或矛盾时保留 read；只有证明不含 TRUE 才 skip。",
                "空文件（0 行）可以跳过：没有行即没有满足条件的行。",
                "Bloom 元数据本版不使用：需已验证哈希协议与完整构建才可据此裁剪。",
            ],
        }


def plan_scan(catalog, node, limits=Limits()):
    plans = []
    used = referenced_fields(node)
    for file_entry in catalog.files:
        analysis = analyze(node, file_entry.domains, limits)
        notes = list(file_entry.notes)
        for name, note in unused_domain_notes(node, file_entry.domains).items():
            if file_entry.stats.get(name) is not None and file_entry.stats[name].usable:
                # 只在确实有可用统计可解释时才写，避免噪声。
                notes.append(f"{name}: {note}")
        decision, reason = _decide(file_entry, analysis, used)
        plans.append(
            FilePlan(
                file_id=file_entry.file_id,
                rel_path=file_entry.rel_path,
                decision=decision,
                reason=reason,
                analysis=analysis,
                row_count=file_entry.row_count,
                notes=tuple(notes),
            )
        )
    return Plan(predicate_text=show(node), files=plans, limits=limits)


def _decide(file_entry, analysis, used_fields=frozenset()):
    if file_entry.row_count == 0:
        return (
            "skip",
            "目录 row_count=0：空文件没有行，不可能含满足条件的行",
        )
    truths = analysis.truth_names
    if TRUE not in analysis.truths:
        return (
            "skip",
            "可能真值集合不含 TRUE（" + "/".join(truths) + "）：文件不可能含满足条件的行",
        )
    unused = sorted(
        name for name, info in file_entry.stats.items()
        if name not in used_fields and info.usable
    )
    unusable = sorted(
        name for name, info in file_entry.stats.items()
        if name in used_fields and not info.usable
    )
    sharing_notes = []
    if unused:
        sharing_notes.append(
            "未被谓词引用的列不计入裁决: " + ", ".join(unused)
        )
    if unusable:
        by_reason = []
        for name in unusable:
            info = file_entry.stats[name]
            if info.declared_trusted:
                tag = "统计矛盾/类型不符"
            else:
                tag = "统计缺失或不可信"
            by_reason.append(f"{name}({tag})")
        sharing_notes.append("相关列统计不可用，必须读取: " + ", ".join(by_reason))
    if not sharing_notes:
        sharing_notes.append("可能真值集合含 TRUE，可信统计无法排除入选行")
    return "read", "；".join(sharing_notes)


__all__ = ["FilePlan", "Plan", "plan_scan"]

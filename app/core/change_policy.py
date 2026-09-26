"""冻结前变化策略：按差异规模、异常类型与敏感字段决定签署链。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable


class Role(StrEnum):
    """审批签署角色，按权限从低到高排列。"""

    OFFICER = "officer"  # 教务员
    DIRECTOR = "director"  # 系主任
    DEAN = "dean"  # 院长


ROLE_ORDER: tuple[Role, ...] = (Role.OFFICER, Role.DIRECTOR, Role.DEAN)
ROLE_RANK: dict[Role, int] = {role: rank for rank, role in enumerate(ROLE_ORDER)}


@dataclass(frozen=True)
class ChangePolicy:
    """变化阈值策略。

    受影响学生数或总学时波动达到阈值（含等于）时升级到系主任；
    出现敏感字段变化或异常类型时升级到院长。
    """

    student_count_threshold: int = 5
    total_seconds_threshold: int = 100 * 3600
    sensitive_fields: tuple[str, ...] = ("meets_requirement", "adjustment_seconds")


DEFAULT_POLICY = ChangePolicy()


@dataclass(frozen=True)
class PolicyEvaluation:
    """一次差异的策略评估结果。"""

    required_roles: tuple[Role, ...]
    triggered_rules: tuple[dict[str, Any], ...]
    anomalies: tuple[str, ...]
    sensitive_changes: tuple[str, ...]
    students_affected: int
    students_added: int
    students_removed: int
    total_seconds_delta: int

    def summary_dict(self) -> dict[str, Any]:
        return {
            "students_affected": self.students_affected,
            "students_added": self.students_added,
            "students_removed": self.students_removed,
            "total_seconds_delta": self.total_seconds_delta,
            "anomalies": list(self.anomalies),
            "sensitive_changes": list(self.sensitive_changes),
            "triggered_rules": [dict(rule) for rule in self.triggered_rules],
        }


def evaluate_policy(
    diff: dict[str, Any],
    candidate_students: Iterable[dict[str, Any]],
    policy: ChangePolicy = DEFAULT_POLICY,
) -> PolicyEvaluation:
    """对候选快照与上一次冻结的差异执行确定性策略评估。"""
    changes = diff.get("student_changes", [])
    added = sum(1 for c in changes if c["change_type"] == "added")
    removed = sum(1 for c in changes if c["change_type"] == "removed")
    affected = len(changes)

    total_delta = 0
    sensitive_touched: set[str] = set()
    sensitive_set = set(policy.sensitive_fields)
    for change in changes:
        change_type = change["change_type"]
        if change_type == "added":
            total_delta += int(change["after"]["total_seconds"])
        elif change_type == "removed":
            total_delta -= int(change["before"]["total_seconds"])
        else:
            fields = change.get("fields", {})
            if "total_seconds" in fields:
                total_delta += int(fields["total_seconds"]["after"]) - int(
                    fields["total_seconds"]["before"]
                )
            sensitive_touched |= sensitive_set & set(fields)

    anomalies: list[str] = []
    if removed:
        anomalies.append("students_removed")
    students = list(candidate_students)
    if any(not s.get("meets_requirement", False) for s in students):
        anomalies.append("requirement_not_met")
    if any(int(s.get("pending_seconds", 0)) > 0 for s in students):
        anomalies.append("pending_confirmation")
    if any(int(s.get("adjustment_seconds", 0)) < 0 for s in students):
        anomalies.append("negative_adjustment")

    triggered: list[dict[str, Any]] = []
    top_rank = -1

    def _fire(rule: str, role: Role, detail: str) -> None:
        nonlocal top_rank
        triggered.append({"rule": rule, "role": role.value, "detail": detail})
        top_rank = max(top_rank, ROLE_RANK[role])

    if affected:
        _fire("any_change", Role.OFFICER, f"{affected} student(s) changed")
    if affected and affected >= policy.student_count_threshold:
        _fire(
            "student_volume",
            Role.DIRECTOR,
            f"{affected} students >= threshold {policy.student_count_threshold}",
        )
    if total_delta and abs(total_delta) >= policy.total_seconds_threshold:
        _fire(
            "total_hours_volume",
            Role.DIRECTOR,
            f"|{total_delta}|s >= threshold {policy.total_seconds_threshold}s",
        )
    if sensitive_touched:
        _fire(
            "sensitive_fields",
            Role.DEAN,
            "sensitive fields changed: " + ",".join(sorted(sensitive_touched)),
        )
    if anomalies:
        _fire("anomalies", Role.DEAN, "anomalies: " + ",".join(anomalies))

    required = ROLE_ORDER[: top_rank + 1] if top_rank >= 0 else ()
    return PolicyEvaluation(
        required_roles=required,
        triggered_rules=tuple(triggered),
        anomalies=tuple(anomalies),
        sensitive_changes=tuple(sorted(sensitive_touched)),
        students_affected=affected,
        students_added=added,
        students_removed=removed,
        total_seconds_delta=total_delta,
    )

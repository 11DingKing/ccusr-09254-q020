"""冻结前快照比较、变化策略与审批路由的纯领域逻辑。

该模块不依赖数据库与 Web 框架：

- ``snapshot_fingerprint`` 把快照语义内容固定为稳定指纹；
- ``assess_changes`` 比较上一次冻结与候选快照，产出数量、学时、
  异常类型与敏感字段等指标；
- ``ChangePolicy`` 按指标决定所需签署人角色；
- 角色满足关系与审批状态机用于签署、失效与发布编排。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Any, Iterable, Mapping, Sequence


class DomainError(ValueError):
    """封装领域状态与业务约束。"""


class Role(StrEnum):
    INSTRUCTOR = "instructor"
    DEPARTMENT_HEAD = "department_head"
    DEAN = "dean"
    REGISTRAR = "registrar"


ROLE_RANK: Mapping[Role, int] = {
    Role.INSTRUCTOR: 1,
    Role.DEPARTMENT_HEAD: 2,
    Role.DEAN: 3,
    Role.REGISTRAR: 4,
}


def parse_role(value: str) -> Role:
    try:
        return Role(str(value).strip())
    except ValueError as exc:
        raise DomainError(f"未知签署角色: {value!r}") from exc


def role_covers(signer: Role, required: Role) -> bool:
    """高权限签署可以覆盖不高于自身级别的签署要求。"""
    return ROLE_RANK[signer] >= ROLE_RANK[required]


class State(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    PUBLISHED = "published"
    INVALIDATED = "invalidated"


ALLOWED_TRANSITIONS: Mapping[State, frozenset[State]] = {
    State.PENDING: frozenset({State.APPROVED, State.INVALIDATED}),
    State.APPROVED: frozenset({State.PUBLISHED, State.INVALIDATED}),
    State.PUBLISHED: frozenset(),
    State.INVALIDATED: frozenset(),
}


class InvalidationReason(StrEnum):
    MANUAL = "manual"
    SUPERSEDED = "superseded"
    CANDIDATE_CHANGED = "candidate_changed"
    BASELINE_CHANGED = "baseline_changed"


def ensure_transition(current: State, target: State) -> None:
    if target == current:
        return
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise DomainError(f"不允许从 {current} 变更到 {target}")


# ---------------------------------------------------------------------------
# 快照指纹
# ---------------------------------------------------------------------------

_FINGERPRINT_EXCLUDED_FIELDS = frozenset({"generated_at", "freeze_id"})


def _canonical_snapshot(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        key: value
        for key, value in snapshot.items()
        if key not in _FINGERPRINT_EXCLUDED_FIELDS
    }


def snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """把快照语义内容压缩为稳定指纹。

    ``generated_at`` 与 ``freeze_id`` 属于易变元数据，不参与指纹；
    内容相同的快照必然得到相同指纹。
    """
    raw = json.dumps(
        _canonical_snapshot(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def approval_identifier(
    plan_version: str, baseline_fingerprint: str, candidate_fingerprint: str
) -> str:
    """审批标识由计划与两个快照指纹派生，重算天然幂等。"""
    raw = f"{plan_version}|{baseline_fingerprint}|{candidate_fingerprint}"
    return "FA-" + sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 变化评估
# ---------------------------------------------------------------------------

ANOMALY_STUDENT_REMOVED = "student_removed"
ANOMALY_REQUIREMENT_REGRESSION = "requirement_regression"
ANOMALY_HOURS_DECREASED = "hours_decreased"
ANOMALY_NEGATIVE_ADJUSTMENT = "negative_adjustment"
ANOMALY_PENDING_HOURS_INCREASE = "pending_hours_increase"

ANOMALY_TYPES: tuple[str, ...] = (
    ANOMALY_STUDENT_REMOVED,
    ANOMALY_REQUIREMENT_REGRESSION,
    ANOMALY_HOURS_DECREASED,
    ANOMALY_NEGATIVE_ADJUSTMENT,
    ANOMALY_PENDING_HOURS_INCREASE,
)

#: 学生维度与计划维度的敏感字段；变化时触发更高级别签署。
SENSITIVE_STUDENT_FIELDS: tuple[str, ...] = (
    "meets_requirement",
    "adjustment_seconds",
)
SENSITIVE_PLAN_FIELDS: tuple[str, ...] = (
    "required_seconds",
    "timezone",
)

#: 参与逐字段比较的学生字段。
_COMPARED_STUDENT_FIELDS: tuple[str, ...] = (
    "confirmed_seconds",
    "pending_seconds",
    "adjustment_seconds",
    "total_seconds",
    "lesson_units",
    "pending_lesson_units",
    "meets_requirement",
)


@dataclass(frozen=True)
class ChangeMetrics:
    students_before: int
    students_after: int
    students_added: int
    students_removed: int
    students_modified: int
    total_seconds_before: int
    total_seconds_after: int
    anomaly_types: tuple[str, ...]
    sensitive_fields: tuple[str, ...]

    @property
    def students_affected(self) -> int:
        return self.students_added + self.students_removed + self.students_modified

    @property
    def total_seconds_delta(self) -> int:
        return abs(self.total_seconds_after - self.total_seconds_before)

    def to_dict(self) -> dict[str, Any]:
        return {
            "students_before": self.students_before,
            "students_after": self.students_after,
            "students_added": self.students_added,
            "students_removed": self.students_removed,
            "students_modified": self.students_modified,
            "students_affected": self.students_affected,
            "total_seconds_before": self.total_seconds_before,
            "total_seconds_after": self.total_seconds_after,
            "total_seconds_delta": self.total_seconds_delta,
            "anomaly_types": list(self.anomaly_types),
            "sensitive_fields": list(self.sensitive_fields),
        }


def _student_index(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {s["student_id"]: s for s in snapshot.get("students", [])}


def assess_changes(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> ChangeMetrics:
    """比较上一次冻结与候选快照，提取路由所需的变化指标。"""
    before_map = _student_index(baseline)
    after_map = _student_index(candidate)

    added = sorted(set(after_map) - set(before_map))
    removed = sorted(set(before_map) - set(after_map))

    modified = 0
    anomalies: set[str] = set()
    sensitive: set[str] = set()

    if removed:
        anomalies.add(ANOMALY_STUDENT_REMOVED)

    for student_id in sorted(set(before_map) & set(after_map)):
        before = before_map[student_id]
        after = after_map[student_id]
        changed = {
            name
            for name in _COMPARED_STUDENT_FIELDS
            if before.get(name) != after.get(name)
        }
        if not changed:
            continue
        modified += 1
        sensitive |= changed & set(SENSITIVE_STUDENT_FIELDS)
        if before.get("meets_requirement") and not after.get("meets_requirement"):
            anomalies.add(ANOMALY_REQUIREMENT_REGRESSION)
        if int(after.get("total_seconds", 0)) < int(before.get("total_seconds", 0)):
            anomalies.add(ANOMALY_HOURS_DECREASED)
        if int(after.get("adjustment_seconds", 0)) < int(
            before.get("adjustment_seconds", 0)
        ):
            anomalies.add(ANOMALY_NEGATIVE_ADJUSTMENT)
        if int(after.get("pending_seconds", 0)) > int(before.get("pending_seconds", 0)):
            anomalies.add(ANOMALY_PENDING_HOURS_INCREASE)

    for name in SENSITIVE_PLAN_FIELDS:
        if baseline.get(name) != candidate.get(name):
            sensitive.add(name)

    return ChangeMetrics(
        students_before=len(before_map),
        students_after=len(after_map),
        students_added=len(added),
        students_removed=len(removed),
        students_modified=modified,
        total_seconds_before=sum(
            int(s.get("total_seconds", 0)) for s in before_map.values()
        ),
        total_seconds_after=sum(
            int(s.get("total_seconds", 0)) for s in after_map.values()
        ),
        anomaly_types=tuple(sorted(anomalies)),
        sensitive_fields=tuple(sorted(sensitive)),
    )


# ---------------------------------------------------------------------------
# 变化策略与审批路由
# ---------------------------------------------------------------------------

DEFAULT_ANOMALY_ROLES: Mapping[str, str] = {
    ANOMALY_STUDENT_REMOVED: Role.DEPARTMENT_HEAD.value,
    ANOMALY_REQUIREMENT_REGRESSION: Role.DEAN.value,
    ANOMALY_HOURS_DECREASED: Role.DEPARTMENT_HEAD.value,
    ANOMALY_NEGATIVE_ADJUSTMENT: Role.INSTRUCTOR.value,
    ANOMALY_PENDING_HOURS_INCREASE: Role.INSTRUCTOR.value,
}

DEFAULT_SENSITIVE_FIELD_ROLES: Mapping[str, str] = {
    "meets_requirement": Role.DEAN.value,
    "adjustment_seconds": Role.DEPARTMENT_HEAD.value,
    "required_seconds": Role.REGISTRAR.value,
    "timezone": Role.REGISTRAR.value,
}


@dataclass(frozen=True)
class ChangePolicy:
    """按学生数量、总学时、异常类型与敏感字段决定签署人的策略。

    阈值语义为“严格大于即升级”：受影响学生数超过
    ``student_moderate_threshold`` 需系主任，超过
    ``student_major_threshold`` 需院长；学时阈值同理。
    """

    base_role: str = Role.INSTRUCTOR.value
    student_moderate_threshold: int = 5
    student_major_threshold: int = 50
    seconds_moderate_threshold: int = 36000
    seconds_major_threshold: int = 360000
    anomaly_type_roles: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_ANOMALY_ROLES)
    )
    sensitive_field_roles: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_SENSITIVE_FIELD_ROLES)
    )

    def __post_init__(self) -> None:
        parse_role(self.base_role)
        if self.student_moderate_threshold < 0 or self.student_major_threshold < 0:
            raise DomainError("学生数量阈值不能为负")
        if self.student_moderate_threshold > self.student_major_threshold:
            raise DomainError("学生数量中级阈值不能高于重大阈值")
        if self.seconds_moderate_threshold < 0 or self.seconds_major_threshold < 0:
            raise DomainError("学时阈值不能为负")
        if self.seconds_moderate_threshold > self.seconds_major_threshold:
            raise DomainError("学中级阈值不能高于重大阈值")
        for mapping in (self.anomaly_type_roles, self.sensitive_field_roles):
            for key, role in mapping.items():
                if not str(key).strip():
                    raise DomainError("策略条目名不能为空")
                parse_role(role)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_role": self.base_role,
            "student_moderate_threshold": self.student_moderate_threshold,
            "student_major_threshold": self.student_major_threshold,
            "seconds_moderate_threshold": self.seconds_moderate_threshold,
            "seconds_major_threshold": self.seconds_major_threshold,
            "anomaly_type_roles": dict(self.anomaly_type_roles),
            "sensitive_field_roles": dict(self.sensitive_field_roles),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ChangePolicy":
        if data is None:
            return cls()
        known = {
            "base_role",
            "student_moderate_threshold",
            "student_major_threshold",
            "seconds_moderate_threshold",
            "seconds_major_threshold",
            "anomaly_type_roles",
            "sensitive_field_roles",
        }
        unknown = set(data) - known
        if unknown:
            raise DomainError(f"未知策略字段: {sorted(unknown)}")
        merged = cls().to_dict()
        for key, value in data.items():
            if value is None:
                continue
            if key in {"anomaly_type_roles", "sensitive_field_roles"}:
                combined = dict(merged[key])
                combined.update(value)
                merged[key] = combined
            else:
                merged[key] = value
        return cls(**merged)


@dataclass(frozen=True)
class RouteReason:
    rule: str
    role: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "role": self.role, "detail": self.detail}


def route_requirements(
    metrics: ChangeMetrics, policy: ChangePolicy
) -> tuple[RouteReason, ...]:
    """按策略把变化指标映射为所需签署角色（含触发原因）。"""
    reasons: list[RouteReason] = [
        RouteReason(
            rule="base",
            role=policy.base_role,
            detail="任何冻结发布都需要基础签署",
        )
    ]

    affected = metrics.students_affected
    if affected > policy.student_major_threshold:
        reasons.append(
            RouteReason(
                rule="student_count",
                role=Role.DEAN.value,
                detail=(
                    f"受影响学生 {affected} 人，超过重大阈值 "
                    f"{policy.student_major_threshold}"
                ),
            )
        )
    elif affected > policy.student_moderate_threshold:
        reasons.append(
            RouteReason(
                rule="student_count",
                role=Role.DEPARTMENT_HEAD.value,
                detail=(
                    f"受影响学生 {affected} 人，超过中级阈值 "
                    f"{policy.student_moderate_threshold}"
                ),
            )
        )

    delta = metrics.total_seconds_delta
    if delta > policy.seconds_major_threshold:
        reasons.append(
            RouteReason(
                rule="total_seconds",
                role=Role.DEAN.value,
                detail=(
                    f"总学时变化 {delta} 秒，超过重大阈值 "
                    f"{policy.seconds_major_threshold}"
                ),
            )
        )
    elif delta > policy.seconds_moderate_threshold:
        reasons.append(
            RouteReason(
                rule="total_seconds",
                role=Role.DEPARTMENT_HEAD.value,
                detail=(
                    f"总学时变化 {delta} 秒，超过中级阈值 "
                    f"{policy.seconds_moderate_threshold}"
                ),
            )
        )

    for anomaly in metrics.anomaly_types:
        role = policy.anomaly_type_roles.get(anomaly)
        if role is not None:
            reasons.append(
                RouteReason(
                    rule=f"anomaly:{anomaly}",
                    role=role,
                    detail=f"检测到异常类型 {anomaly}",
                )
            )

    for field_name in metrics.sensitive_fields:
        role = policy.sensitive_field_roles.get(field_name)
        if role is not None:
            reasons.append(
                RouteReason(
                    rule=f"sensitive:{field_name}",
                    role=role,
                    detail=f"敏感字段 {field_name} 发生变化",
                )
            )

    return tuple(reasons)


def required_roles(reasons: Iterable[RouteReason]) -> tuple[str, ...]:
    """所需签署角色去重后按权限级别升序返回。"""
    roles = {parse_role(reason.role) for reason in reasons}
    return tuple(role.value for role in sorted(roles, key=ROLE_RANK.__getitem__))


# ---------------------------------------------------------------------------
# 签署满足关系
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signature:
    actor_id: str
    actor_role: str

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise DomainError("签署人不能为空")
        parse_role(self.actor_role)


def satisfied_roles(
    required: Sequence[str], signatures: Iterable[Signature]
) -> tuple[str, ...]:
    """已被签署覆盖的角色：签署人级别不低于要求级别即覆盖。"""
    signer_roles = [parse_role(sig.actor_role) for sig in signatures]
    covered: list[str] = []
    for value in required:
        role = parse_role(value)
        if any(role_covers(signer, role) for signer in signer_roles):
            covered.append(role.value)
    return tuple(covered)


def outstanding_roles(
    required: Sequence[str], signatures: Iterable[Signature]
) -> tuple[str, ...]:
    covered = set(satisfied_roles(required, signatures))
    return tuple(role for role in required if role not in covered)


def is_fully_signed(required: Sequence[str], signatures: Iterable[Signature]) -> bool:
    return not outstanding_roles(required, tuple(signatures))


def signature_useful(
    required: Sequence[str], existing: Iterable[Signature], candidate: Signature
) -> bool:
    """新签署至少要覆盖一个尚未满足的角色，否则视为权限不足。"""
    remaining = outstanding_roles(required, tuple(existing))
    signer = parse_role(candidate.actor_role)
    return any(role_covers(signer, parse_role(role)) for role in remaining)

"""冻结前比较与审批服务。

候选快照与上一次冻结比较后生成 Comparison，固定到两个快照指纹；
变化策略决定所需签署链，签署齐全后方可发布为正式冻结。签署或
发布前会重算当前指纹，任一快照变化都会使审批失效。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .core.change_policy import (
    DEFAULT_POLICY,
    ChangePolicy,
    Role,
    evaluate_policy,
)
from .core.snapshot import (
    Snapshot,
    build_snapshot,
    diff_snapshots,
    snapshot_fingerprint,
)
from .models import Comparison, Plan
from .repository import (
    get_comparison,
    get_freeze,
    get_plan,
    insert_comparison,
    insert_freeze,
    insert_signature,
    latest_freeze,
    list_open_comparisons,
    list_signatures,
    load_events,
    max_event_id,
    save_comparison,
)
from .services import PlanNotFoundError


class ComparisonNotFoundError(Exception):
    pass


class ComparisonStaleError(Exception):
    pass


class ComparisonStateError(Exception):
    pass


class SignStepError(Exception):
    pass


class PublishConflictError(Exception):
    pass


def _require_plan(db: Session, plan_version: str) -> Plan:
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def _require_comparison(
    db: Session, plan_version: str, comparison_id: str
) -> Comparison:
    row = get_comparison(db, plan_version, comparison_id)
    if row is None:
        raise ComparisonNotFoundError(
            f"comparison '{comparison_id}' for plan '{plan_version}' does not exist"
        )
    return row


def _empty_base_snapshot(plan: Plan) -> Snapshot:
    return build_snapshot(
        [],
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )


def _current_base(db: Session, plan: Plan) -> tuple[Snapshot, str | None]:
    row = latest_freeze(db, plan.plan_version)
    if row is None:
        return _empty_base_snapshot(plan), None
    return Snapshot.from_dict(row.snapshot), row.freeze_id


def _current_candidate(db: Session, plan: Plan) -> Snapshot:
    cutoff = max_event_id(db, plan.plan_version)
    events = load_events(db, plan.plan_version)
    return build_snapshot(
        events,
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        event_cutoff_id=cutoff,
    )


def _current_fingerprints(db: Session, plan: Plan) -> tuple[str, str]:
    base, _ = _current_base(db, plan)
    candidate = _current_candidate(db, plan)
    return snapshot_fingerprint(base), snapshot_fingerprint(candidate)


def _is_stale(db: Session, plan: Plan, row: Comparison) -> bool:
    base_fp, candidate_fp = _current_fingerprints(db, plan)
    return (
        base_fp != row.base_fingerprint
        or candidate_fp != row.candidate_fingerprint
    )


def _assert_fresh(db: Session, plan: Plan, row: Comparison) -> None:
    """重算两个快照指纹；任一变化即将审批置为失效并抛出冲突。"""
    if _is_stale(db, plan, row):
        row.status = "invalid"
        row.invalidated_reason = "snapshot fingerprints changed; approval voided"
        save_comparison(db, row)
        raise ComparisonStaleError(
            "comparison is stale: a snapshot changed, approval invalidated"
        )


def _ordered_signatures(
    db: Session, row: Comparison
) -> list[dict[str, Any]]:
    route = list(row.route)
    signatures = list_signatures(db, row.plan_version, row.comparison_id)
    ordered = sorted(
        signatures,
        key=lambda s: route.index(s.role) if s.role in route else len(route),
    )
    return [
        {"role": s.role, "signer_id": s.signer_id, "signed_at": s.signed_at}
        for s in ordered
    ]


def _view(db: Session, plan: Plan, row: Comparison) -> dict[str, Any]:
    signatures = _ordered_signatures(db, row)
    route = list(row.route)
    pending_roles = route[len(signatures):]
    stale = row.status in ("pending", "approved") and _is_stale(db, plan, row)
    return {
        "plan_version": row.plan_version,
        "comparison_id": row.comparison_id,
        "base_freeze_id": row.base_freeze_id,
        "base_fingerprint": row.base_fingerprint,
        "candidate_fingerprint": row.candidate_fingerprint,
        "candidate_cutoff_id": row.candidate_cutoff_id,
        "status": row.status,
        "route": route,
        "signatures": signatures,
        "pending_roles": pending_roles,
        "next_role": pending_roles[0] if pending_roles and row.status == "pending" else None,
        "summary": dict(row.summary),
        "diff": dict(row.diff),
        "stale": stale,
        "published_freeze_id": row.published_freeze_id,
        "invalidated_reason": row.invalidated_reason,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def compare_candidate(
    db: Session,
    *,
    plan_version: str,
    policy: ChangePolicy = DEFAULT_POLICY,
) -> tuple[dict[str, Any], bool]:
    """把当前候选快照与上一次冻结比较，返回（视图, 是否新建）。

    comparison_id 由两个快照指纹派生，因此并发或重复重算都会收敛
    到同一条记录。
    """
    plan = _require_plan(db, plan_version)
    base, base_freeze_id = _current_base(db, plan)
    candidate = _current_candidate(db, plan)
    base_fp = snapshot_fingerprint(base)
    candidate_fp = snapshot_fingerprint(candidate)
    comparison_id = f"cmp-{base_fp[:12]}-{candidate_fp[:12]}"

    existing = get_comparison(db, plan_version, comparison_id)
    if existing is not None:
        return _view(db, plan, existing), False

    diff = diff_snapshots(base, candidate)
    evaluation = evaluate_policy(diff, candidate.students, policy)
    route = [role.value for role in evaluation.required_roles]
    status = "approved" if not route else "pending"
    row = insert_comparison(
        db,
        plan_version=plan_version,
        comparison_id=comparison_id,
        base_freeze_id=base_freeze_id,
        base_fingerprint=base_fp,
        candidate_fingerprint=candidate_fp,
        candidate_cutoff_id=candidate.event_cutoff_id,
        diff=diff,
        summary=evaluation.summary_dict(),
        route=route,
        status=status,
    )
    if row is None:
        existing = get_comparison(db, plan_version, comparison_id)
        assert existing is not None
        return _view(db, plan, existing), False
    return _view(db, plan, row), True


def get_comparison_view(
    db: Session, *, plan_version: str, comparison_id: str
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    row = _require_comparison(db, plan_version, comparison_id)
    return _view(db, plan, row)


def get_route(
    db: Session, *, plan_version: str, comparison_id: str
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    row = _require_comparison(db, plan_version, comparison_id)
    signatures = _ordered_signatures(db, row)
    route = list(row.route)
    pending_roles = route[len(signatures):]
    return {
        "plan_version": row.plan_version,
        "comparison_id": row.comparison_id,
        "status": row.status,
        "required_roles": route,
        "signed_roles": [s["role"] for s in signatures],
        "pending_roles": pending_roles,
        "next_role": pending_roles[0] if pending_roles and row.status == "pending" else None,
        "complete": not pending_roles,
        "signatures": signatures,
    }


def sign(
    db: Session,
    *,
    plan_version: str,
    comparison_id: str,
    role: str,
    signer_id: str,
) -> dict[str, Any]:
    """按路由顺序签署；签署前重算指纹，候选或基准变化即失效。"""
    plan = _require_plan(db, plan_version)
    row = _require_comparison(db, plan_version, comparison_id)
    if role not in {r.value for r in Role}:
        raise SignStepError(f"unknown signer role '{role}'")
    if row.status == "invalid":
        raise ComparisonStateError("comparison is invalid and cannot be signed")
    if row.status == "published":
        raise ComparisonStateError("comparison is already published")
    if row.status == "approved":
        raise ComparisonStateError("all required signatures already collected")
    _assert_fresh(db, plan, row)

    route = list(row.route)
    existing = list_signatures(db, plan_version, comparison_id)
    expected = route[len(existing)]
    if role != expected:
        raise SignStepError(
            f"next required signer is '{expected}', got '{role}'"
        )
    inserted = insert_signature(
        db,
        plan_version=plan_version,
        comparison_id=comparison_id,
        role=role,
        signer_id=signer_id,
    )
    if inserted is None:
        current = list_signatures(db, plan_version, comparison_id)
        duplicate = [s for s in current if s.role == role]
        if duplicate and duplicate[0].signer_id == signer_id:
            return _view(db, plan, row)
        raise SignStepError(f"role '{role}' has already been signed")
    if len(existing) + 1 == len(route):
        row.status = "approved"
        save_comparison(db, row)
    return _view(db, plan, row)


def invalidate(
    db: Session,
    *,
    plan_version: str,
    comparison_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """显式作废一条待签或已签齐的比较。"""
    plan = _require_plan(db, plan_version)
    row = _require_comparison(db, plan_version, comparison_id)
    if row.status == "published":
        raise ComparisonStateError("published comparisons cannot be invalidated")
    if row.status != "invalid":
        row.status = "invalid"
        row.invalidated_reason = reason.strip() or "manually invalidated"
        save_comparison(db, row)
    return _view(db, plan, row)


def publish(
    db: Session,
    *,
    plan_version: str,
    comparison_id: str,
    freeze_id: str,
) -> tuple[Snapshot, dict[str, Any]]:
    """签署齐全且指纹未变时，把候选快照发布为正式冻结。"""
    plan = _require_plan(db, plan_version)
    row = _require_comparison(db, plan_version, comparison_id)
    if row.status == "published":
        frozen = get_freeze(db, plan_version, row.published_freeze_id or "")
        assert frozen is not None
        return Snapshot.from_dict(frozen.snapshot), _view(db, plan, row)
    if row.status == "invalid":
        raise ComparisonStateError("invalid comparisons cannot be published")
    if row.status != "approved":
        missing = list(row.route)[len(list_signatures(db, plan_version, comparison_id)):]
        raise ComparisonStateError(
            f"missing required signatures: {', '.join(missing)}"
        )
    _assert_fresh(db, plan, row)

    candidate = _current_candidate(db, plan)
    candidate.freeze_id = freeze_id
    stored = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=candidate.to_dict(),
        event_cutoff_id=candidate.event_cutoff_id,
    )
    if stored is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        if snapshot_fingerprint(Snapshot.from_dict(existing.snapshot)) != (
            row.candidate_fingerprint
        ):
            raise PublishConflictError(
                f"freeze '{freeze_id}' already exists with different content"
            )
    row.status = "published"
    row.published_freeze_id = freeze_id
    save_comparison(db, row)

    # 基准冻结已推进，其余待审批比较全部失效。
    for other in list_open_comparisons(db, plan_version, exclude_id=comparison_id):
        other.status = "invalid"
        other.invalidated_reason = f"superseded by freeze '{freeze_id}'"
        save_comparison(db, other)

    return candidate, _view(db, plan, row)

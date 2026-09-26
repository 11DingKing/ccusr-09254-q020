"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .compliance import freeze_approvals as approval_domain
from .compliance.freeze_approvals import (
    ChangePolicy,
    DomainError,
    InvalidationReason,
    Signature,
    State,
    assess_changes,
    approval_identifier,
    is_fully_signed,
    outstanding_roles,
    required_roles,
    route_requirements,
    satisfied_roles,
    signature_useful,
    snapshot_fingerprint,
)
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .models import FreezeApproval
from .repository import (
    add_signature,
    get_approval,
    get_freeze,
    get_plan,
    insert_approval,
    insert_events,
    insert_freeze,
    latest_approval_for_fingerprints,
    latest_freeze,
    list_active_approvals,
    list_approvals,
    list_signatures,
    load_events,
    load_events_up_to,
    max_event_id,
    save_approval,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class ApprovalNotFoundError(Exception):
    pass


class ApprovalConflictError(Exception):
    """审批状态或快照指纹与请求冲突。"""


class RoleInsufficientError(Exception):
    """签署人角色无法覆盖任何未满足的签署要求。"""


class PolicyValidationError(Exception):
    """变化策略配置不合法。"""


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# ---------------------------------------------------------------------------
# 冻结前比较与审批
# ---------------------------------------------------------------------------


def _empty_baseline_dict(plan) -> dict[str, Any]:
    """没有历史冻结时的空基线快照。"""
    return {
        "plan_version": plan.plan_version,
        "freeze_id": None,
        "timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "event_cutoff_id": None,
        "students": [],
    }


def _current_baseline(db: Session, plan) -> tuple[dict[str, Any], str | None]:
    """上一次冻结（含快照内容与冻结标识）；无冻结时返回空基线。"""
    row = latest_freeze(db, plan.plan_version)
    if row is None:
        return _empty_baseline_dict(plan), None
    return dict(row.snapshot), row.freeze_id


def _live_candidate(db: Session, plan) -> tuple[dict[str, Any], str | None]:
    """当前候选快照与其事件截止点。"""
    cutoff = max_event_id(db, plan.plan_version)
    events = load_events(db, plan.plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        event_cutoff_id=cutoff,
    )
    return snap.to_dict(), cutoff


def _current_fingerprints(
    db: Session, plan
) -> tuple[str, str]:
    baseline_dict, _ = _current_baseline(db, plan)
    candidate_dict, _ = _live_candidate(db, plan)
    return snapshot_fingerprint(baseline_dict), snapshot_fingerprint(candidate_dict)


def _invalidate(
    db: Session, approval: FreezeApproval, reason: InvalidationReason
) -> FreezeApproval:
    try:
        approval_domain.ensure_transition(
            State(approval.state), State.INVALIDATED
        )
    except DomainError as exc:
        raise ApprovalConflictError(str(exc)) from exc
    approval.state = State.INVALIDATED.value
    approval.invalidation_reason = reason.value
    approval.version += 1
    return save_approval(db, approval)


def _refresh_approval(
    db: Session,
    approval: FreezeApproval,
    *,
    current: tuple[str, str] | None = None,
) -> FreezeApproval:
    """惰性失效检查：基线或候选指纹漂移时把审批置为失效。

    比较结果固定到两个快照指纹，任一快照变化都会使审批失效；
    终态（已发布/已失效）不再变化。
    """
    if approval.state not in (State.PENDING.value, State.APPROVED.value):
        return approval
    plan = get_plan(db, approval.plan_version)
    if plan is None:
        return approval
    if current is None:
        current = _current_fingerprints(db, plan)
    baseline_fp, candidate_fp = current
    if baseline_fp != approval.baseline_fingerprint:
        return _invalidate(db, approval, InvalidationReason.BASELINE_CHANGED)
    if candidate_fp != approval.candidate_fingerprint:
        return _invalidate(db, approval, InvalidationReason.CANDIDATE_CHANGED)
    return approval


def _signature_views(db: Session, approval_id: str) -> list[dict[str, Any]]:
    return [
        {
            "actor_id": s.actor_id,
            "actor_role": s.actor_role,
            "note": s.note,
            "created_at": s.created_at,
        }
        for s in list_signatures(db, approval_id)
    ]


def _approval_view(db: Session, approval: FreezeApproval) -> dict[str, Any]:
    signatures = _signature_views(db, approval.approval_id)
    domain_sigs = [
        Signature(actor_id=s["actor_id"], actor_role=s["actor_role"])
        for s in signatures
    ]
    required = list(approval.required_roles)
    return {
        "approval_id": approval.approval_id,
        "plan_version": approval.plan_version,
        "state": approval.state,
        "baseline_freeze_id": approval.baseline_freeze_id,
        "baseline_fingerprint": approval.baseline_fingerprint,
        "candidate_fingerprint": approval.candidate_fingerprint,
        "candidate_event_cutoff_id": approval.candidate_event_cutoff_id,
        "metrics": approval.metrics,
        "policy": approval.policy,
        "required_roles": required,
        "route_reasons": approval.route_reasons,
        "satisfied_roles": list(satisfied_roles(required, domain_sigs)),
        "outstanding_roles": list(outstanding_roles(required, domain_sigs)),
        "signatures": signatures,
        "invalidation_reason": approval.invalidation_reason,
        "published_freeze_id": approval.published_freeze_id,
        "diff": approval.diff,
        "revision": approval.revision,
        "version": approval.version,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
    }


def _require_approval(
    db: Session, plan_version: str, approval_id: str
) -> FreezeApproval:
    approval = get_approval(db, approval_id)
    if approval is None or approval.plan_version != plan_version:
        raise ApprovalNotFoundError(
            f"approval '{approval_id}' for plan '{plan_version}' does not exist"
        )
    return approval


def create_comparison(
    db: Session, *, plan_version: str, policy_data: dict[str, Any] | None = None
) -> tuple[dict[str, Any], bool]:
    """比较候选快照与上一次冻结，按变化策略路由审批。

    审批标识由计划与两个快照指纹派生：内容相同的并发重算幂等，
    内容不同的新比较会使旧的未决审批被取代而失效。
    """
    plan = _require_plan(db, plan_version)
    try:
        policy = ChangePolicy.from_dict(policy_data)
    except DomainError as exc:
        raise PolicyValidationError(str(exc)) from exc

    baseline_dict, baseline_freeze_id = _current_baseline(db, plan)
    candidate_dict, cutoff = _live_candidate(db, plan)
    baseline_fp = snapshot_fingerprint(baseline_dict)
    candidate_fp = snapshot_fingerprint(candidate_dict)
    base_id = approval_identifier(plan_version, baseline_fp, candidate_fp)

    latest = latest_approval_for_fingerprints(
        db, plan_version, baseline_fp, candidate_fp
    )
    if latest is not None and not (
        latest.state == State.INVALIDATED.value
        and latest.invalidation_reason
        in (
            InvalidationReason.SUPERSEDED.value,
            InvalidationReason.CANDIDATE_CHANGED.value,
            InvalidationReason.BASELINE_CHANGED.value,
        )
    ):
        # 进行中的审批直接复用；人工失效的审批保持封锁，
        # 避免通过重算绕过签署人的否决。
        refreshed = _refresh_approval(db, latest)
        return _approval_view(db, refreshed), False

    # 系统原因（被取代/指纹漂移）失效后内容又回到同一对指纹：
    # 以新的修订版本重新发起审批，旧记录保留审计痕迹。
    revision = 1 if latest is None else latest.revision + 1
    approval_id = base_id if revision == 1 else f"{base_id}-r{revision}"

    metrics = assess_changes(baseline_dict, candidate_dict)
    reasons = route_requirements(metrics, policy)
    roles = list(required_roles(reasons))
    diff = diff_snapshots(
        Snapshot.from_dict(baseline_dict), Snapshot.from_dict(candidate_dict)
    )

    row = insert_approval(
        db,
        approval_id=approval_id,
        plan_version=plan_version,
        baseline_freeze_id=baseline_freeze_id,
        baseline_fingerprint=baseline_fp,
        candidate_fingerprint=candidate_fp,
        candidate_event_cutoff_id=cutoff,
        metrics=metrics.to_dict(),
        policy=policy.to_dict(),
        required_roles=roles,
        route_reasons=[r.to_dict() for r in reasons],
        diff=diff,
        revision=revision,
    )
    created = row is not None
    if row is None:
        # 并发重算时另一个事务已插入同一审批标识。
        row = get_approval(db, approval_id)
        assert row is not None

    # 新的比较结果使该计划下更早的未决审批被取代。
    # 只取代“严格更旧”的审批：并发重算时无论事务如何交错，
    # 最终都只有最新的比较保持有效。
    younger = (row.created_at, row.approval_id)
    for other in list_active_approvals(db, plan_version):
        if other.approval_id == row.approval_id:
            continue
        if (other.created_at, other.approval_id) < younger:
            _invalidate(db, other, InvalidationReason.SUPERSEDED)

    return _approval_view(db, row), created


def get_approval_view(
    db: Session, plan_version: str, approval_id: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    approval = _require_approval(db, plan_version, approval_id)
    approval = _refresh_approval(db, approval)
    return _approval_view(db, approval)


def list_approval_views(db: Session, plan_version: str) -> list[dict[str, Any]]:
    plan = _require_plan(db, plan_version)
    current = _current_fingerprints(db, plan)
    views = []
    for approval in list_approvals(db, plan_version):
        views.append(
            _approval_view(db, _refresh_approval(db, approval, current=current))
        )
    return views


def get_route(db: Session, plan_version: str, approval_id: str) -> dict[str, Any]:
    view = get_approval_view(db, plan_version, approval_id)
    return {
        "approval_id": view["approval_id"],
        "state": view["state"],
        "required_roles": view["required_roles"],
        "satisfied_roles": view["satisfied_roles"],
        "outstanding_roles": view["outstanding_roles"],
        "route_reasons": view["route_reasons"],
        "signatures": view["signatures"],
    }


def sign_approval(
    db: Session,
    *,
    plan_version: str,
    approval_id: str,
    actor_id: str,
    actor_role: str,
    note: str = "",
) -> dict[str, Any]:
    """签署审批；角色高于要求级别时向下覆盖（权限升级）。"""
    _require_plan(db, plan_version)
    approval = _require_approval(db, plan_version, approval_id)
    approval = _refresh_approval(db, approval)
    if approval.state == State.INVALIDATED.value:
        raise ApprovalConflictError(
            f"approval already invalidated: {approval.invalidation_reason}"
        )
    if approval.state != State.PENDING.value:
        raise ApprovalConflictError(
            f"approval in state '{approval.state}' cannot be signed"
        )
    try:
        signature = Signature(actor_id=actor_id, actor_role=actor_role)
    except DomainError as exc:
        raise PolicyValidationError(str(exc)) from exc

    existing_rows = list_signatures(db, approval_id)
    if any(s.actor_id == signature.actor_id for s in existing_rows):
        # 同一签署人重复签署幂等。
        return _approval_view(db, approval)
    existing = [
        Signature(actor_id=s.actor_id, actor_role=s.actor_role)
        for s in existing_rows
    ]
    if not signature_useful(approval.required_roles, existing, signature):
        raise RoleInsufficientError(
            f"role '{signature.actor_role}' does not cover any outstanding "
            f"requirement: {outstanding_roles(approval.required_roles, existing)}"
        )

    _, created = add_signature(
        db,
        approval_id=approval.approval_id,
        actor_id=signature.actor_id,
        actor_role=signature.actor_role,
        note=note.strip(),
    )
    db.refresh(approval)
    if not created:
        # 并发重复签署：签名已落库，直接返回当前状态。
        return _approval_view(db, approval)
    if approval.state == State.INVALIDATED.value:
        raise ApprovalConflictError(
            f"approval already invalidated: {approval.invalidation_reason}"
        )
    if approval.state != State.PENDING.value:
        # 并发事务已把审批推进到下一状态；本次签署已记录在案。
        return _approval_view(db, approval)
    domain_sigs = [
        Signature(actor_id=s.actor_id, actor_role=s.actor_role)
        for s in list_signatures(db, approval_id)
    ]
    if is_fully_signed(approval.required_roles, domain_sigs):
        approval.state = State.APPROVED.value
        approval.version += 1
        approval = save_approval(db, approval)
    return _approval_view(db, approval)


def invalidate_approval(
    db: Session,
    *,
    plan_version: str,
    approval_id: str,
    actor_id: str,
    reason: str,
) -> dict[str, Any]:
    """手动使审批失效；已发布审批不可失效，重复失效幂等。"""
    _require_plan(db, plan_version)
    approval = _require_approval(db, plan_version, approval_id)
    approval = _refresh_approval(db, approval)
    if approval.state == State.INVALIDATED.value:
        return _approval_view(db, approval)
    if approval.state == State.PUBLISHED.value:
        raise ApprovalConflictError("published approval cannot be invalidated")
    approval = _invalidate(db, approval, InvalidationReason.MANUAL)
    return _approval_view(db, approval)


def publish_approval(
    db: Session,
    *,
    plan_version: str,
    approval_id: str,
    freeze_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """发布：签署完成且指纹未漂移时，把候选固化为新的冻结快照。"""
    plan = _require_plan(db, plan_version)
    approval = _require_approval(db, plan_version, approval_id)
    approval = _refresh_approval(db, approval)
    if approval.state == State.PUBLISHED.value:
        # 重复发布幂等，返回首次发布的冻结。
        frozen = get_frozen_snapshot(db, plan_version, approval.published_freeze_id)
        return _approval_view(db, approval), frozen.to_dict()
    if approval.state != State.APPROVED.value:
        raise ApprovalConflictError(
            f"approval in state '{approval.state}' cannot be published"
        )

    target_freeze_id = freeze_id or f"F-{approval.approval_id}"
    cutoff = approval.candidate_event_cutoff_id
    if cutoff is None:
        events = load_events(db, plan_version)
    else:
        events = load_events_up_to(db, plan_version, cutoff)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=target_freeze_id,
        event_cutoff_id=cutoff,
    )
    if snapshot_fingerprint(snap.to_dict()) != approval.candidate_fingerprint:
        # 发布前候选发生变化：审批立即失效。
        _invalidate(db, approval, InvalidationReason.CANDIDATE_CHANGED)
        raise ApprovalConflictError(
            "candidate snapshot changed; approval has been invalidated"
        )

    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=target_freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        raise FreezeConflictError(
            f"freeze '{target_freeze_id}' for plan '{plan_version}' already exists"
        )

    approval.state = State.PUBLISHED.value
    approval.published_freeze_id = target_freeze_id
    approval.version += 1
    approval = save_approval(db, approval)
    return _approval_view(db, approval), snap.to_dict()

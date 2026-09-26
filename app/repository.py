"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import Freeze, FreezeApproval, FreezeSignature, Plan


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


def latest_freeze(db: Session, plan_version: str) -> Freeze | None:
    """最近一次冻结（按创建时间与标识倒序），作为比较基线。"""
    stmt = (
        select(Freeze)
        .where(Freeze.plan_version == plan_version)
        .order_by(Freeze.created_at.desc(), Freeze.freeze_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_approval(db: Session, approval_id: str) -> FreezeApproval | None:
    return db.get(FreezeApproval, approval_id)


def latest_approval_for_fingerprints(
    db: Session,
    plan_version: str,
    baseline_fingerprint: str,
    candidate_fingerprint: str,
) -> FreezeApproval | None:
    """同一对指纹的最新修订版本。"""
    stmt = (
        select(FreezeApproval)
        .where(
            FreezeApproval.plan_version == plan_version,
            FreezeApproval.baseline_fingerprint == baseline_fingerprint,
            FreezeApproval.candidate_fingerprint == candidate_fingerprint,
        )
        .order_by(FreezeApproval.revision.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def insert_approval(
    db: Session,
    *,
    approval_id: str,
    plan_version: str,
    baseline_freeze_id: str | None,
    baseline_fingerprint: str,
    candidate_fingerprint: str,
    candidate_event_cutoff_id: str | None,
    metrics: dict[str, Any],
    policy: dict[str, Any],
    required_roles: list[str],
    route_reasons: list[dict[str, Any]],
    diff: dict[str, Any],
    revision: int,
) -> FreezeApproval | None:
    """插入审批记录；同一审批标识的并发插入只有一个成功。"""
    stmt = sqlite_insert(FreezeApproval).values(
        approval_id=approval_id,
        plan_version=plan_version,
        baseline_freeze_id=baseline_freeze_id,
        baseline_fingerprint=baseline_fingerprint,
        candidate_fingerprint=candidate_fingerprint,
        candidate_event_cutoff_id=candidate_event_cutoff_id,
        state="pending",
        metrics=metrics,
        policy=policy,
        required_roles=required_roles,
        route_reasons=route_reasons,
        diff=diff,
        revision=revision,
        version=1,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["approval_id"]
    ).returning(FreezeApproval.approval_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(FreezeApproval, approval_id)
    return None


def list_approvals(db: Session, plan_version: str) -> list[FreezeApproval]:
    stmt = (
        select(FreezeApproval)
        .where(FreezeApproval.plan_version == plan_version)
        .order_by(FreezeApproval.created_at.desc(), FreezeApproval.approval_id.desc())
    )
    return list(db.execute(stmt).scalars().all())


def list_active_approvals(db: Session, plan_version: str) -> list[FreezeApproval]:
    stmt = select(FreezeApproval).where(
        FreezeApproval.plan_version == plan_version,
        FreezeApproval.state.in_(["pending", "approved"]),
    )
    return list(db.execute(stmt).scalars().all())


def save_approval(db: Session, approval: FreezeApproval) -> FreezeApproval:
    approval.updated_at = datetime.now(timezone.utc)
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval


def add_signature(
    db: Session,
    *,
    approval_id: str,
    actor_id: str,
    actor_role: str,
    note: str,
) -> tuple[FreezeSignature | None, bool]:
    """记录签署；同一审批同一签署人重复签署幂等。"""
    stmt = sqlite_insert(FreezeSignature).values(
        approval_id=approval_id,
        actor_id=actor_id,
        actor_role=actor_role,
        note=note,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["approval_id", "actor_id"]
    ).returning(FreezeSignature.id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None, False
    stmt = select(FreezeSignature).where(
        FreezeSignature.approval_id == approval_id,
        FreezeSignature.actor_id == actor_id,
    )
    return db.execute(stmt).scalar_one(), True


def list_signatures(db: Session, approval_id: str) -> list[FreezeSignature]:
    stmt = (
        select(FreezeSignature)
        .where(FreezeSignature.approval_id == approval_id)
        .order_by(FreezeSignature.id)
    )
    return list(db.execute(stmt).scalars().all())

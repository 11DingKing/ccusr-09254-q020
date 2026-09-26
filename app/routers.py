"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    ApprovalOut,
    ComparisonIn,
    ComparisonOut,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    InvalidateIn,
    PlanIn,
    PlanOut,
    PublishIn,
    PublishOut,
    RouteOut,
    SignatureIn,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 冻结前比较与审批：比较、路由、签署、失效、发布
# ---------------------------------------------------------------------------


def _approval_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, services.PlanNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, services.ApprovalNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, services.RoleInsufficientError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, services.PolicyValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(
        exc, (services.ApprovalConflictError, services.FreezeConflictError)
    ):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/plans/{plan_version}/freeze-approvals/comparisons",
    response_model=ComparisonOut,
    status_code=status.HTTP_201_CREATED,
)
def post_comparison(
    plan_version: str, body: ComparisonIn, db: Session = Depends(get_db)
) -> Any:
    """比较候选快照与上一次冻结，按变化策略生成审批路由。"""
    try:
        view, created = services.create_comparison(
            db,
            plan_version=plan_version,
            policy_data=body.policy.model_dump() if body.policy else None,
        )
        return {"created": created, "approval": view}
    except Exception as exc:
        raise _approval_errors(exc) from exc


@router.get(
    "/plans/{plan_version}/freeze-approvals",
    response_model=list[ApprovalOut],
)
def list_freeze_approvals(
    plan_version: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.list_approval_views(db, plan_version)
    except Exception as exc:
        raise _approval_errors(exc) from exc


@router.get(
    "/plans/{plan_version}/freeze-approvals/{approval_id}",
    response_model=ApprovalOut,
)
def get_freeze_approval(
    plan_version: str, approval_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.get_approval_view(db, plan_version, approval_id)
    except Exception as exc:
        raise _approval_errors(exc) from exc


@router.get(
    "/plans/{plan_version}/freeze-approvals/{approval_id}/route",
    response_model=RouteOut,
)
def get_freeze_approval_route(
    plan_version: str, approval_id: str, db: Session = Depends(get_db)
) -> Any:
    """查看所需签署人、已满足与未满足的签署要求。"""
    try:
        return services.get_route(db, plan_version, approval_id)
    except Exception as exc:
        raise _approval_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/freeze-approvals/{approval_id}/signatures",
    response_model=ApprovalOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze_approval_signature(
    plan_version: str,
    approval_id: str,
    body: SignatureIn,
    db: Session = Depends(get_db),
) -> Any:
    """签署审批；更高级别角色可覆盖较低级别的签署要求。"""
    try:
        return services.sign_approval(
            db,
            plan_version=plan_version,
            approval_id=approval_id,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            note=body.note,
        )
    except Exception as exc:
        raise _approval_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/freeze-approvals/{approval_id}/invalidate",
    response_model=ApprovalOut,
)
def post_freeze_approval_invalidate(
    plan_version: str,
    approval_id: str,
    body: InvalidateIn,
    db: Session = Depends(get_db),
) -> Any:
    """手动使审批失效；基线或候选快照变化也会自动失效。"""
    try:
        return services.invalidate_approval(
            db,
            plan_version=plan_version,
            approval_id=approval_id,
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except Exception as exc:
        raise _approval_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/freeze-approvals/{approval_id}/publish",
    response_model=PublishOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze_approval_publish(
    plan_version: str,
    approval_id: str,
    body: PublishIn,
    db: Session = Depends(get_db),
) -> Any:
    """发布：签署完成且指纹未漂移时固化为新的冻结快照。"""
    try:
        view, freeze = services.publish_approval(
            db,
            plan_version=plan_version,
            approval_id=approval_id,
            freeze_id=body.freeze_id,
        )
        return {"approval": view, "freeze": freeze}
    except Exception as exc:
        raise _approval_errors(exc) from exc

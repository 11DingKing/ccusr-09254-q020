"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import approvals, services
from .db import get_db
from .schemas import (
    ComparisonOut,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    InvalidateIn,
    PlanIn,
    PlanOut,
    PublishIn,
    RouteOut,
    SignIn,
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


def _comparison_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, approvals.ComparisonNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/plans/{plan_version}/comparisons",
    response_model=ComparisonOut,
    status_code=status.HTTP_201_CREATED,
    tags=["approvals"],
)
def post_comparison(plan_version: str, db: Session = Depends(get_db)) -> Any:
    """比较：把当前候选快照与上一次冻结比较并生成审批路由。"""
    try:
        view, _ = approvals.compare_candidate(db, plan_version=plan_version)
        return view
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/comparisons/{comparison_id}",
    response_model=ComparisonOut,
    tags=["approvals"],
)
def get_comparison(
    plan_version: str, comparison_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return approvals.get_comparison_view(
            db, plan_version=plan_version, comparison_id=comparison_id
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except approvals.ComparisonNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/comparisons/{comparison_id}/route",
    response_model=RouteOut,
    tags=["approvals"],
)
def get_comparison_route(
    plan_version: str, comparison_id: str, db: Session = Depends(get_db)
) -> Any:
    """路由：查看所需签署人、已签与待签角色。"""
    try:
        return approvals.get_route(
            db, plan_version=plan_version, comparison_id=comparison_id
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except approvals.ComparisonNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/comparisons/{comparison_id}/signatures",
    response_model=ComparisonOut,
    status_code=status.HTTP_201_CREATED,
    tags=["approvals"],
)
def post_signature(
    plan_version: str,
    comparison_id: str,
    body: SignIn,
    db: Session = Depends(get_db),
) -> Any:
    """签署：按路由顺序签署；签署前重算指纹，快照变化即失效。"""
    try:
        return approvals.sign(
            db,
            plan_version=plan_version,
            comparison_id=comparison_id,
            role=body.role,
            signer_id=body.signer_id,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        approvals.ComparisonNotFoundError,
        approvals.ComparisonStaleError,
        approvals.ComparisonStateError,
        approvals.SignStepError,
    ) as exc:
        raise _comparison_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/comparisons/{comparison_id}/invalidate",
    response_model=ComparisonOut,
    tags=["approvals"],
)
def post_invalidate(
    plan_version: str,
    comparison_id: str,
    body: InvalidateIn,
    db: Session = Depends(get_db),
) -> Any:
    """失效：显式作废一条待签或已签齐的比较。"""
    try:
        return approvals.invalidate(
            db,
            plan_version=plan_version,
            comparison_id=comparison_id,
            reason=body.reason,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        approvals.ComparisonNotFoundError,
        approvals.ComparisonStateError,
    ) as exc:
        raise _comparison_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/comparisons/{comparison_id}/publish",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
    tags=["approvals"],
)
def post_publish(
    plan_version: str,
    comparison_id: str,
    body: PublishIn,
    db: Session = Depends(get_db),
) -> Any:
    """发布：签署齐全且指纹未变时，把候选快照冻结为正式版本。"""
    try:
        snapshot, _ = approvals.publish(
            db,
            plan_version=plan_version,
            comparison_id=comparison_id,
            freeze_id=body.freeze_id,
        )
        return snapshot.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        approvals.ComparisonNotFoundError,
        approvals.ComparisonStaleError,
        approvals.ComparisonStateError,
        approvals.PublishConflictError,
    ) as exc:
        raise _comparison_errors(exc) from exc

"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


class PolicyIn(BaseModel):
    """变化策略覆盖项；缺省字段使用系统默认。"""

    model_config = {"extra": "forbid"}

    base_role: str | None = None
    student_moderate_threshold: int | None = Field(None, ge=0)
    student_major_threshold: int | None = Field(None, ge=0)
    seconds_moderate_threshold: int | None = Field(None, ge=0)
    seconds_major_threshold: int | None = Field(None, ge=0)
    anomaly_type_roles: dict[str, str] | None = None
    sensitive_field_roles: dict[str, str] | None = None


class ComparisonIn(BaseModel):
    policy: PolicyIn | None = None


class SignatureOut(BaseModel):
    actor_id: str
    actor_role: str
    note: str
    created_at: datetime


class ApprovalOut(BaseModel):
    approval_id: str
    plan_version: str
    state: str
    baseline_freeze_id: str | None
    baseline_fingerprint: str
    candidate_fingerprint: str
    candidate_event_cutoff_id: str | None
    metrics: dict[str, Any]
    policy: dict[str, Any]
    required_roles: list[str]
    route_reasons: list[dict[str, Any]]
    satisfied_roles: list[str]
    outstanding_roles: list[str]
    signatures: list[SignatureOut]
    invalidation_reason: str | None
    published_freeze_id: str | None
    diff: dict[str, Any]
    revision: int
    version: int
    created_at: datetime
    updated_at: datetime


class ComparisonOut(BaseModel):
    created: bool
    approval: ApprovalOut


class RouteOut(BaseModel):
    approval_id: str
    state: str
    required_roles: list[str]
    satisfied_roles: list[str]
    outstanding_roles: list[str]
    route_reasons: list[dict[str, Any]]
    signatures: list[SignatureOut]


class SignatureIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    actor_role: str = Field(..., min_length=1, max_length=32)
    note: str = Field("", max_length=512)


class InvalidateIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)


class PublishIn(BaseModel):
    freeze_id: str | None = Field(None, min_length=1, max_length=128)


class PublishOut(BaseModel):
    approval: ApprovalOut
    freeze: SnapshotOut

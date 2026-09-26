"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class Comparison(Base):
    """候选快照与上一次冻结的比较及审批状态。

    比较结果固定到 base/candidate 两个快照指纹；任一快照内容变化
    都会使该审批失效。comparison_id 由两个指纹派生，天然幂等。
    """

    __tablename__ = "comparisons"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    comparison_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    base_freeze_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    base_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    diff: Mapped[dict] = mapped_column(JSON, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)
    route: Mapped[list] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    published_freeze_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    invalidated_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "plan_version",
            "base_fingerprint",
            "candidate_fingerprint",
            name="uq_comparisons_fingerprints",
        ),
        CheckConstraint(
            "status in ('pending','approved','published','invalid')",
            name="ck_comparisons_status",
        ),
    )


class ComparisonSignature(Base):
    __tablename__ = "comparison_signatures"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    comparison_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    signer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

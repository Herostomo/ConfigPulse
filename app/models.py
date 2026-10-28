from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MonitoredFile(Base):
    __tablename__ = "monitored_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(2048), unique=True, index=True)
    format: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # initializing = existing file whose baseline is not yet stored
    # waiting = registered missing/new file; its appearance is an event
    # active / missing / error / disabled
    status: Mapped[str] = mapped_column(String(32), default="initializing", nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class FileSnapshot(Base):
    __tablename__ = "file_snapshots"

    file_id: Mapped[int] = mapped_column(ForeignKey("monitored_files.id", ondelete="CASCADE"), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_content: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_mtime_ns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("monitored_files.id", ondelete="SET NULL"), nullable=True, index=True)
    file_path: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)


class DeliveryJob(Base):
    __tablename__ = "delivery_jobs"
    __table_args__ = (
        Index("ix_delivery_jobs_due", "status", "next_attempt_at"),
        Index("ix_delivery_jobs_event", "event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    destination: Mapped[str] = mapped_column(String(64), default="webhook", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replay_of_job_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_jobs.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path, PureWindowsPath
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select, text

from app.config import settings
from app.db import SessionLocal, get_engine, init_db
from app.logging_config import configure_logging
from app.metrics import DEAD_LETTER_JOBS, PENDING_JOBS
from app.models import DeliveryJob, Event, FileSnapshot, MonitoredFile, utc_now
from app.parsers import ConfigParseError
from app.processor import FileProcessor

logger = logging.getLogger(__name__)
processor = FileProcessor(settings, SessionLocal)


class WatchRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2048, description="Path relative to CONFIG_ROOT")


class WatchResponse(BaseModel):
    id: int
    path: str
    format: str
    status: str
    enabled: bool


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    settings.config_root.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(
    title="Configuration Change Monitoring System",
    version="1.0.0",
    description="Monitors configuration files and reliably publishes structured change events.",
    lifespan=lifespan,
)


def _relative(path: str) -> str:
    try:
        return Path(path).resolve(strict=False).relative_to(settings.config_root).as_posix()
    except ValueError:
        return Path(path).name


def _validate_api_relative_path(raw: str) -> Path:
    normalized = raw.strip().replace("\\", "/")
    windows = PureWindowsPath(raw)
    candidate = Path(normalized)
    if not normalized or candidate.is_absolute() or windows.is_absolute() or windows.drive:
        raise HTTPException(status_code=400, detail="path must be relative to CONFIG_ROOT")
    if any(part in {"..", ""} for part in normalized.split("/")):
        raise HTTPException(status_code=400, detail="path must not contain traversal segments")
    try:
        resolved = processor.validated_path(candidate)
    except (ValueError, ConfigParseError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return resolved


@app.get("/health")
def health() -> dict[str, str]:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "healthy", "database": "ok"}
    except Exception as exc:
        logger.error("Health check failed", extra={"error_type": type(exc).__name__})
        raise HTTPException(status_code=503, detail={"status": "unhealthy", "database": "unavailable"}) from exc


@app.get("/files")
def list_files() -> dict[str, Any]:
    with SessionLocal() as db:
        records = db.scalars(select(MonitoredFile).order_by(MonitoredFile.path)).all()
        snapshots = {s.file_id: s for s in db.scalars(select(FileSnapshot)).all()}
        return {"files": [
            {
                "id": r.id,
                "path": _relative(r.path),
                "format": r.format,
                "enabled": r.enabled,
                "status": r.status,
                "version": snapshots[r.id].version if r.id in snapshots else None,
                "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
                "last_error": r.last_error,
            }
            for r in records
        ]}


@app.post("/watch", response_model=WatchResponse, status_code=201)
def add_watch(request: WatchRequest):
    path = _validate_api_relative_path(request.path)
    try:
        record = processor.register_path(path)
    except ConfigParseError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Existing files are baselined immediately; future files stay in 'waiting'
    # and produce a configuration.created event when they appear.
    if path.is_file():
        processor.process_file(record.id, force_hash=True)
    with SessionLocal() as db:
        record = db.get(MonitoredFile, record.id)
        return WatchResponse(id=record.id, path=_relative(record.path), format=record.format, status=record.status, enabled=record.enabled)


@app.delete("/watch/{file_id}")
def remove_watch(file_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        record = db.get(MonitoredFile, file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Monitored file not found")
        record.enabled = False
        record.status = "disabled"
        record.updated_at = utc_now()
        db.commit()
        return {"id": record.id, "path": _relative(record.path), "enabled": False, "status": "disabled"}


@app.get("/events")
def list_events(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    file_id: int | None = Query(None, ge=1),
    event_type: str | None = Query(None, max_length=64),
) -> dict[str, Any]:
    with SessionLocal() as db:
        query = select(Event)
        count_query = select(func.count()).select_from(Event)
        if file_id is not None:
            query = query.where(Event.file_id == file_id)
            count_query = count_query.where(Event.file_id == file_id)
        if event_type:
            query = query.where(Event.event_type == event_type)
            count_query = count_query.where(Event.event_type == event_type)
        total = db.scalar(count_query) or 0
        records = db.scalars(query.order_by(Event.created_at.desc()).limit(limit).offset(offset)).all()
        return {"total": total, "limit": limit, "offset": offset, "events": [r.payload for r in records]}


@app.get("/deliveries")
def list_deliveries(
    status: str | None = Query(None, pattern="^(pending|in_progress|delivered|dead_letter)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with SessionLocal() as db:
        query = select(DeliveryJob)
        count_query = select(func.count()).select_from(DeliveryJob)
        if status:
            query = query.where(DeliveryJob.status == status)
            count_query = count_query.where(DeliveryJob.status == status)
        total = db.scalar(count_query) or 0
        jobs = db.scalars(query.order_by(DeliveryJob.created_at.desc()).limit(limit).offset(offset)).all()
        return {"total": total, "deliveries": [
            {"id": j.id, "event_id": j.event_id, "destination": j.destination, "status": j.status,
             "attempt_count": j.attempt_count, "max_attempts": j.max_attempts,
             "next_attempt_at": j.next_attempt_at.isoformat() if j.next_attempt_at else None,
             "last_error": j.last_error, "delivered_at": j.delivered_at.isoformat() if j.delivered_at else None,
             "replay_of_job_id": j.replay_of_job_id}
            for j in jobs
        ]}


@app.post("/events/{event_id}/replay", status_code=202)
def replay_event(event_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        event = db.get(Event, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        latest_job = db.scalar(select(DeliveryJob).where(DeliveryJob.event_id == event_id).order_by(DeliveryJob.id.desc()).limit(1))
        job = DeliveryJob(
            event_id=event_id,
            destination="webhook",
            status="pending",
            attempt_count=0,
            max_attempts=settings.max_delivery_attempts,
            next_attempt_at=utc_now(),
            replay_of_job_id=latest_job.id if latest_job else None,
            created_at=utc_now(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        logger.info("Event replay queued", extra={"event_id": event_id, "job_id": job.id})
        return {"job_id": job.id, "event_id": event_id, "status": job.status, "replay_of_job_id": job.replay_of_job_id}


@app.get("/metrics")
def metrics() -> Response:
    with SessionLocal() as db:
        PENDING_JOBS.set(db.scalar(select(func.count()).select_from(DeliveryJob).where(DeliveryJob.status.in_(["pending", "in_progress"]))) or 0)
        DEAD_LETTER_JOBS.set(db.scalar(select(func.count()).select_from(DeliveryJob).where(DeliveryJob.status == "dead_letter")) or 0)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

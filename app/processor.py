from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings, settings
from app.db import SessionLocal, init_db
from app.diff_engine import compute_diff, has_changes
from app.metrics import CHANGES_DETECTED, PARSE_ERRORS, PROCESSING_SECONDS
from app.models import DeliveryJob, Event, FileSnapshot, MonitoredFile, SystemState, utc_now
from app.parsers import ConfigParseError, infer_format, parse_config
from app.redaction import redact_changes

logger = logging.getLogger(__name__)
SUPPORTED_SUFFIX_NAMES = {".json", ".yaml", ".yml", ".properties"}


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _flatten_empty_safe(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Diff a config against an empty mapping, used by create/delete events."""
    return compute_diff({}, value)


class FileProcessor:
    """Coordinates safe reads, parsing, diffs and atomic DB event recording."""

    def __init__(self, config: Settings = settings, session_factory: sessionmaker = SessionLocal):
        self.config = config
        self.session_factory = session_factory
        self.root = config.config_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._path_locks: dict[str, Lock] = {}
        self._locks_guard = Lock()

    def validated_path(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError("Path must be inside CONFIG_ROOT")
        if resolved == self.root:
            raise ValueError("A file path is required, not the configuration root")
        return resolved

    def relative_path(self, absolute_path: str | Path) -> str:
        return self.validated_path(absolute_path).relative_to(self.root).as_posix()

    def _path_lock(self, path: str) -> Lock:
        with self._locks_guard:
            return self._path_locks.setdefault(path, Lock())

    def find_file(self, path: str | Path) -> MonitoredFile | None:
        resolved = str(self.validated_path(path))
        with self.session_factory() as db:
            return db.scalar(select(MonitoredFile).where(MonitoredFile.path == resolved))

    def register_path(self, path: str | Path, status: str | None = None) -> MonitoredFile:
        resolved = self.validated_path(path)
        file_format = infer_format(resolved)
        exists = resolved.is_file()
        desired_status = status or ("initializing" if exists else "waiting")
        with self.session_factory() as db:
            record = db.scalar(select(MonitoredFile).where(MonitoredFile.path == str(resolved)))
            if record:
                record.enabled = True
                if record.status == "disabled":
                    record.status = desired_status
                db.commit()
                db.refresh(record)
                return record
            record = MonitoredFile(path=str(resolved), format=file_format, enabled=True, status=desired_status)
            db.add(record)
            db.commit()
            db.refresh(record)
            logger.info("Registered configuration file", extra={"file_id": record.id, "file": self.relative_path(resolved), "status": record.status})
            return record

    def bootstrap_inventory(self) -> None:
        """First run baselines pre-existing files; later unknown files emit create events."""
        paths = list(self.iter_supported_files())
        with self.session_factory() as db:
            state = db.get(SystemState, "inventory_initialized")
            first_run = state is None
            existing_paths = set(db.scalars(select(MonitoredFile.path)).all())
            for path in paths:
                stored_path = str(path.resolve())
                if stored_path not in existing_paths:
                    db.add(MonitoredFile(
                        path=stored_path,
                        format=infer_format(path),
                        enabled=True,
                        status="initializing" if first_run else "waiting",
                    ))
            # The inventory rows and bootstrap marker commit together. If this
            # transaction fails, the next startup can safely repeat the scan.
            if state is None:
                db.add(SystemState(key="inventory_initialized", value="true"))
            db.commit()
        logger.info("Initial inventory scan completed", extra={"files_seen": len(paths), "first_run": first_run})

    def discover_new_files(self) -> None:
        """Bulk-register newly discovered paths without one DB lookup per file."""
        paths = list(self.iter_supported_files())
        with self.session_factory() as db:
            existing_paths = set(db.scalars(select(MonitoredFile.path)).all())
            added = 0
            for path in paths:
                stored_path = str(path.resolve())
                if stored_path not in existing_paths:
                    db.add(MonitoredFile(
                        path=stored_path,
                        format=infer_format(path),
                        enabled=True,
                        status="waiting",
                    ))
                    added += 1
            if added:
                db.commit()
        if added:
            logger.info("New configuration files registered", extra={"files_added": added})

    def iter_supported_files(self):
        ignored_parts = {".git", ".venv", "venv", "__pycache__", "node_modules"}
        for path in self.root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                if any(part in ignored_parts for part in path.relative_to(self.root).parts):
                    continue
                self.validated_path(path)
                infer_format(path)
                yield path.resolve()
            except (ValueError, ConfigParseError):
                continue

    def list_enabled_ids(self) -> list[int]:
        with self.session_factory() as db:
            return list(db.scalars(select(MonitoredFile.id).where(MonitoredFile.enabled.is_(True))).all())

    def process_file(self, file_id: int, *, force_hash: bool = True) -> None:
        start_time = time.monotonic()
        try:
            # Resolve current path and serialize same-file processing in this process.
            with self.session_factory() as db:
                initial = db.get(MonitoredFile, file_id)
                if initial is None or not initial.enabled:
                    return
                stored_path = initial.path
            try:
                path = self.validated_path(stored_path)
            except ValueError as exc:
                logger.error("Monitored path became unsafe; disabling it", extra={"file_id": file_id, "reason": str(exc)})
                with self.session_factory() as db:
                    rec = db.get(MonitoredFile, file_id)
                    if rec:
                        rec.enabled = False
                        rec.status = "error"
                        rec.last_error = "Monitored path resolved outside CONFIG_ROOT"
                        db.commit()
                return

            with self._path_lock(str(path)):
                self._process_locked(file_id, path, force_hash=force_hash)
        finally:
            PROCESSING_SECONDS.observe(time.monotonic() - start_time)

    def _process_locked(self, file_id: int, path: Path, *, force_hash: bool) -> None:
        with self.session_factory() as db:
            # A row lock serializes multiple watcher processes for the same file on PostgreSQL.
            file_record = db.scalar(select(MonitoredFile).where(MonitoredFile.id == file_id).with_for_update())
            if file_record is None or not file_record.enabled:
                return
            previous_status = file_record.status
            snapshot = db.get(FileSnapshot, file_id)

            if not path.exists() or not path.is_file():
                if snapshot is not None and previous_status not in {"missing", "disabled"}:
                    changes = compute_diff(snapshot.parsed_content, {})
                    self._create_event_and_job(db, file_record, "configuration.deleted", changes)
                    file_record.status = "missing"
                    file_record.last_error = None
                    file_record.updated_at = utc_now()
                    db.commit()
                    CHANGES_DETECTED.labels(event_type="configuration.deleted").inc()
                    logger.info("Configuration file deleted", extra={"file_id": file_id, "file": self.relative_path(path)})
                elif snapshot is None and previous_status == "initializing":
                    file_record.status = "waiting"
                    file_record.updated_at = utc_now()
                    db.commit()
                return

            try:
                # Prevent a symlink introduced after registration from escaping the root.
                if path.is_symlink() or not self.root in path.resolve(strict=True).parents:
                    raise OSError("Symlinks and paths outside CONFIG_ROOT are not allowed")
                stat_before = path.stat()
            except OSError as exc:
                self._record_read_error(db, file_record, exc)
                return

            if (
                not force_hash
                and snapshot is not None
                and previous_status == "active"
                and snapshot.last_mtime_ns == stat_before.st_mtime_ns
                and snapshot.last_size == stat_before.st_size
            ):
                file_record.last_seen_at = utc_now()
                db.commit()
                return

            try:
                raw, stable_stat = self._read_stable_file(path)
                content = raw.decode("utf-8-sig")
                parsed = parse_config(path, content)
            except (OSError, UnicodeDecodeError, ConfigParseError) as exc:
                self._record_read_error(db, file_record, exc)
                return

            content_hash = hashlib.sha256(raw).hexdigest()
            snapshot = db.get(FileSnapshot, file_id)
            previous_status = file_record.status
            if snapshot is not None and snapshot.content_hash == content_hash and previous_status == "active":
                file_record.last_seen_at = utc_now()
                file_record.last_error = None
                file_record.updated_at = utc_now()
                snapshot.last_mtime_ns = stable_stat.st_mtime_ns
                snapshot.last_size = stable_stat.st_size
                db.commit()
                return

            event_type: str | None = None
            changes: dict[str, dict[str, Any]] = {"added": {}, "updated": {}, "removed": {}, "type_changes": {}}
            if snapshot is None:
                if previous_status == "waiting":
                    event_type = "configuration.created"
                    changes = _flatten_empty_safe(parsed)
            else:
                changes = compute_diff(snapshot.parsed_content, parsed)
                if previous_status == "missing":
                    event_type = "configuration.created"
                elif has_changes(changes):
                    event_type = "configuration.changed"

            new_version = 1 if snapshot is None else snapshot.version + (snapshot.content_hash != content_hash)
            if snapshot is None:
                snapshot = FileSnapshot(
                    file_id=file_id,
                    content_hash=content_hash,
                    parsed_content=parsed,
                    version=int(new_version),
                    last_mtime_ns=stable_stat.st_mtime_ns,
                    last_size=stable_stat.st_size,
                    updated_at=utc_now(),
                )
                db.add(snapshot)
            else:
                snapshot.content_hash = content_hash
                snapshot.parsed_content = parsed
                snapshot.version = int(new_version)
                snapshot.last_mtime_ns = stable_stat.st_mtime_ns
                snapshot.last_size = stable_stat.st_size
                snapshot.updated_at = utc_now()

            file_record.status = "active"
            file_record.last_seen_at = utc_now()
            file_record.last_error = None
            file_record.updated_at = utc_now()
            if event_type is not None:
                self._create_event_and_job(db, file_record, event_type, changes)
            db.commit()
            if event_type is not None:
                CHANGES_DETECTED.labels(event_type=event_type).inc()
                logger.info("Configuration event recorded", extra={"file_id": file_id, "file": self.relative_path(path), "event_type": event_type})

    def _read_stable_file(self, path: Path, retries: int = 4, delay: float = 0.08):
        last_problem: Exception | None = None
        for attempt in range(retries):
            try:
                before = path.stat()
                with path.open("rb") as stream:
                    content = stream.read()
                after = path.stat()
                if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
                    return content, after
                last_problem = OSError("File changed while being read")
            except OSError as exc:
                last_problem = exc
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
        raise OSError(f"Could not read a stable file after {retries} attempts: {last_problem}")

    def _record_read_error(self, db, file_record: MonitoredFile, exc: Exception) -> None:
        file_record.status = "error"
        # Parser exceptions can include a source-line excerpt; never persist that
        # text because configuration lines may contain credentials.
        file_record.last_error = f"{type(exc).__name__}: configuration read/parse failed"
        file_record.updated_at = utc_now()
        db.commit()
        PARSE_ERRORS.inc()
        logger.warning("Unable to read or parse configuration; retaining last valid snapshot", extra={"file_id": file_record.id, "error_type": type(exc).__name__})

    def _create_event_and_job(self, db, file_record: MonitoredFile, event_type: str, changes: dict[str, dict[str, Any]]) -> Event:
        event_id = str(uuid.uuid4())
        created_at = utc_now()
        try:
            relative = Path(file_record.path).resolve(strict=False).relative_to(self.root).as_posix()
        except ValueError:
            relative = Path(file_record.path).name
        payload = {
            "event_id": event_id,
            "schema_version": 1,
            "event_type": event_type,
            "file": relative,
            "timestamp": _iso_utc(created_at),
            "changes": redact_changes(changes),
        }
        event = Event(
            id=event_id,
            file_id=file_record.id,
            file_path=relative,
            event_type=event_type,
            schema_version=1,
            payload=payload,
            created_at=created_at,
        )
        db.add(event)
        db.flush()
        db.add(DeliveryJob(
            event_id=event_id,
            destination="webhook",
            status="pending",
            attempt_count=0,
            max_attempts=self.config.max_delivery_attempts,
            next_attempt_at=created_at,
            created_at=created_at,
        ))
        return event

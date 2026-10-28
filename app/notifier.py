from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings, settings
from app.db import SessionLocal, init_db
from app.metrics import DEAD_LETTER_JOBS, DELIVERY_ATTEMPTS, DELIVERY_SECONDS, PENDING_JOBS
from app.models import DeliveryJob, Event, utc_now

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class DeliveryWorker:
    """PostgreSQL-backed at-least-once webhook delivery worker."""

    def __init__(self, config: Settings = settings, session_factory: sessionmaker = SessionLocal):
        self.config = config
        self.session_factory = session_factory
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _claim_one(self) -> tuple[int, dict] | None:
        now = utc_now()
        with self.session_factory() as db:
            due_pending = (DeliveryJob.status == "pending") & (DeliveryJob.next_attempt_at <= now)
            abandoned = (DeliveryJob.status == "in_progress") & (DeliveryJob.locked_until.is_not(None)) & (DeliveryJob.locked_until <= now)
            job = db.scalar(
                select(DeliveryJob)
                .where(or_(due_pending, abandoned))
                .order_by(DeliveryJob.next_attempt_at, DeliveryJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                self._update_queue_gauges(db)
                return None
            event = db.get(Event, job.event_id)
            if event is None:
                job.status = "dead_letter"
                job.last_error = "Referenced event does not exist"
                job.locked_until = None
                db.commit()
                return None
            job.status = "in_progress"
            job.attempt_count += 1
            job.locked_until = now + timedelta(seconds=max(self.config.job_lease_seconds, int(self.config.webhook_timeout_seconds) + 10))
            db.commit()
            return job.id, event.payload

    def deliver_one(self) -> bool:
        claimed = self._claim_one()
        if claimed is None:
            return False
        job_id, payload = claimed
        event_id = str(payload.get("event_id", ""))
        started = time.monotonic()
        outcome = "failure"
        failure: str | None = None
        retry_after: float | None = None
        permanent = False
        try:
            headers = {
                "Content-Type": "application/json",
                "X-Config-Monitor-Event-Id": event_id,
                "Idempotency-Key": event_id,
            }
            if self.config.webhook_bearer_token:
                headers["Authorization"] = f"Bearer {self.config.webhook_bearer_token}"
            with httpx.Client(timeout=self.config.webhook_timeout_seconds, follow_redirects=False) as client:
                response = client.post(self.config.webhook_url, json=payload, headers=headers)
            if 200 <= response.status_code < 300:
                self._finish_success(job_id)
                outcome = "success"
                logger.info("Webhook event delivered", extra={"job_id": job_id, "event_id": event_id, "status_code": response.status_code})
            else:
                failure = f"HTTP {response.status_code} from webhook"
                # 408, 425, 429 and 5xx are usually transient. Other responses
                # are treated as permanent payload/auth/configuration failures.
                permanent = response.status_code not in {408, 425, 429} and response.status_code < 500
                if response.status_code == 429:
                    retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
                self._finish_failure(job_id, failure, permanent=permanent, retry_after=retry_after)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            failure = type(exc).__name__
            self._finish_failure(job_id, failure, permanent=False)
        except Exception as exc:
            failure = f"Unexpected delivery error: {type(exc).__name__}"
            self._finish_failure(job_id, failure, permanent=False)
            logger.exception("Unexpected webhook delivery error", extra={"job_id": job_id, "event_id": event_id})
        finally:
            DELIVERY_SECONDS.observe(time.monotonic() - started)
            DELIVERY_ATTEMPTS.labels(outcome=outcome).inc()
            self._refresh_gauges()
        return True

    @staticmethod
    def _parse_retry_after(raw: str | None) -> float | None:
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _finish_success(self, job_id: int) -> None:
        with self.session_factory() as db:
            job = db.get(DeliveryJob, job_id)
            if job is None:
                return
            job.status = "delivered"
            job.delivered_at = utc_now()
            job.locked_until = None
            job.last_error = None
            db.commit()

    def _finish_failure(self, job_id: int, error: str, *, permanent: bool, retry_after: float | None = None) -> None:
        with self.session_factory() as db:
            job = db.get(DeliveryJob, job_id)
            if job is None:
                return
            job.last_error = error[:1000]
            job.locked_until = None
            if permanent or job.attempt_count >= job.max_attempts:
                job.status = "dead_letter"
                logger.error("Delivery job moved to dead letter", extra={"job_id": job.id, "event_id": job.event_id, "attempt_count": job.attempt_count, "error_type": error.split(":", 1)[0]})
            else:
                exponential = min(self.config.retry_max_seconds, self.config.retry_base_seconds * (2 ** max(0, job.attempt_count - 1)))
                delay = retry_after if retry_after is not None else exponential * random.uniform(0.8, 1.2)
                delay = min(max(0.0, delay), self.config.retry_max_seconds)
                job.status = "pending"
                job.next_attempt_at = utc_now() + timedelta(seconds=delay)
                logger.warning("Webhook delivery failed; retry scheduled", extra={"job_id": job.id, "event_id": job.event_id, "attempt_count": job.attempt_count, "retry_in_seconds": round(delay, 2), "error_type": error.split(":", 1)[0]})
            db.commit()

    def _update_queue_gauges(self, db) -> None:
        PENDING_JOBS.set(db.scalar(select(func.count()).select_from(DeliveryJob).where(DeliveryJob.status.in_(["pending", "in_progress"]))) or 0)
        DEAD_LETTER_JOBS.set(db.scalar(select(func.count()).select_from(DeliveryJob).where(DeliveryJob.status == "dead_letter")) or 0)

    def _refresh_gauges(self) -> None:
        with self.session_factory() as db:
            self._update_queue_gauges(db)

    def run_forever(self) -> None:
        init_db()
        logger.info("Notification worker started", extra={"webhook_url_configured": bool(self.config.webhook_url)})
        while not self._stop.is_set():
            try:
                did_work = self.deliver_one()
                if not did_work:
                    self._stop.wait(self.config.worker_poll_seconds)
            except Exception:
                logger.exception("Worker iteration failed; will continue")
                self._stop.wait(self.config.worker_poll_seconds)

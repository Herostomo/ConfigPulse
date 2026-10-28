from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import DeliveryJob, Event, MonitoredFile
from app.notifier import DeliveryWorker
from app.processor import FileProcessor


class CaptureHandler(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).received.append(payload)
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, fmt, *args):
        return


def test_file_change_event_is_persisted_and_delivered(tmp_path):
    config_root = tmp_path / "configs"
    config_root.mkdir()
    config_file = config_root / "app.json"
    config_file.write_text('{"database":{"host":"localhost"},"debug":false}', encoding="utf-8")

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite://",
        config_root=config_root,
        webhook_url="http://127.0.0.1:1/not-used-yet",
        webhook_bearer_token=None,
        poll_interval_seconds=1,
        debounce_seconds=0.1,
        full_hash_interval_seconds=10,
        worker_poll_seconds=0.1,
        webhook_timeout_seconds=2,
        max_delivery_attempts=3,
        retry_base_seconds=0.1,
        retry_max_seconds=1,
        job_lease_seconds=10,
        log_level="ERROR",
    )

    with session_factory() as db:
        tracked = MonitoredFile(path=str(config_file.resolve()), format="json", enabled=True, status="initializing")
        db.add(tracked)
        db.commit()
        db.refresh(tracked)
        file_id = tracked.id

    processor = FileProcessor(settings, session_factory)
    processor.process_file(file_id, force_hash=True)  # baseline; should not create an event
    config_file.write_text('{"database":{"host":"production-db"},"debug":true}', encoding="utf-8")
    processor.process_file(file_id, force_hash=True)

    with session_factory() as db:
        events = db.scalars(select(Event)).all()
        jobs = db.scalars(select(DeliveryJob)).all()
        assert len(events) == 1
        assert len(jobs) == 1
        event_id = events[0].id
        assert events[0].payload["changes"]["updated"]["database.host"] == {"old": "localhost", "new": "production-db"}

    CaptureHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        worker_settings = Settings(
            database_url=settings.database_url,
            config_root=settings.config_root,
            webhook_url=f"http://127.0.0.1:{server.server_port}/webhook",
            webhook_bearer_token=None,
            poll_interval_seconds=1,
            debounce_seconds=0.1,
            full_hash_interval_seconds=10,
            worker_poll_seconds=0.1,
            webhook_timeout_seconds=2,
            max_delivery_attempts=3,
            retry_base_seconds=0.1,
            retry_max_seconds=1,
            job_lease_seconds=10,
            log_level="ERROR",
        )
        worker = DeliveryWorker(worker_settings, session_factory)
        assert worker.deliver_one() is True
        assert len(CaptureHandler.received) == 1
        assert CaptureHandler.received[0]["event_id"] == event_id
        with session_factory() as db:
            job = db.scalar(select(DeliveryJob).where(DeliveryJob.event_id == event_id))
            assert job.status == "delivered"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        engine.dispose()

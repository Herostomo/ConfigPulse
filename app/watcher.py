from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.config import Settings, settings
from app.db import init_db
from app.logging_config import configure_logging
from app.processor import FileProcessor
from app.parsers import ConfigParseError, infer_format

logger = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, service: "WatcherService"):
        self.service = service

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.service.enqueue(getattr(event, "src_path", ""), "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.service.enqueue(getattr(event, "src_path", ""), "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.service.enqueue(getattr(event, "src_path", ""), "deleted")

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self.service.enqueue(getattr(event, "src_path", ""), "deleted")
        self.service.enqueue(getattr(event, "dest_path", ""), "created")


class WatcherService:
    def __init__(self, config: Settings = settings, processor: FileProcessor | None = None):
        self.config = config
        self.processor = processor or FileProcessor(config)
        self.root = config.config_root.resolve()
        self._pending: dict[str, tuple[float, str]] = {}
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._observer: Observer | None = None

    def enqueue(self, raw_path: str, kind: str) -> None:
        if not raw_path:
            return
        try:
            path = self.processor.validated_path(raw_path)
            infer_format(path)
        except (ValueError, ConfigParseError):
            return
        key = str(path)
        now = time.monotonic()
        with self._pending_lock:
            previous = self._pending.get(key)
            chosen_kind = kind
            if previous:
                _, old_kind = previous
                if kind == "deleted":
                    chosen_kind = "deleted"
                elif kind == "created":
                    chosen_kind = "created"
                elif old_kind in {"created", "deleted"}:
                    chosen_kind = "created" if path.exists() else old_kind
            self._pending[key] = (now, chosen_kind)

    def _drain_debounced(self) -> None:
        now = time.monotonic()
        ready: list[tuple[str, str]] = []
        with self._pending_lock:
            for path, (last_event, kind) in list(self._pending.items()):
                if now - last_event >= self.config.debounce_seconds:
                    ready.append((path, kind))
                    del self._pending[path]
        for path_string, kind in ready:
            try:
                path = self.processor.validated_path(path_string)
                record = self.processor.find_file(path)
                if kind == "deleted" and record is None:
                    continue
                if record is None:
                    # Any supported file newly appearing under CONFIG_ROOT is monitored.
                    record = self.processor.register_path(path, status="waiting")
                if record.enabled:
                    self.processor.process_file(record.id, force_hash=True)
            except (ValueError, ConfigParseError) as exc:
                logger.warning("Ignoring unsafe or unsupported file event", extra={"reason": str(exc)})
            except Exception:
                logger.exception("Failed to process a debounced file event", extra={"path": Path(path_string).name})

    def _reconcile(self, force_hash: bool = False) -> None:
        self.processor.discover_new_files()
        for file_id in self.processor.list_enabled_ids():
            try:
                self.processor.process_file(file_id, force_hash=force_hash)
            except Exception:
                logger.exception("Reconciliation failed for monitored file", extra={"file_id": file_id})

    def run_forever(self) -> None:
        configure_logging()
        self.root.mkdir(parents=True, exist_ok=True)
        init_db()
        self.processor.bootstrap_inventory()
        self._reconcile(force_hash=True)
        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(self.root), recursive=True)
        self._observer.start()
        logger.info("File watcher started", extra={"config_root": str(self.root), "poll_interval_seconds": self.config.poll_interval_seconds})
        last_reconcile = time.monotonic()
        last_full_hash = last_reconcile
        try:
            while not self._stop.is_set():
                self._drain_debounced()
                now = time.monotonic()
                if now - last_reconcile >= self.config.poll_interval_seconds:
                    force_hash = now - last_full_hash >= self.config.full_hash_interval_seconds
                    self._reconcile(force_hash=force_hash)
                    last_reconcile = now
                    if force_hash:
                        last_full_hash = now
                self._stop.wait(0.05)
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        finally:
            self._stop.set()
            self._observer.stop()
            self._observer.join(timeout=5)
            logger.info("File watcher stopped")

    def stop(self) -> None:
        self._stop.set()

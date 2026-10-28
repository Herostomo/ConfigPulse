from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.config import settings


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per log record without serializing secrets by default."""

    RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                if key in {"args", "exc_info", "exc_text", "stack_info", "msg", "name", "levelname", "levelno", "pathname", "filename", "module", "lineno", "funcName", "created", "msecs", "relativeCreated", "thread", "threadName", "processName", "process"}:
                    continue
                if isinstance(value, (str, int, float, bool)) or value is None:
                    payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_config_monitor_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    root._config_monitor_configured = True  # type: ignore[attr-defined]

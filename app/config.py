from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load the project's .env when commands are run from the project directory.
load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    database_url: str
    config_root: Path
    webhook_url: str
    webhook_bearer_token: str | None = None
    poll_interval_seconds: float = 5.0
    debounce_seconds: float = 0.4
    full_hash_interval_seconds: float = 60.0
    worker_poll_seconds: float = 1.0
    webhook_timeout_seconds: float = 5.0
    max_delivery_attempts: int = 5
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 60.0
    job_lease_seconds: int = 30
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(os.getenv("CONFIG_ROOT", "./config_samples")).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        return cls(
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql+psycopg://config_monitor:config_monitor@localhost:5432/config_monitor",
            ),
            config_root=root.resolve(),
            webhook_url=os.getenv("WEBHOOK_URL", "http://127.0.0.1:9001/webhook"),
            webhook_bearer_token=os.getenv("WEBHOOK_BEARER_TOKEN") or None,
            poll_interval_seconds=max(0.25, _float("POLL_INTERVAL_SECONDS", 5.0)),
            debounce_seconds=max(0.05, _float("DEBOUNCE_SECONDS", 0.4)),
            full_hash_interval_seconds=max(1.0, _float("FULL_HASH_INTERVAL_SECONDS", 60.0)),
            worker_poll_seconds=max(0.1, _float("WORKER_POLL_SECONDS", 1.0)),
            webhook_timeout_seconds=max(0.5, _float("WEBHOOK_TIMEOUT_SECONDS", 5.0)),
            max_delivery_attempts=max(1, _int("MAX_DELIVERY_ATTEMPTS", 5)),
            retry_base_seconds=max(0.1, _float("RETRY_BASE_SECONDS", 1.0)),
            retry_max_seconds=max(1.0, _float("RETRY_MAX_SECONDS", 60.0)),
            job_lease_seconds=max(5, _int("JOB_LEASE_SECONDS", 30)),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


settings = Settings.from_env()

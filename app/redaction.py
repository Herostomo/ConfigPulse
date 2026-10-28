from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY = re.compile(r"(password|passwd|passphrase|secret|token|api[_-]?key|authorization|credential|private[_-]?key)", re.IGNORECASE)
REDACTED = "[REDACTED]"


def is_sensitive_path(path: str) -> bool:
    return any(SENSITIVE_KEY.search(part) for part in re.split(r"[.\[\]/]+", path) if part)


def redact_changes(changes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Redact sensitive values before events are persisted or delivered."""
    redacted: dict[str, dict[str, Any]] = {kind: {} for kind in changes}
    for kind, values in changes.items():
        for path, value in values.items():
            if kind == "type_changes":
                redacted[kind][path] = value
            elif is_sensitive_path(path):
                if kind == "updated":
                    redacted[kind][path] = {"old": REDACTED, "new": REDACTED}
                else:
                    redacted[kind][path] = REDACTED
            else:
                redacted[kind][path] = value
    return redacted

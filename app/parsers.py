from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values


class ConfigParseError(ValueError):
    """Raised when a configuration file cannot be parsed safely."""


def infer_format(path: str | Path) -> str:
    p = Path(path)
    name = p.name.lower()
    if name == ".env" or name.startswith(".env."):
        return "env"
    suffix = p.suffix.lower()
    mapping = {".json": "json", ".yaml": "yaml", ".yml": "yaml", ".properties": "properties"}
    if suffix not in mapping:
        raise ConfigParseError(f"Unsupported configuration file type: {p.name}")
    return mapping[suffix]


def _require_mapping(value: Any, source_format: str) -> dict[str, Any]:
    if value is None and source_format == "yaml":
        return {}
    if not isinstance(value, dict):
        raise ConfigParseError(f"The root of a {source_format} configuration must be an object/mapping")
    return value


def parse_config(path: str | Path, content: str) -> dict[str, Any]:
    """Parse supported config syntax to a common dictionary representation."""
    source_format = infer_format(path)
    try:
        if source_format == "json":
            parsed = json.loads(content)
        elif source_format == "yaml":
            parsed = yaml.safe_load(content)
        elif source_format == "env":
            parsed = {str(k): ("" if v is None else str(v)) for k, v in dotenv_values(stream=StringIO(content), interpolate=False).items()}
        elif source_format == "properties":
            parsed: dict[str, Any] = {}
            for line_number, raw_line in enumerate(content.splitlines(), start=1):
                line = raw_line.strip()
                if not line or line.startswith(("#", "!")):
                    continue
                # This intentionally supports the common key=value / key:value subset.
                separator_positions = [i for i in (line.find("="), line.find(":")) if i >= 0]
                if not separator_positions:
                    key, value = line, ""
                else:
                    position = min(separator_positions)
                    key, value = line[:position].strip(), line[position + 1 :].strip()
                if not key:
                    raise ConfigParseError(f"Empty property key on line {line_number}")
                parsed[key] = value
        else:  # Defensive; infer_format should reject this first.
            raise ConfigParseError(f"Unsupported format: {source_format}")
    except ConfigParseError:
        raise
    except Exception as exc:
        raise ConfigParseError(f"Invalid {source_format} configuration: {exc}") from exc
    return _require_mapping(parsed, source_format)

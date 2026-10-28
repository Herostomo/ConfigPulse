from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _flatten_value(value: Any, path: str, output: dict[str, Any]) -> None:
    if isinstance(value, Mapping) and value:
        for key in sorted(value, key=str):
            child_path = f"{path}.{key}" if path else str(key)
            _flatten_value(value[key], child_path, output)
    else:
        output[path] = value


def _record_added(value: Any, path: str, added: dict[str, Any]) -> None:
    _flatten_value(value, path, added)


def compute_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return added/removed/updated/type_changes with dot-separated paths.

    Nested mappings are compared recursively. Arrays are treated as atomic values
    and reported as an update when their content differs.
    """
    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}
    updated: dict[str, Any] = {}
    type_changes: dict[str, Any] = {}

    def compare(old_value: Any, new_value: Any, path: str) -> None:
        old_type, new_type = value_type(old_value), value_type(new_value)
        if old_type != new_type:
            type_changes[path] = {"oldType": old_type, "newType": new_type}
            return
        if isinstance(old_value, Mapping) and isinstance(new_value, Mapping):
            old_keys, new_keys = set(old_value), set(new_value)
            for key in sorted(old_keys - new_keys, key=str):
                child_path = f"{path}.{key}" if path else str(key)
                _flatten_value(old_value[key], child_path, removed)
            for key in sorted(new_keys - old_keys, key=str):
                child_path = f"{path}.{key}" if path else str(key)
                _record_added(new_value[key], child_path, added)
            for key in sorted(old_keys & new_keys, key=str):
                child_path = f"{path}.{key}" if path else str(key)
                compare(old_value[key], new_value[key], child_path)
            return
        if old_value != new_value:
            updated[path] = {"old": old_value, "new": new_value}

    compare(old, new, "")
    return {"added": added, "updated": updated, "removed": removed, "type_changes": type_changes}


def has_changes(changes: dict[str, dict[str, Any]]) -> bool:
    return any(bool(items) for items in changes.values())

from app.redaction import redact_changes


def test_sensitive_values_are_redacted():
    changes = {
        "added": {"database.password": "pw-123", "auth.api_key": "key-123", "region": "local"},
        "updated": {"service.token": {"old": "old-secret", "new": "new-secret"}},
        "removed": {"private_key": "old-private"},
        "type_changes": {"featureFlags": {"oldType": "string", "newType": "object"}},
    }
    result = redact_changes(changes)
    assert result["added"]["database.password"] == "[REDACTED]"
    assert result["added"]["auth.api_key"] == "[REDACTED]"
    assert result["added"]["region"] == "local"
    assert result["updated"]["service.token"] == {"old": "[REDACTED]", "new": "[REDACTED]"}
    assert result["removed"]["private_key"] == "[REDACTED]"
    assert result["type_changes"] == changes["type_changes"]

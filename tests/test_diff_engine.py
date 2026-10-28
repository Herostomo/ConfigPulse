from app.diff_engine import compute_diff, has_changes


def test_added_removed_and_updated_values():
    old = {"app": {"debug": False, "name": "demo"}, "cache": {"enabled": True}}
    new = {"app": {"debug": True, "name": "demo", "version": "2"}, "database": {"host": "localhost"}}
    result = compute_diff(old, new)
    assert result["added"] == {"app.version": "2", "database.host": "localhost"}
    assert result["updated"] == {"app.debug": {"old": False, "new": True}}
    assert result["removed"] == {"cache.enabled": True}
    assert result["type_changes"] == {}
    assert has_changes(result)


def test_nested_type_change_is_reported_at_parent():
    result = compute_diff({"featureFlags": "enabled"}, {"featureFlags": {"payments": True}})
    assert result["type_changes"] == {"featureFlags": {"oldType": "string", "newType": "object"}}
    assert not result["added"]
    assert not result["updated"]


def test_no_changes():
    result = compute_diff({"db": {"port": 5432}}, {"db": {"port": 5432}})
    assert not has_changes(result)


def test_array_is_treated_as_atomic_value():
    result = compute_diff({"allowed": ["a", "b"]}, {"allowed": ["a", "c"]})
    assert result["updated"]["allowed"] == {"old": ["a", "b"], "new": ["a", "c"]}


def test_empty_object_addition_is_not_lost():
    result = compute_diff({}, {"feature": {}})
    assert result["added"] == {"feature": {}}

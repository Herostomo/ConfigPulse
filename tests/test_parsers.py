import pytest

from app.parsers import ConfigParseError, infer_format, parse_config


def test_parse_json_nested_object(tmp_path):
    path = tmp_path / "app.json"
    assert parse_config(path, '{"db":{"host":"localhost"},"port":5432}') == {"db": {"host": "localhost"}, "port": 5432}


def test_parse_yaml_nested_object(tmp_path):
    path = tmp_path / "app.yaml"
    assert parse_config(path, "database:\n  host: localhost\n  port: 5432\n") == {"database": {"host": "localhost", "port": 5432}}


def test_parse_env_values_are_strings(tmp_path):
    path = tmp_path / ".env"
    assert parse_config(path, "APP_MODE=dev\nFEATURE_X=true\n") == {"APP_MODE": "dev", "FEATURE_X": "true"}


def test_parse_properties(tmp_path):
    path = tmp_path / "app.properties"
    assert parse_config(path, "database.host=localhost\ndatabase.port:5432\n# comment\n") == {"database.host": "localhost", "database.port": "5432"}


def test_invalid_json_rejected(tmp_path):
    with pytest.raises(ConfigParseError):
        parse_config(tmp_path / "app.json", '{"unfinished":')


def test_root_must_be_mapping(tmp_path):
    with pytest.raises(ConfigParseError):
        parse_config(tmp_path / "app.json", '[1,2,3]')


def test_unsupported_extension_rejected(tmp_path):
    with pytest.raises(ConfigParseError):
        infer_format(tmp_path / "notes.txt")

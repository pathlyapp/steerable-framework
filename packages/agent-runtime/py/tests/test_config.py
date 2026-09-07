"""Layered user config: merge order, provenance, fail-loud on a bad file."""

from __future__ import annotations

from pathlib import Path

import pytest

from steerable_agent_runtime import (
    ConfigError,
    load_user_config,
    resolve_config,
)

DEFAULTS = {"log_level": "INFO", "grace_period_seconds": 5.0, "storage_path": None}


def test_defaults_only_when_no_file_no_env(tmp_path: Path) -> None:
    resolved = resolve_config(
        dict(DEFAULTS), config_path=tmp_path / "absent.json", environ={}
    )
    assert resolved.values == DEFAULTS
    assert all(s == "default" for s in resolved.sources.values())


def test_user_file_overrides_defaults(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"log_level": "DEBUG", "extra_key": 1}', encoding="utf-8")
    resolved = resolve_config(dict(DEFAULTS), config_path=cfg, environ={})
    assert resolved.values["log_level"] == "DEBUG"
    assert resolved.sources["log_level"] == "file"
    # The file may add keys the defaults did not declare.
    assert resolved.values["extra_key"] == 1
    assert resolved.sources["extra_key"] == "file"
    # Untouched keys stay at default.
    assert resolved.values["grace_period_seconds"] == 5.0


def test_env_beats_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"log_level": "DEBUG"}', encoding="utf-8")
    resolved = resolve_config(
        dict(DEFAULTS), config_path=cfg, environ={"STEERABLE_LOG_LEVEL": "WARNING"}
    )
    assert resolved.values["log_level"] == "WARNING"
    assert resolved.sources["log_level"] == "env"


def test_override_beats_env_and_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"log_level": "DEBUG"}', encoding="utf-8")
    resolved = resolve_config(
        dict(DEFAULTS),
        config_path=cfg,
        environ={"STEERABLE_LOG_LEVEL": "WARNING"},
        overrides={"log_level": "ERROR"},
    )
    assert resolved.values["log_level"] == "ERROR"
    assert resolved.sources["log_level"] == "override"


def test_env_only_resolves_declared_keys(tmp_path: Path) -> None:
    """The env layer cannot invent keys the caller did not declare."""
    resolved = resolve_config(
        dict(DEFAULTS),
        config_path=tmp_path / "absent.json",
        environ={"STEERABLE_UNKNOWN_KEY": "x", "STEERABLE_LOG_LEVEL": "DEBUG"},
    )
    assert "unknown_key" not in resolved.values
    assert resolved.values["log_level"] == "DEBUG"


def test_describe_reports_value_and_source(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"log_level": "DEBUG"}', encoding="utf-8")
    resolved = resolve_config(dict(DEFAULTS), config_path=cfg, environ={})
    described = resolved.describe()
    assert described["log_level"] == {"value": "DEBUG", "source": "file"}
    assert described["grace_period_seconds"] == {"value": 5.0, "source": "default"}


def test_malformed_file_fails_loud(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="config.json"):
        load_user_config(cfg)


def test_non_object_file_fails_loud(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('["a", "b"]', encoding="utf-8")
    with pytest.raises(ConfigError, match="top level must be a JSON object"):
        load_user_config(cfg)

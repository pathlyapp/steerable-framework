"""Layered user config: merge order, provenance, fail-loud on a bad file."""

from __future__ import annotations

import json
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


class TestSchemaEnforcement:
    """The defaults dict is the schema: a mistyped value at any layer fails
    loud naming the key, the source, and the expected type."""

    def test_file_value_with_the_wrong_type_fails_loud(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"max_tokens": "sixty-thousand"}), encoding="utf-8")
        with pytest.raises(ConfigError, match="'max_tokens'.*file.*integer"):
            resolve_config({"max_tokens": 60_000}, config_path=cfg, environ={})

    def test_env_strings_coerce_to_the_declared_type(self) -> None:
        resolved = resolve_config(
            {"max_tokens": 60_000, "debug": False, "ratio": 0.8},
            config_path=Path("/absent"),
            environ={
                "STEERABLE_MAX_TOKENS": "120000",
                "STEERABLE_DEBUG": "true",
                "STEERABLE_RATIO": "0.5",
            },
        )
        assert resolved.values == {"max_tokens": 120_000, "debug": True, "ratio": 0.5}
        assert resolved.sources["max_tokens"] == "env"

    def test_env_string_with_the_wrong_type_fails_loud(self) -> None:
        with pytest.raises(ConfigError, match="'max_tokens'.*env.*integer"):
            resolve_config(
                {"max_tokens": 60_000},
                config_path=Path("/absent"),
                environ={"STEERABLE_MAX_TOKENS": "lots"},
            )

    def test_list_keys_parse_json_from_env(self) -> None:
        resolved = resolve_config(
            {"blocked_hosts": []},
            config_path=Path("/absent"),
            environ={"STEERABLE_BLOCKED_HOSTS": '["evil.example"]'},
        )
        assert resolved.values["blocked_hosts"] == ["evil.example"]

    def test_undeclared_file_keys_pass_through_unvalidated(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"future_key": "anything"}), encoding="utf-8")
        resolved = resolve_config({"known": 1}, config_path=cfg, environ={})
        assert resolved.values["future_key"] == "anything"


class TestProfiles:
    def test_profile_layer_sits_between_file_and_env(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({
                "model": "base-model",
                "profiles": {"work": {"model": "work-model", "timeout": 30}},
            }),
            encoding="utf-8",
        )
        resolved = resolve_config(
            {"model": "default-model", "timeout": 10},
            config_path=cfg,
            environ={"STEERABLE_PROFILE": "work", "STEERABLE_TIMEOUT": "99"},
        )
        assert resolved.values["model"] == "work-model"
        assert resolved.sources["model"] == "profile"
        # env still beats the profile
        assert resolved.values["timeout"] == 99
        assert resolved.sources["timeout"] == "env"

    def test_unknown_profile_fails_loud_listing_available(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"profiles": {"work": {}, "home": {}}}), encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="unknown profile 'travel'.*home.*work"):
            resolve_config({}, config_path=cfg, environ={"STEERABLE_PROFILE": "travel"})

    def test_profile_argument_beats_the_env_var(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"profiles": {"a": {"x": "from-a"}, "b": {"x": "from-b"}}}),
            encoding="utf-8",
        )
        resolved = resolve_config(
            {"x": "default"}, config_path=cfg,
            environ={"STEERABLE_PROFILE": "a"}, profile="b",
        )
        assert resolved.values["x"] == "from-b"

    def test_the_profiles_block_is_not_a_config_key(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"profiles": {"p": {}}}), encoding="utf-8")
        resolved = resolve_config({}, config_path=cfg, environ={})
        assert "profiles" not in resolved.values


class TestManagedLayer:
    def test_managed_pins_beat_every_user_layer(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"sandbox_enabled": False}), encoding="utf-8")
        managed = tmp_path / "managed.json"
        managed.write_text(json.dumps({"sandbox_enabled": True}), encoding="utf-8")
        resolved = resolve_config(
            {"sandbox_enabled": False},
            config_path=cfg,
            environ={
                "STEERABLE_SANDBOX_ENABLED": "false",
                "STEERABLE_MANAGED_CONFIG_PATH": str(managed),
            },
            overrides={"sandbox_enabled": False},
        )
        assert resolved.values["sandbox_enabled"] is True
        assert resolved.sources["sandbox_enabled"] == "managed"

    def test_managed_values_are_schema_checked(self, tmp_path: Path) -> None:
        managed = tmp_path / "managed.json"
        managed.write_text(json.dumps({"max_tokens": "lots"}), encoding="utf-8")
        with pytest.raises(ConfigError, match="'max_tokens'.*managed.*integer"):
            resolve_config(
                {"max_tokens": 60_000},
                config_path=Path("/absent"),
                environ={"STEERABLE_MANAGED_CONFIG_PATH": str(managed)},
            )

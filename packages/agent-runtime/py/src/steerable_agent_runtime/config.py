"""Layered user configuration.

One user config file (``~/.steerable/config.json``) holds deployment-varying
defaults; the effective value of a key is resolved through four layers, last
wins:

1. ``defaults`` — shipped defaults the caller passes in;
2. the user file — ``~/.steerable/config.json`` (or ``config_path``);
3. environment — ``STEERABLE_<KEY>`` for each known key;
4. per-request RPC overrides — applied by the caller, never written here.

JSON, not TOML: the runtime supports Python 3.10, and ``tomllib`` only
landed in 3.11. JSON is a zero-dependency subset every supported interpreter
reads, and the file is hand-edited rarely enough that comments are not worth
a third-party TOML dependency.

``resolve_config`` returns both the merged mapping and a per-key provenance
record so a ``config.get`` / CLI can show *where* each effective value came
from — the "preview the merged result" capability (dsh's ``--dump-config``
counterpart). A malformed user file fails loud at load with the path named,
never a silent fall-back to defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Default location of the user config file.
DEFAULT_CONFIG_PATH = Path.home() / ".steerable" / "config.json"

#: Environment prefix: ``STEERABLE_LOG_LEVEL`` overrides the ``log_level`` key.
ENV_PREFIX = "STEERABLE_"


class ConfigError(ValueError):
    """The user config file exists but is not a readable JSON object."""


@dataclass(slots=True)
class ResolvedConfig:
    """The merged config plus where each key's effective value came from."""

    values: dict[str, Any]
    #: key → "default" | "file" | "env" | "override"
    sources: dict[str, str] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        """The ``config.get`` payload: each key's value and its source."""
        return {
            key: {"value": self.values[key], "source": self.sources.get(key, "default")}
            for key in self.values
        }


def _env_key(key: str) -> str:
    return ENV_PREFIX + key.upper()


def load_user_config(path: Path) -> dict[str, Any]:
    """Read the user config file; absent → empty. Malformed → ConfigError."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path}: not a readable JSON object: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")
    return data


def resolve_config(
    defaults: dict[str, Any],
    *,
    config_path: Path | None = None,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> ResolvedConfig:
    """Merge the four layers over ``defaults`` and record provenance.

    Only keys present in ``defaults`` are resolved from the environment (the
    env layer cannot invent keys the caller did not declare); the user file
    and overrides may add keys. ``environ``/``config_path`` are injectable for
    tests; the real process environment and ``DEFAULT_CONFIG_PATH`` are the
    defaults.
    """
    env = os.environ if environ is None else environ
    path = DEFAULT_CONFIG_PATH if config_path is None else config_path

    values = dict(defaults)
    sources = {key: "default" for key in defaults}

    for key, value in load_user_config(path).items():
        values[key] = value
        sources[key] = "file"

    for key in defaults:
        env_name = _env_key(key)
        if env_name in env:
            values[key] = env[env_name]
            sources[key] = "env"

    for key, value in (overrides or {}).items():
        values[key] = value
        sources[key] = "override"

    return ResolvedConfig(values=values, sources=sources)

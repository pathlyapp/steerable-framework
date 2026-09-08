"""Layered user configuration.

One user config file (``~/.steerable/config.json``) holds deployment-varying
defaults; the effective value of a key is resolved through six layers, last
wins:

1. ``defaults`` — shipped defaults the caller passes in; they double as the
   **schema**: every layer's value for a declared key must match the
   default's type (env strings coerce), or the load fails loud naming the
   key, the source, and the expected type;
2. the user file — ``~/.steerable/config.json`` (or ``config_path``);
3. the selected **profile** — a named ``{"profiles": {"work": {...}}}``
   block in the user file, selected by ``STEERABLE_PROFILE`` or the
   ``profile`` argument; an unknown name fails loud listing the available
   profiles;
4. environment — ``STEERABLE_<KEY>`` for each known key;
5. per-request RPC overrides — applied by the caller, never written here;
6. the **managed** file — ``STEERABLE_MANAGED_CONFIG_PATH``-pointed JSON,
   applied last so enterprise pins (e.g. a restrictive sandbox posture)
   cannot be loosened by any user-writable layer (CC managed-settings
   parity).

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

#: Env var selecting a named profile from the user file's ``profiles`` block.
PROFILE_ENV = "STEERABLE_PROFILE"

#: Env var pointing at the managed (enterprise-pinned) config file, applied
#: after every other layer so its keys cannot be loosened from below.
MANAGED_CONFIG_ENV = "STEERABLE_MANAGED_CONFIG_PATH"

#: The user-file key carrying named profiles; never a config key itself.
_PROFILES_KEY = "profiles"


class ConfigError(ValueError):
    """The user config file exists but is not a readable JSON object."""


@dataclass(slots=True)
class ResolvedConfig:
    """The merged config plus where each key's effective value came from."""

    values: dict[str, Any]
    #: key → "default" | "file" | "profile" | "env" | "override" | "managed"
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


def _coerce(key: str, value: Any, template: Any, source: str) -> Any:
    """Validate ``value`` against the declared default's type.

    The defaults dict is the schema: a layer's value for a declared key must
    be the default's type. Environment values arrive as strings and coerce
    (``"60000"`` → int, ``"true"`` → bool, JSON for list/dict); every other
    source must carry the type already. A mismatch fails loud — a silently
    mistyped config key (``"60000"`` where 60000 was meant) is a
    misconfiguration, and misconfiguration fails loud.
    """
    if template is None:
        return value
    if isinstance(template, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {
            "1", "true", "yes", "on", "0", "false", "no", "off",
        }:
            return value.strip().lower() in {"1", "true", "yes", "on"}
        raise ConfigError(
            f"config key {key!r} ({source}): expected a boolean, got {value!r}"
        )
    if isinstance(template, int):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
        raise ConfigError(
            f"config key {key!r} ({source}): expected an integer, got {value!r}"
        )
    if isinstance(template, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise ConfigError(
            f"config key {key!r} ({source}): expected a number, got {value!r}"
        )
    if isinstance(template, str):
        if isinstance(value, str):
            return value
        raise ConfigError(
            f"config key {key!r} ({source}): expected a string, got {value!r}"
        )
    if isinstance(template, (list, dict)):
        if isinstance(value, type(template)):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, type(template)):
                return parsed
        raise ConfigError(
            f"config key {key!r} ({source}): expected "
            f"{type(template).__name__}, got {value!r}"
        )
    return value


def resolve_config(
    defaults: dict[str, Any],
    *,
    config_path: Path | None = None,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
    profile: str | None = None,
) -> ResolvedConfig:
    """Merge the six layers over ``defaults`` and record provenance.

    Only keys present in ``defaults`` are resolved from the environment (the
    env layer cannot invent keys the caller did not declare); the user file
    and overrides may add keys. Values for declared keys are type-checked
    against the default's type at every layer (the defaults are the schema).
    ``environ``/``config_path`` are injectable for tests; the real process
    environment and ``DEFAULT_CONFIG_PATH`` are the defaults.
    """
    env = os.environ if environ is None else environ
    path = DEFAULT_CONFIG_PATH if config_path is None else config_path

    values = dict(defaults)
    sources = {key: "default" for key in defaults}

    def apply(mapping: dict[str, Any], source: str) -> None:
        for key, value in mapping.items():
            if key in defaults:
                value = _coerce(key, value, defaults[key], source)
            values[key] = value
            sources[key] = source

    file_config = load_user_config(path)
    profiles = file_config.get(_PROFILES_KEY) or {}
    if not isinstance(profiles, dict):
        raise ConfigError(f"{path}: {_PROFILES_KEY!r} must be an object")
    apply({k: v for k, v in file_config.items() if k != _PROFILES_KEY}, "file")

    selected = profile if profile is not None else (env.get(PROFILE_ENV) or "").strip()
    if selected:
        block = profiles.get(selected)
        if block is None:
            raise ConfigError(
                f"unknown profile {selected!r} (from {PROFILE_ENV} or the "
                f"profile argument); {path} declares: "
                + (", ".join(sorted(profiles)) or "(none)")
            )
        if not isinstance(block, dict):
            raise ConfigError(f"{path}: profile {selected!r} must be an object")
        apply(block, "profile")

    for key, template in defaults.items():
        env_name = _env_key(key)
        if env_name in env:
            values[key] = _coerce(key, env[env_name], template, "env")
            sources[key] = "env"

    apply(overrides or {}, "override")

    managed_path = (env.get(MANAGED_CONFIG_ENV) or "").strip()
    if managed_path:
        apply(load_user_config(Path(managed_path).expanduser()), "managed")

    return ResolvedConfig(values=values, sources=sources)

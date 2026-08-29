from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class TraceSpan:
    span_id: str
    name: str
    start_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    end_at: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    def finish(self) -> None:
        if self.end_at is None:
            self.end_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Secret redaction (spec/runtime/README.md "Secret redaction").
#
# Every `payload` / `attrs` / `stageData` field persisted to a trace must be
# secret-redacted first. `sanitize_for_trace` is the canonical scrubber: it
# walks a JSON-ish value and redacts (a) any dict value whose key names a
# credential, and (b) any string that *looks* like a live credential even
# under a benign key (defense in depth — a tool result echoing an
# `Authorization: Bearer …` header or an `sk-…` key must not land in a trace).
# ---------------------------------------------------------------------------

#: Replacement text for anything redacted.
REDACTED = "***"

#: Secret key names, compared after normalizing to lowercase alphanumerics
#: (so `api_key`, `apiKey`, `api-key`, `ApiKey` all match `apikey`). Exact
#: match only — substring matching would false-positive on `tokenize`,
#: `monkey`, `authority`, etc.
_SECRET_KEY_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "clientsecret",
        "privatekey",
        "sessionkey",
        "sessionid",
        "cookie",
        "setcookie",
        "xapikey",
    }
)

_KEY_NORMALIZE = re.compile(r"[^a-z0-9]")

#: High-confidence live-credential value patterns. Kept narrow on purpose —
#: each is a well-known secret prefix/format, so matches are almost never
#: benign. Long opaque strings under a non-secret key are left alone (a hash
#: or id is not a credential).
_SECRET_VALUE_PATTERNS = (
    # PEM private keys (any algorithm).
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Authorization: Bearer <token>.
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    # Common API-key prefixes: OpenAI/DeepSeek `sk-…`, GitHub
    # (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`github_pat_…`), GitLab `glpat-…`,
    # Slack `xox…-…`, AWS `AKIA`/`ASIA…`.
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}"
        r"|glpat-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{8,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
    ),
)


def _is_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return _KEY_NORMALIZE.sub("", key.lower()) in _SECRET_KEY_NAMES


def _scrub_string(text: str) -> str:
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def sanitize_for_trace(value: Any, *, extra_keys: frozenset[str] | None = None) -> Any:
    """Return ``value`` with credentials redacted, safe to persist in a trace.

    Recursively walks dicts / lists / tuples / strings:

    - a dict value whose key names a credential (see ``_SECRET_KEY_NAMES``,
      plus ``extra_keys``) is replaced with ``REDACTED`` regardless of content;
    - a string is scanned for live-credential patterns (Bearer tokens, ``sk-…``
      keys, PEM private keys, …) and each match replaced with ``REDACTED``;
    - all other values pass through unchanged.

    The input is never mutated; a new structure is returned. Non-JSON scalar
    types (numbers, booleans, ``None``) are returned as-is.
    """
    secret_names = (
        _SECRET_KEY_NAMES | {_KEY_NORMALIZE.sub("", k.lower()) for k in extra_keys}
        if extra_keys
        else _SECRET_KEY_NAMES
    )

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[Any, Any] = {}
            for k, v in node.items():
                if isinstance(k, str) and _KEY_NORMALIZE.sub("", k.lower()) in secret_names:
                    out[k] = REDACTED
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, (list, tuple)):
            return [walk(item) for item in node]
        if isinstance(node, str):
            return _scrub_string(node)
        return node

    return walk(value)

"""``sanitize_for_trace``: the spec-mandated secret scrubber for traces."""

from __future__ import annotations

from steerable_agent_harness.tracing import REDACTED, sanitize_for_trace


def test_redacts_secret_keys_by_name_case_and_separator_insensitive() -> None:
    payload = {
        "password": "hunter2",
        "api_key": "abc",
        "apiKey": "abc",
        "API-KEY": "abc",
        "Authorization": "abc",
        "credentials": {"nested": "abc"},
        "token": "abc",
    }
    out = sanitize_for_trace(payload)
    for key in payload:
        assert out[key] == REDACTED, key


def test_does_not_over_redact_benign_keys() -> None:
    payload = {
        "tokenize": "split into tokens",  # contains "token" but isn't a secret
        "monkey": "animal",  # contains "key"
        "authority": "cert auth",  # contains "auth"
        "author": "a writer",
        "name": "add",
        "arguments": {"a": 1},
    }
    assert sanitize_for_trace(payload) == payload


def test_recurses_into_nested_dicts_and_lists() -> None:
    payload = {
        "outer": {
            "items": [
                {"apiKey": "secret-value", "ok": 1},
                {"nested": {"password": "p"}},
            ]
        }
    }
    out = sanitize_for_trace(payload)
    assert out["outer"]["items"][0]["apiKey"] == REDACTED
    assert out["outer"]["items"][0]["ok"] == 1
    assert out["outer"]["items"][1]["nested"]["password"] == REDACTED


def test_scrubs_credential_values_even_under_benign_keys() -> None:
    payload = {
        # A tool result that echoed a request/response header or env dump.
        "output": "Authorization: Bearer abcdef1234567890abcdef",
        "note": "use key sk-1234567890abcdefghijkl",
        "text": "token ghp_0123456789abcdefghijklmn in log",
    }
    out = sanitize_for_trace(payload)
    assert "abcdef1234567890abcdef" not in out["output"]
    assert "sk-1234567890abcdefghijkl" not in out["note"]
    assert "ghp_0123456789abcdefghijklmn" not in out["text"]
    assert REDACTED in out["output"]


def test_scrubs_pem_private_key_blocks() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA7\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = sanitize_for_trace({"content": f"before {pem} after"})
    assert "MIIEpAIBAAKCAQEA7" not in out["content"]
    assert "before" in out["content"] and "after" in out["content"]


def test_does_not_mutate_the_input() -> None:
    payload = {"apiKey": "secret", "nested": {"token": "t"}}
    snapshot = {"apiKey": "secret", "nested": {"token": "t"}}
    sanitize_for_trace(payload)
    assert payload == snapshot


def test_scalar_passthrough() -> None:
    assert sanitize_for_trace(42) == 42
    assert sanitize_for_trace(None) is None
    assert sanitize_for_trace(True) is True
    assert sanitize_for_trace("plain text") == "plain text"


def test_extra_keys_extend_the_secret_set() -> None:
    payload = {"xCustomSecret": "v", "other": "w"}
    out = sanitize_for_trace(payload, extra_keys=frozenset({"x-custom-secret"}))
    assert out["xCustomSecret"] == REDACTED
    assert out["other"] == "w"

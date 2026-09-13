"""Gateway catalog (live GET /models): parsing, catalog join, TTL/stale fetch."""

from __future__ import annotations

import pytest
from steerable_agent_runtime.gateway_catalog import (
    GatewayCatalogError,
    clear_gateway_cache,
    fetch_gateway_models,
    listing_request,
    merge_with_catalog,
    parse_models_listing,
)
from steerable_agent_runtime.model_info import clamp_reasoning_effort


@pytest.fixture(autouse=True)
def _clean_gateway_cache():
    clear_gateway_cache()
    try:
        yield
    finally:
        clear_gateway_cache()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_openai_data_shape_with_openrouter_rich_fields() -> None:
    payload = {
        "data": [
            {
                "id": "qwen/qwen3.8-27b",
                "name": "Qwen 3.8 27B",
                "context_length": 262144,
                "architecture": {"input_modalities": ["image", "text"]},
                "top_provider": {"max_completion_tokens": 32768},
                "pricing": {"prompt": "0.0000002", "completion": "0.0000008"},
                "supported_parameters": ["tools", "reasoning"],
            }
        ]
    }
    (row,) = parse_models_listing(payload)
    assert row.id == "qwen/qwen3.8-27b"
    assert row.name == "Qwen 3.8 27B"
    assert row.context_window == 262144
    assert row.max_output_tokens == 32768
    assert row.input_modalities == ("image", "text")
    assert row.prompt_price_per_mtok == pytest.approx(0.2)
    assert row.completion_price_per_mtok == pytest.approx(0.8)
    assert "reasoning" in row.supported_parameters


def test_parse_models_map_shape_with_limit_variants() -> None:
    # models.dev-style: the property key is the id, limits nest under "limit".
    payload = {
        "models": {
            "glm-5.3-flash": {
                "display_name": "GLM 5.3 Flash",
                "limit": {"context": 1048576, "output": 131072},
            },
            "broken-entry": "not-a-dict",
        }
    }
    rows = parse_models_listing(payload)
    assert len(rows) == 1
    assert rows[0].id == "glm-5.3-flash"
    assert rows[0].name == "GLM 5.3 Flash"
    assert rows[0].context_window == 1048576
    assert rows[0].max_output_tokens == 131072


def test_parse_skips_id_less_entries_and_rejects_garbage() -> None:
    assert parse_models_listing({"data": [{"name": "no id"}, 42]}) == []
    with pytest.raises(ValueError, match="neither"):
        parse_models_listing({"unexpected": True})
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_models_listing([1, 2, 3])


def test_parse_google_resource_name_as_id() -> None:
    (row,) = parse_models_listing(
        {
            "models": [
                {
                    "name": "models/gemini-2.5-flash",
                    "displayName": "Gemini 2.5 Flash",
                }
            ]
        }
    )
    assert row.id == "gemini-2.5-flash"
    assert row.name == "Gemini 2.5 Flash"


def test_listing_request_matches_vendor_wire() -> None:
    url, headers = listing_request("https://api.deepseek.com", "sk-x")
    assert url == "https://api.deepseek.com/models"
    assert headers["Authorization"] == "Bearer sk-x"

    url, headers = listing_request(
        "https://api.anthropic.com", "sk-ant", provider="anthropic"
    )
    assert url == "https://api.anthropic.com/v1/models"
    assert headers["x-api-key"] == "sk-ant"
    assert headers["anthropic-version"] == "2023-06-01"

    url, headers = listing_request(
        "https://api.anthropic.com/v1", "sk-ant", provider="anthropic"
    )
    assert url == "https://api.anthropic.com/v1/models"

    url, headers = listing_request(
        "https://generativelanguage.googleapis.com", "g-key", provider="google"
    )
    assert url == "https://generativelanguage.googleapis.com/v1beta/models"
    assert headers["x-goog-api-key"] == "g-key"


# ---------------------------------------------------------------------------
# Catalog join
# ---------------------------------------------------------------------------


def test_merge_joins_cross_provider_leaf_and_gateway_fields_win() -> None:
    (row,) = merge_with_catalog(parse_models_listing({
        "data": [
            {
                # The gateway's own namespace — absent from every catalog tier.
                "id": "openai/qwen/qwen3.8-27b",
                "context_length": 500_000,
            }
        ]
    }))
    # The leaf join found a catalog qwen3.8-27b entry (case-insensitive)…
    assert row.joined_from is not None
    assert row.joined_from.lower().endswith("qwen3.8-27b")
    assert row.info.reasoning_levels == frozenset({"low", "medium", "xhigh"})
    # …but the gateway-advertised window wins over the catalog's.
    assert row.info.context_window == 500_000


def test_merge_marks_unknown_ids_without_blocking_them() -> None:
    (row,) = merge_with_catalog(
        parse_models_listing({"data": [{"id": "acme/internal-fine-tune-9"}]})
    )
    assert row.joined_from is None
    assert row.info.reasoning_levels == frozenset()
    # Discovery, not routing: the row exists so the picker can list it.
    assert row.id == "acme/internal-fine-tune-9"


def test_merge_prefers_serving_provider_over_smallest_leaf_window() -> None:
    # Bare DeepSeek ids on api.deepseek.com must not inherit Cloudflare's
    # 131k clone; the first-party catalog row is 1M with thinking knobs.
    (row,) = merge_with_catalog(
        parse_models_listing({"data": [{"id": "deepseek-v4-pro"}]}),
        base_url="https://api.deepseek.com",
    )
    assert row.joined_from == "deepseek/deepseek-v4-pro"
    assert row.info.context_window == 1_000_000
    assert row.info.reasoning_levels == frozenset({"high", "max"})


def test_merge_levels_match_the_request_path() -> None:
    # The wire row must expose the same knob the request path enforces:
    # resolve_model_info unions hand-owned legacy levels (GLM's "max") with
    # catalog levels, and models.list must not hide what clamp would accept.
    (row,) = merge_with_catalog(
        parse_models_listing({"data": [{"id": "z-ai/glm-5.3-flash"}]})
    )
    assert row.info.reasoning_levels == frozenset({"low", "high", "max"})
    assert clamp_reasoning_effort(row.id, "max", strict=True) == "max"


# ---------------------------------------------------------------------------
# Fetch: TTL cache, stale-on-error, offline
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Counts GETs; fails when ``payload`` is an exception instance."""

    calls = 0
    last_url = ""
    last_headers: dict | None = None
    payload: object = {"data": [{"id": "openai/qwen/qwen3.8-27b"}]}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        type(self).calls += 1
        type(self).last_url = url
        type(self).last_headers = headers
        if isinstance(type(self).payload, Exception):
            raise type(self).payload
        return _FakeResponse(type(self).payload)


@pytest.fixture
def fake_httpx(monkeypatch):
    import httpx

    _FakeClient.calls = 0
    _FakeClient.last_url = ""
    _FakeClient.last_headers = None
    _FakeClient.payload = {"data": [{"id": "openai/qwen/qwen3.8-27b"}]}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return _FakeClient


async def test_fetch_caches_within_ttl(fake_httpx) -> None:
    first = await fetch_gateway_models("http://gw.test/v1")
    second = await fetch_gateway_models("http://gw.test/v1")
    assert fake_httpx.calls == 1
    assert first.stale is False
    assert second.entries == first.entries


async def test_fetch_ttl_zero_bypasses_cache(fake_httpx) -> None:
    await fetch_gateway_models("http://gw.test/v1")
    await fetch_gateway_models("http://gw.test/v1", ttl_sec=0)
    assert fake_httpx.calls == 2


async def test_fetch_uses_anthropic_listing_url(fake_httpx) -> None:
    await fetch_gateway_models(
        "https://api.anthropic.com", "sk-ant", provider="anthropic"
    )
    assert fake_httpx.last_url == "https://api.anthropic.com/v1/models"
    assert fake_httpx.last_headers["x-api-key"] == "sk-ant"


async def test_fetch_serves_stale_on_refresh_failure(fake_httpx) -> None:
    await fetch_gateway_models("http://gw.test/v1")
    fake_httpx.payload = ConnectionError("gateway down")
    # ttl_sec=0 forces a refresh attempt; the failure serves the last listing.
    listing = await fetch_gateway_models("http://gw.test/v1", ttl_sec=0)
    assert listing.stale is True
    assert [entry.id for entry in listing.entries] == ["openai/qwen/qwen3.8-27b"]


async def test_fetch_raises_without_cache(fake_httpx) -> None:
    fake_httpx.payload = ConnectionError("gateway down")
    with pytest.raises(GatewayCatalogError, match="gateway down"):
        await fetch_gateway_models("http://gw.test/v1")

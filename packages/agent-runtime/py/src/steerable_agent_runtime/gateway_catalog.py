"""Live gateway model listing — the discovery half of the model catalog.

The bundled catalog (``model_catalog.py``) is a models.dev snapshot, stale by
construction; the gateway's own ``GET /models`` is the live list of ids the
endpoint actually accepts. This module fetches and parses that listing into
``GatewayModel`` rows, tolerating the two listing shapes in the wild
(OpenAI-style ``data`` array, models.dev-style ``models`` object) and the
field variants each uses for window / max-output / pricing — the tolerance
dsh's ``readListing`` (llm-pi-ai ``discovery.ts``) implements, verified
against its recorded provider-listing fixtures.

Discovery, not routing: a gateway id absent from every catalog still appears
in the listing with empty capability fields and ``joined_from=None``; the
listing never gates whether a request may be sent.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from .model_info import ModelInfo

_log = logging.getLogger(__name__)

#: Default freshness window for the in-process listing cache. The gateway
#: listing changes on deployment boundaries, not per request; 60s keeps a
#: settings UI snappy without hammering the endpoint.
DEFAULT_TTL_SEC = 60.0

#: Default timeout for the listing request itself.
DEFAULT_TIMEOUT_SEC = 10.0


class GatewayCatalogError(Exception):
    """The gateway listing could not be fetched and no cache survives."""

    def __init__(self, base_url: str, reason: str) -> None:
        self.base_url = base_url
        self.reason = reason
        super().__init__(f"gateway catalog fetch failed for {base_url!r}: {reason}")


@dataclass(frozen=True, slots=True)
class GatewayModel:
    """One row of the gateway's live listing, normalized across shapes.

    Capability fields are ``None``/empty when the gateway does not advertise
    them — the catalog join (``merge_with_catalog``) fills what models.dev
    knows, and what neither knows stays unknown.
    """

    id: str
    name: str
    context_window: int | None
    max_output_tokens: int | None
    input_modalities: tuple[str, ...]
    #: USD per million tokens, parsed from OpenRouter-style ``pricing``
    #: strings; ``None`` when the gateway does not advertise pricing.
    prompt_price_per_mtok: float | None
    completion_price_per_mtok: float | None
    #: OpenRouter-style ``supported_parameters`` (``"reasoning"`` present
    #: means the endpoint accepts a reasoning knob, levels unknown).
    supported_parameters: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GatewayListing:
    """A fetched listing plus its freshness provenance."""

    entries: tuple[GatewayModel, ...]
    #: Epoch seconds of the successful fetch (a stale listing keeps the
    #: timestamp of the fetch that produced it, not of the failed refresh).
    fetched_at: float
    #: True when the last refresh failed and this is the previous listing.
    stale: bool


@dataclass(frozen=True, slots=True)
class GatewayCatalogRow:
    """A gateway id joined with catalog capabilities, ready for the wire.

    ``joined_from`` names the catalog key that supplied capability fields
    (``None`` when no catalog tier matched — the "capabilities unknown"
    case the UI marks). ``info`` is the merged capability descriptor:
    gateway-advertised fields win where the gateway provides them, catalog
    fields (reasoning levels above all) fill the rest.
    """

    id: str
    name: str
    info: ModelInfo
    joined_from: str | None
    prompt_price_per_mtok: float | None
    completion_price_per_mtok: float | None


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def _first_str_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if isinstance(v, str))
    return ()


def _price_per_mtok(pricing: Any, key: str) -> float | None:
    """OpenRouter ``pricing`` values are USD-per-token strings."""
    if not isinstance(pricing, dict):
        return None
    raw = pricing.get(key)
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        return None
    try:
        return float(raw) * 1_000_000
    except (TypeError, ValueError):
        return None


def _parse_entry(entry: Any, *, key_hint: str | None) -> GatewayModel | None:
    if not isinstance(entry, dict):
        return None
    model_id = entry.get("id") or key_hint
    resource_name = entry.get("name") if isinstance(entry.get("name"), str) else None
    if not isinstance(model_id, str) or not model_id:
        if resource_name and resource_name.startswith("models/"):
            model_id = resource_name[7:]
        else:
            return None
    display = entry.get("display_name") or entry.get("displayName")
    if not isinstance(display, str) or not display:
        display = (
            None
            if resource_name is not None and resource_name.startswith("models/")
            else resource_name
        )
    limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
    top_provider = (
        entry.get("top_provider") if isinstance(entry.get("top_provider"), dict) else {}
    )
    architecture = (
        entry.get("architecture") if isinstance(entry.get("architecture"), dict) else {}
    )
    return GatewayModel(
        id=model_id,
        name=str(display) if isinstance(display, str) and display else model_id,
        context_window=_first_int(
            entry.get("context_length"),
            entry.get("context_window"),
            entry.get("contextWindow"),
            entry.get("max_input_tokens"),
            limit.get("context"),
        ),
        max_output_tokens=_first_int(
            top_provider.get("max_completion_tokens"),
            entry.get("max_completion_tokens"),
            entry.get("max_output_tokens"),
            entry.get("maxOutputTokens"),
            entry.get("maxTokens"),
            entry.get("max_tokens"),
            limit.get("output"),
        ),
        input_modalities=_first_str_list(architecture.get("input_modalities")),
        prompt_price_per_mtok=_price_per_mtok(entry.get("pricing"), "prompt"),
        completion_price_per_mtok=_price_per_mtok(entry.get("pricing"), "completion"),
        supported_parameters=_first_str_list(entry.get("supported_parameters")),
    )


def parse_models_listing(payload: Any) -> list[GatewayModel]:
    """Parse a ``GET /models`` body into normalized rows.

    Two shapes are in the wild: OpenAI-style ``{"data": [...]}`` (the common
    case, OpenRouter's enriched variant included) and models.dev-style
    ``{"models": {...}}`` where the property key is the id. Anything else is
    a parse error the caller surfaces as ``GatewayCatalogError``.
    """
    if not isinstance(payload, dict):
        raise ValueError("listing payload is not a JSON object")
    data = payload.get("data")
    if isinstance(data, list):
        rows = [_parse_entry(entry, key_hint=None) for entry in data]
        return [row for row in rows if row is not None]
    models = payload.get("models")
    if isinstance(models, dict):
        rows = [_parse_entry(entry, key_hint=key) for key, entry in models.items()]
        return [row for row in rows if row is not None]
    if isinstance(models, list):
        rows = [_parse_entry(entry, key_hint=None) for entry in models]
        return [row for row in rows if row is not None]
    raise ValueError("listing has neither a 'data' array nor a 'models' map")


# ---------------------------------------------------------------------------
# Catalog join (pure)
# ---------------------------------------------------------------------------


def merge_with_catalog(
    models: Iterable[GatewayModel],
    *,
    base_url: str | None = None,
) -> list[GatewayCatalogRow]:
    """Join gateway rows with catalog capabilities.

    Serving-provider lookup (from the listing ``base_url``) is preferred
    over the conservative cross-provider leaf: a DeepSeek first-party
    listing of ``deepseek-v4-pro`` must join ``deepseek/deepseek-v4-pro``
    (1M, high/max), not the smallest-window gateway clone. When the URL
    is unknown, the leaf join remains the provenance marker.

    The gateway's own fields (window, modalities) win where advertised —
    they describe this deployment. Capability fields come from
    ``resolve_model_info`` with the same ``base_url`` the request path's
    ``clamp_reasoning_effort`` uses, legacy-union rule included, so
    ``models.list`` and the request path never disagree about a model's
    knob. ``joined_from`` is ``None`` when no catalog tier matched, even if
    the hand-owned legacy table knows the model.
    """
    from .model_info import (
        TOOL_FORMAT_OPENAI,
        ModelInfo,
        resolve_model_info,
    )
    from .model_resolve import (
        catalog_provider_for_base_url,
        resolve_in_catalog,
        resolve_leaf_cross_provider,
    )

    serving = catalog_provider_for_base_url(base_url)
    rows: list[GatewayCatalogRow] = []
    for model in models:
        hit = resolve_in_catalog(serving, model.id) if serving else None
        if hit is None:
            hit = resolve_leaf_cross_provider(model.id)
        base = resolve_model_info(model.id, base_url=base_url)
        context_window = (
            model.context_window
            if model.context_window is not None
            else base.context_window
        )
        modalities = (
            frozenset(model.input_modalities)
            if model.input_modalities
            else base.modalities
        )
        rows.append(
            GatewayCatalogRow(
                id=model.id,
                name=model.name,
                info=ModelInfo(
                    pattern=model.id.lower(),
                    context_window=context_window,
                    modalities=modalities,
                    tool_format=TOOL_FORMAT_OPENAI,
                    reasoning_levels=base.reasoning_levels,
                ),
                joined_from=hit.key if hit is not None else None,
                prompt_price_per_mtok=model.prompt_price_per_mtok,
                completion_price_per_mtok=model.completion_price_per_mtok,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Fetch (network, TTL-cached, stale-on-error)
# ---------------------------------------------------------------------------

#: base_url (normalized) -> (time.monotonic() at fetch, listing). The wall
#: clock ``listing.fetched_at`` is for display; the monotonic clock drives
#: TTL so a clock jump cannot expire or resurrect a listing.
_cache: dict[str, tuple[float, GatewayListing]] = {}


def _cache_key(base_url: str) -> str:
    return base_url.rstrip("/").lower()


def clear_gateway_cache() -> None:
    """Drop every cached listing (tests, forced refresh)."""
    _cache.clear()


def listing_request(
    base_url: str,
    api_key: str | None,
    *,
    provider: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Vendor listing URL and headers for the sidecar factory kind.

    OpenAI-compat / ollama / openai-responses: ``GET {base}/models`` + Bearer.
    Anthropic: ``GET {base}/v1/models`` + ``x-api-key``.
    Google AI Studio: ``GET {base}/v1beta/models`` + ``x-goog-api-key``.
    """
    base = base_url.rstrip("/")
    kind = (provider or "").replace("-", "_")
    if kind == "anthropic":
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        headers: dict[str, str] = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        return url, headers
    if kind == "google":
        if base.endswith("/models"):
            url = base
        elif "/v1beta" in base:
            url = f"{base}/models"
        else:
            url = f"{base}/v1beta/models"
        return url, ({"x-goog-api-key": api_key} if api_key else {})
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return f"{base}/models", headers


async def fetch_gateway_models(
    base_url: str,
    api_key: str | None = None,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ttl_sec: float = DEFAULT_TTL_SEC,
    provider: str | None = None,
) -> GatewayListing:
    """Fetch the gateway's live model listing, with a process-local cache.

    A fresh-enough cached listing is served without network. ``ttl_sec=0``
    always hits the vendor (settings refresh). On refresh failure the
    previous listing is returned marked ``stale``. With no cache to fall
    back on, ``GatewayCatalogError`` is raised and the caller decides (the
    sidecar answers ``catalog_status: "offline"``).
    """
    import httpx  # local import — keeps the runtime importable without httpx

    from .llm.system_proxy import client_env_kwargs

    key = _cache_key(base_url)
    cached = _cache.get(key)
    if ttl_sec > 0 and cached is not None and (time.monotonic() - cached[0]) < ttl_sec:
        return cached[1]

    url, headers = listing_request(base_url, api_key, provider=provider)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            **client_env_kwargs(base_url),
        ) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            entries = tuple(parse_models_listing(response.json()))
    except Exception as exc:
        if cached is not None:
            _log.info("gateway listing refresh failed (%s); serving stale", exc)
            return GatewayListing(
                entries=cached[1].entries, fetched_at=cached[1].fetched_at, stale=True
            )
        raise GatewayCatalogError(base_url, str(exc)) from exc

    listing = GatewayListing(entries=entries, fetched_at=time.time(), stale=False)
    _cache[key] = (time.monotonic(), listing)
    return listing

"""Three-tier catalog resolution (W5.2.1): exact id → provider-scoped → prefix.

Prefix matching was the *only* resolution path before the catalog existed,
and silent prefix drift is how wrong context windows went unnoticed for
months. With the catalog, prefix is a fallback, not the main path:

1. **exact** — ``provider/model`` is a catalog key verbatim.
2. **scoped** — the model id alone, resolved within the named provider's
   namespace (``openai`` + ``gpt-5.5`` → ``openai/gpt-5.5``).
3. **prefix** — longest catalog-key prefix of the model id, only when the
   match clears a minimum length so ``gpt-5`` cannot claim ``gpt-5.5``'s
   entry by accident.

Every result carries its ``source`` so the caller can emit an observability
event when resolution fell through to the conservative default (W5.2.3) —
silent defaults are exactly how the drift stayed invisible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .model_catalog import MODEL_ENTRIES, PROVIDER_ENTRIES

ResolveSource = Literal["exact", "scoped", "prefix"]

#: Minimum model-id length a prefix match may claim. Below this, prefixes
#: are too greedy ("gpt-5" claiming "gpt-5.5-20251001"'s entry).
MIN_PREFIX_CHARS = 6


@dataclass(frozen=True, slots=True)
class CatalogHit:
    key: str
    context_window: int
    input_modalities: tuple[str, ...]
    tool_format: str
    reasoning_levels: tuple[str, ...]
    source: ResolveSource


def _entry(key: str, source: ResolveSource) -> CatalogHit:
    context, modalities, tool_format, reasoning = MODEL_ENTRIES[key]
    return CatalogHit(
        key=key,
        context_window=context,
        input_modalities=modalities,
        tool_format=tool_format,
        reasoning_levels=reasoning,
        source=source,
    )


def resolve_in_catalog(provider: str | None, model: str) -> CatalogHit | None:
    """Resolve a model against the built-in catalog; None = truly unknown."""
    if not model:
        return None

    # Tier 1: exact "provider/model".
    if provider:
        exact_key = f"{provider}/{model}"
        if exact_key in MODEL_ENTRIES:
            return _entry(exact_key, "exact")
    if "/" in model and model in MODEL_ENTRIES:
        # The model string already carries its provider ("openai/gpt-5.5").
        return _entry(model, "exact")

    # Tier 2: bare id within the named provider's namespace — gateways
    # namespace upstream vendors ("glm-5.3-flash" under openrouter resolves
    # "openrouter/z-ai/glm-5.3-flash" by final-segment match).
    if provider:
        prefix = f"{provider}/"
        scoped = [
            key
            for key in MODEL_ENTRIES
            if key.startswith(prefix) and key[len(prefix):].rsplit("/", 1)[-1] == model
        ]
        if scoped:
            return _entry(sorted(scoped)[0], "scoped")

    # Tier 3: longest-prefix fallback, SAME provider only. Cross-provider
    # prefix is never offered: another vendor's deployment has different
    # facts (302ai's deepseek-reasoner lists no reasoning knob; first-party
    # does). Bare model ids without provider context fall through to the
    # legacy table, observably.
    if not provider:
        return None
    best: str | None = None
    best_len = 0
    for key in MODEL_ENTRIES:
        key_provider, _, model_id = key.partition("/")
        if key_provider != provider:
            continue
        if len(model_id) < MIN_PREFIX_CHARS or len(model_id) <= best_len:
            continue
        if model.startswith(model_id):
            best = key
            best_len = len(model_id)
    if best is not None:
        return _entry(best, "prefix")
    return None


@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    """What the catalog knows about a provider's wire endpoint (W5.3.1).

    ``api_base_url`` is None for providers whose SDK default applies
    (models.dev omits the field for first-party endpoints).
    """

    provider: str
    api_base_url: str | None
    env_vars: tuple[str, ...]


def provider_endpoint(provider: str) -> ProviderEndpoint | None:
    """Catalog endpoint facts for a provider; None = provider not catalogued.

    Feeding ``compat_for_base_url`` so a new gateway needs no hand-written
    host entry. Compat *flags* stay hand-owned (W5.3.2): they describe wire
    behavior facts the upstream catalog does not track.
    """
    row = PROVIDER_ENTRIES.get(provider)
    if row is None:
        return None
    api, env = row
    return ProviderEndpoint(provider=provider, api_base_url=api, env_vars=env)


def catalog_provider_for_base_url(base_url: str | None) -> str | None:
    """The catalog provider serving ``base_url``, matched by URL host.

    Our configs name the *wire* provider (``openai`` for any
    OpenAI-compatible gateway); the catalog namespaces models under the
    *serving* provider. A deployment pointing at openrouter.ai must resolve
    models in openrouter's namespace, or every scoped lookup misses.
    """
    if not base_url:
        return None
    host = base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    if not host:
        return None
    for provider, (api, _env) in PROVIDER_ENTRIES.items():
        if not api:
            continue
        api_host = api.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        if api_host == host:
            return provider
    return None

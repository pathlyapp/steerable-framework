"""W5.1: the generated model catalog artifact and its generator.

The artifact tests pin the row contract the W5.2 resolver will consume;
the generator tests exercise projection and the overlay's fail-loud rules
against tiny fixtures (never the real 4 MB upstream file).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from steerable_agent_runtime import model_catalog
from steerable_agent_runtime.model_info import (
    REASONING_EFFORT_ORDER,
    TOOL_FORMAT_ANTHROPIC,
    TOOL_FORMAT_NONE,
    TOOL_FORMAT_OPENAI,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GENERATOR_PATH = _REPO_ROOT / "scripts" / "generate_model_catalog.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_model_catalog", _GENERATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- artifact contract -----------------------------------------------------


def test_artifact_pins_provenance() -> None:
    assert model_catalog.GENERATED_AT
    assert len(model_catalog.UPSTREAM_SHA256) == 64


def test_model_entry_rows_match_the_resolver_contract() -> None:
    assert model_catalog.MODEL_ENTRIES, "catalog must not be empty"
    valid_tool_formats = {TOOL_FORMAT_OPENAI, TOOL_FORMAT_ANTHROPIC, TOOL_FORMAT_NONE}
    valid_levels = set(REASONING_EFFORT_ORDER)
    for key, row in model_catalog.MODEL_ENTRIES.items():
        assert "/" in key, f"key must be provider-qualified: {key!r}"
        context, modalities, tool_format, reasoning = row
        assert isinstance(context, int) and context > 0, key
        # Non-text-only inputs exist upstream (ASR models are audio-only), so
        # the contract is non-empty, not "contains text".
        assert modalities, key
        assert tool_format in valid_tool_formats, key
        assert set(reasoning) <= valid_levels, key
        # Reasoning levels are stored in canonical order so the resolver can
        # clamp by index.
        assert list(reasoning) == sorted(
            reasoning, key=REASONING_EFFORT_ORDER.index
        ), key


def test_provider_entries_cover_model_prefixes() -> None:
    for key in model_catalog.MODEL_ENTRIES:
        provider = key.split("/", 1)[0]
        assert provider in model_catalog.PROVIDER_ENTRIES, provider


def test_known_entries_match_upstream_facts() -> None:
    """Spot-checks against models.dev (2026-08-31 snapshot). If upstream
    corrects these, regenerate and update the expectations together."""
    context, modalities, tool_format, _ = model_catalog.MODEL_ENTRIES[
        "anthropic/claude-sonnet-4-5"
    ]
    assert context == 1_000_000
    assert "image" in modalities
    assert tool_format == TOOL_FORMAT_ANTHROPIC

    api, env = model_catalog.PROVIDER_ENTRIES["openrouter"]
    assert api == "https://openrouter.ai/api/v1"
    assert "OPENROUTER_API_KEY" in env


# -- generator logic ---------------------------------------------------------


def _fixture() -> dict:
    return {
        "acme": {
            "id": "acme",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://api.acme.test/v1",
            "env": ["ACME_API_KEY"],
            "models": {
                "acme-large": {
                    "id": "acme-large",
                    "tool_call": True,
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "effort", "values": ["low", "high", "xhigh"]}
                    ],
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                    "limit": {"context": 200_000},
                },
                "acme-no-limit": {"id": "acme-no-limit", "tool_call": False},
            },
        },
        "anthropic": {
            "id": "anthropic",
            "npm": "@ai-sdk/anthropic",
            "env": ["ANTHROPIC_API_KEY"],
            "models": {
                "claude-test": {
                    "id": "claude-test",
                    "tool_call": True,
                    "reasoning": False,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 500_000},
                }
            },
        },
    }


def test_build_entries_projects_and_skips() -> None:
    gen = _load_generator()
    models, providers, skipped = gen.build_entries(_fixture())

    assert skipped == 1  # acme-no-limit has no context limit
    assert models["acme/acme-large"] == (
        200_000,
        ("image", "text"),
        TOOL_FORMAT_OPENAI,
        ("low", "high"),  # xhigh is outside our canonical ordering — dropped
    )
    # The anthropic AI-SDK package implies the anthropic wire format.
    assert models["anthropic/claude-test"][2] == TOOL_FORMAT_ANTHROPIC
    assert providers["acme"] == ("https://api.acme.test/v1", ("ACME_API_KEY",))


def test_overlay_corrects_and_removes() -> None:
    gen = _load_generator()
    models, _, _ = gen.build_entries(_fixture())
    applied = gen.apply_overlay(
        models,
        {
            "models": {
                "acme/acme-large": {"context_window": 180_000, "reason": "API caps it"},
                "anthropic/claude-test": {"remove": True, "reason": "retired"},
            }
        },
    )
    assert applied == 2
    assert models["acme/acme-large"][0] == 180_000
    assert "anthropic/claude-test" not in models


def test_overlay_fails_loud_on_dangling_entry() -> None:
    gen = _load_generator()
    models, _, _ = gen.build_entries(_fixture())
    with pytest.raises(ValueError, match="absent from the upstream"):
        gen.apply_overlay(models, {"models": {"acme/ghost": {"context_window": 1}}})


def test_overlay_fails_loud_on_unknown_field() -> None:
    gen = _load_generator()
    models, _, _ = gen.build_entries(_fixture())
    with pytest.raises(ValueError, match="unknown fields"):
        gen.apply_overlay(
            models, {"models": {"acme/acme-large": {"contextWindo": 1}}}
        )


def test_overlay_fails_loud_on_unknown_reasoning_level() -> None:
    gen = _load_generator()
    models, _, _ = gen.build_entries(_fixture())
    with pytest.raises(ValueError, match="unknown reasoning levels"):
        gen.apply_overlay(
            models,
            {"models": {"acme/acme-large": {"reasoning_levels": ["xhigh"]}}},
        )

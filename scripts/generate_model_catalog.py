#!/usr/bin/env python3
"""Generate the built-in model catalog from models.dev (W5.1).

Fetches ``https://models.dev/api.json`` (or reads a cached copy via
``--input``), applies our correction overlay, and writes
``packages/agent-runtime/py/src/steerable_agent_runtime/model_catalog.py``.

Design notes (HARNESS_TODO.md W5):

- Build-time, checked in, auditable: "which catalog did this run use" is a
  git fact, not a runtime fetch. The artifact pins the fetch date and the
  upstream SHA-256.
- The overlay (``scripts/model_catalog_overlay.json``) is corrections only:
  every entry must reference a model present in the upstream snapshot —
  a dangling correction fails generation loudly instead of rotting into
  dead code. New models go through ``register_model_info`` at runtime.
- ``--check`` regenerates in memory and compares byte-for-byte, so CI can
  fail on catalog drift the same way it fails on lockfile drift.

models.dev data is MIT licensed (Copyright (c) 2025 models.dev); the full
notice is embedded in the generated module's header.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    ROOT
    / "packages"
    / "agent-runtime"
    / "py"
    / "src"
    / "steerable_agent_runtime"
    / "model_catalog.py"
)
DEFAULT_OVERLAY = ROOT / "scripts" / "model_catalog_overlay.json"
UPSTREAM_URL = "https://models.dev/api.json"

#: Keep in sync with ``model_info.REASONING_EFFORT_ORDER`` — the catalog
#: stores levels in this canonical order so the resolver can clamp by index.
REASONING_EFFORT_ORDER = ("minimal", "low", "medium", "high")

TOOL_FORMAT_OPENAI = "openai"
TOOL_FORMAT_ANTHROPIC = "anthropic"
TOOL_FORMAT_NONE = "none"

#: Overlay fields recognized per model entry. Unknown fields fail generation
#: — a typo must not silently produce a no-op correction.
_OVERLAY_FIELDS = frozenset(
    {"context_window", "modalities", "tool_format", "reasoning_levels", "remove", "reason"}
)

_LICENSE_NOTICE = """
# --- models.dev license notice -------------------------------------------
# The model and provider data below is derived from the models.dev catalog
# (https://github.com/anomalyco/models.dev), used under the MIT License:
#
# Copyright (c) 2025 models.dev
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -------------------------------------------------------------------------
""".strip("\n")


def _tool_format(provider_npm: str | None, tool_call: bool) -> str:
    """Derive the wire format from the provider's AI SDK package.

    models.dev is SDK-agnostic and carries no wire-format field; the
    provider's ``npm`` package is the reliable signal (``@ai-sdk/anthropic``
    speaks the Anthropic tools format, the OpenAI-compatible long tail speaks
    OpenAI's). The overlay can override per model when this heuristic misses.
    """
    if not tool_call:
        return TOOL_FORMAT_NONE
    if provider_npm == "@ai-sdk/anthropic":
        return TOOL_FORMAT_ANTHROPIC
    return TOOL_FORMAT_OPENAI


def _reasoning_levels(model: dict) -> tuple[str, ...]:
    """Intersect upstream reasoning options with our canonical ordering."""
    if not model.get("reasoning"):
        return ()
    values: set[str] = set()
    for option in model.get("reasoning_options") or []:
        if isinstance(option, dict) and option.get("type") == "effort":
            values.update(str(v) for v in option.get("values") or [])
    return tuple(lv for lv in REASONING_EFFORT_ORDER if lv in values)


def build_entries(
    upstream: dict,
) -> tuple[dict[str, tuple], dict[str, tuple], int]:
    """Project the upstream snapshot into catalog rows.

    Returns ``(model_entries, provider_entries, skipped)``. Models without a
    context limit are skipped (the resolver cannot do anything with them) and
    counted in the header stats.
    """
    models: dict[str, tuple] = {}
    providers: dict[str, tuple] = {}
    skipped = 0
    for provider_id in sorted(upstream):
        provider = upstream[provider_id]
        if not isinstance(provider, dict):
            continue
        providers[provider_id] = (
            provider.get("api"),
            tuple(sorted(str(e) for e in provider.get("env") or [])),
        )
        npm = provider.get("npm")
        for model_id in sorted(provider.get("models") or {}):
            model = provider["models"][model_id]
            if not isinstance(model, dict):
                continue
            limit = model.get("limit") or {}
            context = limit.get("context")
            if not isinstance(context, int) or context <= 0:
                skipped += 1
                continue
            modalities = tuple(
                sorted(str(m) for m in (model.get("modalities") or {}).get("input") or [])
            )
            models[f"{provider_id}/{model_id}"] = (
                context,
                modalities,
                _tool_format(npm, bool(model.get("tool_call"))),
                _reasoning_levels(model),
            )
    return models, providers, skipped


def apply_overlay(models: dict[str, tuple], overlay: dict) -> int:
    """Apply correction entries; fail loud on dangling or unknown fields."""
    overrides = overlay.get("models") or {}
    if not isinstance(overrides, dict):
        raise ValueError("overlay 'models' must be an object")
    applied = 0
    for key, patch in sorted(overrides.items()):
        if not isinstance(patch, dict):
            raise ValueError(f"overlay entry {key!r} must be an object")
        unknown = set(patch) - _OVERLAY_FIELDS
        if unknown:
            raise ValueError(
                f"overlay entry {key!r} has unknown fields: {sorted(unknown)}"
            )
        if key not in models:
            raise ValueError(
                f"overlay entry {key!r} references a model absent from the "
                "upstream snapshot — remove or refresh the correction"
            )
        if patch.get("remove"):
            del models[key]
            applied += 1
            continue
        context, modalities, tool_format, reasoning = models[key]
        if "context_window" in patch:
            context = int(patch["context_window"])
        if "modalities" in patch:
            modalities = tuple(sorted(str(m) for m in patch["modalities"]))
        if "tool_format" in patch:
            tool_format = str(patch["tool_format"])
        if "reasoning_levels" in patch:
            requested = [str(lv) for lv in patch["reasoning_levels"]]
            bad = [lv for lv in requested if lv not in REASONING_EFFORT_ORDER]
            if bad:
                raise ValueError(
                    f"overlay entry {key!r} has unknown reasoning levels: {bad}"
                )
            reasoning = tuple(
                lv for lv in REASONING_EFFORT_ORDER if lv in set(requested)
            )
        models[key] = (context, modalities, tool_format, reasoning)
        applied += 1
    return applied


def render(
    models: dict[str, tuple],
    providers: dict[str, tuple],
    *,
    fetched_at: str,
    upstream_sha256: str,
    skipped: int,
    overlay_applied: int,
) -> str:
    lines = [
        "# GENERATED by scripts/generate_model_catalog.py — do not edit by hand.",
        f"# Source: {UPSTREAM_URL}",
        f"# Fetched: {fetched_at}  Upstream SHA-256: {upstream_sha256}",
        f"# Entries: {len(models)} models, {len(providers)} providers "
        f"({skipped} upstream models skipped: no context limit; "
        f"{overlay_applied} overlay corrections applied).",
        _LICENSE_NOTICE,
        "",
        '"""Built-in model catalog (W5): the external-directory half of model_info.',
        "",
        "Row formats (plain tuples keep the artifact compact and diff-stable):",
        "",
        "- ``MODEL_ENTRIES``: ``provider/model_id`` ->",
        "  ``(context_window, input_modalities, tool_format, reasoning_levels)``",
        "- ``PROVIDER_ENTRIES``: ``provider_id`` -> ``(api_base_url | None, env_vars)``",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f'GENERATED_AT = "{fetched_at}"',
        f'UPSTREAM_SHA256 = "{upstream_sha256}"',
        "",
        "MODEL_ENTRIES: dict[str, tuple[int, tuple[str, ...], str, tuple[str, ...]]] = {",
    ]
    for key in sorted(models):
        context, modalities, tool_format, reasoning = models[key]
        lines.append(
            f"    {key!r}: ({context}, {modalities!r}, {tool_format!r}, {reasoning!r}),"
        )
    lines.append("}")
    lines.append("")
    lines.append("PROVIDER_ENTRIES: dict[str, tuple[str | None, tuple[str, ...]]] = {")
    for key in sorted(providers):
        api, env = providers[key]
        lines.append(f"    {key!r}: ({api!r}, {env!r}),")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input",
        help="read a cached api.json instead of fetching (offline/reproducible)",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--overlay", default=str(DEFAULT_OVERLAY))
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the checked-in artifact differs from a fresh generation",
    )
    args = parser.parse_args(argv)

    if args.input:
        raw = Path(args.input).read_bytes()
        fetched_at = (
            datetime.fromtimestamp(
                Path(args.input).stat().st_mtime, tz=timezone.utc
            ).isoformat()
            + " (input file mtime)"
        )
    else:
        # models.dev sits behind Cloudflare, which 403s urllib's default
        # User-Agent; identify as the repo's generator instead.
        request = urllib.request.Request(
            UPSTREAM_URL,
            headers={"User-Agent": "steerable-framework-model-catalog-generator"},
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            raw = resp.read()
        fetched_at = datetime.now(tz=timezone.utc).isoformat()
    upstream = json.loads(raw)
    if not isinstance(upstream, dict):
        print("upstream api.json is not an object", file=sys.stderr)
        return 1
    digest = hashlib.sha256(raw).hexdigest()

    overlay_path = Path(args.overlay)
    overlay = (
        json.loads(overlay_path.read_text(encoding="utf-8"))
        if overlay_path.exists()
        else {}
    )

    models, providers, skipped = build_entries(upstream)
    try:
        applied = apply_overlay(models, overlay)
    except ValueError as exc:
        print(f"overlay error: {exc}", file=sys.stderr)
        return 1

    artifact = render(
        models,
        providers,
        fetched_at=fetched_at,
        upstream_sha256=digest,
        skipped=skipped,
        overlay_applied=applied,
    )

    output = Path(args.output)
    if args.check:
        existing = output.read_text(encoding="utf-8") if output.exists() else ""
        if existing != artifact:
            print(
                f"catalog drift: {output} differs from a fresh generation "
                "(re-run scripts/generate_model_catalog.py)",
                file=sys.stderr,
            )
            return 1
        print(f"catalog up to date: {output}")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(artifact, encoding="utf-8")
    print(
        f"wrote {output}: {len(models)} models, {len(providers)} providers, "
        f"{skipped} skipped, {applied} overlay corrections"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

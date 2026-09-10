"""Workflow contract gates for the GHA eval path (evals-arms / evals-weekly).

Text-level assertions on the workflow files: the contracts below broke
once each (2026-08-31) and both failures were silent — structural zeros
from an unpaired API key, and attribution reports skipped because
upload-artifact strips the path up to the first glob wildcard so no
``jobs/`` prefix survives in the artifact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.suite import LIVE_AGENTS, load_suite

ROOT = Path(__file__).resolve().parents[2]
ARMS = (ROOT / ".github/workflows/evals-arms.yml").read_text(encoding="utf-8")
WEEKLY = (ROOT / ".github/workflows/evals-weekly.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize("workflow", [ARMS, WEEKLY], ids=["arms", "weekly"])
def test_steerable_key_is_forwarded_with_its_base_url(workflow: str) -> None:
    """harbor_steerable resolves STEERABLE_API_KEY/STEERABLE_BASE_URL as a
    pair; a key without the URL hits the stock OpenAI endpoint and every
    trial scores a structural zero."""
    assert "STEERABLE_API_KEY: ${{ secrets.STEERABLE_API_KEY }}" in workflow
    assert "STEERABLE_BASE_URL: ${{ secrets.STEERABLE_BASE_URL }}" in workflow


@pytest.mark.parametrize("workflow", [ARMS, WEEKLY], ids=["arms", "weekly"])
def test_attribution_find_does_not_depend_on_stripped_prefix(workflow: str) -> None:
    """upload-artifact keeps only the path after the first glob wildcard;
    filtering the download tree on the upload-side ``jobs/`` directory
    matches nothing and the report silently skips."""
    assert '-path "*jobs/*"' not in workflow


@pytest.mark.parametrize("workflow", [ARMS, WEEKLY], ids=["arms", "weekly"])
def test_agent_logs_are_uploaded_for_efficiency_metrics(workflow: str) -> None:
    """The W1.4.3.3 efficiency table reads STEERABLE_RUN_SUMMARY from each
    trial's agent/headless.log; without the glob the columns render n/a."""
    assert "**/agent/headless.log" in workflow


def test_flaky_feishu_copy_matches_the_split() -> None:
    """The start card still said 25 ids after suite.yaml shrank flaky to 20,
    then 20 after the `8e260de` rebuild shrank it to 15."""
    assert "15 题 × 2 臂" in WEEKLY
    assert "25 题" not in WEEKLY


def test_flaky_start_card_names_arm_b_overrides() -> None:
    """A defaulted arm_b_env is a silent no-op A/B; the start card has to
    show what B actually received."""
    assert "ARM_B_ENV: ${{ github.event.inputs.arm_b_env }}" in WEEKLY
    assert 'os.environ.get("ARM_B_ENV")' in WEEKLY


def test_flaky_gather_runs_the_paired_scorer() -> None:
    """Harbor Mean on a flaky dispatch is last-attempt overwrite; the
    verdict that decides a catalog run is flaky_score on every attempt."""
    assert "python3 -m evals.flaky_score --root statuses" in WEEKLY
    assert '::warning::flaky_score failed' in WEEKLY


def test_attribution_waits_for_every_scored_split() -> None:
    """needs: [eval] ran the report before a flaky/catalog matrix finished."""
    assert "needs: [eval, flaky, spiral, catalog, failed-prev, probe]" in WEEKLY
    assert WEEKLY.count("needs: [eval, flaky, spiral, catalog, failed-prev, probe]") >= 2


def test_catalog_dispatch_offers_both_harnesses() -> None:
    """The catalog split is the only run that produces a reportable Mean, so
    the same-model pi comparison has to be dispatchable there."""
    assert "- claude-code-glm" in WEEKLY
    assert "- pi-glm" in WEEKLY
    assert "- terminus-2" in WEEKLY
    assert 'uv run python -m evals.run --agent "$AGENT" --split catalog' in WEEKLY


def test_catalog_feishu_label_names_the_agent() -> None:
    """A catalog Mean posted without its agent reads as the product score."""
    assert 'label="GHA catalog 89 × $EVAL_AGENT"' in WEEKLY


def test_catalog_concurrency_separates_the_agents() -> None:
    """The group holds one running plus one pending run. Sharing it across
    agents makes a second dispatch cancel the first's pending run. The model
    is in the group so a qwen catalog does not queue behind a GLM catalog."""
    assert (
        "group: evals-${{ github.event.inputs.split || 'cheap-12' }}-"
        "${{ github.event.inputs.agent || 'steerable' }}-"
        "${{ github.event.inputs.model || 'default' }}-"
        "${{ github.event.inputs.probe_agent || 'all' }}" in WEEKLY
    )


def test_catalog_forwards_model_override() -> None:
    """A catalog Mean without `--model` is the suite.yaml default, so a
    qwen/glm comparison has to be an explicit dispatch input, not a silent
    suite.yaml edit that also moves the product default."""
    assert "EVAL_MODEL: ${{ github.event.inputs.model }}" in WEEKLY
    assert 'extra+=(--model "$EVAL_MODEL")' in WEEKLY


def test_catalog_forwards_reasoning_effort() -> None:
    """Reasoning effort used to be hardwired to medium for qwen because the
    catalog knew no higher level. With the gateway catalog the level is
    validated strict against the model's entry, so the dispatch input must
    reach the env verbatim — and the result card must name it, or an xhigh
    Mean gets read as a medium one."""
    assert "EVAL_EFFORT: ${{ github.event.inputs.reasoning_effort }}" in WEEKLY
    assert 'export STEERABLE_REASONING_EFFORT="$EVAL_EFFORT"' in WEEKLY
    assert 'label="$label @$EVAL_EFFORT"' in WEEKLY


def test_catalog_model_override_pins_like_probe() -> None:
    """A catalog --model without the probe pins silently uses OpenRouter's
    default route and GLM @max, which is not the cost-effective baseline."""
    catalog = WEEKLY.split("name: Harbor catalog shard", 1)[1]
    assert "STEERABLE_OPENROUTER_ALLOW_FALLBACKS" in catalog
    assert 'STEERABLE_OPENROUTER_PROVIDER="${STEERABLE_OPENROUTER_PROVIDER:-alibaba}"' in catalog
    assert 'STEERABLE_OPENROUTER_PROVIDER="${STEERABLE_OPENROUTER_PROVIDER:-z-ai}"' in catalog
    assert 'STEERABLE_REASONING_EFFORT="${EVAL_EFFORT:-high}"' in catalog
    assert (
        "ANTHROPIC_API_KEY: ${{ github.event.inputs.agent == 'claude-code-glm' "
        "&& secrets.STEERABLE_API_KEY || '' }}" in WEEKLY
    )
    assert (
        "ANTHROPIC_BASE_URL: ${{ github.event.inputs.agent == 'claude-code-glm' "
        "&& secrets.STEERABLE_BASE_URL || '' }}" in WEEKLY
    )


def test_catalog_skips_pi_qwen_and_cc_deepseek() -> None:
    """Those cells are designed skips. A catalog dispatch must fail before
    Docker, not after 49 shards of structural zeros."""
    catalog = WEEKLY.split("name: TB 2.1 catalog", 1)[1]
    assert "pi-glm skipped on Qwen" in catalog
    assert "claude-code-glm skipped on DeepSeek" in catalog


def test_weekly_uploads_the_pi_transcript() -> None:
    """Harbor's Pi agent writes agent/pi.txt. Without it a pi failure arrives as
    token counts alone, and the first pi-glm run had to infer a runaway first
    turn from `n_output_tokens` sitting exactly on the cap."""
    assert "**/agent/pi.txt" in WEEKLY


def test_catalog_start_card_names_the_agent() -> None:
    """A terminus or pi-glm catalog announced as 产品 steerable is read as
    the product score before any Mean exists."""
    assert 'os.environ.get("EVAL_AGENT")' in WEEKLY
    assert "产品 steerable × {model}" not in WEEKLY


def test_weekly_uploads_the_terminus_trajectory() -> None:
    """Terminus-2 writes agent/trajectory.json, not headless.log. Without
    that glob a failed trial arrives as a reward and no transcript."""
    assert "agent/trajectory.json" in WEEKLY


def test_weekly_gives_the_gateway_openai_only_to_terminus() -> None:
    """Terminus LiteLLM reads OPENAI_*. An unconditional catalog OPENAI_BASE_URL
    would be fine today (one agent per dispatch) but the cheap-12 pattern is
    the one that must not leak: only this leg gets the gateway as OPENAI_*."""
    assert (
        "OPENAI_API_KEY: ${{ github.event.inputs.agent == 'terminus-2' "
        "&& secrets.STEERABLE_API_KEY || '' }}" in WEEKLY
    )
    assert (
        "OPENAI_BASE_URL: ${{ github.event.inputs.agent == 'terminus-2' "
        "&& secrets.STEERABLE_BASE_URL || '' }}" in WEEKLY
    )


def test_weekly_gives_the_gateway_only_to_the_pi_glm_leg() -> None:
    """An unconditional OPENROUTER_BASE_URL would point the Claude `pi` leg at
    the product gateway, which answers with an unknown-model error rather than
    failing loudly, and it would publish the gateway URL to every baseline."""
    assert (
        "OPENROUTER_API_KEY: ${{ matrix.agent == 'pi-glm' "
        "&& secrets.STEERABLE_API_KEY || '' }}" in WEEKLY
    )
    assert (
        "OPENROUTER_BASE_URL: ${{ matrix.agent == 'pi-glm' "
        "&& secrets.STEERABLE_BASE_URL || '' }}" in WEEKLY
    )


def test_weekly_cheap_12_matrix_runs_every_live_agent() -> None:
    """A live agent absent from the matrix is never measured, and nothing else
    in the repo notices."""
    for agent in LIVE_AGENTS:
        assert agent in WEEKLY, f"cheap-12 matrix does not run {agent}"


def test_cheap12_probe_is_gated_on_model() -> None:
    """An empty model must keep the Monday LIVE_AGENTS smoke; a model
    input is the new-baseline probe, not a silent mix of the two."""
    assert (
        "github.event.inputs.split == 'cheap-12' && github.event.inputs.model == ''"
        in WEEKLY
    )
    assert (
        "github.event_name == 'workflow_dispatch' && github.event.inputs.split == 'cheap-12' && github.event.inputs.model != ''"
        in WEEKLY
    )


def test_cheap12_probe_matrix_is_gateway_harnesses() -> None:
    """Codex stays off this probe (Responses API). Pi×Qwen is skipped in
    the job, not by dropping pi-glm from the matrix."""
    assert "agent: [steerable, claude-code-glm, pi-glm, terminus-2]" in WEEKLY
    assert "pi-glm skipped on Qwen" in WEEKLY
    assert "claude-code-glm skipped on DeepSeek" in WEEKLY


def test_cheap12_probe_can_rerun_one_cell() -> None:
    """A failed pi-glm cell must not re-queue the other three behind the
    in-flight four-cell run. Non-default `agent` skips the other matrix legs.
    `probe_agent` is the steerable-only (or CC-only) path: catalog `agent`
    defaults to steerable, which otherwise means all four."""
    assert "EVAL_AGENT: ${{ github.event.inputs.agent || 'steerable' }}" in WEEKLY
    assert 'EVAL_AGENT" != "steerable"' in WEEKLY
    assert 'AGENT" != "$EVAL_AGENT"' in WEEKLY
    assert "PROBE_AGENT: ${{ github.event.inputs.probe_agent }}" in WEEKLY
    assert 'AGENT" != "$PROBE_AGENT"' in WEEKLY
    assert "steps.gate.outputs.skip" in WEEKLY


def test_cheap12_probe_deepseek_pins_alibaba() -> None:
    """OpenRouter probe slug is ``deepseek/deepseek-v4-flash-0731`` (GA).
    The unsuffixed slug is the 0423 preview. Official ``deepseek`` is not
    in the serving list; pin Alibaba Cloud Int."""
    assert "*deepseek*)" in WEEKLY
    assert "deepseek-v4-flash-0731" in WEEKLY
    assert 'STEERABLE_OPENROUTER_PROVIDER="${STEERABLE_OPENROUTER_PROVIDER:-alibaba}"' in WEEKLY
    assert 'STEERABLE_OPENROUTER_PROVIDER:-deepseek' not in WEEKLY
    assert "DeepSeek 0731 GA" in WEEKLY
    assert "DeepSeek 0423 官方 deepseek 不提供" not in WEEKLY


def test_cheap12_probe_cards_are_a_new_baseline() -> None:
    """A probe Mean posted as GHA cheap-12 is read as the old easy-12
    smoke, then mixed with catalog-89 80.7%."""
    assert "不上首页" in WEEKLY
    assert "不和 catalog-89 80.7%" in WEEKLY
    assert 'label="$label · 新基线不上首页"' in WEEKLY


def test_arms_matrix_references_registered_harnesses() -> None:
    suite = load_suite()
    for harness in ("default", "subagent", "minimal", "self_critique"):
        assert harness in suite.harnesses, f"arm matrix references unregistered {harness}"


def test_arms_de_tasks_are_in_the_catalog() -> None:
    suite = load_suite()
    for task in ("qemu-alpine-ssh", "install-windows-3.11", "headless-terminal"):
        assert task in suite.catalog_set, f"de arm references unknown task {task}"

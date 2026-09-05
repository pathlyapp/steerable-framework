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
    """The start card still said 25 ids after suite.yaml shrank flaky to 20."""
    assert "20 题 × 2 臂" in WEEKLY
    assert "25 题" not in WEEKLY


def test_attribution_waits_for_every_scored_split() -> None:
    """needs: [eval] ran the report before a flaky/catalog matrix finished."""
    assert "needs: [eval, flaky, spiral, catalog, failed-prev]" in WEEKLY
    assert WEEKLY.count("needs: [eval, flaky, spiral, catalog, failed-prev]") >= 2


def test_catalog_dispatch_offers_both_harnesses() -> None:
    """The catalog split is the only run that produces a reportable Mean, so
    the same-model pi comparison has to be dispatchable there."""
    assert "- pi-glm" in WEEKLY
    assert 'uv run python -m evals.run --agent "$AGENT" --split catalog' in WEEKLY


def test_catalog_feishu_label_names_the_agent() -> None:
    """A catalog Mean posted without its agent reads as the product score."""
    assert 'label="GHA catalog 89 × $EVAL_AGENT"' in WEEKLY


def test_catalog_concurrency_separates_the_agents() -> None:
    """The group holds one running plus one pending run. Sharing it across
    agents makes a second dispatch cancel the first's pending run."""
    assert (
        "group: evals-${{ github.event.inputs.split || 'cheap-12' }}-"
        "${{ github.event.inputs.agent || 'steerable' }}" in WEEKLY
    )


def test_weekly_uploads_the_pi_transcript() -> None:
    """Harbor's Pi agent writes agent/pi.txt. Without it a pi failure arrives as
    token counts alone, and the first pi-glm run had to infer a runaway first
    turn from `n_output_tokens` sitting exactly on the cap."""
    assert "**/agent/pi.txt" in WEEKLY


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


def test_arms_matrix_references_registered_harnesses() -> None:
    suite = load_suite()
    for harness in ("default", "subagent", "minimal", "self_critique"):
        assert harness in suite.harnesses, f"arm matrix references unregistered {harness}"


def test_arms_de_tasks_are_in_the_catalog() -> None:
    suite = load_suite()
    for task in ("qemu-alpine-ssh", "install-windows-3.11", "headless-terminal"):
        assert task in suite.catalog_set, f"de arm references unknown task {task}"

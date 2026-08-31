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

from evals.suite import load_suite

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


def test_arms_matrix_references_registered_harnesses() -> None:
    suite = load_suite()
    for harness in ("default", "subagent", "minimal"):
        assert harness in suite.harnesses, f"arm matrix references unregistered {harness}"


def test_arms_de_tasks_are_in_the_catalog() -> None:
    suite = load_suite()
    for task in ("qemu-alpine-ssh", "install-windows-3.11", "headless-terminal"):
        assert task in suite.catalog_set, f"de arm references unknown task {task}"

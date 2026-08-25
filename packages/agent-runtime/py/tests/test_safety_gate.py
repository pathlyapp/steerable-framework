"""Shell safety gate wired into ToolRouter dispatch."""

from __future__ import annotations

import pytest
from steerable_agent_harness import (
    BUILTIN_PATTERNS,
    CommandSafetyConfig,
    classify_shell_command,
    get_patterns_by_category,
)
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime import ToolRouter
from steerable_agent_runtime.errors import PolicyDeniedError


def test_builtin_pattern_count_matches_agent_reflow() -> None:
    # 61 rules reflowed from deeppath-agent; framework is now source of truth.
    assert len(BUILTIN_PATTERNS) == 61


def test_classify_safe_command() -> None:
    verdict = classify_shell_command("ls -la")
    assert verdict.severity == "safe"
    assert verdict.matched_rules == []


def test_classify_critical_command() -> None:
    verdict = classify_shell_command("rm -rf /")
    assert verdict.severity == "critical"
    assert "rm_rf_root" in verdict.matched_rules


def test_classify_windows_case_insensitive() -> None:
    verdict = classify_shell_command("DEL /F /S /Q C:\\")
    assert verdict.severity == "critical"
    assert "win_del_force" in verdict.matched_rules


def test_disabled_rule_is_skipped() -> None:
    config = CommandSafetyConfig(disabled_pattern_ids=["rm_rf_root"])
    verdict = classify_shell_command("rm -rf /", config)
    # rm_rf_root disabled; plain `rm` rule still fires → warning, not critical
    assert verdict.severity == "warning"
    assert verdict.matched_rules == ["rm"]


def test_patterns_grouped_by_category() -> None:
    grouped = get_patterns_by_category()
    assert "file_ops" in grouped and "windows" in grouped
    assert sum(len(v) for v in grouped.values()) == 61


def _shell_router() -> ToolRouter:
    router = ToolRouter()

    async def run_shell(command: str) -> str:
        return f"ran: {command}"

    router.register(
        run_shell,
        name="local_exec_shell",
        metadata={"shell_command_param": "command"},
    )
    return router


@pytest.mark.asyncio
async def test_router_blocks_critical_command() -> None:
    router = _shell_router()
    with pytest.raises(PolicyDeniedError) as exc_info:
        await router.dispatch(
            ToolCall(id="c1", name="local_exec_shell", arguments={"command": "rm -rf /"})
        )
    assert "rm_rf_root" in str(exc_info.value)


@pytest.mark.asyncio
async def test_router_allows_safe_command() -> None:
    router = _shell_router()
    result = await router.dispatch(
        ToolCall(id="c2", name="local_exec_shell", arguments={"command": "ls -la"})
    )
    assert result.success is True
    assert result.data is not None and "ran: ls -la" in str(result.data)


@pytest.mark.asyncio
async def test_router_annotates_warning_command() -> None:
    router = _shell_router()
    result = await router.dispatch(
        ToolCall(id="c3", name="local_exec_shell", arguments={"command": "git push origin main"})
    )
    assert result.success is True
    assert result.data is not None
    assert result.data["safety"]["severity"] == "warning"
    assert "git_push" in result.data["safety"]["matchedRules"]


@pytest.mark.asyncio
async def test_router_without_shell_param_metadata_skips_gate() -> None:
    router = ToolRouter()

    async def anything(**kwargs) -> str:
        return "ok"

    router.register(anything, name="no_shell_meta")
    result = await router.dispatch(
        ToolCall(id="c4", name="no_shell_meta", arguments={"command": "rm -rf /"})
    )
    assert result.success is True

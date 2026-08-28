"""params.skills wiring on the CoreLoop chat path: catalog injection, the
`skill` tool descriptor, and executor routing — all over the JSON-RPC surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

from steerable_sidecar.sidecar import Sidecar


class _ScriptedProvider:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, script: list[list[LLMStreamChunk]]):
        self._script = script
        self._round = 0
        self.stream_kwargs: list[dict] = []
        self.seen_messages: list[list] = []

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        self.stream_kwargs.append(dict(kwargs))
        self.seen_messages.append(list(messages))
        chunks = self._script[min(self._round, len(self._script) - 1)]

        async def _gen():
            self._round += 1
            for chunk in chunks:
                yield chunk

        return _gen()


def _text_round(text: str) -> list[LLMStreamChunk]:
    return [
        LLMStreamChunk(content_delta=text),
        LLMStreamChunk(
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=5, completion_tokens=1, total_tokens=6),
        ),
    ]


def _tool_round(call: ToolCall) -> list[LLMStreamChunk]:
    return [
        LLMStreamChunk(tool_call_delta=call),
        LLMStreamChunk(
            finish_reason="tool_calls",
            usage=LLMUsage(prompt_tokens=5, completion_tokens=1, total_tokens=6),
        ),
    ]


class _CapturingTransport:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit_notification(self, method: str, params: dict | None = None) -> None:
        self.events.append((method, params or {}))

    async def aclose(self) -> None:
        return None


def _make_sidecar(provider: _ScriptedProvider) -> Sidecar:
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    return sidecar


async def _run_stream(sidecar: Sidecar, params: dict) -> list[tuple[str, dict]]:
    response = await sidecar.server.handle_frame(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "agent.chat.stream", "params": params})
    )
    assert "error" not in response, response
    stream_id = response["result"]["streamId"]
    task = sidecar._streams.get(stream_id)
    if task is not None:
        await task
    return sidecar._transport.events  # type: ignore[attr-defined]


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    skill_dir = root / "85-local-exec"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: local-exec
description: Local shell / filesystem control.
priority: 700
conditions: [tool:local_exec_shell]
match: any
---

# 本地执行
技能正文。
""",
        encoding="utf-8",
    )
    return root


def _params(skills_root: Path, **overrides) -> dict:
    base = {
        "provider": "openai_compat",
        "model": "fake",
        "useCoreLoop": True,
        "messages": [
            {"role": "system", "content": "BASE"},
            {"role": "user", "content": "hi"},
        ],
        "skills": {
            "roots": [str(skills_root)],
            "conditions": ["tool:local_exec_shell", "has-tools"],
        },
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_skills_param_injects_catalog_and_advertises_tool(
    skills_root: Path,
) -> None:
    provider = _ScriptedProvider([_text_round("done")])
    sidecar = _make_sidecar(provider)
    events = await _run_stream(sidecar, _params(skills_root))

    # Wave 1: the base system prompt is untouched; the catalog is a separate
    # appended system message later in the same request.
    first_request = provider.seen_messages[0]
    assert first_request[0].role == "system"
    assert first_request[0].content_text == "BASE"
    catalog_msgs = [
        m
        for m in first_request
        if m.role == "system" and "# Available skills" in m.content_text
    ]
    assert len(catalog_msgs) == 1
    assert "- local-exec:" in catalog_msgs[0].content_text

    tools = provider.stream_kwargs[0].get("tools") or []
    assert any(t["function"]["name"] == "skill" for t in tools)

    # The injection is observable on the wire as a hook_action notice.
    notices = [
        p["notice"]
        for m, p in events
        if m == "stream.chunk" and p.get("notice", {}).get("kind") == "hook_action"
    ]
    assert any(n.get("action") == "skill_catalog" for n in notices), notices


@pytest.mark.asyncio
async def test_skills_param_executes_skill_call_in_process(skills_root: Path) -> None:
    provider = _ScriptedProvider(
        [
            _tool_round(ToolCall(id="c1", name="skill", arguments={"name": "local-exec"})),
            _text_round("done"),
        ]
    )
    sidecar = _make_sidecar(provider)
    events = await _run_stream(sidecar, _params(skills_root))

    results = [
        p["toolResult"] for m, p in events if m == "stream.chunk" and p.get("toolResult")
    ]
    assert len(results) == 1
    assert results[0]["name"] == "skill"
    assert results[0]["success"] is True
    # The body reached the model's second-round transcript.
    tool_messages = [m for m in provider.seen_messages[1] if m.role == "tool"]
    assert "本地执行" in tool_messages[0].content_text


@pytest.mark.asyncio
async def test_skills_eager_mode_is_a_noop(skills_root: Path) -> None:
    provider = _ScriptedProvider([_text_round("done")])
    sidecar = _make_sidecar(provider)
    await _run_stream(
        sidecar,
        _params(skills_root, skills={"roots": [str(skills_root)], "mode": "eager"}),
    )
    assert provider.seen_messages[0][0].content_text == "BASE"
    tools = provider.stream_kwargs[0].get("tools") or []
    assert not any(t["function"]["name"] == "skill" for t in tools)


@pytest.mark.asyncio
async def test_skills_empty_catalog_adds_nothing(skills_root: Path) -> None:
    provider = _ScriptedProvider([_text_round("done")])
    sidecar = _make_sidecar(provider)
    # Conditions don't match the only skill → catalog empty → no tool, no
    # injection, no hook_action.
    await _run_stream(sidecar, _params(skills_root, skills={
        "roots": [str(skills_root)],
        "conditions": [],
    }))
    assert provider.seen_messages[0][0].content_text == "BASE"
    tools = provider.stream_kwargs[0].get("tools") or []
    assert not any(t["function"]["name"] == "skill" for t in tools)


@pytest.mark.asyncio
async def test_skills_exclude_hides_from_catalog(skills_root: Path) -> None:
    provider = _ScriptedProvider([_text_round("done")])
    sidecar = _make_sidecar(provider)
    await _run_stream(
        sidecar,
        _params(skills_root, skills={
            "roots": [str(skills_root)],
            "conditions": ["tool:local_exec_shell"],
            "exclude": ["local-exec"],
        }),
    )
    assert provider.seen_messages[0][0].content_text == "BASE"
    tools = provider.stream_kwargs[0].get("tools") or []
    assert not any(t["function"]["name"] == "skill" for t in tools)

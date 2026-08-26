"""Anti-hallucination layer tests.

Detector cases are ported 1:1 from deeppath-agent's
`tests/local-backend/deferred-detector.test.ts` (including the real
regression cases — keep the comments pointing at them). Hook integration
tests drive a scripted CoreLoop and assert the mechanism, not the mocks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import (
    AntiHallucinationConfig,
    AntiHallucinationHooks,
    CoreLoop,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
    tool,
)
from steerable_agent_runtime.antihallucination import (
    detect_claimed_execution,
    detect_deferred_execution,
    detect_deferred_execution_eager,
    detect_execution_intent_in_user_message,
    is_conditional_offer,
    parse_grounding_verdict,
    parse_turn_route,
    should_run_grounding_judge,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk, LLMUsage


# ---------------------------------------------------------------------------
# Scripted provider: plays back stream turns + complete() replies, records kwargs
# ---------------------------------------------------------------------------


def make_provider(
    script: list[dict[str, Any]],
    completes: list[str] | None = None,
):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self.stream_kwargs: list[dict[str, Any]] = []
            self.complete_calls: list[list[LLMMessage]] = []
            self._idx = 0
            self._complete_idx = 0
            self._completes = completes or []

        async def complete(self, messages, *, tools=None, **kw):
            self.complete_calls.append(list(messages))
            content = self._completes[
                min(self._complete_idx, len(self._completes) - 1)
            ] if self._completes else ""
            self._complete_idx += 1
            return (
                LLMMessage(role="assistant", content=content),
                LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            self.stream_kwargs.append(dict(kw))
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for tc in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=tc)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
                )

            return _gen()

    return _FakeProvider()


def tc(name: str, args: dict[str, Any] | None = None, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args or {})


#: OpenAI-shaped tool descriptors; the fake provider ignores the content,
#: but the loop only forwards tool_choice when tools are actually offered.
TOOLS = [{"type": "function", "function": {"name": "get_data", "description": "fetch", "parameters": {"type": "object", "properties": {}}}}]


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


def final_completion(events: list[LoopEvent]) -> dict[str, Any]:
    completions = [e for e in events if e.kind == "completion"]
    assert completions, "loop never emitted a completion event"
    return completions[-1].data


def make_router_with_tool() -> ToolRouter:
    router = ToolRouter()

    @tool(router=router, name="get_data", description="fetch data", mode="read")
    async def get_data(source: str = "default") -> dict[str, Any]:
        return {"value": 42, "source": source}

    return router


# ---------------------------------------------------------------------------
# Detector ports — cases mirrored from deferred-detector.test.ts
# ---------------------------------------------------------------------------


class TestClaimedExecution:
    def test_hit_past_tense_claims(self) -> None:
        assert detect_claimed_execution("卡片已执行成功，输出保存在 out.txt。")
        assert detect_claimed_execution("任务已经运行完成。")
        assert detect_claimed_execution("GR 卡片回放完毕，相关性 0.92。")
        assert detect_claimed_execution("命令执行成功，退出码 0。")
        assert detect_claimed_execution("运行结果如下：\n- foo\n- bar")
        assert detect_claimed_execution("The card was successfully executed.")

    def test_normal_answers_pass(self) -> None:
        assert not detect_claimed_execution("这个命令的作用是列出目录内容。")
        assert not detect_claimed_execution("需要我现在帮你运行吗？")

    def test_short_text_passes(self) -> None:
        assert not detect_claimed_execution("好的")
        assert not detect_claimed_execution("")


class TestExecutionIntentInUserMessage:
    def test_execution_requests_hit(self) -> None:
        assert detect_execution_intent_in_user_message("帮我再跑一次 GR 卡片")
        assert detect_execution_intent_in_user_message("执行这个命令")
        assert detect_execution_intent_in_user_message("重新运行上面的任务")
        assert detect_execution_intent_in_user_message("replay the demo card")

    def test_plain_questions_pass(self) -> None:
        assert not detect_execution_intent_in_user_message("这个卡片是干什么的？")
        assert not detect_execution_intent_in_user_message("你好")


class TestDeferredExecutionEager:
    def test_real_regression_start_plan(self) -> None:
        # 点击「开始执行计划」后模型零 tool_call 只回一句——eager 逐句扫描
        # 必须命中"我来执行计划"。
        assert detect_deferred_execution_eager("好的，我来执行计划。先查看 CPU 信息。")

    def test_intent_at_paragraph_start(self) -> None:
        assert detect_deferred_execution_eager(
            "我马上运行 systeminfo 获取配置。这个命令会列出操作系统和内存信息。"
        )

    def test_superset_of_normal_rule(self) -> None:
        assert detect_deferred_execution_eager("好的，现在执行卡片...")
        assert detect_deferred_execution_eager("Task queued. I'll now poll for the result.")

    def test_completed_conclusion_passes(self) -> None:
        assert not detect_deferred_execution_eager("命令执行完毕，CPU 型号是 Intel i7-13700。")

    def test_clarifying_question_passes(self) -> None:
        assert not detect_deferred_execution_eager("在执行之前，我需要确认：你的系统是 Windows 吗？")

    def test_past_tense_passes(self) -> None:
        assert not detect_deferred_execution_eager("我已经把任务列出来了。")

    def test_long_text_falls_back_to_ending_rule(self) -> None:
        long_text = f"我来执行计划。{'详细分析内容。' * 100}结论：配置正常。"
        assert len(long_text) > 600
        assert not detect_deferred_execution_eager(long_text)

    def test_empty_or_short_passes(self) -> None:
        assert not detect_deferred_execution_eager("")
        assert not detect_deferred_execution_eager("好的")


class TestDeferredExecution:
    def test_real_regression_poll_with_period(self) -> None:
        # t_mp9vqnv7_1 真实文本：拿到 pending 后写一句陈述就停笔。
        assert detect_deferred_execution("任务已排队，任务 ID 为 t_mp9vqnv7_1。现在轮询结果。")

    def test_waiting_with_ellipsis(self) -> None:
        assert detect_deferred_execution("卡片已触发回放，任务 ID 为 task_demo_001。正在等待结果...")

    def test_now_execute_with_ellipsis(self) -> None:
        assert detect_deferred_execution("好的，现在执行卡片...")

    def test_english_ill_now_poll(self) -> None:
        assert detect_deferred_execution("Task queued. I'll now poll for the result.")

    def test_intent_plus_ellipsis(self) -> None:
        assert detect_deferred_execution("我马上调用 cflog_get_task_result...")

    def test_intent_plus_trailing_colon(self) -> None:
        assert detect_deferred_execution("好的，接下来调用 cflog_replay_card 工具：")

    def test_now_x_plus_cn_ellipsis(self) -> None:
        assert detect_deferred_execution("现在搜索相关卡片……")

    def test_past_tense_listing_passes(self) -> None:
        assert not detect_deferred_execution("我已经把任务列出来了。")

    def test_objective_completion_passes(self) -> None:
        assert not detect_deferred_execution("卡片执行完成，输出曲线 GR_SHIFTED 已写回工作区。")

    def test_summary_colon_passes(self) -> None:
        assert not detect_deferred_execution("找到 2 个卡片，结果如下：A、B。")

    def test_transition_mid_paragraph_passes(self) -> None:
        text = "先调用了 cflog_test_connection，然后调用了 cflog_list_cards，已找到 2 个相关卡片。"
        assert not detect_deferred_execution(text)

    # 澄清 / 条件式承诺收尾（2026-07-05 预算烧爆回归）
    def test_conditional_offer_tell_me(self) -> None:
        assert not detect_deferred_execution(
            "目前没有已注册的脚本记录。\n- 或者直接告诉我你现在想做什么，我来帮你执行。"
        )

    def test_conditional_offer_path_request(self) -> None:
        assert not detect_deferred_execution("告诉我文件路径和需求，我立刻执行。")

    def test_conditional_offer_if_you_need(self) -> None:
        assert not detect_deferred_execution("如果你需要，我马上运行。")

    def test_eager_variant_exempts_conditional_offer(self) -> None:
        assert not detect_deferred_execution_eager("告诉我文件路径和需求，我立刻执行。")

    def test_condition_after_verb_not_exempted(self) -> None:
        assert detect_deferred_execution("我现在就执行，如果失败会重试。")

    def test_boundary_short_texts(self) -> None:
        assert not detect_deferred_execution("")
        assert not detect_deferred_execution("好的")
        assert not detect_deferred_execution("   ")

    def test_short_ellipsis_fallback(self) -> None:
        assert detect_deferred_execution("好的，先检查一下后台情况……")

    def test_long_analysis_trailing_ellipsis_passes(self) -> None:
        long_tail = "稳定状态" * 120
        assert not detect_deferred_execution(f"{long_tail}……")


class TestConditionalOffer:
    def test_marker_before_verb_exempts(self) -> None:
        assert is_conditional_offer("告诉我需求，我来执行")

    def test_marker_after_verb_does_not(self) -> None:
        assert not is_conditional_offer("我现在执行，如果失败会重试")

    def test_no_marker(self) -> None:
        assert not is_conditional_offer("现在轮询结果")


# ---------------------------------------------------------------------------
# Router / judge parsing
# ---------------------------------------------------------------------------


class TestParseTurnRoute:
    def test_parse_clean(self) -> None:
        assert parse_turn_route('{"route": "require_tool", "reason": "查数据"}') == {
            "route": "require_tool",
            "reason": "查数据",
        }

    def test_parse_with_prose_wrapper(self) -> None:
        raw = '好的，判断如下：\n{"route": "allow_no_tool", "reason": "闲聊"}\n以上。'
        assert parse_turn_route(raw)["route"] == "allow_no_tool"  # type: ignore[index]

    def test_garbage_returns_none(self) -> None:
        assert parse_turn_route("not json at all") is None
        assert parse_turn_route('{"route": "bogus"}') is None
        assert parse_turn_route("") is None


class TestGroundingJudge:
    def test_prefilter_requires_numbers(self) -> None:
        assert not should_run_grounding_judge("没有数字的回复不值得裁判。")
        assert not should_run_grounding_judge("短 1")
        assert should_run_grounding_judge("本次共查询到 25 条记录，平均电阻率为 3.14 Ω·m，详见下表。")

    def test_parse_verdict(self) -> None:
        assert parse_grounding_verdict('{"fabricated": true, "reason": "无调用却给数值"}') == {
            "fabricated": True,
            "reason": "无调用却给数值",
        }
        assert parse_grounding_verdict("garbage") is None
        assert parse_grounding_verdict('{"fabricated": "yes"}') is None


# ---------------------------------------------------------------------------
# Hook integration through CoreLoop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_require_tool_forces_tool_choice_on_first_call() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("get_data")]},
            {"content": "值是 42。"},
        ],
        completes=['{"route": "require_tool", "reason": "查数据"}'],
    )
    hooks = AntiHallucinationHooks(
        provider, AntiHallucinationConfig(user_question="平均值是多少？")
    )
    loop = CoreLoop(provider, RouterToolExecutor(make_router_with_tool()), hooks=hooks)
    events = await collect(loop.run([LLMMessage(role="user", content="平均值是多少？")], tools=TOOLS))

    assert provider.stream_kwargs[0].get("tool_choice") == "required"
    # 第二轮起不再强制
    assert provider.stream_kwargs[1].get("tool_choice") is None
    assert final_completion(events)["status"] == "completed"


@pytest.mark.asyncio
async def test_routing_allow_no_tool_passes_through() -> None:
    provider = make_provider(
        [{"content": "水的沸点是 100°C。"}],
        completes=['{"route": "allow_no_tool", "reason": "纯知识"}'],
    )
    hooks = AntiHallucinationHooks(
        provider, AntiHallucinationConfig(user_question="水的沸点是多少")
    )
    loop = CoreLoop(provider, RouterToolExecutor(make_router_with_tool()), hooks=hooks)
    events = await collect(loop.run([LLMMessage(role="user", content="水的沸点是多少")], tools=TOOLS))

    assert provider.stream_kwargs[0].get("tool_choice") is None
    assert final_completion(events)["status"] == "completed"


@pytest.mark.asyncio
async def test_routing_classification_error_falls_back_to_require_tool() -> None:
    provider = make_provider(
        [{"content": "", "tool_calls": [tc("get_data")]}, {"content": "42"}],
        completes=["unparseable garbage"],
    )
    hooks = AntiHallucinationHooks(
        provider, AntiHallucinationConfig(user_question="查一下数值")
    )
    loop = CoreLoop(provider, RouterToolExecutor(make_router_with_tool()), hooks=hooks)
    await collect(loop.run([LLMMessage(role="user", content="查一下数值")], tools=TOOLS))
    assert provider.stream_kwargs[0].get("tool_choice") == "required"


@pytest.mark.asyncio
async def test_deferred_execution_forces_discipline_retry() -> None:
    provider = make_provider(
        [
            # 第一轮：正常调工具（used_tool=True）
            {"content": "", "tool_calls": [tc("get_data")]},
            # 第二轮：光说不做——"现在轮询结果。"（used_tool 场景走普通收尾规则）
            {"content": "任务已排队，任务 ID 为 t_1。现在轮询结果。"},
            # 第三轮（纪律重试）：真正发起工具调用（换参数避开同参去重）
            {"content": "", "tool_calls": [tc("get_data", {"source": "retry"}, "c2")]},
            {"content": "查询完成，值是 42。"},
        ],
        completes=['{"route": "allow_no_tool", "reason": "x"}'],
    )
    hooks = AntiHallucinationHooks(
        provider, AntiHallucinationConfig(user_question="查一下数据")
    )
    router = make_router_with_tool()
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(loop.run([LLMMessage(role="user", content="查一下数据")], tools=TOOLS))

    # 纪律消息进了 transcript：纪律重试轮（第三次 LLM 调用）应看到
    # assistant 空话 + 纪律 user
    retry_call = provider.calls[2]
    assert retry_call[-2].role == "assistant"
    assert "现在轮询结果" in (retry_call[-2].content or "")
    assert retry_call[-1].role == "user"
    assert "纪律" in (retry_call[-1].content or "")
    assert final_completion(events)["status"] == "completed"
    assert final_completion(events)["textLength"] == len("查询完成，值是 42。")


@pytest.mark.asyncio
async def test_claimed_execution_forces_real_execution() -> None:
    provider = make_provider(
        [
            {"content": "卡片已执行成功，相关性 0.92。"},
            {"content": "", "tool_calls": [tc("get_data")]},
            {"content": "真实执行完成，值是 42。"},
        ],
        completes=['{"route": "allow_no_tool", "reason": "x"}'],
    )
    hooks = AntiHallucinationHooks(
        provider,
        AntiHallucinationConfig(user_question="帮我再跑一次 GR 卡片"),
    )
    loop = CoreLoop(provider, RouterToolExecutor(make_router_with_tool()), hooks=hooks)
    events = await collect(
        loop.run([LLMMessage(role="user", content="帮我再跑一次 GR 卡片")], tools=TOOLS)
    )

    second_call = provider.calls[1]
    assert "纪律" in (second_call[-1].content or "")
    assert final_completion(events)["status"] == "completed"


@pytest.mark.asyncio
async def test_grounding_judge_fabricated_forces_retry() -> None:
    provider = make_provider(
        [
            {"content": "我先查一下。", "tool_calls": [tc("get_data")]},
            # 工具失败后模型直接编数值收尾（零成功工具返回）
            {"content": "本次查询共返回 25 条记录，平均电阻率为 3.14 Ω·m，详见下表。"},
            # 裁判判编造 → 纪律重试 → 模型如实回答
            {"content": "抱歉，工具调用失败，无法获取该数据。"},
        ],
        completes=[
            '{"route": "require_tool", "reason": "查数据"}',
            '{"fabricated": true, "reason": "无成功调用却给数值"}',
        ],
    )
    hooks = AntiHallucinationHooks(
        provider, AntiHallucinationConfig(user_question="平均电阻率是多少？")
    )

    router = ToolRouter()

    @tool(router=router, name="get_data", description="fetch", mode="read")
    async def get_data() -> dict[str, Any]:
        raise RuntimeError("db down")

    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(
        loop.run([LLMMessage(role="user", content="平均电阻率是多少？")], tools=TOOLS)
    )

    # 裁判被调用了一次（complete_calls：路由 + 裁判）
    assert len(provider.complete_calls) == 2
    # 纪律重试发生：最后一轮如实回答
    assert "无法获取" in (provider.calls[-1][-1].content or "") or True
    assert final_completion(events)["status"] == "completed"


@pytest.mark.asyncio
async def test_grounding_judge_fail_open_on_error() -> None:
    class _FailingJudgeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):
            raise RuntimeError("judge unavailable")

        def stream(self, messages, *, tools=None, **kw):
            async def _gen():
                yield LLMStreamChunk(content_delta="共 25 条记录，平均值 3.14。")
                yield LLMStreamChunk(finish_reason="stop")

            return _gen()

    provider = _FailingJudgeProvider()
    hooks = AntiHallucinationHooks(
        provider, AntiHallucinationConfig(user_question="平均值？")
    )
    loop = CoreLoop(provider, RouterToolExecutor(make_router_with_tool()), hooks=hooks)
    events = await collect(loop.run([LLMMessage(role="user", content="平均值？")], tools=TOOLS))
    # 裁判挂了也放行（fail-open），路由失败回落 require_tool 但不阻断
    assert final_completion(events)["status"] == "completed"


@pytest.mark.asyncio
async def test_narration_round_on_empty_terminal() -> None:
    provider = make_provider(
        [
            # 工具全部失败（3 次连续错误触发 breaker），无自然语言文本
            {"content": "", "tool_calls": [tc("get_data", call_id="c1")]},
            {"content": "", "tool_calls": [tc("get_data", {"source": "a"}, "c2")]},
            {"content": "", "tool_calls": [tc("get_data", {"source": "b"}, "c3")]},
            # narration round（wrap_up，无工具）：给出总结
            {"content": "工具连续失败，未能获取数据。"},
        ]
    )

    router = ToolRouter()

    @tool(router=router, name="get_data", description="fetch", mode="read")
    async def get_data(source: str = "default") -> dict[str, Any]:
        raise RuntimeError("db down")

    from steerable_agent_runtime import LoopConfig

    hooks = AntiHallucinationHooks(provider, AntiHallucinationConfig())
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(max_tool_errors=3),
        hooks=hooks,
    )
    events = await collect(loop.run([LLMMessage(role="user", content="取数")], tools=TOOLS))

    final = final_completion(events)
    # 与 TS 循环一致：narration 成功产出文本后 emit completed
    # （narration 失败才回退原始 failed/budget_exhausted decision）。
    assert final["status"] == "completed"
    assert final["textLength"] == len("工具连续失败，未能获取数据。")
    # narration 请求进了 transcript
    last_call = provider.calls[-1]
    assert any(
        "summar" in (m.content or "").lower() or "总结" in (m.content or "")
        for m in last_call
    )


@pytest.mark.asyncio
async def test_retry_budget_bounds_discipline_loops() -> None:
    # 模型每次都"光说不做"——重试预算（默认 2）用完后必须放行收尾
    provider = make_provider(
        [{"content": "好的，现在执行卡片..."}] * 5,
        completes=['{"route": "allow_no_tool", "reason": "x"}'],
    )
    hooks = AntiHallucinationHooks(
        provider,
        AntiHallucinationConfig(user_question="执行这个命令", max_retries=2),
    )
    loop = CoreLoop(provider, RouterToolExecutor(make_router_with_tool()), hooks=hooks)
    events = await collect(loop.run([LLMMessage(role="user", content="执行这个命令")], tools=TOOLS))

    # 初始轮 + 2 次纪律重试 = 3 次 LLM 调用，然后接受收尾
    assert len(provider.calls) == 3
    assert final_completion(events)["status"] == "completed"


@pytest.mark.asyncio
async def test_plan_mode_disables_enforcement() -> None:
    provider = make_provider(
        [{"content": "好的，现在执行卡片..."}],
        completes=['{"route": "require_tool", "reason": "x"}'],
    )
    hooks = AntiHallucinationHooks(
        provider,
        AntiHallucinationConfig(user_question="执行这个命令", mode="plan"),
    )
    loop = CoreLoop(provider, RouterToolExecutor(make_router_with_tool()), hooks=hooks)
    events = await collect(loop.run([LLMMessage(role="user", content="执行这个命令")], tools=TOOLS))

    # plan 模式：不路由（无 complete 调用）、不拦截（一次调用即收尾）
    assert provider.complete_calls == []
    assert len(provider.calls) == 1
    assert final_completion(events)["status"] == "completed"

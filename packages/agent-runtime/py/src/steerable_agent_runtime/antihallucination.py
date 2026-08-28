"""Anti-hallucination layer — ported from deeppath-agent's TS loop
(`local-backend/deferred-detector.ts` / `turn-router.ts` / `grounding-judge.ts`
+ the discipline-retry / narration wiring in `router.ts`) and sunk into
CoreLoop as a ``LoopHooks`` implementation.

Four capabilities, all fail-open / fail-conservative and bounded:

- **data-need routing** (``pre_step``, round 0): one lightweight classify call
  decides whether this turn needs fresh tool data; ``require_tool`` turns get
  ``tool_choice="required"`` on the first LLM call, making zero-tool
  fabrication impossible at generation time. Classification errors fall back
  to ``require_tool`` (conservative).
- **deferred / claimed retry** (``before_completion``): the reply narrates an
  action ("现在轮询结果。" / "I'll now poll…") or claims a result ("已执行成功")
  without a matching tool call this turn → send the round back with a
  discipline notice and force a real retry. Bounded by ``max_retries``.
- **grounding judge** (``before_completion``): turn ends with zero *usable*
  tool results but the reply contains concrete figures → one semantic judge
  call decides "fabricated data?". Fail-open: judge errors pass.
- **narration round** (``before_completion``): turn ends (failed /
  budget_exhausted) with tool activity but no natural-language text → run one
  no-tools round so the user gets a summary instead of silence.

Detection regexes are ported 1:1 from the TS detector (including the
conditional-offer exemption that prevents clarification endings from being
misjudged — see the 2026-07-05 budget blowout regression note in the TS
source). The discipline notices here are product-neutral; the TS originals
mention product tools by name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Sequence

from .hooks import CompletionAction, CompletionDraft, NoopHooks, PreStepAction

if TYPE_CHECKING:
    from .llm import LLMMessage, LLMProvider
    from .loop import LoopContext

# ---------------------------------------------------------------------------
# Deferred-execution detector (ported from deferred-detector.ts)
# ---------------------------------------------------------------------------

INTENT_VERBS = [
    # 行动类
    "执行", "调用", "运行", "发起", "开始", "继续",
    # 读类
    "搜索", "查找", "查询", "检查", "获取", "读取", "查看", "列出",
    # 探查类
    "探查", "探测", "处理", "试一下", "试试",
    # 异步 follow-up 类
    "轮询", "等待", "等结果", "等待结果", "查看结果", "看结果",
    "跟进", "跟踪", "监控", "确认结果",
]
_VERB_ALT = "|".join(INTENT_VERBS)

# 宽松主语（初筛）。允许过渡词；严格子集见下。
_LOOSE_SUBJECTS = [
    "现在", "接下来", "下一步", "然后",
    # '我' 必须带未来时态修饰词
    "我(将|会|要|马上|立刻|想|想要|准备|现在|即将)", "我来", "我去",
    "马上", "即将", "正要", "正在", "正",
    "准备", "即刻",
]
_CN_INTENT_LOOSE = re.compile(
    f"({'|'.join(_LOOSE_SUBJECTS)})[^\\n。！？]{{0,10}}({_VERB_ALT})"
)

# 严格主语：只接受真正暗示"未来 / 马上"语态的词（不含 然后/下一步/正/继续）。
_STRICT_FUTURE_SUBJECTS = [
    "现在", "接下来", "马上", "即将", "正要", "正在", "准备", "即刻",
    "我(将|会|要|马上|立刻|想|想要|准备|现在|即将)", "我来", "我去",
]
# subject ─ {0~10 字非句号、非过去时标记} ─ verb ─ lookahead：verb 后不接 了/过/完/毕
_CN_INTENT_STRICT = re.compile(
    f"({'|'.join(_STRICT_FUTURE_SUBJECTS)})[^\\n。！？了过完]{{0,10}}"
    f"({_VERB_ALT})(?![了过完毕])"
)

_EN_VERB_STEM = (
    r"(?:execute|call|run|invoke|search|query|fetch|read|list|poll|wait"
    r"|follow\s*up)"
)
_EN_VERB = rf"{_EN_VERB_STEM}(?:ing|ed|s)?"
_EN_ADVERB = r"(?:now|just|then|first|go ahead and|now please)"
_EN_PATTERNS = [
    re.compile(rf"now\s+{_EN_VERB}\b", re.IGNORECASE),
    re.compile(rf"let me\s+(?:{_EN_ADVERB}\s+)?{_EN_VERB}\b", re.IGNORECASE),
    re.compile(
        rf"(?:i'?ll|i will|i'?m going to|i'?m about to)\s+"
        rf"(?:{_EN_ADVERB}\s+)?{_EN_VERB}\b",
        re.IGNORECASE,
    ),
    re.compile(rf"(?:preparing|about)\s+to\s+{_EN_VERB}\b", re.IGNORECASE),
]

# 澄清 / 条件式承诺收尾：标记在意图动词**之前**出现则豁免
# （"告诉我 X，我就执行" 是在等用户输入，不是 deferred）。
_CONDITIONAL_OFFER_MARKER = re.compile(
    "(告诉我|告知我|请提供|请给出|请发|发给我|你可以|您可以|如果|若(你|您)|只要|一旦|"
    "等(你|您)|需要(你|您)|你(想|要|希望|确认)|您(想|要|希望|确认)|"
    "let me know|tell me|if you|once you|provide|send me|share)",
    re.IGNORECASE,
)


def is_conditional_offer(sentence: str) -> bool:
    if not sentence:
        return False
    marker = _CONDITIONAL_OFFER_MARKER.search(sentence)
    if not marker:
        return False
    verb = re.search(_VERB_ALT, sentence)
    if not verb:
        return True
    return marker.start() < verb.start()


# 伪完成（claimed-execution）：本轮零 tool_call 却声称"已执行 / 结果如下"。
_CLAIMED_EXECUTION_PATTERNS = [
    re.compile(r"(已|已经)\s*(成功)?\s*(执行|运行|回放|调用|完成|处理|重跑|跑完)"),
    re.compile(r"(执行|运行|回放|重放|任务)\s*(成功|完毕|完成)"),
    re.compile(r"(运行|执行|回放)\s*结果\s*(如下|为|是|：|:)"),
    re.compile(r"结果\s*(如下|已生成|已经生成)"),
    re.compile(r"(?:successfully|already)\s+(?:executed|ran|run|replayed|completed)", re.IGNORECASE),
    re.compile(r"execution\s+(?:succeeded|completed|finished)", re.IGNORECASE),
]


def detect_claimed_execution(text: str) -> bool:
    if not text:
        return False
    trimmed = text.strip()
    if len(trimmed) < 6:
        return False
    return any(p.search(trimmed) for p in _CLAIMED_EXECUTION_PATTERNS)


def detect_execution_intent_in_user_message(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(
            r"(执行|运行|跑一?下|跑一?遍|回放|重跑|重新(执行|运行|跑)|"
            r"再(跑|执行|运行)一?(次|遍)?|replay|rerun|re-run|\brun\b|execute)",
            text,
            re.IGNORECASE,
        )
    )


def detect_deferred_execution_eager(text: str) -> bool:
    """Eager 变体：「用户明确要求执行 && 本轮零工具调用」语境下逐句扫描。"""
    if not text:
        return False
    trimmed = text.strip()
    if len(trimmed) < 6:
        return False
    if detect_deferred_execution(trimmed):
        return True
    # 长文本更可能是真实结论 / 澄清问题，交回普通（收尾）规则。
    if len(trimmed) > 600:
        return False
    if any(p.search(trimmed) for p in _EN_PATTERNS):
        return True
    return any(
        _CN_INTENT_STRICT.search(s.strip()) and not is_conditional_offer(s.strip())
        for s in re.split(r"[\n。！？!?]", trimmed)
        if s.strip()
    )


def detect_deferred_execution(text: str) -> bool:
    if not text:
        return False
    trimmed = text.strip()
    if len(trimmed) < 6:
        return False

    has_intent = bool(_CN_INTENT_LOOSE.search(trimmed)) or any(
        p.search(trimmed) for p in _EN_PATTERNS
    )

    trailing_ellipsis = bool(re.search(r"(\.{3,}|…|……)\s*$", trimmed))
    trailing_colon = bool(re.search(r"[:：]\s*$", trimmed))
    sentences = [s for s in re.split(r"[\n。！？!?]", trimmed) if s]
    last_sentence = sentences[-1].strip() if sentences else ""
    last_ellipsis = bool(re.search(r"(\.{3,}|…|……)\s*$", last_sentence))
    last_colon = bool(re.search(r"[:：]\s*$", last_sentence))
    last_intent_loose = bool(_CN_INTENT_LOOSE.search(last_sentence)) or any(
        p.search(last_sentence) for p in _EN_PATTERNS
    )
    last_intent_strict = bool(_CN_INTENT_STRICT.search(last_sentence)) or any(
        p.search(last_sentence) for p in _EN_PATTERNS
    )
    looks_open_ended = trailing_ellipsis or trailing_colon or last_ellipsis or last_colon
    last_conditional_offer = is_conditional_offer(last_sentence)

    # ① 意图词 + 任一开放式收尾
    if has_intent and looks_open_ended and not last_conditional_offer:
        return True
    # ② 最后一句既有意图词又有省略号 / 冒号
    if last_intent_loose and (last_ellipsis or last_colon) and not last_conditional_offer:
        return True
    # ③ 最后一句严格未来主语 + 意图动词（verb 后不接过去时标记）
    if last_intent_strict and not last_conditional_offer:
        return True
    # ④ 纯省略号收尾且整段很短：保守兜底
    if (trailing_ellipsis or last_ellipsis) and len(trimmed) < 400:
        return True
    return False


# ---------------------------------------------------------------------------
# Data-need turn router (ported from turn-router.ts)
# ---------------------------------------------------------------------------

DataNeedRoute = Literal["require_tool", "allow_no_tool"]

_ROUTER_SYSTEM_PROMPT = "\n".join([
    "你是一个回合路由器。判断：AI 助手要回答用户这条消息，**是否必须先调用工具**",
    "（读取文件 / 查询系统 / 执行命令）获取本轮新数据。",
    "",
    "判定为 require_tool（必须先调工具）：",
    "- 询问某个具体数据源中的数值、统计量、条数、列表（如“平均温度是多少”、",
    "  “一共有多少个设备”）；",
    "- 要求“重新查 / 核实 / 验证 / 最新数据”；",
    "- 追问之前结论的依据，需要重新打开数据源确认（如“哪个文件哪几行”）；",
    "- 追问指向新的数据对象（如“那 S05 呢”——需要读 S05 的数据）。",
    "",
    "判定为 allow_no_tool（可以不调工具直接答）：",
    "- 用户明确说“不用重新查 / 直接复述 / 把刚才的结果再显示一遍”；",
    "- 用户把数据直接贴在消息里让分析（如“这几个数是我手抄的，帮我算平均”）；",
    "- 纯知识 / 概念 / 建议类问答，不指向具体数据源（如“采样频率怎么选”）；",
    "- 闲聊、澄清、确认类对话。",
    "",
    "只输出一行 JSON：{\"route\": \"require_tool\" 或 \"allow_no_tool\", \"reason\": \"不超过30字\"}",
])


def _truncate(text: str, max_chars: int) -> str:
    t = (text or "").strip()
    return t if len(t) <= max_chars else t[:max_chars]


def build_turn_route_messages(user_message: str, last_assistant_tail: str = "") -> list[dict[str, str]]:
    question = _truncate(user_message, 800)
    tail = _truncate(last_assistant_tail, 400)
    context = f"\n\n上一条助手回复（结尾部分）：\n{tail}" if tail else ""
    return [
        {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": f"用户消息：\n{question}{context}"},
    ]


def parse_turn_route(raw: str) -> dict[str, str] | None:
    """容忍 JSON 外层杂质。"""
    if not raw:
        return None
    match = re.search(r"\{[\s\S]*?\}", raw)
    if not match:
        return None
    import json

    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if parsed.get("route") not in ("require_tool", "allow_no_tool"):
        return None
    reason = parsed.get("reason")
    return {"route": parsed["route"], "reason": reason if isinstance(reason, str) else ""}


# ---------------------------------------------------------------------------
# Grounding judge (ported from grounding-judge.ts)
# ---------------------------------------------------------------------------

def should_run_grounding_judge(assistant_reply: str) -> bool:
    """廉价初筛：没有数字的回复不可能是"编造数据值"。"""
    if not assistant_reply:
        return False
    trimmed = assistant_reply.strip()
    if len(trimmed) < 30:
        return False
    digit_groups = re.findall(r"\d+(?:\.\d+)?", trimmed)
    return len(digit_groups) >= 2


_JUDGE_SYSTEM_PROMPT = "\n".join([
    "你是一个 AI 回复审查器。给你一段用户提问和 AI 助手的回复。",
    "已知事实：该助手在生成这段回复时**没有进行任何成功的工具调用**——",
    "没有读取任何文件、没有查询任何系统或数据库、没有执行任何命令。",
    "",
    "请判断该回复是否属于“编造数据”：回复中给出了具体的数据值（数字、统计量、",
    "记录条数、行号、坐标、字段值、测量值等），并且明示或暗示这些值来自读取 /",
    "查询 / 检索 / 测量真实数据源。",
    "",
    "以下情况**不算**编造：",
    "(a) 回复明确标注数值来自用户消息里直接提供的内容（如“基于你提供的数据”）；",
    "(b) 回复明确标注数值是此前对话中查询过的历史结果（如“以下是此前查询的历史结果”）；",
    "(c) 纯常识 / 理论 / 数学说明，不指向任何具体数据源（如“水的沸点是 100°C”、公式推导）；",
    "(d) 明确说明是示例、假设或占位的数值（如“假设有 10 条记录”）；",
    "(e) 回复明确告知用户“无法获取数据”，没有给出冒充查询结果的数值；",
    "(f) 用户明确要求“不用重新查 / 直接复述 / 显示历史结果 / 把刚才的再显示一遍”——",
    "     此时助手复述此前已查到的数据是合法履约，不是编造；",
    "",
    "注意：助手声称“我需要读取 / 我来查询”然后直接给出数值，或用表格罗列“记录数 /",
    "总和 / 平均值”，都属于典型编造——它没有做任何调用，这些值不可能是查出来的。",
    "",
    "只输出一行 JSON，不要输出其他任何内容：",
    "{\"fabricated\": true 或 false, \"reason\": \"不超过 50 字的简短判定理由\"}",
])


def _truncate_for_judge(text: str, max_chars: int) -> str:
    """截断中部（保留开头的叙述和结尾的结论，判定信号最密集）。"""
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    half = max_chars // 2
    return f"{t[:half]}\n…（中间截断）…\n{t[-half:]}"


def build_grounding_messages(user_question: str, assistant_reply: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"用户提问：\n{_truncate_for_judge(user_question, 800)}\n\n"
                f"助手回复：\n{_truncate_for_judge(assistant_reply, 4000)}"
            ),
        },
    ]


def parse_grounding_verdict(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    match = re.search(r"\{[\s\S]*?\}", raw)
    if not match:
        return None
    import json

    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    fabricated = parsed.get("fabricated")
    if not isinstance(fabricated, bool):
        return None
    reason = parsed.get("reason")
    return {
        "fabricated": fabricated,
        "reason": reason if isinstance(reason, str) else "",
    }


# ---------------------------------------------------------------------------
# Discipline notices (product-neutral; the TS originals name product tools)
# ---------------------------------------------------------------------------

_DEFERRED_RETRY_NOTICE = "\n".join([
    "【纪律】你刚才只是用文字描述了“我将执行 / 现在执行 / 准备运行 …”，但**没有**真正发起任何 tool_call。",
    "光打字不等于执行，用户那边什么都不会发生。",
    "",
    "**请立即**发起对应的工具调用，把你刚才计划的下一步真正执行掉。",
    "如果你判断这一步**真的不需要**继续调用工具，请直接给出最终结论，",
    "不要再用省略号 / “现在执行...” 这种悬而未决的句式。",
    "",
    "本次不允许再用纯文字描述意图。",
])

_CLAIMED_RETRY_NOTICE = "\n".join([
    "【纪律】（这是系统自动核查，不是用户发言）你刚才声称“已执行 / 运行成功 / 结果如下”，但本轮你**没有发起任何 tool_call**。",
    "历史对话中的旧执行结果**不能**当作本次请求的结果——用户要求的是**现在重新真实执行**。",
    "",
    "**请立即**发起对应的工具调用，拿到本轮的真实返回后再汇报。",
    "禁止复述历史结果冒充本次执行；禁止在没有真实 tool_call 的情况下说“已执行 / 已完成 / 结果如下”。",
    "重试时直接执行并汇报，不要出现“您说得对 / 抱歉”之类对用户的回应——用户没有发言。",
])

_GROUNDING_RETRY_NOTICE = "\n".join([
    "【纪律】（这是系统自动核查，不是用户发言）你刚才的回复给出了具体数值，但本轮没有任何**成功的**工具调用，",
    "这些数值无法核实来源，可能是编造的。",
    "",
    "请立即发起真实的工具调用获取数据后再作答；如果工具无法获取，",
    "请明确告知用户“无法获取该数据”，禁止给出未经核实的数值。",
])

_NARRATION_REQUEST = "\n".join([
    "[system notice] The task ended without a natural-language summary. Do",
    "NOT call any tools. Summarize what was done and what the tool results",
    "showed, and give the user a clear final answer now.",
])

# Second-chance narration when the first wrap-up round itself came back
# empty (observed on real-data replay: models occasionally return an empty
# completion on error-heavy transcripts; a more direct ask recovers some).
_NARRATION_REQUEST_RETRY = "\n".join([
    "[system notice] You still have not answered the user. Do NOT call any",
    "tools. Reply with at least one plain sentence telling the user what",
    "happened, even if it is just that the attempts failed.",
])

# Narration rounds per turn. One-shot proved too brittle against stochastic
# empty completions; more than two just burns tokens on a wedged model.
_MAX_NARRATIONS = 2


# ---------------------------------------------------------------------------
# The hooks implementation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AntiHallucinationConfig:
    """Tunables for ``AntiHallucinationHooks``.

    ``mode="plan"`` disables all enforcement (a plan is supposed to be words,
    not actions). ``force_tool_choice`` is the value forwarded to the provider
    when routing says require_tool — set ``None`` to disable the mechanism on
    providers that reject OpenAI-style tool_choice. ``tools_available``
    mirrors the TS guard that only runs the grounding judge when the turn
    actually had tools to call.
    """

    mode: str | None = None
    user_question: str = ""
    last_assistant_tail: str = ""
    max_retries: int = 2
    force_tool_choice: str | None = "required"
    tools_available: bool = True
    enable_routing: bool = True
    enable_deferred: bool = True
    enable_grounding: bool = True
    enable_narration: bool = True


class AntiHallucinationHooks(NoopHooks):
    """LoopHooks implementation stacking the four anti-hallucination checks.

    One instance per run (it carries per-turn state: the route verdict, the
    retry counter, whether narration already happened). Construct with the
    turn's LLM provider — routing and judging reuse it via ``complete()``.
    Tool-result and request-error hook points pass through (``NoopHooks``).
    """

    def __init__(self, provider: "LLMProvider", config: AntiHallucinationConfig | None = None) -> None:
        self._provider = provider
        self._config = config or AntiHallucinationConfig()
        self._route: DataNeedRoute | None = None
        self._retries = 0
        self._narrations = 0

    # -- pre_step: data-need routing → tool_choice on the first round ------

    async def pre_step(
        self, transcript: "list[LLMMessage]", ctx: "LoopContext"
    ) -> PreStepAction:
        if (
            not self._config.enable_routing
            or self._config.mode == "plan"
            or self._route is not None
            or ctx.round_index != 0
            or ctx.tool_calls_used > 0
            or not self._config.force_tool_choice
            or not self._config.tools_available
        ):
            return PreStepAction(kind="proceed")
        self._route = await self._classify_route()
        if self._route == "require_tool":
            return PreStepAction(
                kind="proceed",
                tool_choice=self._config.force_tool_choice,
            )
        return PreStepAction(kind="proceed")

    async def _classify_route(self) -> DataNeedRoute:
        from .llm import LLMMessage

        try:
            messages = [
                LLMMessage.text_of(m["role"], m["content"])
                for m in build_turn_route_messages(
                    self._config.user_question, self._config.last_assistant_tail
                )
            ]
            reply, _usage = await self._provider.complete(messages, temperature=0)
            parsed = parse_turn_route(reply.content_text)
            if parsed:
                return parsed["route"]  # type: ignore[return-value]
        except Exception:
            pass
        # 保守方向：分类失败一律按"需要工具"处理，不给编造留口子。
        return "require_tool"

    # -- before_completion: deferred/claimed/grounding retry + narration ---

    async def before_completion(
        self, draft: CompletionDraft, ctx: "LoopContext"
    ) -> CompletionAction:
        cfg = self._config
        if cfg.mode == "plan":
            return CompletionAction(kind="accept")

        content = draft.content.strip()

        # Narration: terminal with tool activity but no text. Only when there
        # is something to summarize (a naked no-tool no-content failure gains
        # nothing from a summary round). Bounded at two rounds: the first
        # wrap-up occasionally comes back empty on error-heavy transcripts.
        if (
            cfg.enable_narration
            and not content
            and self._narrations < _MAX_NARRATIONS
            and draft.status in ("failed", "budget_exhausted")
            and draft.tool_calls_used > 0
        ):
            self._narrations += 1
            return CompletionAction(
                kind="narrate",
                message=(
                    _NARRATION_REQUEST
                    if self._narrations == 1
                    else _NARRATION_REQUEST_RETRY
                ),
                reason="narration",
            )

        # The retry checks below apply to the no-tool-calls terminal with
        # content (the loop passes had_tool_calls=False there).
        if draft.had_tool_calls or not content or self._retries >= cfg.max_retries:
            return CompletionAction(kind="accept")

        used_tool = draft.tool_calls_used > 0
        user_has_exec_intent = detect_execution_intent_in_user_message(cfg.user_question)

        if cfg.enable_deferred:
            is_deferred = (
                detect_deferred_execution(content)
                if used_tool
                else user_has_exec_intent and detect_deferred_execution_eager(content)
            )
            if is_deferred:
                self._retries += 1
                return CompletionAction(
                    kind="retry",
                    message=_DEFERRED_RETRY_NOTICE,
                    reason="deferred_execution",
                )

            is_claimed = (
                not used_tool
                and user_has_exec_intent
                and detect_claimed_execution(content)
            )
            if is_claimed:
                self._retries += 1
                return CompletionAction(
                    kind="retry",
                    message=_CLAIMED_RETRY_NOTICE,
                    reason="claimed_execution",
                )

        if (
            cfg.enable_grounding
            and draft.tool_successes == 0
            and cfg.tools_available
            and self._route != "allow_no_tool"
            and should_run_grounding_judge(content)
        ):
            verdict = await self._judge_grounding(cfg.user_question, content)
            if verdict and verdict.get("fabricated"):
                self._retries += 1
                return CompletionAction(
                    kind="retry",
                    message=_GROUNDING_RETRY_NOTICE,
                    reason="fabricated_data",
                )

        return CompletionAction(kind="accept")

    async def _judge_grounding(
        self, user_question: str, assistant_reply: str
    ) -> dict[str, Any] | None:
        """Fail-open: any judge error / unparseable output passes (None)."""
        from .llm import LLMMessage

        try:
            messages = [
                LLMMessage.text_of(m["role"], m["content"])
                for m in build_grounding_messages(user_question, assistant_reply)
            ]
            reply, _usage = await self._provider.complete(messages, temperature=0)
            return parse_grounding_verdict(reply.content_text)
        except Exception:
            return None


__all__ = [
    "AntiHallucinationConfig",
    "AntiHallucinationHooks",
    "build_grounding_messages",
    "build_turn_route_messages",
    "detect_claimed_execution",
    "detect_deferred_execution",
    "detect_deferred_execution_eager",
    "detect_execution_intent_in_user_message",
    "is_conditional_offer",
    "parse_grounding_verdict",
    "parse_turn_route",
    "should_run_grounding_judge",
]

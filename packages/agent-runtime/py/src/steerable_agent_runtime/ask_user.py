"""Structured user questions — the ``ask_user`` tool.

The agent pauses mid-run and asks the user a structured set of questions
(select / text / password) before continuing. The wire payload mirrors the
protocol's ``AskUserQuestionsPayload`` (TS) so the desktop card renders it
unchanged; the handler is the seam a product injects to actually collect the
answers (a UI card over the reverse channel, an ACP elicitation, a CLI
prompt).

The tool is blocking by construction: ``dispatch`` awaits the handler, and
the answers are returned as the tool result so they land in the durable
record and the model's next context. This mirrors dsh's ``ask_user_question``
— the loop does not continue until the user (or a timeout) answers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .errors import ToolDispatchError

#: The tool name the model calls.
ASK_USER_TOOL_NAME = "ask_user"

#: JSON Schema for the tool's arguments — one intro plus a list of questions.
#: Field names match ``AskUserQuestionsPayload`` so the host can render the
#: arguments directly as the card payload.
ASK_USER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intro": {
            "type": "string",
            "description": "One-line framing shown above the questions.",
        },
        "outro": {
            "type": "string",
            "description": "Optional line shown below the questions.",
        },
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "header": {
                        "type": "string",
                        "maxLength": 12,
                        "description": (
                            "Short chip label shown above the question "
                            "(<=12 chars). Optional; the host derives one "
                            "from `text` when absent."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "enum": ["select", "text", "password"],
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 4,
                        "description": (
                            "Choices for a select question (2-4). The host "
                            "auto-appends an 'Other' free-text escape; do "
                            "not list it yourself."
                        ),
                    },
                    "placeholder": {"type": "string"},
                    "multiSelect": {"type": "boolean"},
                },
                "required": ["id", "text"],
            },
        },
    },
    "required": ["intro", "questions"],
}

#: Alias keys real models emit for the canonical ``AskUserQuestionsPayload``
#: fields (observed: gpt-oss via Ollama answers the schema with Inquirer-style
#: ``name``/``message``/``choices``). Normalized at this model-JSON boundary so
#: hosts only ever render the canonical payload; the canonical key wins when
#: both are present.
_QUESTION_ALIASES = {"id": "name", "text": "message", "options": "choices"}


#: Claude Code ``AskUserQuestion`` parity bounds. The card renders a batched
#: decision, not an interview — cap the question count so the model cannot
#: bombard the user, and cap select options so it pre-categorizes instead of
#: dumping a long list. The host auto-appends an "Other" free-text escape, so
#: the model never lists it (the option space the model thought of is not the
#: complete space).
_MIN_QUESTIONS = 1
_MAX_QUESTIONS = 4
_MIN_OPTIONS = 2
_MAX_OPTIONS = 4
#: ``header`` is the short chip label shown above the question (CC parity).
#: Optional on the wire; derived from ``text`` when absent.
_MAX_HEADER_LEN = 12


def _derive_header(text: str) -> str:
    """Derive a <=12-char chip label from the question text when the model did
    not supply ``header``. Strips trailing punctuation and truncates on a word
    boundary so the chip stays readable."""
    cleaned = text.strip().rstrip("?.!。")
    if len(cleaned) <= _MAX_HEADER_LEN:
        return cleaned
    truncated = cleaned[:_MAX_HEADER_LEN]
    # Prefer cutting on the last space so the chip does not end mid-word.
    space = truncated.rfind(" ")
    if space > 0:
        truncated = truncated[:space]
    return truncated


def _normalize_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map known alias keys onto the canonical payload fields, enforce the
    Claude Code ``AskUserQuestion`` bounds (1-4 questions, 2-4 options per
    select, ``header`` <=12 chars, ``multiSelect`` explicit), and require the
    two fields the host card cannot render without (``id`` to key the answer,
    ``text`` to label the control). A violation raises ``ToolDispatchError`` —
    the router wraps it as the tool result, so the model sees exactly what to
    fix and retries within the same turn."""
    if not isinstance(questions, list):
        raise ToolDispatchError(
            f"ask_user: questions must be an array, got {type(questions).__name__}"
        )
    if not (_MIN_QUESTIONS <= len(questions) <= _MAX_QUESTIONS):
        raise ToolDispatchError(
            f"ask_user: questions must contain {_MIN_QUESTIONS}-{_MAX_QUESTIONS} "
            f"items, got {len(questions)}. Batch a small decision, not an interview."
        )
    normalized: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise ToolDispatchError(
                f"ask_user: questions[{index}] must be an object, "
                f"got {type(question).__name__}"
            )
        q = dict(question)
        for canonical, alias in _QUESTION_ALIASES.items():
            if canonical not in q and alias in q:
                q[canonical] = q.pop(alias)
        if not isinstance(q.get("id"), str) or not q["id"]:
            raise ToolDispatchError(
                f"ask_user: questions[{index}] is missing a non-empty string "
                '"id" (the answer-map key).'
            )
        if not isinstance(q.get("text"), str) or not q["text"]:
            raise ToolDispatchError(
                f"ask_user: questions[{index}] is missing a non-empty string "
                '"text" (the question label shown to the user).'
            )
        # header: optional on the wire; validate length when present, derive
        # from text when absent so the card always has a chip label.
        header = q.get("header")
        if header is None:
            q["header"] = _derive_header(q["text"])
        elif not isinstance(header, str) or not header:
            raise ToolDispatchError(
                f"ask_user: questions[{index}].header must be a non-empty string."
            )
        elif len(header) > _MAX_HEADER_LEN:
            raise ToolDispatchError(
                f"ask_user: questions[{index}].header is {len(header)} chars; "
                f"the chip label must be <= {_MAX_HEADER_LEN}."
            )
        # multiSelect: CC requires the model to commit to single vs multi.
        # Default to False when absent so existing single-select calls keep
        # working, but stamp it so hosts always read an explicit boolean.
        if "multiSelect" not in q or q["multiSelect"] is None:
            q["multiSelect"] = False
        elif not isinstance(q["multiSelect"], bool):
            raise ToolDispatchError(
                f"ask_user: questions[{index}].multiSelect must be a boolean, "
                f"got {type(q['multiSelect']).__name__}."
            )
        # options: only a select question carries choices; enforce the 2-4
        # bound so the model pre-categorizes instead of dumping a long list.
        qtype = q.get("type", "select")
        options = q.get("options")
        if qtype == "select":
            if options is not None:
                if not isinstance(options, list):
                    raise ToolDispatchError(
                        f"ask_user: questions[{index}].options must be an array, "
                        f"got {type(options).__name__}."
                    )
                if not (_MIN_OPTIONS <= len(options) <= _MAX_OPTIONS):
                    raise ToolDispatchError(
                        f"ask_user: questions[{index}] has {len(options)} options; "
                        f"a select question needs {_MIN_OPTIONS}-{_MAX_OPTIONS}. "
                        "The host auto-appends an 'Other' escape — do not list it."
                    )
        normalized.append(q)
    return normalized


class AskUserHandler(Protocol):
    """Product-injected seam that collects answers for one question set.

    Receives the validated ``intro`` and ``questions`` list and returns a
    mapping of question id → answer (a string, or a list of strings for a
    multi-select). Implementations must not raise on a user cancel — return
    an empty mapping so the loop records "no answer" and moves on.
    """

    def __call__(
        self, intro: str, questions: list[dict[str, Any]]
    ) -> Awaitable[dict[str, Any]]: ...


def make_ask_user_tool(handler: AskUserHandler) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Build the ``ask_user`` tool handler bound to ``handler``.

    The returned coroutine function carries the registration metadata the
    router reads (name, description, schema) so a host registers it with one
    call. The tool is marked ``require_consent=False`` and
    ``concurrency_safe=False``: asking the user is itself the interaction, and
    two concurrent question cards would race for the same UI surface.
    """

    async def ask_user(
        intro: str, questions: list[dict[str, Any]], outro: str | None = None
    ) -> dict[str, Any]:
        normalized = _normalize_questions(questions)
        answers = await handler(intro, normalized)
        return {
            "intro": intro,
            "outro": outro,
            "questions": normalized,
            "answers": answers,
        }

    ask_user.__steerable_tool_meta__ = {  # type: ignore[attr-defined]
        "name": ASK_USER_TOOL_NAME,
        "mode": "read",  # no side effects on the workspace; it gathers input
        "description": (
            "Pause and ask the user a structured set of questions (select / "
            "text / password) when you need information only they have. "
            "Blocks until they answer; the answers come back as the result."
        ),
        "schema": ASK_USER_SCHEMA,
        "require_consent": False,
        "concurrency_safe": False,
        "exposure": "direct",
    }
    return ask_user

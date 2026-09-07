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
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["select", "text", "password"],
                    },
                    "options": {"type": "array", "items": {"type": "string"}},
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


def _normalize_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map known alias keys onto the canonical payload fields and require the
    two fields the host card cannot render without (``id`` to key the answer,
    ``text`` to label the control). A violation raises ``ToolDispatchError`` —
    the router wraps it as the tool result, so the model sees exactly what to
    fix and retries within the same turn."""
    if not isinstance(questions, list):
        raise ToolDispatchError(
            f"ask_user: questions must be an array, got {type(questions).__name__}"
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

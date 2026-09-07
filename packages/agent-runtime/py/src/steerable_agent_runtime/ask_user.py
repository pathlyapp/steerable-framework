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

from typing import Any, Awaitable, Callable, Protocol

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
        answers = await handler(intro, questions)
        return {
            "intro": intro,
            "outro": outro,
            "questions": questions,
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

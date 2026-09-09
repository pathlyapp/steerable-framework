"""The todo_write tool: full-replace semantics, validation, chat scoping."""

from __future__ import annotations

import pytest
from steerable_agent_protocol.generated import ToolCall

from steerable_agent_runtime import (
    TODO_TOOL_NAME,
    TodoStore,
    ToolRouter,
    make_todo_write_tool,
)
from steerable_agent_runtime.errors import ToolDispatchError


def _make() -> tuple[TodoStore, ToolRouter]:
    store = TodoStore()
    router = ToolRouter()
    tool = make_todo_write_tool(store)
    meta = tool.__steerable_tool_meta__
    router.register(
        tool,
        name=meta["name"],
        mode=meta["mode"],
        description=meta["description"],
        schema=meta["schema"],
        require_consent=meta["require_consent"],
        concurrency_safe=meta["concurrency_safe"],
        exposure=meta["exposure"],
    )
    return store, router


def _call(todos: object) -> ToolCall:
    return ToolCall(id="c1", name=TODO_TOOL_NAME, arguments={"todos": todos})


def _todos(*specs: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"id": f"t{i}", "content": content, "status": status}
        for i, (content, status) in enumerate(specs)
    ]


async def test_full_replace_returns_list_and_summary() -> None:
    store, router = _make()
    result = await router.dispatch(
        _call(_todos(("read code", "completed"), ("patch it", "in_progress"))),
        context={"chat_id": "chat-1"},
    )
    assert result.success, result.error
    data = result.data["value"]
    assert [t["id"] for t in data["todos"]] == ["t0", "t1"]
    assert data["summary"] == {
        "total": 2,
        "pending": 0,
        "inProgress": 1,
        "completed": 1,
    }
    # The store holds the new list for the chat.
    assert [t["content"] for t in store.get("chat-1")] == ["read code", "patch it"]


async def test_second_call_replaces_the_first() -> None:
    store, router = _make()
    await router.dispatch(
        _call(_todos(("old task", "pending"))), context={"chat_id": "chat-1"}
    )
    result = await router.dispatch(
        _call(_todos(("new task", "in_progress"))), context={"chat_id": "chat-1"}
    )
    assert result.success, result.error
    assert [t["content"] for t in store.get("chat-1")] == ["new task"]


async def test_chats_are_isolated() -> None:
    store, router = _make()
    await router.dispatch(
        _call(_todos(("chat one task", "pending"))), context={"chat_id": "chat-1"}
    )
    await router.dispatch(
        _call(_todos(("chat two task", "in_progress"))),
        context={"chat_id": "chat-2"},
    )
    assert [t["content"] for t in store.get("chat-1")] == ["chat one task"]
    assert [t["content"] for t in store.get("chat-2")] == ["chat two task"]


async def test_missing_context_uses_the_default_bucket() -> None:
    store, router = _make()
    result = await router.dispatch(_call(_todos(("orphan", "pending"))))
    assert result.success, result.error
    assert [t["content"] for t in store.get("")] == ["orphan"]


@pytest.mark.parametrize(
    ("todos", "fragment"),
    [
        ("not-a-list", "must be an array"),
        ([], "must contain 1-50 items"),
        (["not-an-object"], "must be an object"),
        ([{"content": "x", "status": "pending"}], 'non-empty string "id"'),
        (
            [
                {"id": "a", "content": "one", "status": "pending"},
                {"id": "a", "content": "two", "status": "pending"},
            ],
            'duplicate id "a"',
        ),
        ([{"id": "a", "content": "  ", "status": "pending"}], '"content"'),
        (
            [{"id": "a", "content": "x", "status": "doing"}],
            "invalid status",
        ),
        (
            [
                {"id": "a", "content": "one", "status": "in_progress"},
                {"id": "b", "content": "two", "status": "in_progress"},
            ],
            "at most one",
        ),
    ],
)
async def test_invalid_lists_are_rejected_without_touching_state(
    todos: object, fragment: str
) -> None:
    store, router = _make()
    await router.dispatch(
        _call(_todos(("kept", "pending"))), context={"chat_id": "chat-1"}
    )
    result = await router.dispatch(_call(todos), context={"chat_id": "chat-1"})
    assert not result.success
    assert fragment in (result.error or "")
    # A rejected rewrite leaves the previous list intact.
    assert [t["content"] for t in store.get("chat-1")] == ["kept"]


def test_normalize_rejects_overlong_lists() -> None:
    todos = [
        {"id": f"t{i}", "content": f"task {i}", "status": "pending"}
        for i in range(51)
    ]
    with pytest.raises(ToolDispatchError, match="1-50"):
        from steerable_agent_runtime.todo import _normalize_todos

        _normalize_todos(todos)


def test_meta_advertises_direct_exposure() -> None:
    tool = make_todo_write_tool(TodoStore())
    meta = tool.__steerable_tool_meta__
    assert meta["name"] == "todo_write"
    assert meta["exposure"] == "direct"
    assert meta["require_consent"] is False
    assert meta["concurrency_safe"] is False

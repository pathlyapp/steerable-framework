from __future__ import annotations

import pytest

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import (
    PolicyDeniedError,
    ToolRouter,
    tool,
)


@pytest.mark.asyncio
async def test_register_and_dispatch_sync_handler() -> None:
    router = ToolRouter()

    @tool(router=router, description="Return a greeting")
    def greet(name: str = "world") -> str:
        return f"hello, {name}"

    result = await router.dispatch(ToolCall(id="1", name="greet", arguments={"name": "tai"}))
    assert result.success is True
    assert result.data is not None
    assert result.data["value"] == "hello, tai"
    assert "durationMs" in result.data


@pytest.mark.asyncio
async def test_register_and_dispatch_async_handler() -> None:
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add, description="adder")

    result = await router.dispatch(ToolCall(id="2", name="add", arguments={"a": 1, "b": 2}))
    assert result.success is True
    assert result.data is not None
    assert result.data["value"] == 3


@pytest.mark.asyncio
async def test_handler_returning_tool_result_passthrough() -> None:
    router = ToolRouter()

    async def custom() -> ToolResult:
        return ToolResult(success=True, message="done", data={"foo": "bar"})

    router.register(custom)
    result = await router.dispatch(ToolCall(id="3", name="custom", arguments={}))
    assert result.success is True
    assert result.message == "done"
    assert result.data is not None
    assert result.data["foo"] == "bar"
    assert "durationMs" in result.data


@pytest.mark.asyncio
async def test_handler_returning_dict_with_success_passthrough() -> None:
    router = ToolRouter()

    async def maybe() -> dict:
        return {"success": False, "error": "boom", "needsFollowup": True}

    router.register(maybe)
    result = await router.dispatch(ToolCall(id="4", name="maybe", arguments={}))
    assert result.success is False
    assert result.error == "boom"
    assert result.needsFollowup is True


@pytest.mark.asyncio
async def test_unknown_tool_returns_failure() -> None:
    router = ToolRouter()
    result = await router.dispatch(ToolCall(id="5", name="missing", arguments={}))
    assert result.success is False
    assert "missing" in (result.error or "")


@pytest.mark.asyncio
async def test_handler_exception_wrapped_into_result() -> None:
    router = ToolRouter()

    async def bad() -> None:
        raise ValueError("kaboom")

    router.register(bad)
    result = await router.dispatch(ToolCall(id="6", name="bad", arguments={}))
    assert result.success is False
    assert result.error == "kaboom"
    assert result.needsFollowup is True


@pytest.mark.asyncio
async def test_destructive_tool_requires_consent() -> None:
    router = ToolRouter()

    async def delete_thing() -> None:
        return None

    registered = router.register(delete_thing)
    assert registered.mode == "destructive"
    assert registered.require_consent is True

    with pytest.raises(PolicyDeniedError):
        await router.dispatch(ToolCall(id="7", name="delete_thing", arguments={}))

    result = await router.dispatch(
        ToolCall(id="7", name="delete_thing", arguments={}),
        consent_granted=True,
    )
    assert result.success is True


@pytest.mark.asyncio
async def test_register_with_explicit_overrides() -> None:
    router = ToolRouter()

    async def something() -> int:
        return 42

    registered = router.register(
        something,
        name="custom-name",
        mode="safe_write",
        description="manual",
        schema={"type": "object", "properties": {"x": {"type": "integer"}}},
        require_consent=False,
    )
    assert registered.name == "custom-name"
    assert registered.mode == "safe_write"
    assert registered.require_consent is False
    descriptors = router.describe()
    assert any(item["function"]["name"] == "custom-name" for item in descriptors)


@pytest.mark.asyncio
async def test_register_duplicate_raises() -> None:
    router = ToolRouter()

    async def thing() -> int:
        return 1

    router.register(thing)
    with pytest.raises(Exception):
        router.register(thing)


@pytest.mark.asyncio
async def test_handler_can_request_context() -> None:
    router = ToolRouter()
    seen: list[dict] = []

    async def echo_ctx(context: dict) -> dict:
        seen.append(context)
        return {"who": context.get("user")}

    router.register(echo_ctx)
    result = await router.dispatch(
        ToolCall(id="8", name="echo_ctx", arguments={}),
        context={"user": "tai"},
    )
    assert result.success is True
    assert result.data is not None
    assert result.data["value"] == {"who": "tai"}
    assert seen == [{"user": "tai"}]

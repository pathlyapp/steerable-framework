"""JSON Schema derivation from tool-handler type hints.

The README advertises ``@tool`` as auto-deriving JSON Schema from Python type
hints. These tests pin the derivation contract: which parameters become
properties, how annotations map to JSON Schema types, what lands in
``required``, and when an explicit ``schema=`` still wins.
"""

from __future__ import annotations

from typing import Any, Literal

from steerable_agent_runtime import ToolRouter
from steerable_agent_runtime.tool_schema import derive_schema


def test_basic_scalar_types_and_required() -> None:
    async def read_file(
        path: str, offset: int = 0, limit: float = 1.5, raw: bool = False
    ) -> str:
        return path

    schema = derive_schema(read_file)
    assert schema["type"] == "object"
    assert schema["properties"]["path"] == {"type": "string"}
    assert schema["properties"]["offset"] == {"type": "integer"}
    assert schema["properties"]["limit"] == {"type": "number"}
    assert schema["properties"]["raw"] == {"type": "boolean"}
    # Only parameters without defaults are required.
    assert schema["required"] == ["path"]


def test_containers() -> None:
    def batch(paths: list[str], options: dict[str, Any], tags: list) -> None: ...

    schema = derive_schema(batch)
    assert schema["properties"]["paths"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert schema["properties"]["options"] == {"type": "object"}
    assert schema["properties"]["tags"] == {"type": "array"}
    assert schema["required"] == ["paths", "options", "tags"]


def test_literal_becomes_enum() -> None:
    def set_mode(mode: Literal["fast", "safe"]) -> None: ...

    schema = derive_schema(set_mode)
    assert schema["properties"]["mode"] == {"type": "string", "enum": ["fast", "safe"]}


def test_optional_is_nullable_and_not_required_when_defaulted() -> None:
    def search(query: str, filter: str | None = None) -> None: ...

    schema = derive_schema(search)
    assert schema["properties"]["filter"] == {"type": ["string", "null"]}
    assert schema["required"] == ["query"]


def test_pep604_union_with_none() -> None:
    def edit(path: str, note: str | None = None) -> None: ...

    schema = derive_schema(edit)
    assert schema["properties"]["note"] == {"type": ["string", "null"]}
    assert schema["required"] == ["path"]


def test_context_param_is_not_a_model_argument() -> None:
    def whoami(context: dict[str, Any]) -> None: ...

    schema = derive_schema(whoami)
    assert schema["properties"] == {}
    assert "required" not in schema


def test_var_keyword_only_handler_derives_empty_schema() -> None:
    # Remote-proxy handlers accept **kwargs; there is nothing to derive.
    async def proxy(**kwargs: Any) -> Any: ...

    assert derive_schema(proxy) == {"type": "object", "properties": {}}


def test_unannotated_param_gets_unconstrained_property() -> None:
    def legacy(freeform) -> None: ...

    schema = derive_schema(legacy)
    assert schema["properties"]["freeform"] == {}
    assert schema["required"] == ["freeform"]


def test_register_derives_schema_when_none_given() -> None:
    router = ToolRouter()

    async def list_events(limit: int = 20) -> list[dict]:
        return []

    registered = router.register(list_events)
    assert registered.schema["properties"]["limit"] == {"type": "integer"}
    assert "required" not in registered.schema


def test_explicit_schema_always_wins() -> None:
    router = ToolRouter()
    explicit = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }

    def search(query: str, limit: int = 10) -> None: ...

    registered = router.register(search, schema=explicit)
    assert registered.schema is explicit

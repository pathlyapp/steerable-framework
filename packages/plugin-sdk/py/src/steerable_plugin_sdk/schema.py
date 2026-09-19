"""Conservative JSON Schema derivation for plugin tool handlers."""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from typing import Any, get_args, get_origin

_SCALAR_TYPES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    origin = get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        members = list(get_args(annotation))
        nullable = type(None) in members
        non_null = [member for member in members if member is not type(None)]
        if len(non_null) == 1:
            inner = _annotation_to_schema(non_null[0])
            inner_type = inner.get("type")
            if nullable and isinstance(inner_type, str):
                return {**inner, "type": [inner_type, "null"]}
            return inner if inner else ({"type": "null"} if nullable else {})
        types_seen = [
            _SCALAR_TYPES[member]
            for member in non_null
            if member in _SCALAR_TYPES
        ]
        if nullable:
            types_seen.append("null")
        return {"type": types_seen} if types_seen else {}
    if origin is list:
        args = get_args(annotation)
        items = _annotation_to_schema(args[0]) if args else {}
        return {"type": "array", **({"items": items} if items else {})}
    if origin is dict:
        return {"type": "object"}
    if origin is typing.Literal:
        values = list(get_args(annotation))
        schema: dict[str, Any] = {"enum": values}
        if values and all(isinstance(value, type(values[0])) for value in values):
            literal_type = _SCALAR_TYPES.get(type(values[0]))
            if literal_type:
                schema["type"] = literal_type
        return schema
    if annotation in _SCALAR_TYPES:
        return {"type": _SCALAR_TYPES[annotation]}
    if annotation is list:
        return {"type": "array"}
    if annotation is dict:
        return {"type": "object"}
    return {}


def derive_schema(handler: Callable[..., Any]) -> dict[str, Any]:
    """Build an object schema from the model-supplied handler parameters."""
    try:
        hints = typing.get_type_hints(handler)
    except (NameError, TypeError, AttributeError, SyntaxError, ValueError):
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in inspect.signature(handler).parameters.values():
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ) or parameter.name == "context":
            continue
        properties[parameter.name] = _annotation_to_schema(
            hints.get(parameter.name, parameter.annotation)
        )
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema

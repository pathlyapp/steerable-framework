"""Derive a tool's JSON Schema from its handler's Python type hints.

Used when a tool is registered without an explicit ``schema=`` — an explicit
schema always wins. The derivation is deliberately conservative:

* Only parameters the model can actually supply become properties. The
  dispatch-injected ``context`` parameter and variadic parameters
  (``*args`` / ``**kwargs``) are excluded.
* Parameters without defaults land in ``required``.
* ``Optional[X]`` / ``X | None`` map to ``{"type": [<x>, "null"]}``.
* ``Literal[...]`` maps to ``enum`` with the member type inferred from the
  literal values.
* Unannotated or unresolvable parameters degrade to an unconstrained ``{}``
  property — derivation never raises, because a registration-time failure
  here would break tools that previously worked with the empty default
  schema.
"""

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
    """Map a single resolved annotation to a JSON Schema fragment."""

    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}

    origin = get_origin(annotation)

    if origin is typing.Union or origin is types.UnionType:
        members = [a for a in get_args(annotation)]
        nullable = type(None) in members
        non_null = [a for a in members if a is not type(None)]
        if len(non_null) == 1:
            inner = _annotation_to_schema(non_null[0])
            inner_type = inner.get("type")
            if nullable and isinstance(inner_type, str):
                return {**inner, "type": [inner_type, "null"]}
            return inner if inner else ({"type": "null"} if nullable else {})
        # Multi-member unions: keep only the type union, no per-member detail.
        types_seen = [_SCALAR_TYPES[a] for a in non_null if a in _SCALAR_TYPES]
        if nullable:
            types_seen.append("null")
        return {"type": types_seen} if types_seen else {}

    if origin is list:
        args = get_args(annotation)
        if args:
            items = _annotation_to_schema(args[0])
            return {"type": "array", "items": items} if items else {"type": "array"}
        return {"type": "array"}

    if origin is dict:
        return {"type": "object"}

    if origin is typing.Literal:
        values = list(get_args(annotation))
        schema: dict[str, Any] = {"enum": values}
        if values and all(isinstance(v, type(values[0])) for v in values):
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
    """Build the ``parameters`` JSON Schema for ``handler`` from its hints.

    The result is always a valid object schema; it may have empty
    ``properties`` when there is nothing derivable (e.g. a ``**kwargs``
    remote-proxy handler).
    """

    try:
        hints = typing.get_type_hints(handler)
    except (NameError, TypeError, AttributeError, SyntaxError, ValueError):
        # Forward references to unimportable names etc. — degrade those
        # parameters to unconstrained properties rather than fail registration.
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in inspect.signature(handler).parameters.values():
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        # ``context`` is injected from the dispatch context, never from model
        # arguments (see ToolRouter._invoke).
        if parameter.name == "context":
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

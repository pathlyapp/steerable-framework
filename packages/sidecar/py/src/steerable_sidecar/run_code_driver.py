"""Confined ``run_code`` child: execute the model program, JSON-IPC tools.

This process must not hold provider credentials. The parent sidecar speaks
one JSON object per line on this process's stdin/stdout; user ``print``
is redirected to log frames so it cannot break the protocol. Imports of
``os`` / ``subprocess`` / ``socket`` (and other process/network modules)
are refused.
"""

from __future__ import annotations

import argparse
import builtins
import json
import sys
import textwrap
from typing import Any

ALLOWED_MODULES = frozenset(
    {
        "json",
        "math",
        "datetime",
        "re",
        "collections",
        "itertools",
        "functools",
        "decimal",
        "statistics",
        "textwrap",
        "string",
        "unicodedata",
        "html",
        "typing",
        "operator",
        "copy",
        "numbers",
    }
)

_SAFE_BUILTIN_NAMES = (
    "abs",
    "all",
    "any",
    "bool",
    "bytes",
    "callable",
    "chr",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "getattr",
    "hasattr",
    "hash",
    "hex",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "object",
    "oct",
    "ord",
    "pow",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "RuntimeError",
    "StopIteration",
    "AssertionError",
    "True",
    "False",
    "None",
)

_protocol_out = sys.stdout
_orig_import = builtins.__import__


class ToolCallError(RuntimeError):
    """Raised in the program when a nested tool returns ``success: false``."""


def _emit(payload: dict[str, Any]) -> None:
    _protocol_out.write(json.dumps(payload, ensure_ascii=False, default=str))
    _protocol_out.write("\n")
    _protocol_out.flush()


def _safe_import(
    name: str,
    globals: Any = None,
    locals: Any = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    root = name.split(".", 1)[0]
    if root not in ALLOWED_MODULES:
        raise ImportError(f"import {name!r} is not allowed in run_code")
    return _orig_import(name, globals, locals, fromlist, level)


class _LogStream:
    def write(self, text: str) -> int:
        if text and text != "\n":
            _emit({"v": 1, "type": "log", "text": text})
        return len(text) if isinstance(text, str) else 0

    def flush(self) -> None:
        return None


class Tools:
    """``tools.call(name, **kwargs)`` / ``tools.name(**kwargs)`` nested IPC."""

    def __init__(self) -> None:
        self._next_id = 0

    def call(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if name == "run_code":
            raise ToolCallError("nested run_code is not allowed")
        if arguments is not None and kwargs:
            raise TypeError("pass either a dict or keyword arguments, not both")
        payload = dict(arguments) if arguments is not None else dict(kwargs)
        self._next_id += 1
        call_id = self._next_id
        _emit(
            {
                "v": 1,
                "type": "call",
                "id": call_id,
                "tool": name,
                "arguments": payload,
            }
        )
        line = sys.stdin.readline()
        if not line:
            raise ToolCallError("sidecar closed the tool IPC")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise ToolCallError(str(reply.get("error") or "tool failed"))
        return reply.get("result")

    def __getattr__(self, name: str) -> Any:
        def _fn(*args: Any, **kwargs: Any) -> Any:
            if args and kwargs:
                raise TypeError("pass either a dict or keyword arguments, not both")
            if len(args) == 1 and isinstance(args[0], dict):
                return self.call(name, arguments=args[0])
            if args:
                raise TypeError("tools.<name> takes a dict or keywords")
            return self.call(name, **kwargs)

        return _fn


def restricted_builtins() -> dict[str, Any]:
    ns: dict[str, Any] = {
        name: getattr(builtins, name)
        for name in _SAFE_BUILTIN_NAMES
        if hasattr(builtins, name)
    }
    ns["__import__"] = _safe_import
    ns["print"] = print
    return ns


def run_program(source: str) -> Any:
    """Execute ``source`` as the body of a function; return its return value."""

    body = textwrap.indent(source.strip() or "return None", "    ")
    wrapped = f"def __user_main():\n{body}\n"
    compiled = compile(wrapped, "<run_code>", "exec")
    ns: dict[str, Any] = {
        "__builtins__": restricted_builtins(),
        "tools": Tools(),
        "ToolCallError": ToolCallError,
    }
    exec(compiled, ns, ns)  # noqa: S102 — confined child, restricted builtins
    result = ns["__user_main"]()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="steerable-run-code-driver")
    parser.add_argument("--program", required=True, help="Path to the user program.")
    args = parser.parse_args(argv)
    sys.stdout = _LogStream()  # type: ignore[assignment]
    try:
        source = open(args.program, encoding="utf-8").read()  # noqa: SIM115
        value = run_program(source)
        _emit({"v": 1, "type": "done", "ok": True, "value": value})
        return 0
    except Exception as exc:
        _emit(
            {
                "v": 1,
                "type": "done",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

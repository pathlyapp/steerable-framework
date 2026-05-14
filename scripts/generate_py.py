#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def render_pydantic_model(name: str, schema: dict) -> str:
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    lines: list[str] = [f"class {name}(BaseModel):"]
    if not properties:
        lines.append("    pass")
        return "\n".join(lines)
    for field_name, spec in properties.items():
        typ = "Any"
        kind = spec.get("type")
        if kind == "string":
            typ = "str"
        elif kind == "integer":
            typ = "int"
        elif kind == "number":
            typ = "float"
        elif kind == "boolean":
            typ = "bool"
        elif kind == "array":
            typ = "list[Any]"
        elif kind == "object":
            typ = "dict[str, Any]"
        if field_name not in required:
            typ = f"{typ} | None"
            lines.append(f"    {field_name}: {typ} = None")
        else:
            lines.append(f"    {field_name}: {typ}")
    return "\n".join(lines)


def generate_package(package: str) -> None:
    schema_dir = ROOT / "spec"
    out = ROOT / "packages" / package / "py" / "src" / f"steerable_{package.replace('-', '_')}" / "generated.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    models: list[str] = [
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "from pydantic import BaseModel",
        "",
    ]
    for schema_path in sorted(schema_dir.rglob("*.schema.json")):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        title = schema.get("title")
        if not title:
            continue
        models.append(render_pydantic_model(title, schema))
        models.append("")
    out.write_text("\n".join(models).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    for pkg in ("agent-protocol", "agent-harness"):
        generate_package(pkg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

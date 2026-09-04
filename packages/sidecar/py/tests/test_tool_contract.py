"""Conformance of ``workspace_tools`` to the canonical tool contract (WS4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_sidecar.file_edit import content_version
from steerable_sidecar.tool_contract import canonical_contract
from steerable_sidecar.workspace_tools import workspace_tools_for_cwd

CONTRACT = canonical_contract()


async def _call(router, name: str, arguments: dict) -> object:
    return await router.dispatch(
        ToolCall(id="t", name=name, arguments=arguments),
        consent_granted=True,
    )


def _schema(router, name: str) -> dict:
    tool = next(t for t in router.list_tools() if t.name == name)
    return tool.schema


def test_version_algorithm_matches_hardcoded_vectors() -> None:
    for vector in CONTRACT["versionVectors"]:
        assert content_version(vector["input"]) == vector["sha256"], (
            f"content_version drifted for input {vector['input']!r}"
        )


def test_schemas_expose_required_input_fields(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    token_field = CONTRACT["versionToken"]["inputField"]
    for canonical_name, spec in CONTRACT["tools"].items():
        schema = _schema(router, canonical_name)
        required = set(schema.get("required", []))
        properties = set(schema.get("properties", {}))
        for field in spec["requiredInput"]:
            assert field in required, f"{canonical_name} must require {field!r}"
            assert field in properties, f"{canonical_name} must declare {field!r}"
        if "optionalInput" in spec:
            assert token_field in spec["optionalInput"]
            assert token_field in properties, (
                f"{canonical_name} must accept {token_field!r}"
            )


@pytest.mark.asyncio
async def test_result_shapes_cover_required_fields(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)

    written = await _call(router, "write_file", {"path": "a.txt", "content": "hello"})
    assert written.success is True
    for field in CONTRACT["tools"]["write_file"]["requiredResult"]:
        assert field in written.data, f"write_file result missing {field!r}"

    read = await _call(router, "read_file", {"path": "a.txt"})
    assert read.success is True
    for field in CONTRACT["tools"]["read_file"]["requiredResult"]:
        assert field in read.data, f"read_file result missing {field!r}"

    edited = await _call(
        router,
        "edit_file",
        {"path": "a.txt", "edits": [{"oldText": "hello", "newText": "world"}]},
    )
    assert edited.success is True
    for field in CONTRACT["tools"]["edit_file"]["requiredResult"]:
        assert field in edited.data, f"edit_file result missing {field!r}"

    run = await _call(router, "bash", {"command": "printf hi"})
    assert run.success is True
    for field in CONTRACT["tools"]["bash"]["requiredResult"]:
        assert field in run.data, f"bash result missing {field!r}"


@pytest.mark.asyncio
async def test_version_token_protocol_rejects_stale_write(tmp_path: Path) -> None:
    router = workspace_tools_for_cwd(tmp_path)
    token_field = CONTRACT["versionToken"]["inputField"]
    result_field = CONTRACT["versionToken"]["resultField"]

    await _call(router, "write_file", {"path": "a.txt", "content": "v1"})
    read = await _call(router, "read_file", {"path": "a.txt"})
    token = read.data[result_field]
    assert isinstance(token, str) and token

    ok = await _call(
        router, "write_file", {"path": "a.txt", "content": "v2", token_field: token}
    )
    assert ok.success is True

    stale = await _call(
        router, "write_file", {"path": "a.txt", "content": "v3", token_field: token}
    )
    assert stale.success is False
    reread = await _call(router, "read_file", {"path": "a.txt"})
    assert reread.data["content"] == "v2"


def test_tool_search_constants_match_the_contract() -> None:
    """`tool_search` has two implementations (this one and the desktop host's),
    which is what the contract exists to hold together."""
    from steerable_agent_runtime import tool_search

    section = CONTRACT["toolSearch"]
    assert section["bm25"] == {
        "k1": tool_search._BM25_K1,
        "b": tool_search._BM25_B,
        "nameWeight": tool_search._NAME_WEIGHT,
    }
    assert section["defaultMaxResults"] == tool_search.DEFAULT_MAX_RESULTS
    assert section["maxResultsCeiling"] == tool_search.MAX_RESULTS_CEILING


def test_tool_search_reproduces_the_contract_score_vectors() -> None:
    """Scores, not just ranks: a drift in k1, b, the name weight or the idf
    form can leave the order intact on a given inventory."""
    from types import SimpleNamespace

    from steerable_agent_runtime.tool_search import _rank

    section = CONTRACT["toolSearch"]
    inventory = [
        SimpleNamespace(name=tool["name"], description=tool["description"])
        for tool in section["inventory"]
    ]
    tolerance = section["scoreTolerance"]
    for vector in section["scoreVectors"]:
        ranked = _rank(inventory, vector["query"])
        assert [tool.name for _, tool in ranked] == [
            match["name"] for match in vector["ranked"]
        ], f"ranking drifted for query {vector['query']!r}"
        for (score, _), match in zip(ranked, vector["ranked"], strict=True):
            assert abs(score - match["score"]) < tolerance, (
                f"score drifted for query {vector['query']!r}"
            )

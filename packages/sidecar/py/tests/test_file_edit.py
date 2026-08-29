from __future__ import annotations

from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall

from steerable_sidecar.file_edit import (
    EditError,
    EditOp,
    apply_edits,
    content_version,
)
from steerable_sidecar.workspace_tools import workspace_tools_for_cwd


async def _call(router, name: str, arguments: dict) -> object:
    return await router.dispatch(
        ToolCall(id="t", name=name, arguments=arguments),
        consent_granted=True,
    )


class TestApplyEdits:
    def test_exact_single(self) -> None:
        out = apply_edits("hello world\nfoo bar\n", [EditOp("foo bar", "baz qux")])
        assert out.content == "hello world\nbaz qux\n"
        assert out.matches[0].level == "exact"

    def test_multiple_reverse_order(self) -> None:
        out = apply_edits(
            "a = 1\nb = 2\nc = 3\n",
            [EditOp("a = 1", "a = 10"), EditOp("c = 3", "c = 30")],
        )
        assert out.content == "a = 10\nb = 2\nc = 30\n"

    def test_trim_level_multiline(self) -> None:
        src = "function f() {\n    if (x) {\n        return 1;\n    }\n}\n"
        out = apply_edits(src, [EditOp("if (x) {\nreturn 1;\n}", "if (x) {\n  return 2;\n}")])
        assert out.matches[0].level == "trim"
        assert out.content == "function f() {\nif (x) {\n  return 2;\n}\n}\n"

    def test_unicode_level(self) -> None:
        out = apply_edits('const s = "hello";\n', [EditOp("“hello”", '"bye"')])
        assert out.content == 'const s = "bye";\n'
        assert out.matches[0].level == "unicode"

    def test_not_found(self) -> None:
        with pytest.raises(EditError) as exc:
            apply_edits("abc\n", [EditOp("xyz", "q")])
        assert exc.value.code == "not_found"

    def test_ambiguous(self) -> None:
        with pytest.raises(EditError) as exc:
            apply_edits("foo\nfoo\n", [EditOp("foo", "bar")])
        assert exc.value.code == "ambiguous"

    def test_overlap(self) -> None:
        with pytest.raises(EditError) as exc:
            apply_edits("abcdef\n", [EditOp("abc", "X"), EditOp("cde", "Y")])
        assert exc.value.code == "overlap"

    def test_empty(self) -> None:
        with pytest.raises(EditError) as exc:
            apply_edits("abc", [EditOp("", "x")])
        assert exc.value.code == "empty_old"
        with pytest.raises(EditError) as exc2:
            apply_edits("abc", [])
        assert exc2.value.code == "no_edits"

    def test_diff_hunks_split_and_merge(self) -> None:
        far = "\n".join(f"l{i}" for i in range(30)) + "\n"
        out = apply_edits(far, [EditOp("l2", "L2"), EditOp("l25", "L25")], file_path="f.txt")
        assert out.diff.count("@@ ") == 2
        near = apply_edits("a\nb\nc\nd\n", [EditOp("a", "A"), EditOp("b", "B")], file_path="f.txt")
        assert near.diff.count("@@ ") == 1
        assert "-a" in near.diff and "+A" in near.diff


@pytest.mark.asyncio
class TestEditFileTool:
    async def test_edit_roundtrip_with_version(self, tmp_path: Path) -> None:
        router = workspace_tools_for_cwd(tmp_path)
        await _call(router, "write_file", {"path": "a.txt", "content": "foo = 1\nbar = 2\n"})
        read = await _call(router, "read_file", {"path": "a.txt"})
        assert read.data["version"] == content_version("foo = 1\nbar = 2\n")
        edited = await _call(
            router,
            "edit_file",
            {
                "path": "a.txt",
                "edits": [{"oldText": "bar = 2", "newText": "bar = 20"}],
                "expectedVersion": read.data["version"],
            },
        )
        assert edited.success is True
        assert edited.data["applied"] == 1
        assert "-bar = 2" in edited.data["diff"]
        assert "+bar = 20" in edited.data["diff"]
        assert (tmp_path / "a.txt").read_text() == "foo = 1\nbar = 20\n"

    async def test_conflict_rejected_and_file_untouched(self, tmp_path: Path) -> None:
        router = workspace_tools_for_cwd(tmp_path)
        await _call(router, "write_file", {"path": "a.txt", "content": "x = 1\n"})
        read = await _call(router, "read_file", {"path": "a.txt"})
        (tmp_path / "a.txt").write_text("x = 999\n")  # external change
        edited = await _call(
            router,
            "edit_file",
            {
                "path": "a.txt",
                "edits": [{"oldText": "x = 1", "newText": "x = 2"}],
                "expectedVersion": read.data["version"],
            },
        )
        assert edited.success is False
        assert "冲突" in (edited.error or "")
        assert (tmp_path / "a.txt").read_text() == "x = 999\n"

    async def test_failed_edit_writes_nothing(self, tmp_path: Path) -> None:
        router = workspace_tools_for_cwd(tmp_path)
        await _call(router, "write_file", {"path": "a.txt", "content": "alpha\nbeta\n"})
        edited = await _call(
            router,
            "edit_file",
            {"path": "a.txt", "edits": [{"oldText": "gamma", "newText": "x"}]},
        )
        assert edited.success is False
        assert (tmp_path / "a.txt").read_text() == "alpha\nbeta\n"

    async def test_path_escape_rejected(self, tmp_path: Path) -> None:
        router = workspace_tools_for_cwd(tmp_path)
        edited = await _call(
            router,
            "edit_file",
            {"path": "../out.txt", "edits": [{"oldText": "a", "newText": "b"}]},
        )
        assert edited.success is False
        assert "escapes" in (edited.error or "")

    async def test_write_file_conflict_token(self, tmp_path: Path) -> None:
        router = workspace_tools_for_cwd(tmp_path)
        await _call(router, "write_file", {"path": "a.txt", "content": "v1\n"})
        read = await _call(router, "read_file", {"path": "a.txt"})
        (tmp_path / "a.txt").write_text("v2\n")
        write = await _call(
            router,
            "write_file",
            {"path": "a.txt", "content": "v3\n", "expectedVersion": read.data["version"]},
        )
        assert write.success is False
        assert (tmp_path / "a.txt").read_text() == "v2\n"

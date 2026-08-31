"""W1.4.1.3: multi-file atomic patch — planning, atomicity, rollback, diffs."""

from __future__ import annotations

from pathlib import Path

import pytest

from steerable_sidecar.file_edit import EditError, EditOp
from steerable_sidecar.multi_file_edit import FilePatch, apply_patch


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")
    return tmp_path


def _patch(path: str, old: str, new: str) -> FilePatch:
    return FilePatch(path=path, edits=(EditOp(old_text=old, new_text=new),))


async def test_multi_file_patch_applies_atomically(repo: Path) -> None:
    summary = await apply_patch(
        repo,
        [
            _patch("a.py", "return 1", "return 10"),
            _patch("b.py", "return 2", "return 20"),
        ],
    )
    assert summary.files_changed == ("a.py", "b.py")
    assert (repo / "a.py").read_text() == "def a():\n    return 10\n"
    assert (repo / "b.py").read_text() == "def b():\n    return 20\n"
    assert len(summary.diffs) == 2
    # Trim-level matches span the text without its indent; the diff renders
    # the replaced span as matched.
    assert "+return 10" in summary.diffs[0]


async def test_locate_failure_aborts_with_nothing_written(repo: Path) -> None:
    """The second file's bad edit must not leave the first file changed."""
    with pytest.raises(EditError):
        await apply_patch(
            repo,
            [
                _patch("a.py", "return 1", "return 10"),
                _patch("b.py", "return 999", "return 20"),  # not in b.py
            ],
        )
    assert (repo / "a.py").read_text() == "def a():\n    return 1\n"
    assert (repo / "b.py").read_text() == "def b():\n    return 2\n"


async def test_missing_file_aborts_with_nothing_written(repo: Path) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        await apply_patch(
            repo,
            [
                _patch("a.py", "return 1", "return 10"),
                _patch("ghost.py", "x", "y"),
            ],
        )
    assert (repo / "a.py").read_text() == "def a():\n    return 1\n"


async def test_duplicate_file_entries_rejected(repo: Path) -> None:
    with pytest.raises(ValueError, match="duplicate patch"):
        await apply_patch(
            repo,
            [_patch("a.py", "return 1", "return 10"), _patch("a.py", "def a", "def aa")],
        )


async def test_empty_patch_rejected(repo: Path) -> None:
    with pytest.raises(ValueError, match="patches is empty"):
        await apply_patch(repo, [])


async def test_write_phase_failure_rolls_back(repo: Path, monkeypatch) -> None:
    """A mid-write failure (disk, permissions) restores what was written."""
    original_write = Path.write_text
    calls = {"n": 0}

    def fail_second(self, content, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return original_write(self, content, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_second)
    with pytest.raises(ValueError, match="write failed mid-patch"):
        await apply_patch(
            repo,
            [_patch("a.py", "return 1", "return 10"), _patch("b.py", "return 2", "return 20")],
        )
    monkeypatch.undo()
    assert (repo / "a.py").read_text() == "def a():\n    return 1\n"


async def test_workspace_escape_rejected_by_resolver(repo: Path) -> None:
    """The resolver is the workspace-jurisdiction hook (1.4.1.4)."""

    def jail(path: str) -> Path:
        target = (repo / path).resolve()
        if repo not in target.parents and target != repo:
            raise ValueError(f"path escapes workspace: {path}")
        return target

    with pytest.raises(ValueError, match="escapes workspace"):
        await apply_patch(repo, [_patch("../outside.py", "x", "y")], resolve=jail)


async def test_whitespace_tolerant_matching_carries_over(repo: Path) -> None:
    """The three-tier matcher is the same one edit_file uses."""
    (repo / "c.py").write_text("def c():\n        return 3\n")  # deep indent
    await apply_patch(repo, [_patch("c.py", "return 3", "return 30")])
    assert "return 30" in (repo / "c.py").read_text()


async def test_patch_flows_through_a_workspace_fs_channel(repo: Path) -> None:
    """3.4.3.1: apply_patch reads and writes through the injected channel,
    so the ACP editor bridge serves buffers instead of disk."""
    from steerable_sidecar.workspace_fs import LOCAL_FS

    class _RecordingFs:
        def __init__(self) -> None:
            self.reads: list[Path] = []
            self.writes: list[tuple[Path, str]] = []

        async def read_text(self, target: Path) -> str:
            self.reads.append(target)
            return await LOCAL_FS.read_text(target)

        async def write_text(self, target: Path, content: str) -> None:
            self.writes.append((target, content))
            await LOCAL_FS.write_text(target, content)

    channel = _RecordingFs()
    summary = await apply_patch(repo, [_patch("a.py", "return 1", "return 10")], fs=channel)
    assert summary.files_changed == ("a.py",)
    assert channel.reads == [repo / "a.py"]
    assert [str(w[0]) for w in channel.writes] == [str(repo / "a.py")]
    assert (repo / "a.py").read_text() == "def a():\n    return 10\n"

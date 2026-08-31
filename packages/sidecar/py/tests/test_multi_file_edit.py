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


def test_multi_file_patch_applies_atomically(repo: Path) -> None:
    summary = apply_patch(
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


def test_locate_failure_aborts_with_nothing_written(repo: Path) -> None:
    """The second file's bad edit must not leave the first file changed."""
    with pytest.raises(EditError):
        apply_patch(
            repo,
            [
                _patch("a.py", "return 1", "return 10"),
                _patch("b.py", "return 999", "return 20"),  # not in b.py
            ],
        )
    assert (repo / "a.py").read_text() == "def a():\n    return 1\n"
    assert (repo / "b.py").read_text() == "def b():\n    return 2\n"


def test_missing_file_aborts_with_nothing_written(repo: Path) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        apply_patch(
            repo,
            [
                _patch("a.py", "return 1", "return 10"),
                _patch("ghost.py", "x", "y"),
            ],
        )
    assert (repo / "a.py").read_text() == "def a():\n    return 1\n"


def test_duplicate_file_entries_rejected(repo: Path) -> None:
    with pytest.raises(ValueError, match="duplicate patch"):
        apply_patch(
            repo,
            [_patch("a.py", "return 1", "return 10"), _patch("a.py", "def a", "def aa")],
        )


def test_empty_patch_rejected(repo: Path) -> None:
    with pytest.raises(ValueError, match="patches is empty"):
        apply_patch(repo, [])


def test_write_phase_failure_rolls_back(repo: Path, monkeypatch) -> None:
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
        apply_patch(
            repo,
            [_patch("a.py", "return 1", "return 10"), _patch("b.py", "return 2", "return 20")],
        )
    monkeypatch.undo()
    assert (repo / "a.py").read_text() == "def a():\n    return 1\n"


def test_workspace_escape_rejected_by_resolver(repo: Path) -> None:
    """The resolver is the workspace-jurisdiction hook (1.4.1.4)."""

    def jail(path: str) -> Path:
        target = (repo / path).resolve()
        if repo not in target.parents and target != repo:
            raise ValueError(f"path escapes workspace: {path}")
        return target

    with pytest.raises(ValueError, match="escapes workspace"):
        apply_patch(repo, [_patch("../outside.py", "x", "y")], resolve=jail)


def test_whitespace_tolerant_matching_carries_over(repo: Path) -> None:
    """The three-tier matcher is the same one edit_file uses."""
    (repo / "c.py").write_text("def c():\n        return 3\n")  # deep indent
    apply_patch(repo, [_patch("c.py", "return 3", "return 30")])
    assert "return 30" in (repo / "c.py").read_text()

"""W1.4.1.1/1.4.1.2: structured grep/glob — hits, limits, ignore set, fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from steerable_sidecar import search_tools
from steerable_sidecar.search_tools import glob_files, search


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def main():\n    print('hello world')\n")
    (tmp_path / "src" / "util.py").write_text("def helper():\n    return 'hello'\n")
    (tmp_path / "README.md").write_text("# hello project\n")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("// hello from dep\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("hello git internals\n")
    return tmp_path


def test_search_returns_structured_hits(repo: Path) -> None:
    hits = search(repo, "hello world")
    assert [(h.path, h.line) for h in hits] == [("src/main.py", 2)]
    assert "hello world" in hits[0].text


def test_search_ignores_node_modules_and_git(repo: Path) -> None:
    hits = search(repo, "hello")
    paths = {h.path for h in hits}
    assert "node_modules/pkg/index.js" not in paths
    assert ".git/config" not in paths
    assert {"src/main.py", "src/util.py", "README.md"} <= paths


def test_search_limit_caps_results(repo: Path) -> None:
    hits = search(repo, "hello", limit=2)
    assert len(hits) == 2


def test_search_regex_and_case(repo: Path) -> None:
    hits = search(repo, "HELLO", ignore_case=True)
    assert hits
    regex_hits = search(repo, r"def \w+\(", is_regex=True)
    assert {h.path for h in regex_hits} == {"src/main.py", "src/util.py"}


def test_search_invalid_regex_fails_loud(repo: Path) -> None:
    with pytest.raises(ValueError, match="invalid regex"):
        search(repo, "[unclosed", is_regex=True)


def test_search_empty_query_fails_loud(repo: Path) -> None:
    with pytest.raises(ValueError, match="query is empty"):
        search(repo, "")


def test_pure_python_fallback_matches_rg(repo: Path, monkeypatch) -> None:
    """The fallback is a first-class path: containers may lack rg."""
    monkeypatch.setattr(search_tools.shutil, "which", lambda _: None)
    hits = search(repo, "hello")
    assert {h.path for h in hits} >= {"src/main.py", "src/util.py", "README.md"}
    assert all("node_modules" not in h.path for h in hits)


def test_glob_matches_by_relative_path_or_basename(repo: Path) -> None:
    assert glob_files(repo, "*.py") == ["src/main.py", "src/util.py"]
    assert glob_files(repo, "src/*.py") == ["src/main.py", "src/util.py"]
    assert glob_files(repo, "README*") == ["README.md"]


def test_glob_applies_ignore_set(repo: Path) -> None:
    assert glob_files(repo, "*.js") == []  # node_modules never surfaces


def test_glob_limit_and_empty_pattern(repo: Path) -> None:
    assert len(glob_files(repo, "*", limit=2)) == 2
    with pytest.raises(ValueError, match="pattern is empty"):
        glob_files(repo, "  ")

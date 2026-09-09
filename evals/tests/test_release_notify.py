"""Release.yml must page Feishu after the npm/PyPI chain settles."""

from __future__ import annotations

from pathlib import Path

RELEASE = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
).read_text(encoding="utf-8")


def test_release_posts_feishu_after_both_publish_jobs() -> None:
    """A tag that publishes (or fails midway) has to page the team; evals
    already do this, and a silent registry push is how 0.6.x releases
    used to land without anyone noticing."""
    assert "FEISHU_BOT_WEBHOOK: ${{ secrets.FEISHU_BOT_WEBHOOK }}" in RELEASE
    assert "needs: [validate, publish-npm, publish-pypi]" in RELEASE
    assert "if: always()" in RELEASE
    assert "FEISHU_BOT_WEBHOOK unset; skip" in RELEASE
    assert "::warning::Feishu post failed" in RELEASE
    assert "Steerable {tag}" in RELEASE

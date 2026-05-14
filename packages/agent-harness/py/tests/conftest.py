from __future__ import annotations

import json
from pathlib import Path

import pytest


GOLDEN_DIR = Path(__file__).parent / "golden"


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    return GOLDEN_DIR


@pytest.fixture()
def assert_golden(golden_dir: Path):
    """Compare a serialized payload against an on-disk JSON snapshot.

    Set the env var ``UPDATE_GOLDEN=1`` to regenerate goldens.
    """

    import os

    update = os.environ.get("UPDATE_GOLDEN") == "1"

    def _check(name: str, payload: object) -> None:
        target = golden_dir / f"{name}.json"
        encoded = json.dumps(payload, indent=2, sort_keys=True, default=str)
        if update or not target.exists():
            target.write_text(encoded + "\n", encoding="utf-8")
            return
        actual = encoded + "\n"
        expected = target.read_text(encoding="utf-8")
        assert actual == expected, (
            f"Golden mismatch for {name}.\n"
            f"--- expected ({target}):\n{expected}\n"
            f"--- actual:\n{actual}\n"
            "Re-run with UPDATE_GOLDEN=1 to refresh."
        )

    return _check

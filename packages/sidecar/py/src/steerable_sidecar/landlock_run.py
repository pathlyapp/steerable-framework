"""Landlock launcher entrypoint: ``python -m steerable_sidecar.landlock_run``.

Kept out of the package's import graph on purpose: nothing imports this
module, so ``-m`` executes it cleanly (importing it from ``__init__`` would
put it in ``sys.modules`` before runpy executes it and trigger the runpy
double-import warning on every wrapped command). The policy and ruleset
machinery lives in ``landlock.py``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from .landlock import _install_ruleset, parse_launcher_argv


def main(argv: Sequence[str] | None = None) -> int:
    """Install the ruleset, then exec the command. Any failure before exec
    is loud on stderr with a non-zero exit, never a silent unconfined run."""

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        roots, network, cmd = parse_launcher_argv(args)
        _install_ruleset(roots, network)
        os.execvp(cmd[0], cmd)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"steerable-landlock: {exc}", file=sys.stderr)
        return 1
    raise AssertionError("unreachable: execvp replaces the process")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

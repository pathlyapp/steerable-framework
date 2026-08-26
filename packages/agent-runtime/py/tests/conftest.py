"""Pytest bootstrap for the agent-runtime test suite.

This directory has no ``__init__.py`` (dropped to avoid the three-way
``tests`` package collision under ``--import-mode=importlib``), so sibling
helper imports like ``from test_trace_recorder import make_provider`` resolve
as top-level modules. pytest's importlib mode does not put the test file's
directory on ``sys.path``; do it here — conftest is imported before any test
module in this directory. Basenames are kept unique across all three python
test suites (agent-harness / agent-runtime / sidecar), so the path insert
cannot shadow a sibling suite's module.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

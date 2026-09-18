"""Package version resolved from installed distribution metadata.

Kept in a dedicated module so both ``__init__`` and ``sidecar`` can import it
without a circular import (``__init__`` imports ``sidecar``). The release
script bumps ``pyproject.toml`` only; reading the installed metadata keeps the
reported version in lockstep with the published package.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("steerable-sidecar")
except PackageNotFoundError:  # source tree without installed metadata
    __version__ = "0.0.0"

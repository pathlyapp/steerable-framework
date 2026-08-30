"""steerable-sidecar — portable runtime entrypoint."""

from .landlock import LandlockExecBackend, landlock_available
from .sandbox import (
    BwrapExecBackend,
    SeatbeltExecBackend,
    build_seatbelt_profile,
    bwrap_available,
    seatbelt_argv,
    seatbelt_available,
    select_exec_backend,
)
from .sidecar import Sidecar, SidecarConfig

__all__ = [
    "BwrapExecBackend",
    "LandlockExecBackend",
    "SeatbeltExecBackend",
    "Sidecar",
    "SidecarConfig",
    "build_seatbelt_profile",
    "bwrap_available",
    "landlock_available",
    "seatbelt_argv",
    "seatbelt_available",
    "select_exec_backend",
]
__version__ = "0.1.0"

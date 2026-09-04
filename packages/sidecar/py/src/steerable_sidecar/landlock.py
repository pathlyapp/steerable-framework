"""Linux Landlock per-exec backend (layer 2) and its launcher.

Landlock is a kernel LSM (5.13+), not a command wrapper: restrictions are
installed on the *current* process via syscalls and inherited by children.
To fit the executor's command-rewriting model, the backend rewrites the
command to run through our own launcher — ``python -m
steerable_sidecar.landlock --root R ... -- sh -c <cmd>`` — which installs
the ruleset on itself and then ``execvp`` the target. No external binary,
no user namespaces, no setuid: the only dependency is the kernel, so this
backend works inside containers where bwrap's namespace creation is
refused. The launcher runs under the same interpreter that runs the
sidecar package, so it is importable by construction.

Policy (mirrors the bwrap profile's semantics):

- **Reads + execute**: open (rule on ``/``) — skill roots are
  host-configured per request; confinement targets writes and egress.
- **Writes**: declared writable roots plus ``/tmp`` / ``/var/tmp`` and the
  ``/dev/null`` sink. Unlike bwrap, ``/tmp`` is the host's shared scratch
  (Landlock has no mount namespaces) — noted in docs/spec/safety.md.
- **Network**: all-or-nothing, and only on ABI v4 (kernel 6.7+) which can
  gate TCP bind/connect. ``network=False`` on ABI >= 4 denies both
  (``full``); on ABI < 4 egress is inexpressible, so the backend honestly
  reports ``partial`` and ``require_full`` refuses the call instead of
  pretending confinement. Per-host egress pinning does not exist in
  Landlock — ``allowed_hosts`` is accepted for interface parity only.

Availability is a functional probe (run the launcher wrapping a no-op),
not a version check: Docker's default seccomp profile errno-rejects the
landlock syscalls, and only the probe sees that.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shlex
import struct
import subprocess
import sys
from collections.abc import Sequence
from functools import cache

# asm-generic syscall numbers (x86_64 and aarch64 agree).
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446

_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

# Filesystem access rights (ABI v1); REFER is v2, TRUNCATE is v3.
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13
FS_TRUNCATE = 1 << 14

FS_MASK_ABI1 = (
    FS_EXECUTE
    | FS_WRITE_FILE
    | FS_READ_FILE
    | FS_READ_DIR
    | FS_REMOVE_DIR
    | FS_REMOVE_FILE
    | FS_MAKE_CHAR
    | FS_MAKE_DIR
    | FS_MAKE_REG
    | FS_MAKE_SOCK
    | FS_MAKE_FIFO
    | FS_MAKE_BLOCK
    | FS_MAKE_SYM
)

# Network access rights (ABI v4, kernel 6.7+).
NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1

_READ_ONLY_ROOT_RIGHTS = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
_SCRATCH_PATHS = ("/tmp", "/var/tmp")

_libc: ctypes.CDLL | None = None


def _get_libc() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    return _libc


def landlock_abi() -> int:
    """Highest Landlock ABI the kernel supports, 0 when unavailable.

    The version query is the syscall itself, so a seccomp-filtered or
    pre-5.13 kernel surfaces here as 0 (the libc ``syscall`` wrapper
    returns -1 with errno set: ENOSYS / EOPNOTSUPP / EPERM).
    """

    if platform.system() != "Linux":
        return 0
    try:
        libc = _get_libc()
    except OSError:
        return 0
    ret = libc.syscall(
        ctypes.c_long(_NR_CREATE_RULESET),
        None,
        ctypes.c_size_t(0),
        ctypes.c_uint(_CREATE_RULESET_VERSION),
    )
    return 0 if ret == -1 else int(ret)


def _fs_mask(abi: int) -> int:
    """FS rights the ABI knows — a ruleset must not handle unknown bits."""

    mask = FS_MASK_ABI1
    if abi >= 2:
        mask |= FS_REFER
    if abi >= 3:
        mask |= FS_TRUNCATE
    return mask


def ruleset_attr(*, abi: int, network: bool) -> bytes:
    """Serialize ``struct landlock_ruleset_attr`` for ``abi``.

    Pre-v4 kernels only know ``handled_access_fs`` (8 bytes) and E2BIG a
    larger struct; v4 adds ``handled_access_net``. Net rights are handled
    (i.e. denied-by-default, no net rules are ever added) exactly when
    egress is undeclared and the ABI can express that.
    """

    handled_fs = _fs_mask(abi)
    if abi >= 4:
        handled_net = 0 if network else (NET_BIND_TCP | NET_CONNECT_TCP)
        return struct.pack("=QQ", handled_fs, handled_net)
    return struct.pack("=Q", handled_fs)


def parse_launcher_argv(argv: Sequence[str]) -> tuple[list[str], bool, list[str]]:
    """Parse launcher argv: ``--root R`` (repeatable), ``--network``, then
    ``--`` followed by the command to exec."""

    roots: list[str] = []
    network = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if arg == "--root":
            i += 1
            if i >= len(argv):
                raise ValueError("--root needs a path argument")
            roots.append(argv[i])
        elif arg == "--network":
            network = True
        else:
            raise ValueError(f"unknown launcher flag {arg!r}")
        i += 1
    else:
        raise ValueError("missing '--' separator before the command")
    cmd = list(argv[i + 1 :])
    if not cmd:
        raise ValueError("missing command after '--'")
    return roots, network, cmd


def _normalize_root(root: str) -> str:
    """Canonicalize a writable root: ``~`` expansion, absolutized,
    symlink-free (the launcher opens the path it is given — a symlinked
    component would restrict a path other than the one the host declared).
    """

    return os.path.realpath(os.path.abspath(os.path.expanduser(root)))


def _add_path_rule(ruleset_fd: int, path: str, rights: int, handled_fs: int) -> None:
    """Grant ``rights`` (masked to handled) on ``path`` and everything
    beneath it. The path is opened with O_PATH — it must exist."""

    rights &= handled_fs
    if rights == 0:
        return
    fd = os.open(path, os.O_PATH)
    try:
        # struct landlock_path_beneath_attr is __attribute__((packed)):
        # { __u64 allowed_access;  __s32 parent_fd; } — 12 bytes, access
        # mask FIRST. Reversing the fields makes the kernel read the mask
        # as an fd and fail with EBADF.
        attr = struct.pack("=Qi", rights, fd)
        buf = ctypes.create_string_buffer(attr, len(attr))
        ret = _get_libc().syscall(
            ctypes.c_long(_NR_ADD_RULE),
            ctypes.c_int(ruleset_fd),
            ctypes.c_uint(_RULE_PATH_BENEATH),
            ctypes.cast(buf, ctypes.c_void_p),
            ctypes.c_uint(0),
        )
        if ret == -1:
            err = ctypes.get_errno()
            raise OSError(err, f"landlock_add_rule({path}): {os.strerror(err)}")
    finally:
        os.close(fd)


def _install_ruleset(roots: Sequence[str], network: bool) -> None:
    """Build the ruleset for this process and restrict ourselves. One-way
    by design — call only in the launcher, right before exec."""

    abi = landlock_abi()
    if abi < 1:
        raise RuntimeError("Landlock is not supported on this kernel")
    attr = ruleset_attr(abi=abi, network=network)
    handled_fs = int.from_bytes(attr[:8], "little")
    libc = _get_libc()
    buf = ctypes.create_string_buffer(attr, len(attr))
    ruleset_fd = libc.syscall(
        ctypes.c_long(_NR_CREATE_RULESET),
        ctypes.cast(buf, ctypes.c_void_p),
        ctypes.c_size_t(len(attr)),
        ctypes.c_uint(0),
    )
    if ruleset_fd == -1:
        err = ctypes.get_errno()
        raise OSError(err, f"landlock_create_ruleset: {os.strerror(err)}")
    try:
        _add_path_rule(ruleset_fd, "/", _READ_ONLY_ROOT_RIGHTS, handled_fs)
        for path in _SCRATCH_PATHS:
            if os.path.exists(path):
                _add_path_rule(ruleset_fd, path, handled_fs, handled_fs)
        for root in roots:
            _add_path_rule(ruleset_fd, root, handled_fs, handled_fs)
        _add_path_rule(ruleset_fd, "/dev/null", FS_READ_FILE | FS_WRITE_FILE, handled_fs)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"prctl(NO_NEW_PRIVS): {os.strerror(err)}")
        ret = libc.syscall(
            ctypes.c_long(_NR_RESTRICT_SELF), ctypes.c_int(ruleset_fd), ctypes.c_uint(0)
        )
        if ret == -1:
            err = ctypes.get_errno()
            raise OSError(err, f"landlock_restrict_self: {os.strerror(err)}")
    finally:
        os.close(ruleset_fd)


class LandlockExecBackend:
    """Per-exec Landlock backend for ``SandboxedToolExecutor`` (layer 2).

    Selected on Linux when bwrap is unavailable (see
    ``select_exec_backend`` — bwrap confines more dimensions when it can
    run). ``abi``/``executable`` are test seams; production construction
    queries the running kernel and uses the sidecar's own interpreter.
    """

    name = "landlock"

    def __init__(
        self,
        *,
        writable_roots: Sequence[str] | None = None,
        network: bool = False,
        allowed_hosts: Sequence[str] | None = None,
        shell: str = "/bin/sh",
        abi: int | None = None,
        executable: str | None = None,
    ) -> None:
        self._executable = executable or sys.executable
        self._abi = landlock_abi() if abi is None else abi
        self._roots = []
        for root in writable_roots or []:
            normalized = _normalize_root(root)
            if not os.path.isdir(normalized):
                raise ValueError(
                    f"writable root {root!r} does not exist: landlock rules "
                    "open the path when the launcher installs them"
                )
            self._roots.append(normalized)
        self._network = network
        # Accepted for interface parity; Landlock has no per-host egress.
        self._allowed_hosts = list(allowed_hosts) if allowed_hosts is not None else None
        self._shell = shell

    @property
    def enforcement(self) -> str:
        """``full`` only when egress is denied AND the ABI can enforce that
        (v4+). ``partial`` when egress is open or inexpressible — fs is
        still confined, but the honest value lets ``require_full`` refuse."""
        if self._network:
            return "partial"
        return "full" if self._abi >= 4 else "partial"

    def argv_for(self, command: str) -> list[str]:
        """Full sandboxed argv through the launcher (also used by the
        availability probe, so the probe exercises the real entry path)."""

        return self.argv_for_exec([self._shell, "-c", command])

    def argv_for_exec(self, argv: Sequence[str]) -> list[str]:
        """Wrap an argv (not a shell string) for layer-1 process confinement."""

        if not argv:
            raise ValueError("linux-wrap command argv is empty")
        wrapped = [self._executable, "-m", "steerable_sidecar.landlock_run"]
        for root in self._roots:
            wrapped += ["--root", root]
        if self._network:
            wrapped.append("--network")
        return [*wrapped, "--", *argv]

    def wrap_command(self, command: str) -> str:
        return " ".join(shlex.quote(part) for part in self.argv_for(command))


@cache
def _probe_landlock(executable: str) -> bool:
    """Functional availability probe: the ABI query must succeed AND the
    launcher must confine+exec a no-op. Cached per interpreter path."""

    if landlock_abi() < 1:
        return False
    try:
        probe = LandlockExecBackend(executable=executable)
        result = subprocess.run(
            probe.argv_for("exit 0"),
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def landlock_available() -> bool:
    """True on Linux when the launcher probe confines this interpreter."""

    return platform.system() == "Linux" and _probe_landlock(sys.executable)

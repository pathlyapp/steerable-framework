"""Landlock per-exec backend (Linux layer 2).

Landlock is a kernel LSM (5.13+), not a command wrapper, so the backend
rewrites the command to run through our own launcher
(``python -m steerable_sidecar.landlock``), which installs the ruleset on
itself and then execs the target — children inherit the restriction.

Unit tests here are platform-independent (policy rendering, enforcement
mapping, the selection ladder). Real confinement is verified with
Linux-gated smoke tests that run in CI.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from steerable_sidecar.landlock import (
    FS_MASK_ABI1,
    FS_REFER,
    FS_TRUNCATE,
    NET_BIND_TCP,
    NET_CONNECT_TCP,
    LandlockExecBackend,
    landlock_abi,
    landlock_available,
    parse_launcher_argv,
    ruleset_attr,
)


class TestLauncherArgvParsing:
    def test_roots_network_and_command(self) -> None:
        roots, network, cmd = parse_launcher_argv(
            ["--root", "/a", "--root", "/b c", "--network", "--", "/bin/sh", "-c", "echo hi"]
        )
        assert roots == ["/a", "/b c"]
        assert network is True
        assert cmd == ["/bin/sh", "-c", "echo hi"]

    def test_defaults_no_roots_no_network(self) -> None:
        roots, network, cmd = parse_launcher_argv(["--", "true"])
        assert roots == []
        assert network is False
        assert cmd == ["true"]

    def test_missing_separator_or_command_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="--"):
            parse_launcher_argv(["--root", "/a"])
        with pytest.raises(ValueError, match="command"):
            parse_launcher_argv(["--"])


class TestRulesetAttr:
    """The binary ruleset header: fs rights masked by ABI, net rights only
    handled (denied-by-default) when egress is undeclared AND the ABI can
    express it (v4+, kernel 6.7)."""

    def test_abi1_masks_to_v1_rights_only(self) -> None:
        attr = ruleset_attr(abi=1, network=False)
        handled_fs = int.from_bytes(attr[:8], "little")
        assert handled_fs == FS_MASK_ABI1
        assert len(attr) == 8  # pre-v4 kernels reject a larger struct

    def test_abi3_adds_refer_and_truncate(self) -> None:
        handled_fs = int.from_bytes(ruleset_attr(abi=3, network=False)[:8], "little")
        assert handled_fs & FS_REFER
        assert handled_fs & FS_TRUNCATE

    def test_net_denied_only_when_abi4_and_undeclared(self) -> None:
        attr = ruleset_attr(abi=4, network=False)
        handled_net = int.from_bytes(attr[8:16], "little")
        assert handled_net == NET_BIND_TCP | NET_CONNECT_TCP

    def test_net_open_when_declared(self) -> None:
        attr = ruleset_attr(abi=4, network=True)
        assert int.from_bytes(attr[8:16], "little") == 0

    def test_net_unenforceable_below_abi4(self) -> None:
        # The struct stays 8 bytes: an ABI-3 kernel would E2BIG a larger one,
        # and it has no net rights to handle anyway. Enforcement honesty is
        # the backend's job (reports partial), not the struct's.
        assert len(ruleset_attr(abi=3, network=False)) == 8


class TestLandlockExecBackend:
    def test_argv_routes_through_the_launcher(self, tmp_path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        backend = LandlockExecBackend(writable_roots=[str(root)], abi=4)
        argv = backend.argv_for("true")

        assert argv[:3] == [sys.executable, "-m", "steerable_sidecar.landlock_run"]
        assert argv[argv.index("--root") + 1] == str(root)
        # network=False is the default and needs no flag (deny is default).
        assert "--network" not in argv
        assert argv[-4:] == ["--", "/bin/sh", "-c", "true"]

    def test_network_declared_adds_flag(self) -> None:
        argv = LandlockExecBackend(network=True, abi=4).argv_for("true")
        assert "--network" in argv

    def test_wrapped_string_is_shell_parseable(self, tmp_path) -> None:
        backend = LandlockExecBackend(writable_roots=[str(tmp_path)], abi=4)
        wrapped = backend.wrap_command("echo 'hello world' && ls /tmp")
        parsed = subprocess.run(
            ["/bin/sh", "-n", "-c", wrapped], capture_output=True, check=False
        )
        assert parsed.returncode == 0, parsed.stderr.decode()

    def test_enforcement_full_only_when_net_denied_and_abi4(self) -> None:
        assert LandlockExecBackend(network=False, abi=4).enforcement == "full"
        assert LandlockExecBackend(network=False, abi=5).enforcement == "full"

    def test_enforcement_partial_when_net_unenforceable(self) -> None:
        # ABI < 4 cannot deny TCP connect/bind — fs is confined but egress
        # is open, so the honest value is partial (require_full will refuse).
        assert LandlockExecBackend(network=False, abi=3).enforcement == "partial"
        assert LandlockExecBackend(network=False, abi=1).enforcement == "partial"

    def test_enforcement_partial_when_egress_open(self) -> None:
        assert LandlockExecBackend(network=True, abi=4).enforcement == "partial"
        # Like bwrap, Landlock cannot pin egress per host: an allow-list is
        # accepted for interface parity but does NOT raise enforcement.
        assert (
            LandlockExecBackend(
                network=True, allowed_hosts=["localhost:11434"], abi=4
            ).enforcement
            == "partial"
        )

    def test_writable_root_must_exist(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            LandlockExecBackend(writable_roots=[str(tmp_path / "missing")], abi=4)

    def test_symlinked_root_is_resolved(self, tmp_path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        argv = LandlockExecBackend(writable_roots=[str(link)], abi=4).argv_for("true")
        assert argv[argv.index("--root") + 1] == str(real)


class TestAvailability:
    def test_abi_zero_off_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("steerable_sidecar.landlock.platform.system", lambda: "Darwin")
        assert landlock_abi() == 0

    def test_unavailable_off_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("steerable_sidecar.landlock.platform.system", lambda: "Darwin")
        assert landlock_available() is False

    def test_probe_failure_means_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A kernel/container that refuses the syscalls (seccomp-filtered
        landlock, < 5.13) is rejected by the functional probe — fail closed."""
        import steerable_sidecar.landlock as landlock_mod

        monkeypatch.setattr(landlock_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(landlock_mod, "landlock_abi", lambda: 0)
        landlock_mod._probe_landlock.cache_clear()
        try:
            assert landlock_mod.landlock_available() is False
        finally:
            landlock_mod._probe_landlock.cache_clear()


class TestSelectExecBackendLadder:
    """Linux ladder: bwrap (more isolation dimensions) → Landlock (no
    external binary needed) → none. Degradation stays visible through the
    backend name + enforcement in the result's _sandbox marker."""

    def _linux(self, mp: pytest.MonkeyPatch) -> None:
        import steerable_sidecar.sandbox as sandbox_mod

        mp.setattr(sandbox_mod, "seatbelt_available", lambda: False)
        mp.setattr(sandbox_mod.platform, "system", lambda: "Linux")

    def test_bwrap_wins_when_it_probes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import steerable_sidecar.sandbox as sandbox_mod
        from steerable_sidecar.sandbox import BwrapExecBackend, select_exec_backend

        self._linux(monkeypatch)
        monkeypatch.setattr(sandbox_mod, "bwrap_path", lambda: "/usr/bin/bwrap")
        monkeypatch.setattr(sandbox_mod, "landlock_available", lambda: True)
        backend = select_exec_backend()
        assert isinstance(backend, BwrapExecBackend)

    def test_landlock_is_the_no_bwrap_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import steerable_sidecar.sandbox as sandbox_mod
        from steerable_sidecar.sandbox import select_exec_backend

        self._linux(monkeypatch)
        monkeypatch.setattr(sandbox_mod, "bwrap_path", lambda: None)
        monkeypatch.setattr(sandbox_mod, "landlock_available", lambda: True)
        backend = select_exec_backend()
        assert isinstance(backend, LandlockExecBackend)
        assert backend.name == "landlock"

    def test_none_when_neither_probes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import steerable_sidecar.sandbox as sandbox_mod
        from steerable_sidecar.sandbox import select_exec_backend

        self._linux(monkeypatch)
        monkeypatch.setattr(sandbox_mod, "bwrap_path", lambda: None)
        monkeypatch.setattr(sandbox_mod, "landlock_available", lambda: False)
        assert select_exec_backend() is None


# ---------------------------------------------------------------------------
# Real-confinement smoke tests — Linux with a usable Landlock only (CI).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not landlock_available(), reason="Linux with Landlock only")
class TestLandlockRealConfinement:
    def test_probe_wrap_runs(self) -> None:
        backend = LandlockExecBackend()
        ran = subprocess.run(
            backend.wrap_command("exit 0"),
            shell=True,
            capture_output=True,
            check=False,
            executable="/bin/sh",
        )
        assert ran.returncode == 0, ran.stderr.decode()

    def test_writes_confined_to_declared_roots(self, tmp_path) -> None:
        writable = tmp_path / "allowed"
        writable.mkdir()
        backend = LandlockExecBackend(writable_roots=[str(writable)])

        ok = subprocess.run(
            backend.wrap_command(f"echo hi > {writable}/f.txt && echo s > /tmp/ll-s.txt"),
            shell=True,
            capture_output=True,
            check=False,
            executable="/bin/sh",
        )
        assert ok.returncode == 0, ok.stderr.decode()
        assert (writable / "f.txt").read_text().strip() == "hi"

        denied = subprocess.run(
            backend.wrap_command("echo hi > $HOME/should-not-exist-steerable"),
            shell=True,
            capture_output=True,
            check=False,
            executable="/bin/sh",
        )
        assert denied.returncode != 0

    def test_reads_stay_open(self) -> None:
        backend = LandlockExecBackend()
        read = subprocess.run(
            backend.wrap_command("head -1 /etc/hostname || head -1 /etc/hosts"),
            shell=True,
            capture_output=True,
            check=False,
            executable="/bin/sh",
        )
        assert read.returncode == 0, read.stderr.decode()

    @pytest.mark.skipif(landlock_abi() < 4, reason="net rights need Landlock ABI v4 (6.7+)")
    def test_network_denied_by_default(self) -> None:
        backend = LandlockExecBackend()  # network=False
        denied = subprocess.run(
            backend.wrap_command(
                f"{sys.executable} -c \""
                "import socket;s=socket.create_connection(('127.0.0.1',9),timeout=2)\""
            ),
            shell=True,
            capture_output=True,
            check=False,
            executable="/bin/sh",
        )
        assert denied.returncode != 0

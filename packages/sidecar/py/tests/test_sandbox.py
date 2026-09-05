"""Seatbelt profile generation for the sidecar sandbox.

These tests pin the policy text's load-bearing rules (closed-by-default,
write whitelist, no network-bind), the egress allow-list semantics, and the
argv wrapping. The allow-list enforcement itself is verified with real
sandbox-exec smoke tests on macOS (skipped elsewhere).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from collections.abc import Iterator

import pytest
from steerable_sidecar.landlock import landlock_available
from steerable_sidecar.sandbox import (
    MACOS_SEATBELT_EXECUTABLE,
    BwrapExecBackend,
    SeatbeltExecBackend,
    build_seatbelt_profile,
    bwrap_available,
    linux_process_wrap,
    main,
    seatbelt_argv,
    seatbelt_available,
    select_exec_backend,
)


def test_profile_is_closed_by_default() -> None:
    profile = build_seatbelt_profile()
    assert "(deny default)" in profile
    # The sidecar never listens — binding must stay denied.
    assert "network-bind" not in profile


def test_profile_allows_broad_reads_and_outbound_network() -> None:
    profile = build_seatbelt_profile()
    assert "(allow file-read*)" in profile
    assert "(allow network-outbound)" in profile
    # DNS/TLS platform services HTTPS providers need.
    assert "com.apple.SystemConfiguration.DNSConfiguration" in profile


def test_profile_no_network_omits_outbound() -> None:
    profile = build_seatbelt_profile(network=False)
    assert "network-outbound" not in profile


def test_profile_writable_roots_are_normalized_literals() -> None:
    profile = build_seatbelt_profile(writable_roots=["~/Library/Caches/x"])
    assert '(subpath "' in profile
    # ~ must not leak into the profile — sbpl does not expand it.
    assert "~" not in profile


def test_profile_writable_root_escapes_quotes() -> None:
    profile = build_seatbelt_profile(writable_roots=['/tmp/we"ird'])
    assert '\\"' in profile


def test_profile_without_writable_roots_has_no_subpath_write() -> None:
    profile = build_seatbelt_profile()
    # Only the scratch dirs may be writable.
    assert profile.count("file-write*") == 4


def test_seatbelt_argv_wraps_with_inline_profile() -> None:
    argv = seatbelt_argv("(deny default)", ["/usr/bin/python3", "-m", "steerable_sidecar"])
    assert argv[:3] == [MACOS_SEATBELT_EXECUTABLE, "-p", "(deny default)"]
    assert argv[3:] == ["/usr/bin/python3", "-m", "steerable_sidecar"]


def test_seatbelt_available_matches_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    assert seatbelt_available() is False


def test_cli_prints_profile(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "sys.argv",
            ["sandbox", "profile", "--writable-root", "/tmp/x", "--no-network"],
        )
        assert main() == 0
    out = capsys.readouterr().out
    assert "(deny default)" in out
    assert "/private/tmp" in out  # realpath'd /tmp/x lands under /private/tmp
    assert "network-outbound" not in out


@pytest.mark.skipif(not seatbelt_available(), reason="macOS sandbox-exec only")
def test_profile_actually_confines_a_child_process() -> None:
    """Real sandbox-exec smoke: confined child can read but not write."""

    profile = build_seatbelt_profile()
    probe = (
        "import pathlib,sys;"
        "pathlib.Path('/tmp').joinpath('sb-ok').read_text() if False else None;"
        "sys.exit("
        "  0 if pathlib.Path('/etc/hosts').exists() else 1)"
    )
    ok = subprocess.run(
        seatbelt_argv(profile, ["python3", "-c", probe]),
        capture_output=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr.decode()

    denied = subprocess.run(
        seatbelt_argv(
            profile,
            ["python3", "-c", "open('/etc/sb-denied-test','w').write('x')"],
        ),
        capture_output=True,
        check=False,
    )
    assert denied.returncode != 0


# ---------------------------------------------------------------------------
# Egress allow-list (Wave 0, safety stage 1)
# ---------------------------------------------------------------------------


def test_allow_list_unconfigured_keeps_outbound_open() -> None:
    # Default-preserving: no allow-list → the same open policy as before.
    profile = build_seatbelt_profile(allowed_hosts=None)
    assert "(allow network-outbound)" in profile


def test_allow_list_localhost_entry_pins_host_and_port() -> None:
    profile = build_seatbelt_profile(allowed_hosts=["127.0.0.1:11434"])
    assert '(remote tcp "localhost:11434")' in profile
    # The open rule is gone — every remaining grant carries a remote filter.
    assert "\n(allow network-outbound)\n" not in profile
    # DNS/TLS mach services stay, or resolving the allowed hosts would break.
    assert "com.apple.SystemConfiguration.DNSConfiguration" in profile


def test_allow_list_bare_remote_host_degrades_to_ports() -> None:
    # Seatbelt cannot match hostnames; a bare remote host allows 443+80 on
    # any host, and the profile documents its own limitation.
    profile = build_seatbelt_profile(allowed_hosts=["api.openai.com"])
    assert '(remote tcp "*:443")' in profile
    assert '(remote tcp "*:80")' in profile
    assert "cannot match hostnames" in profile


def test_allow_list_explicit_port_and_dedup() -> None:
    profile = build_seatbelt_profile(
        allowed_hosts=["api.deepseek.com:8443", "api.deepseek.com:8443"]
    )
    assert profile.count('(remote tcp "*:8443")') == 1
    assert "*:443" not in profile


def test_allow_list_empty_list_denies_all_outbound() -> None:
    # Configured-but-empty is fail-closed: no outbound grants at all.
    profile = build_seatbelt_profile(allowed_hosts=[])
    assert "network-outbound" not in profile
    assert "com.apple.SystemConfiguration.configd" in profile


def test_web_egress_off_by_default_even_with_an_allow_list() -> None:
    profile = build_seatbelt_profile(allowed_hosts=["127.0.0.1:11434"])
    assert "mDNSResponder" not in profile
    assert '(remote tcp "*:443")' not in profile


def test_web_egress_adds_the_resolver_socket_and_http_ports() -> None:
    # web_fetch/web_search reach hosts the model names at runtime: the
    # resolver socket (no IP reach) plus the two http(s) ports are all a
    # profile can grant ahead of the call. Without the resolver the SSRF
    # pre-check fails as "cannot resolve <host>".
    profile = build_seatbelt_profile(
        allowed_hosts=["127.0.0.1:11434"], web_egress=True
    )
    assert '(literal "/private/var/run/mDNSResponder")' in profile
    assert '(remote tcp "*:443")' in profile
    assert '(remote tcp "*:80")' in profile
    # Still fail-closed for everything else.
    assert "\n(allow network-outbound)\n" not in profile


def test_web_egress_is_a_noop_when_outbound_is_already_open() -> None:
    open_profile = build_seatbelt_profile(allowed_hosts=None, web_egress=True)
    assert open_profile == build_seatbelt_profile(allowed_hosts=None)
    no_network = build_seatbelt_profile(network=False, web_egress=True)
    assert "network-outbound" not in no_network


def test_allow_list_rejects_invalid_entries() -> None:
    with pytest.raises(ValueError, match="invalid allow-list entry"):
        build_seatbelt_profile(allowed_hosts=['evil.com";(allow network-outbound)'])
    with pytest.raises(ValueError, match="invalid allow-list entry"):
        build_seatbelt_profile(allowed_hosts=["host:99999"])
    with pytest.raises(ValueError, match="invalid allow-list entry"):
        build_seatbelt_profile(allowed_hosts=["not a host"])


def test_cli_allow_host_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "sys.argv",
            ["sandbox", "profile", "--allow-host", "localhost:11434"],
        )
        assert main() == 0
    out = capsys.readouterr().out
    assert '(remote tcp "localhost:11434")' in out
    assert "\n(allow network-outbound)\n" not in out


def test_cli_allow_web_egress_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "sys.argv",
            [
                "sandbox",
                "profile",
                "--allow-host",
                "localhost:11434",
                "--allow-web-egress",
            ],
        )
        assert main() == 0
    out = capsys.readouterr().out
    assert '(literal "/private/var/run/mDNSResponder")' in out
    assert '(remote tcp "*:443")' in out


@pytest.fixture
def tcp_server_port() -> Iterator[int]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def accept_loop() -> None:
        try:
            while True:
                conn, _ = srv.accept()
                conn.close()
        except OSError:
            pass  # socket closed at teardown

    threading.Thread(target=accept_loop, daemon=True).start()
    yield port
    srv.close()


def _probe_connect(port: int) -> str:
    return (
        "import socket;"
        f"s=socket.create_connection(('127.0.0.1',{port}),timeout=3);"
        "s.close()"
    )


@pytest.mark.skipif(not seatbelt_available(), reason="macOS sandbox-exec only")
def test_allow_list_actually_enforced_by_seatbelt(tcp_server_port: int) -> None:
    """Real sandbox-exec smoke: the declared localhost port connects, an
    undeclared port on the same host is denied by the kernel."""

    profile = build_seatbelt_profile(allowed_hosts=[f"localhost:{tcp_server_port}"])

    allowed = subprocess.run(
        seatbelt_argv(profile, [sys.executable, "-c", _probe_connect(tcp_server_port)]),
        capture_output=True,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr.decode()

    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        denied_port = other.getsockname()[1]
        denied = subprocess.run(
            seatbelt_argv(profile, [sys.executable, "-c", _probe_connect(denied_port)]),
            capture_output=True,
            check=False,
        )
    assert denied.returncode != 0
    assert "Operation not permitted" in denied.stderr.decode()


@pytest.mark.skipif(not seatbelt_available(), reason="macOS sandbox-exec only")
def test_web_egress_profile_is_accepted_by_the_kernel() -> None:
    """The widened profile must still load: a rule sbpl rejects would turn
    every sandboxed spawn into the unsandboxed fallback."""

    profile = build_seatbelt_profile(
        allowed_hosts=["localhost:11434"], web_egress=True
    )
    loaded = subprocess.run(
        seatbelt_argv(profile, [sys.executable, "-c", "print('loaded')"]),
        capture_output=True,
        check=False,
    )
    assert loaded.returncode == 0, loaded.stderr.decode()
    assert b"loaded" in loaded.stdout


class TestSeatbeltExecBackend:
    """The per-exec backend (layer 2): command rewriting + enforcement value."""

    def test_wrapped_string_is_shell_parseable(self) -> None:
        backend = SeatbeltExecBackend()
        wrapped = backend.wrap_command("echo 'hello world' && ls /tmp")

        # sandbox-exec with the profile inline, running sh -c <original>.
        # (shlex.quote leaves the safe executable path unquoted.)
        assert wrapped.startswith(f"{MACOS_SEATBELT_EXECUTABLE} -p '")
        # sh -n -c parses without executing: the string must never be a
        # syntax error, on any platform (quoting survives the profile's
        # parens and the command's own quotes).
        parsed = subprocess.run(
            ["/bin/sh", "-n", "-c", wrapped], capture_output=True, check=False
        )
        assert parsed.returncode == 0, parsed.stderr.decode()

    def test_enforcement_full_when_network_denied(self) -> None:
        assert SeatbeltExecBackend().enforcement == "full"
        assert SeatbeltExecBackend(network=False).enforcement == "full"

    def test_enforcement_partial_when_egress_open_or_port_only(self) -> None:
        assert SeatbeltExecBackend(network=True).enforcement == "partial"
        # Non-localhost allow-list entries degrade to port-only enforcement.
        assert (
            SeatbeltExecBackend(network=True, allowed_hosts=["api.example.com:443"]).enforcement
            == "partial"
        )

    def test_enforcement_full_when_egress_pinned_to_localhost(self) -> None:
        assert (
            SeatbeltExecBackend(network=True, allowed_hosts=["localhost:11434"]).enforcement
            == "full"
        )

    @pytest.mark.skipif(not seatbelt_available(), reason="macOS sandbox-exec only")
    def test_wrapped_command_actually_runs_confined(self, tmp_path) -> None:
        """Real sandbox-exec smoke: the wrapped command runs, can write into
        a declared root, and is denied outside it by the kernel."""
        writable = tmp_path / "allowed"
        writable.mkdir()
        backend = SeatbeltExecBackend(writable_roots=[str(writable)])

        ok = subprocess.run(
            backend.wrap_command(f"echo hi > {writable}/f.txt"),
            shell=True, capture_output=True, check=False, executable="/bin/sh",
        )
        assert ok.returncode == 0, ok.stderr.decode()
        assert (writable / "f.txt").read_text().strip() == "hi"

        denied = subprocess.run(
            backend.wrap_command("echo hi > $HOME/should-not-exist-steerable"),
            shell=True, capture_output=True, check=False, executable="/bin/sh",
        )
        assert denied.returncode != 0

    @pytest.mark.skipif(not seatbelt_available(), reason="macOS sandbox-exec only")
    def test_wrapped_command_denies_network_by_default(self) -> None:
        backend = SeatbeltExecBackend()  # network=False
        denied = subprocess.run(
            backend.wrap_command(
                f"{sys.executable} -c \""
                "import socket;s=socket.create_connection(('127.0.0.1',9),timeout=2)\""
            ),
            shell=True, capture_output=True, check=False, executable="/bin/sh",
        )
        assert denied.returncode != 0


class TestBwrapExecBackend:
    """The Linux per-exec backend: command rewriting + enforcement value.

    Construction and rendering are platform-independent; confinement itself
    is verified with real-bwrap smoke tests (skipped without a usable bwrap
    — the probe is the availability signal, not the platform alone).
    """

    def test_wrapped_string_is_shell_parseable(self, tmp_path) -> None:
        backend = BwrapExecBackend(
            writable_roots=[str(tmp_path)], executable="/usr/bin/bwrap"
        )
        wrapped = backend.wrap_command("echo 'hello world' && ls /tmp")

        assert wrapped.startswith("/usr/bin/bwrap ")
        parsed = subprocess.run(
            ["/bin/sh", "-n", "-c", wrapped], capture_output=True, check=False
        )
        assert parsed.returncode == 0, parsed.stderr.decode()

    def test_profile_pins_the_namespace_invariants(self) -> None:
        args = BwrapExecBackend(executable="/usr/bin/bwrap").argv_for("true")

        # dsh's bwrap invariant: a private PID namespace with its own
        # /proc — without it, procfs magic links (/proc/<pid>/root et al.)
        # cross the read-only root bind into host processes' mount views.
        assert "--unshare-pid" in args
        assert "--proc" in args
        assert "--ro-bind" in args
        assert "--die-with-parent" in args
        # Command runs after the separator, under the configured shell.
        assert args[-4:] == ["--", "/bin/sh", "-c", "true"]

    def test_network_denied_by_default(self) -> None:
        args = BwrapExecBackend(executable="/usr/bin/bwrap").argv_for("true")
        assert "--unshare-net" in args

    def test_network_declared_shares_host_network(self) -> None:
        args = BwrapExecBackend(executable="/usr/bin/bwrap", network=True).argv_for("true")
        assert "--unshare-net" not in args

    def test_enforcement_full_when_network_denied(self) -> None:
        assert BwrapExecBackend(executable="/usr/bin/bwrap").enforcement == "full"
        assert (
            BwrapExecBackend(executable="/usr/bin/bwrap", network=False).enforcement
            == "full"
        )

    def test_enforcement_partial_when_egress_open(self) -> None:
        assert (
            BwrapExecBackend(executable="/usr/bin/bwrap", network=True).enforcement
            == "partial"
        )
        # bwrap cannot pin egress per host: a declared allow-list is
        # accepted for interface parity but does NOT raise enforcement.
        assert (
            BwrapExecBackend(
                executable="/usr/bin/bwrap",
                network=True,
                allowed_hosts=["localhost:11434"],
            ).enforcement
            == "partial"
        )

    def test_writable_root_is_bound_read_write(self, tmp_path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        args = BwrapExecBackend(
            writable_roots=[str(root)], executable="/usr/bin/bwrap"
        ).argv_for("true")
        bind_at = args.index("--bind")
        assert args[bind_at + 1] == str(root)
        assert args[bind_at + 2] == str(root)
        # Scratch is a private tmpfs, not the host's /tmp.
        tmpfs_at = args.index("--tmpfs")
        assert args[tmpfs_at + 1] == "/tmp"

    def test_writable_root_must_exist(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            BwrapExecBackend(
                writable_roots=[str(tmp_path / "missing")], executable="/usr/bin/bwrap"
            )

    def test_symlinked_root_is_resolved(self, tmp_path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        backend = BwrapExecBackend(
            writable_roots=[str(link)], executable="/usr/bin/bwrap"
        )
        args = backend.argv_for("true")
        assert args[args.index("--bind") + 1] == str(real)

    @pytest.mark.skipif(not bwrap_available(), reason="Linux bwrap only")
    def test_wrapped_command_actually_runs_confined(self, tmp_path) -> None:
        """Real bwrap smoke: the wrapped command runs, can write into a
        declared root and the private /tmp, and is denied outside them."""
        writable = tmp_path / "allowed"
        writable.mkdir()
        backend = BwrapExecBackend(writable_roots=[str(writable)])

        ok = subprocess.run(
            backend.wrap_command(f"echo hi > {writable}/f.txt && echo scratch > /tmp/s.txt"),
            shell=True, capture_output=True, check=False, executable="/bin/sh",
        )
        assert ok.returncode == 0, ok.stderr.decode()
        assert (writable / "f.txt").read_text().strip() == "hi"

        denied = subprocess.run(
            backend.wrap_command("echo hi > $HOME/should-not-exist-steerable"),
            shell=True, capture_output=True, check=False, executable="/bin/sh",
        )
        assert denied.returncode != 0

    @pytest.mark.skipif(not bwrap_available(), reason="Linux bwrap only")
    def test_wrapped_command_denies_network_by_default(self) -> None:
        backend = BwrapExecBackend()  # network=False
        denied = subprocess.run(
            backend.wrap_command(
                f"{sys.executable} -c \""
                "import socket;s=socket.create_connection(('127.0.0.1',9),timeout=2)\""
            ),
            shell=True, capture_output=True, check=False, executable="/bin/sh",
        )
        assert denied.returncode != 0

    @pytest.mark.skipif(not bwrap_available(), reason="Linux bwrap only")
    def test_procfs_magic_links_do_not_escape(self, tmp_path) -> None:
        """The dsh procfs invariant: with a private PID namespace, the
        command's /proc/1 is bwrap's own init — following /proc/1/root must
        NOT land on the host's mount view."""
        marker = tmp_path / "host-only-marker"
        marker.write_text("host")
        backend = BwrapExecBackend()
        # /proc/1/root is the container's own root (same ro-bind view), so
        # the host-only path is simply absent — but crucially the lookup
        # cannot reach the HOST's /proc/1 (init/systemd) whose root would
        # expose host mounts beyond the profile.
        seen = subprocess.run(
            backend.wrap_command("cat /proc/1/comm && ls /proc/1/root"),
            shell=True, capture_output=True, check=False, executable="/bin/sh",
        )
        assert seen.returncode == 0, seen.stderr.decode()
        assert seen.stdout.decode().splitlines()[0] != "systemd"

    @pytest.mark.skipif(not bwrap_available(), reason="Linux bwrap only")
    def test_private_pid_namespace_hides_host_processes(self) -> None:
        backend = BwrapExecBackend()
        seen = subprocess.run(
            backend.wrap_command("ls /proc | grep -c '^[0-9]'"),
            shell=True, capture_output=True, check=False, executable="/bin/sh",
        )
        assert seen.returncode == 0, seen.stderr.decode()
        # A private namespace holds only bwrap's init + the command's own
        # descendants — a handful of entries, not the host's process table.
        assert int(seen.stdout.decode().strip()) < 20


class TestSelectExecBackend:
    """The platform ladder: Seatbelt on macOS, bwrap on Linux, none else."""

    def test_macos_picks_seatbelt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.seatbelt_available", lambda: True
        )
        backend = select_exec_backend()
        assert isinstance(backend, SeatbeltExecBackend)

    def test_linux_picks_bwrap_when_probe_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.seatbelt_available", lambda: False
        )
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.bwrap_path", lambda: "/usr/bin/bwrap"
        )
        backend = select_exec_backend()
        assert isinstance(backend, BwrapExecBackend)
        assert backend.name == "bwrap"

    def test_no_backend_when_neither_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.seatbelt_available", lambda: False
        )
        monkeypatch.setattr("steerable_sidecar.sandbox.bwrap_path", lambda: None)
        # The Linux ladder's last rung must also fail before we report none.
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.landlock_available", lambda: False
        )
        assert select_exec_backend() is None

    def test_bwrap_path_rejects_non_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.platform.system", lambda: "Windows"
        )
        from steerable_sidecar.sandbox import bwrap_path

        assert bwrap_path() is None

    def test_windows_constructs_no_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Recorded stance (safety.md): Windows has no command-rewriting
        primitive, so every availability gate must say no on its own —
        no special-casing in the selector."""
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.platform.system", lambda: "Windows"
        )
        assert seatbelt_available() is False
        assert bwrap_available() is False
        assert landlock_available() is False
        assert select_exec_backend() is None

    def test_bwrap_probe_failure_means_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A bwrap that cannot actually confine (namespace-refusing kernel
        or container) is rejected by the functional probe — fail closed."""
        import steerable_sidecar.sandbox as sandbox_mod

        monkeypatch.setattr(sandbox_mod.platform, "system", lambda: "Linux")
        fake = tmp_path / "bwrap"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        monkeypatch.setattr(
            sandbox_mod, "BWRAP_CANDIDATE_PATHS", (str(fake),)
        )
        sandbox_mod._probe_bwrap.cache_clear()
        try:
            assert sandbox_mod.bwrap_path() is None
        finally:
            sandbox_mod._probe_bwrap.cache_clear()


class TestLinuxProcessWrap:
    def test_bwrap_wraps_argv_not_shell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.bwrap_path", lambda: "/usr/bin/bwrap"
        )
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.landlock_available", lambda: False
        )
        plan = linux_process_wrap(
            ["/opt/python", "-m", "steerable_sidecar"],
            writable_roots=[],
            network=True,
        )
        assert plan["backend"] == "bwrap"
        assert plan["enforcement"] == "partial"
        argv = plan["argv"]
        assert isinstance(argv, list)
        assert argv[0] == "/usr/bin/bwrap"
        assert argv[-3:] == ["/opt/python", "-m", "steerable_sidecar"]
        assert "-c" not in argv

    def test_landlock_is_the_no_bwrap_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("steerable_sidecar.sandbox.bwrap_path", lambda: None)
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.landlock_available", lambda: True
        )
        plan = linux_process_wrap(["python", "-m", "steerable_sidecar"], network=True)
        assert plan["backend"] == "landlock"
        assert plan["enforcement"] == "partial"
        argv = plan["argv"]
        assert isinstance(argv, list)
        assert "-m" in argv
        assert "steerable_sidecar.landlock_run" in argv
        assert argv[-3:] == ["python", "-m", "steerable_sidecar"]

    def test_no_backend_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("steerable_sidecar.sandbox.bwrap_path", lambda: None)
        monkeypatch.setattr(
            "steerable_sidecar.sandbox.landlock_available", lambda: False
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            linux_process_wrap(["python", "-m", "steerable_sidecar"])

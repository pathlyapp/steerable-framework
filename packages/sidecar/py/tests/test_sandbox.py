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
from steerable_sidecar.sandbox import (
    MACOS_SEATBELT_EXECUTABLE,
    SeatbeltExecBackend,
    build_seatbelt_profile,
    main,
    seatbelt_argv,
    seatbelt_available,
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

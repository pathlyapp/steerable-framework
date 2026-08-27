"""Seatbelt profile generation for the sidecar sandbox.

These tests pin the policy text's load-bearing rules (closed-by-default,
write whitelist, no network-bind) and the argv wrapping. They do not
execute sandbox-exec — that integration is covered by the desktop
dogfood/E2E path, which fails loudly if the profile starves Python.
"""

from __future__ import annotations

import subprocess

import pytest
from steerable_sidecar.sandbox import (
    MACOS_SEATBELT_EXECUTABLE,
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

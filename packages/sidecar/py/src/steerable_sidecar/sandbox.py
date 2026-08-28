"""OS sandbox confinement for the sidecar process (macOS Seatbelt first).

Threat model: the sidecar holds the provider API key and runs the agent
loop, which ingests untrusted content (tool results, skill files, model
output). A compromised or confused loop process must not be able to plant
files outside a small whitelist or escape into arbitrary code execution.
The loop's *tool* calls are not the attack surface here — in the desktop
deployment those execute in the host over the reverse channel, gated by
the host's safety classifier + approval UI (the second layer; this module
is the first, OS-enforced one — the codex two-layer structure).

What the sidecar legitimately needs:

- **Network-outbound**: open by default — provider baseUrl is
  user-configurable (cloud endpoints, LAN, localhost Ollama). Hosts that
  know their provider endpoints can declare an egress allow-list
  (``allowed_hosts``) and the profile fails closed instead: outbound is
  denied except to the declared endpoints. Seatbelt's ``remote`` filter
  only accepts ``*`` or ``localhost`` as the host (verified on macOS 26:
  hostnames and IP literals are rejected at profile compile time), so a
  localhost entry pins ``localhost:PORT`` exactly while any other entry
  degrades to its port (``*:PORT``). True per-hostname egress enforcement
  is inexpressible in sbpl — run a local allow-listing egress proxy and
  declare only ``localhost:<proxy port>`` when that guarantee is required.
  The sidecar never listens — no ``network-bind``.
- **Reads**: open. Skill roots are host-configured paths passed per
  request; the Python runtime and user config must be readable too.
- **Writes**: a whitelist — ``~/.steerable`` (token calibration, atomic
  tmp+rename inside the dir) and the system scratch dirs. Hosts should
  set ``PYTHONDONTWRITEBYTECODE=1`` so the sandbox can deny ``__pycache__``
  writes, and must create ``~/.steerable`` before spawning (creating the
  dir itself needs write on ``$HOME``, which the sandbox denies).
- **Subprocesses**: the sidecar spawns none, but exec/fork stay allowed
  because children inherit the same sandbox — allowing them is not an
  escape hatch (the codex stance), and denying them breaks Python
  internals that fork helpers.

Linux Landlock is a deliberate follow-up; ``seatbelt_available()`` gates
the macOS path so other platforms fall back to unsandboxed today.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import sys
from collections.abc import Sequence

MACOS_SEATBELT_EXECUTABLE = "/usr/bin/sandbox-exec"

# Modelled on codex's seatbelt_base_policy.sbpl (itself inspired by
# Chrome's sandbox policy), trimmed to what a Python LLM-loop process
# needs. Closed-by-default; every rule below is an explicit exception.
_BASE_POLICY = """\
(version 1)
(deny default)

; Child processes inherit this policy, so allowing exec is not an escape.
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))

; Python runtime queries (CPU info, hostname, OS version, page size).
; Broad read: these leak no user data and scoping risks denials that only
; surface under specific provider/model combinations.
(allow sysctl-read)

; User/group lookup (os.getpwuid via libinfo).
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
)

; /dev/null sinks (Python opens it for subprocess redirection etc.).
(allow file-write-data
  (require-all
    (path "/dev/null")
    (vnode-type CHARACTER-DEVICE)))

; Reads stay open: skill roots are host-configured per request and the
; loop legitimately reads user-chosen directories. Confinement targets
; writes and execution, not reads.
(allow file-read*)

; System scratch dirs.
(allow file-read* file-test-existence file-write* (subpath "/tmp"))
(allow file-read* file-write* (subpath "/private/tmp"))
(allow file-read* file-write* (subpath "/var/tmp"))
(allow file-read* file-write* (subpath "/private/var/tmp"))
"""

# From codex's seatbelt_network_policy.sbpl: the platform services
# HTTPS/DNS clients actually consult (OpenSSL still needs configd for
# resolver state; trustd/ocspd cover TLS validation paths some Python
# builds hit through the system APIs). These are local mach services, not
# egress — they stay allowed even when outbound is allow-listed, or DNS
# resolution of the *allowed* hosts would break.
_NETWORK_SERVICES = """\
(allow system-socket
  (require-all
    (socket-domain AF_SYSTEM)
    (socket-protocol 2)
  )
)

(allow mach-lookup
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.networkd")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd")
)

(allow sysctl-read
  (sysctl-name-regex #"^net.routetable")
)
"""

_NETWORK_POLICY = "(allow network-outbound)\n" + _NETWORK_SERVICES

#: Entries naming a local endpoint get an exact ``localhost:PORT`` rule —
#: the only host Seatbelt's ``remote`` filter can pin besides ``*``.
_LOCALHOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

#: Bare hosts (no port) allow the two plausible LLM endpoint schemes.
_DEFAULT_EGRESS_PORTS = (443, 80)

#: ``host`` or ``host:port``. Deliberately stricter than DNS: the entry is
#: interpolated into sbpl, so anything outside this alphabet (quotes,
#: whitespace, parens) is a profile-injection attempt, not a hostname.
_HOST_ENTRY_RE = re.compile(r"^(?P<host>[A-Za-z0-9._-]+)(?::(?P<port>[0-9]+))?$")


def _parse_host_entry(entry: str) -> tuple[str, tuple[int, ...]]:
    match = _HOST_ENTRY_RE.match(entry.strip())
    if match is None:
        raise ValueError(
            f"invalid allow-list entry {entry!r}: expected host or host:port "
            "(letters, digits, dot, dash, underscore)"
        )
    host = match.group("host")
    if match.group("port") is None:
        return host, _DEFAULT_EGRESS_PORTS
    port = int(match.group("port"))
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid allow-list entry {entry!r}: port out of range")
    return host, (port,)


def _egress_policy(allowed_hosts: Sequence[str]) -> str:
    """Fail-closed network rules: deny outbound except declared endpoints.

    Seatbelt cannot match hostnames (only ``*``/``localhost``), so remote
    entries degrade to their port — declared in the profile comment so the
    generated policy documents its own limitation.
    """

    rules: list[str] = []
    seen: set[str] = set()
    degraded = False
    for entry in allowed_hosts:
        host, ports = _parse_host_entry(entry)
        for port in ports:
            if host in _LOCALHOST_NAMES:
                rule = f'(allow network-outbound (remote tcp "localhost:{port}"))'
            else:
                degraded = True
                rule = f'(allow network-outbound (remote tcp "*:{port}"))'
            if rule not in seen:
                seen.add(rule)
                rules.append(rule)
    header = "; egress allow-list (fail-closed): outbound denied except the endpoints below."
    if degraded:
        header += (
            "\n; NOTE: sbpl cannot match hostnames — non-localhost entries are"
            "\n; enforced by port only. For per-host enforcement, proxy egress"
            "\n; through a local allow-listing proxy and declare localhost:<port>."
        )
    return header + "\n" + "\n".join(rules) + "\n\n" + _NETWORK_SERVICES


def seatbelt_available() -> bool:
    """True on macOS when Apple's sandbox-exec is present.

    Only ``/usr/bin/sandbox-exec`` is trusted — a PATH-relative lookup
    could resolve to an attacker-planted binary (codex's rule).
    """

    return platform.system() == "Darwin" and os.path.isfile(MACOS_SEATBELT_EXECUTABLE)


def _sbpl_string(path: str) -> str:
    """Quote a filesystem path as an sbpl string literal."""

    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_seatbelt_profile(
    *,
    writable_roots: list[str] | None = None,
    network: bool = True,
    allowed_hosts: Sequence[str] | None = None,
) -> str:
    """Render a complete Seatbelt profile for the sidecar process.

    ``writable_roots`` are host-absolute directories the sidecar may write
    into (e.g. ``~/.steerable`` for token calibration). They must exist
    before spawn — creating them needs write access on their parent, which
    the profile deliberately does not grant. Paths are normalized
    (``~`` expansion, absolutized) and symlink-free; a symlinked component
    would make the subpath rule match nothing real.

    ``allowed_hosts`` is the egress allow-list (entries ``host`` or
    ``host:port``; bare hosts allow ports 443 and 80). ``None`` keeps
    outbound fully open — the default, so existing hosts are unaffected.
    A list (even empty) fails closed: outbound is denied except to the
    declared endpoints. Invalid entries raise ``ValueError`` at generation
    time, never a malformed profile.
    """

    parts = [_BASE_POLICY]
    for root in writable_roots or []:
        normalized = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
        parts.append(
            f"; host-declared writable root\n"
            f"(allow file-read* file-write* (subpath {_sbpl_string(normalized)}))\n"
        )
    if network:
        parts.append(
            _NETWORK_POLICY if allowed_hosts is None else _egress_policy(allowed_hosts)
        )
    return "\n".join(parts)


def seatbelt_argv(profile: str, argv: list[str]) -> list[str]:
    """Wrap ``argv`` in sandbox-exec with the profile passed inline.

    ``-p`` keeps the profile in argv (a few KB — far under the macOS
    limit), so no profile temp file is left behind.
    """

    return [MACOS_SEATBELT_EXECUTABLE, "-p", profile, *argv]


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="steerable-sidecar-sandbox",
        description="Sandbox profile tooling for the steerable sidecar.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    profile_cmd = sub.add_parser(
        "profile", help="Print a Seatbelt profile on stdout."
    )
    profile_cmd.add_argument(
        "--writable-root",
        action="append",
        default=[],
        metavar="PATH",
        help="Directory the sidecar may write into (repeatable).",
    )
    profile_cmd.add_argument(
        "--no-network",
        action="store_true",
        help="Deny network access (for embedders that proxy LLM traffic).",
    )
    profile_cmd.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help=(
            "Egress allow-list entry (repeatable). Once any entry is given, "
            "outbound is denied except to the declared endpoints; bare hosts "
            "allow ports 443 and 80. Omit entirely to keep outbound open."
        ),
    )
    args = parser.parse_args()

    if args.command == "profile":
        sys.stdout.write(
            build_seatbelt_profile(
                writable_roots=list(args.writable_root),
                network=not args.no_network,
                allowed_hosts=list(args.allow_host) or None,
            )
        )
        return 0
    raise AssertionError(f"unreachable command {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

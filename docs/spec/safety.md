# Safety Spec

Steerable ships a small but opinionated safety classifier for shell
commands. The schema is `CommandSafetyPattern`; the runtime helper
`classify_shell_command(cmd)` returns a severity grade plus the
matching rule IDs.

## CommandSafetyPattern

| Field         | Type                                | Required | Notes                                  |
| ------------- | ----------------------------------- | -------- | -------------------------------------- |
| `id`          | `string`                            | yes      | Stable rule identifier                 |
| `label`       | `string`                            | yes      | Short human-readable name              |
| `description` | `string`                            | yes      | Why the rule exists                    |
| `pattern`     | `string` (regex)                    | yes      | Matched against the full command line  |
| `category`    | `string`                            | yes      | Free-form taxonomy bucket              |
| `severity`    | `'critical' \| 'warning'`           | yes      | Determines UI behavior                 |
| `platform`    | `'all' \| 'unix' \| 'windows'`      | yes      | Only evaluated on matching platforms   |

`additionalProperties` is **disabled** to keep rule processing
deterministic across runtimes.

## Severity grades

| Severity   | UI treatment                                              |
| ---------- | --------------------------------------------------------- |
| `critical` | Block by default. Surface a confirmation dialog if user-initiated. |
| `warning`  | Show a banner but allow the command. Log every match.    |
| `safe`     | Auto-allow. (Returned by the classifier when **no** rule matches.) |

`safe` is not a rule, it's the absence of a match.

## Built-in rules (excerpt)

| Category         | Examples                                                               |
| ---------------- | ---------------------------------------------------------------------- |
| Risky FS ops     | `rm -rf /`, `rm -rf ~`, `rm -rf $HOME`                                 |
| Privilege esc.   | `sudo`, `su -`, `runas`                                                |
| Remote pipe-exec | `curl … \| sh`, `wget … \| bash`                                       |
| Dangerous git    | `git push --force`, `git reset --hard`, `git clean -fd`                |
| Disk destruction | `mkfs`, `dd if=… of=/dev/`                                             |
| Network probes   | `nmap`, `tcpdump` (warning, not critical)                              |

The full rule set lives in
`packages/agent-runtime/py/src/steerable_agent_runtime/safety/builtins.py`
(canonical) and `packages/agent-harness/ts/src/safety-patterns.ts`
(TS facade for parity tests).

## Adding custom rules

```python
from steerable_agent_protocol import CommandSafetyPattern
from steerable_agent_runtime.safety import classify_shell_command, register_rule

register_rule(CommandSafetyPattern(
    id="my-org-no-prod-db",
    label="No direct prod DB",
    description="Block any psql/mysql against the prod hostname.",
    pattern=r"(psql|mysql).*--host.*prod-db",
    category="data",
    severity="critical",
    platform="all",
))

result = classify_shell_command("psql --host prod-db -U admin")
# → {"severity": "critical", "matchedRules": ["my-org-no-prod-db"]}
```

## Why a regex layer instead of a parser

Shell parsing is ambiguous (interactive shells, here-docs, eval, command
substitution, …). A regex layer gives you a fast, conservative
classifier that's easy to audit. **It is not a sandbox** — pair it with
a real consent UI for `local`-mode tools (see
[Tools spec](tools.md#toolmode-harness-classifier)).

## OS sandbox: the sidecar process (layer 1)

The classifier above is **layer 2** — an in-loop hint that gates tool
dispatch. Layer 1 is OS-enforced confinement of the sidecar process
itself, the codex two-layer structure: even if the loop process is
confused or compromised (it holds the provider API key and ingests
untrusted tool output), the kernel keeps it inside a write whitelist.

macOS uses Seatbelt (`/usr/bin/sandbox-exec`); the sidecar package owns
the policy end to end:

```bash
python -m steerable_sidecar.sandbox profile \
    --writable-root ~/.steerable > sidecar.sb
/usr/bin/sandbox-exec -p "$(cat sidecar.sb)" python -m steerable_sidecar
```

The profile is deny-by-default with exactly these exceptions:

| Resource | Policy | Why |
| -------- | ------ | --- |
| Reads | open | Skill roots are host-configured per request; confinement targets writes, not reads. |
| Network-outbound | open by default; allow-listable | Provider `baseUrl` is user-configured (cloud, LAN, localhost Ollama). Hosts that know their endpoints can fail closed — see below. No `network-bind` — the sidecar never listens. |
| Writes | whitelist | `~/.steerable` (token calibration, atomic tmp+rename) + system scratch dirs. Nothing else. |
| exec/fork | allowed | Children inherit the same sandbox, so this is not an escape hatch; denying it breaks Python internals. |

### Egress allow-list

Open egress means the process holding the provider API key also ingests
untrusted tool output and can send anywhere — the lethal trifecta. Hosts
that know their provider endpoints can declare them and the profile fails
closed instead:

```bash
python -m steerable_sidecar.sandbox profile \
    --writable-root ~/.steerable \
    --allow-host api.deepseek.com \
    --allow-host localhost:11434 > sidecar.sb
```

Semantics (`build_seatbelt_profile(allowed_hosts=...)`):

- **Unconfigured (`None`) → open.** The default is unchanged; existing
  hosts are unaffected.
- **Configured (any list, even empty) → fail-closed.** Outbound is denied
  except to the declared endpoints. Entries are `host` or `host:port`;
  bare hosts allow ports 443 and 80. Invalid entries raise `ValueError`
  at generation time — a malformed entry never produces a malformed
  profile. DNS/TLS platform services stay allowed (they are local mach
  services, not egress), or resolving the allowed hosts would break.
- **Seatbelt cannot match hostnames.** The `remote` filter accepts only
  `*` or `localhost` (verified on macOS 26: hostnames and IP literals are
  rejected at profile compile time). A localhost entry therefore pins
  `localhost:PORT` exactly; any other entry degrades to its port
  (`*:PORT`), and the generated profile says so in a comment. This still
  breaks the common exfiltration channels — reverse shells, beacons, and
  DNS tunnelling live on non-443 ports — but it does **not** stop
  exfiltration to an attacker HTTPS endpoint on 443. For true per-host
  enforcement, run the shipped allow-listing egress proxy
  (`steerable-egress-proxy`, `packages/egress-proxy/py`) and declare only
  `localhost:<proxy port>`; Seatbelt then pins the sidecar to the proxy
  and the proxy owns the host list. The proxy serves `CONNECT` only (no
  TLS interception, no plain-HTTP forwarding in v1), fails closed on an
  empty allow-list, and its bare-host entries allow 443/80 to mirror the
  profile semantics above. Point the sidecar's HTTP stack at it with
  `HTTPS_PROXY=http://127.0.0.1:<port>` (httpx honors proxy env vars), and set
  `STEERABLE_EGRESS_CONFINED=1` beside it. The marker declares that the proxy
  owns every host: without it, proxy variables read as ambient host
  configuration, and a provider endpoint the OS lists as direct (loopback, and
  on macOS the private LAN ranges) is exempted from them so a local model stays
  reachable behind a system proxy — see `llm/system_proxy.py`. Set the marker
  only when the proxy really is running; the desktop omits it on its
  startup-failure fallback so the sidecar never believes it is confined when it
  is not.

The desktop supervisor passes the list through `SidecarStartOptions.sandboxAllowedHosts` (env fallback `STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS`, comma-separated).

Hosts integrating the sandbox must:

1. **Create `~/.steerable` before spawn** — creating the directory needs
   write on `$HOME`, which the profile denies.
2. **Set `PYTHONDONTWRITEBYTECODE=1`** — the profile denies `__pycache__`
   writes; skipping bytecode keeps boot clean.
3. If confinement cannot be applied, **refuse spawn** — do not fall back
   to an unsandboxed sidecar. macOS uses Seatbelt; Linux uses bwrap then
   Landlock wrapping the whole `python -m steerable_sidecar` process
   (`python -m steerable_sidecar.sandbox linux-wrap`); Windows uses
   `win-spawn-helper --passthrough`. The only unconfined path is an
   explicit opt-out (`STEERABLE_SIDECAR_SANDBOX=0` / `sandbox: false`).
   Harbor / headless eval containers are themselves the boundary and
   must not inherit this refuse-to-start rule.

The reference integration is the desktop supervisor
(`deeppath-agent/src/sidecar/supervisor.ts`, `STEERABLE_SIDECAR_SANDBOX=1`).

## Tool-execution sandbox (layer 3, per-exec)

Wave 3 added the opt-in third layer: `SandboxedToolExecutor`
(`agent-runtime/sandboxed.py`), a `ToolExecutor` decorator that rewrites a
shell/subprocess call's `command` into a sandboxed invocation before
dispatch. Because the rewrite happens before the reverse channel, the
desktop host's shell spawns the confined command without learning any
sandbox mechanics — per-exec Seatbelt in the Electron deployment. The
backend is pluggable (`SandboxBackend`); the reference
`SeatbeltExecBackend` reuses the layer-1 profile generator with
tool-execution defaults (deny-by-default, no network unless declared,
writes confined to declared roots plus system scratch).

Enforcement is reported as a **value**, not a log line: every sandboxed
result carries `data["_sandbox"] = {backend, enforcement}` with
`enforcement: full | partial | none` (full = OS-enforced deny-by-default;
partial = documented gap such as open or port-only egress; none = no
backend on this platform). Deployments that require an absolute boundary
set `requireFull` and the call is denied (`sandbox_unavailable`) before
execution instead of passing through unconfined. The sidecar wires it as
`execSandbox: {enabled, writableRoots, network, allowedHosts, shell,
tools, commandArg, requireFull}` on `chat.stream`; absent means unconfined
(legacy behavior).

**Backend ladder** (`select_exec_backend`, fail-closed): macOS → Seatbelt;
Linux → bubblewrap (`BwrapExecBackend`), then Landlock
(`LandlockExecBackend`); anything else → no backend
(`enforcement: "none"`). The bwrap profile is the dsh-proven minimal set:
read-only host-root bind, private PID namespace with its own `/proc`
(without it, procfs magic links such as `/proc/<pid>/root` cross the
read-only bind into host processes' mount views), private `/tmp` tmpfs,
`--die-with-parent`, and `--unshare-net` unless the call declares egress.

Landlock (`landlock.py`) is the kernel-LSM rung for hosts where bwrap
cannot run — it needs no external binary and no user namespaces, so it
works inside containers whose runtimes refuse namespace creation. Because
Landlock is not a command wrapper, the backend rewrites the command
through our own launcher (`python -m steerable_sidecar.landlock_run`),
which installs the ruleset on itself and `execvp`s the target; children
inherit the restriction. Coverage differences from bwrap, all surfaced
through the enforcement value:

| Dimension | bwrap | Landlock |
| --------- | ----- | -------- |
| Filesystem writes | read-only root bind + declared roots | read-only rule on `/` + declared roots |
| `/tmp` | private tmpfs | host's shared `/tmp` (no mount namespaces) |
| Process view | private PID namespace, own `/proc` | host's process table visible (reads stay open) |
| Egress `network: false` | network namespace removed → `full` | TCP bind/connect denied on ABI v4+ (kernel 6.7) → `full`; below v4 egress is inexpressible → `partial` and `requireFull` refuses |
| Per-host egress | not enforceable (`partial`) | not enforceable (`partial`) |
| Dependencies | bwrap binary + namespace privileges | kernel 5.13+ only |

Two deliberate semantics differences from Seatbelt, both surfaced through
the enforcement value rather than hidden:

- bwrap's only egress control is the network namespace, so `network:
  false` → `full`, `network: true` → `partial`. A declared `allowedHosts`
  is accepted for interface parity but **not enforced** under bwrap (no
  per-host pinning exists to degrade to) — hosts needing per-host egress
  run `steerable-egress-proxy`, the same remedy as Seatbelt's port-only
  note above.
- Writable roots must exist when the backend is constructed (bwrap fails
  the whole wrapped command on a missing bind source), so a nonexistent
  root raises at construction instead of failing every tool call.

Availability is a **functional probe**, not a version or platform check:
`bwrap_path()` runs a real maximal wrap (network namespace included) and
caches the verdict; `landlock_available()` runs the launcher wrapping a
no-op and caches the verdict. This matters in practice — Docker Desktop's
VM denies `pivot_root` even with `CAP_SYS_ADMIN` and only passes under
`--privileged`, and Docker's default seccomp profile errno-rejects the
landlock syscalls; version checks would misjudge all of these. A host
that cannot confine gets `enforcement: "none"` (and `requireFull`
refusals), never a weaker wrap.

### Windows: no backend (recorded decision, 2026-08-30)

Windows constructs no backend, and this is a deliberate scope decision,
not an oversight. The platform's confinement primitive — restricted
token + job object, cf. dsh's `sandbox-windows-acl` — is host-side spawn
support: the token must be created and applied *by the process that
spawns the child* (`CreateProcessWithTokenW` & friends). It cannot be
expressed as a command-line rewrite, so it does not fit this layer's
rewriter architecture, and no wrapper-string backend can fake it.

What real support requires (a future workstream, in order): a native
spawn helper shipped per Windows arch; a reverse-channel protocol
extension so the host (which legitimately holds that capability) performs
the confined spawn on the loop's behalf; Windows CI coverage for the
confinement matrix. Until that lands, Windows reports
`enforcement: "none"` on every call and `requireFull` refuses — the
honest-degradation contract holds there exactly as elsewhere.

## Host capability surface (W2.2, contract 2026-08-30)

Two capabilities live on the host side because only the host can hold
them: **confined spawn** (the spawning process must own the restricted
token) and **credential brokerage** (the secret must never enter the
sandboxed process). This section is their shared contract.

### `host.process.spawn` (reverse channel)

The sidecar delegates a shell call to the host when the platform has no
command-rewriting backend *and* the request opted in via
`execSandbox.hostSpawn: true`. Routing: `select_exec_backend` returns
`None` → `HostSpawnExecutor` replaces `SandboxedToolExecutor`.

Request params:

```json
{
  "command": "echo hi",
  "cwd": "C:\\work",
  "policy": { "writableRoots": ["C:\\work"], "network": true, "allowedHosts": ["api.deepseek.com"] },
  "context": { "chatId": "…" }
}
```

Reply:

```json
{
  "exitCode": 0,
  "stdout": "…", "stderr": "…", "truncated": false,
  "sandbox": { "backend": "windows-restricted-token", "enforcement": "full" }
}
```

Rules:

- The host reports the enforcement it **actually** applied in
  `sandbox.enforcement` (`full` / `partial` / `none`). A missing report is
  surfaced as `none` — the sidecar never upgrades on the host's behalf.
- A host without the capability rejects the reverse call; the sidecar
  fails closed: the command never runs unsandboxed just because the
  capability is absent (tool error, `needsFollowup`).
- The policy mirrors `execSandbox` semantics: `writableRoots` are the only
  writable paths, `network: false` means no egress, `allowedHosts` bounds
  egress when network is on. Hosts that cannot honor a policy field must
  report `partial`/`none`, not silently ignore the field.
- TS hosts implement it via `AgentRuntime.onProcessSpawn`; the Electron
  desktop wires its own reverse handler to the same method name.

#### Reference implementation: `win-spawn-helper` (deeppath-agent, 2026-08-31)

The desktop host implements the contract with a Rust helper binary
(`native/windows-spawn-helper/`, packaged as an extraResource), following
codex's windows-sandbox-rs legacy path:

- **Token**: `CreateRestrictedToken` with `DISABLE_MAX_PRIVILEGE | LUA_TOKEN
  | WRITE_RESTRICTED`; restricting SIDs are the per-root capability SIDs,
  the logon SID, and Everyone. The token user is deliberately *not* a
  restricting SID, so user-owned directories deny writes; a root accepts
  writes only after a persistent `SET_ACCESS` ACE grants its capability SID
  read+write+execute+delete (descendant `DELETE`, never parent
  `FILE_DELETE_CHILD`). Capability SIDs derive deterministically from the
  normalized root path, so repeat runs reuse the existing ACE.
- **Job**: `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; the child joins atomically
  at creation via `PROC_THREAD_ATTRIBUTE_JOB_LIST` (no CREATE_SUSPENDED
  window). Helper death — crash, kill, host gone — closes the last job
  handle and tears down the whole tree.
- **Stdio**: anonymous pipes, framed back to the host as JSON lines with a
  256 KiB per-stream cap (`truncated` flag); `lpDesktop` is pinned to
  `Winsta0\Default` so restricted-token children don't die in DLL init.
- **Honesty**: the helper enforces filesystem writes only. `network` /
  `allowedHosts` policy fields come back in the exit frame's
  `sandbox.notEnforced`, and the host reports `partial` when that list is
  non-empty. Residual escape surface: Everyone-writable paths stay writable
  (Everyone is a restricting SID) — the same posture as codex's legacy
  backend; closing it needs the elevated/WFP machinery, which is out of
  scope for this helper.
- **Verification**: `cargo test` on the windows-2022 GHA runner
  (`.github/workflows/test-windows-spawn.yml`) proves the acceptance
  claims — write outside the writable roots is denied, `{"kill":true}`
  tree-kills the child, unenforced policy fields are reported.

### Credential broker (egress-proxy inject mode)

`steerable_egress_proxy --inject-host api.deepseek.com --inject-secret-env
STEERABLE_EGRESS_SECRET` turns the proxy into the credential holder: the
sandboxed sidecar points its provider `baseUrl` at the *http* scheme of
the same host and sends requests through the proxy, which terminates the
plain-HTTP request, dials the real upstream over TLS, and injects the
credential header. The secret enters the proxy process via env var (never
argv, never the sidecar, never the sandboxed child) — the agent side holds
no real token, the codex network-proxy route.

Fail-closed rules: no inject rule → non-CONNECT methods stay 405;
absolute-URI host ≠ rule host → 403; client-supplied credential headers
are stripped, never forwarded; chunked request bodies → 501. One rule per
proxy (one provider per sidecar is the deployment reality). CONNECT
tunneling is unaffected and still governed by the allow-list.

### Network-read tools under the proxy (W5-2)

`web_search` / `web_fetch` (spec: `tools.md` "Web tools") execute inside
the sidecar process, so both egress layers apply to them as to the LLM
client. Two honest consequences:

- **Port-level enforcement (default):** remote allow-list entries degrade
  to `*:443`, so `web_fetch` over HTTPS is reachable under layer 1 — the
  tool's own SSRF policy (per-hop DNS validation against
  `ipaddress.is_global`, v4-mapped/NAT64 unwrapping, same-origin redirect
  cap) is the real boundary, not the sandbox.
- **Per-host proxy (`STEERABLE_EGRESS_PROXY=1`):** outbound is confined to
  a proxy that tunnels only the provider endpoint, so an arbitrary
  `web_fetch` cannot succeed. The desktop sets
  `STEERABLE_EGRESS_CONFINED=1` in the sidecar env on exactly the
  proxy-started path; both tools then fail loud with an actionable error
  naming the remedies instead of hanging behind the proxy's 403/405. The
  marker is deliberately distinct from the opt-in var so a proxy startup
  failure (which falls back to port-level enforcement) never leaves the
  sidecar believing it is confined when it is not.

## Current product posture (2026-08-29, Wave 4 wired)

All three layers are **on by default** in the DeepPath desktop build:

- **Layer 1 (sidecar process sandbox)** spawns under Seatbelt (macOS),
  bwrap then Landlock (Linux), or `win-spawn-helper --passthrough`
  (Windows) unless `STEERABLE_SIDECAR_SANDBOX=0`; the egress allow-list
  is derived per boot
  from the provider `baseUrl` **plus ambient proxy endpoints** (proxy env
  vars and, on macOS, the System Configuration proxy via `scutil`) —
  the sidecar's httpx stack honors ambient proxies, so a configured proxy
  is an effective egress point and must be allow-listed or every LLM call
  is denied for proxy users (found by dogfooding, 2026-08-29). Provider
  endpoints that the platform's own bypass list declares direct do not
  route through an ambient proxy at all (`llm/system_proxy.py` restores
  the macOS ExceptionsList that `urllib.getproxies()` drops, without
  which a local Ollama's traffic reaches the system proxy and comes back
  as its HTTP 502 rather than the model's reply). The allow-list still
  unions both kinds of endpoint. Explicit
  override: `STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS`. The hostname
  limitation documented above applies: remote entries degrade to port-only
  enforcement, which still breaks reverse shells / beacons / DNS
  tunnelling but not exfiltration to an attacker HTTPS endpoint on 443.
  If confinement cannot be applied, the desktop **refuses to start** the
  sidecar rather than running the key-holding process unsandboxed. The
  desktop records the spawn outcome as a
  structured posture (`backend`/`enforcement`/`reason`, mirroring the
  layer-3 `partial | none` vocabulary) and the settings page's security
  section renders it as a durable row: an explicit `sandbox: false` /
  `STEERABLE_SIDECAR_SANDBOX=0` opt-out is shown neutrally, while a
  refused start warns that the sidecar was not launched (copy: 无法收容、已拒绝启动)
  rather than that it is running unconfined.
- **Layer 3 (per-exec sandbox)** is sent on every chat turn as
  `execSandbox: {enabled, writableRoots: [project root], network: true,
  allowedHosts: [provider endpoint], requireFull: false,
  requireBackend: true}`. The backend is
  picked per platform: Seatbelt on macOS, bwrap → Landlock on Linux (both
  probe-gated), Windows `hostSpawn` via `win-spawn-helper`. `requireBackend`
  refuses `enforcement: "none"` so a command never runs unsandboxed when
  confinement was requested. `requireFull` stays false: the desktop's
  `network: true` makes every current backend honest-`partial` (open egress
  on bwrap/Landlock, port-only on Seatbelt, network unenforced on Windows),
  and `requireFull` would refuse shell even on macOS. `STEERABLE_EXEC_SANDBOX=0`
  restores unconfined execution.
- **Approval algebra** runs in host mode on every turn: the sidecar's
  `ApprovalExecutor` asks the Electron approval modal over the reverse
  channel (`approval.request`), the user picks among the seven variants
  (allow/deny × once/session/always + abort), and durable decisions
  persist to `~/.steerable/approvals.json`. Unanswered prompts fail
  closed as `timed_out` after 120s. `STEERABLE_APPROVAL=0` restores the
  legacy ungated behavior.
- **Approval policy rules** (W2.4, codex execpolicy's counterpart):
  `approval.policyPath` loads an ordered rule list — `{tool, decision,
  commandPrefix}` — consulted after the lattice's own caches and before
  the interactive prompt; first match decides with no host round-trip.
  The host reply may carry an **amendment**
  (`{"kind": ..., "amendment": {"decision": "allow"|"deny",
  "commandPrefix": [...]}}`) — "allow, and keep allowing commands like
  this": the sidecar persists it as a rule and applies it within the
  same run. A corrupt policy file fails closed (no rules → every call
  falls through to the prompt).
- **Network-read tools** (`web_search` / `web_fetch`, W5-2) run the same
  approval algebra as every other tool: category = tool name (an
  `allow_always` on `web_fetch` grants exactly that tool, nothing wider),
  mode `read`, and the target URL is the first key the approval modal
  surfaces. Under the per-host egress proxy they fail loud instead of
  hanging — see "Network-read tools under the proxy" above.

One wiring gap found and closed during Wave 4: the CoreLoop path used to
drop `projectRoot` on reverse-channel tool calls, so the project-mode
fence (cwd confinement + file-path jail) did not apply to sidecar-driven
turns. The host now resolves the chat's project binding per `tool.invoke`
and passes it to the `ToolRouter`.

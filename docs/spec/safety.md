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
| Network-outbound | open | Provider `baseUrl` is user-configured (cloud, LAN, localhost Ollama). No `network-bind` — the sidecar never listens. |
| Writes | whitelist | `~/.steerable` (token calibration, atomic tmp+rename) + system scratch dirs. Nothing else. |
| exec/fork | allowed | Children inherit the same sandbox, so this is not an escape hatch; denying it breaks Python internals. |

Hosts integrating the sandbox must:

1. **Create `~/.steerable` before spawn** — creating the directory needs
   write on `$HOME`, which the profile denies.
2. **Set `PYTHONDONTWRITEBYTECODE=1`** — the profile denies `__pycache__`
   writes; skipping bytecode keeps boot clean.
3. Treat sandboxing as hardening, never a correctness dependency: if the
   platform lacks Seatbelt (Linux today — Landlock is a follow-up) or
   profile generation fails, hosts should fall back to an unsandboxed
   spawn with a loud log line, not brick the app.

The reference integration is the desktop supervisor
(`deeppath-agent/src/sidecar/supervisor.ts`, `STEERABLE_SIDECAR_SANDBOX=1`).
What the sandbox deliberately does **not** confine: tool execution. In the
desktop deployment tools run in the host process over the reverse channel,
gated by the layer-2 classifier plus the consent UI.

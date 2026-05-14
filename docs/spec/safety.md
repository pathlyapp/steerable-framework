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

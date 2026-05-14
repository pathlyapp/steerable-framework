# Safety Spec

Safety rules use `CommandSafetyPattern`:

- `id`, `label`, `description`
- `pattern` (regex string)
- `category`
- `severity` (`critical` | `warning`)
- `platform` (`all` | `unix` | `windows`)

This schema is strict (`additionalProperties: false`) to keep rule processing
deterministic across runtimes.

## Runtime usage

In TypeScript harness, built-ins live in `safety-patterns.ts` and are evaluated by
`classifyShellCommand(command)`, which returns:

- `severity`: `safe` / `warning` / `critical`
- `matchedRules`: matched rule IDs

Built-in examples include patterns for:

- risky file ops (`rm`, `rm -rf /`)
- privilege escalation (`sudo`)
- remote execution pipes (`curl | sh`)
- dangerous git actions (`git push`, `reset --hard`, `clean -fd`)

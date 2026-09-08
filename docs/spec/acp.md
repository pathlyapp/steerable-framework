# ACP: embed Steerable in an editor

Steerable serves the [Agent Client Protocol](https://agentclientprotocol.com) —
JSON-RPC over stdio, the editor↔agent wire — so any ACP client (Zed,
JetBrains, Neovim, …) drives a Steerable loop instead of a vendor CLI. The
adapter is `steerable_sidecar.acp_adapter`; it implements the stable core of
`acp.Agent` plus the session-lifecycle and configuration RPCs (11 of 13
<!-- anchor: packages/sidecar/py/src/steerable_sidecar/acp_adapter.py :: def (authenticate|ext_method) -->
methods; only `authenticate` and `ext_method` are unimplemented).

## Install

```sh
pip install steerable-sidecar
```

This puts three entry points on `PATH`; the ACP one is `steerable-sidecar-acp`.

## Point your editor at it

ACP editors spawn the agent as a subprocess and speak JSON-RPC on its
stdin/stdout. Configure the editor to launch:

```sh
steerable-sidecar-acp
```

The agent reads provider config from the environment the editor spawns it
with: `STEERABLE_PROVIDER` / `STEERABLE_MODEL` / `STEERABLE_BASE_URL` /
`STEERABLE_API_KEY` (falling back to `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`,
and to the catalog's per-provider `env` names such as `DEEPSEEK_API_KEY`).
Set these in the editor's agent environment, not your interactive shell.

### Zed

In `settings.json`:

```json
{
  "agent_servers": {
    "steerable": {
      "command": "steerable-sidecar-acp",
      "args": [],
      "env": {
        "STEERABLE_PROVIDER": "openai_compat",
        "STEERABLE_MODEL": "glm-5.3-flash",
        "STEERABLE_BASE_URL": "https://your-gateway/v1",
        "STEERABLE_API_KEY": "sk-…"
      }
    }
  }
}
```

### JetBrains

The JetBrains AI Assistant's external-agent setting takes the same command
and environment. Point it at `steerable-sidecar-acp` with the env above.

## What the editor gets

- **Session lifecycle** — `new_session` / `list_sessions` / `load_session` /
  `resume_session` / `fork_session`. Forking branches the durable record, a
  capability some vendor agents do not expose over ACP.
- **Session modes** — `set_session_mode` with a real `read-only` gate
  (non-read tools are refused before approval). Plan mode is an honest gap:
  the runtime has no plan mode, so the adapter does not fake one.
- **Config overrides** — `set_config_option` for per-session
  `provider` / `model` / `baseUrl`.
- **Editor bridges** — when the client advertises `fs.readTextFile` /
  `fs.writeTextFile`, file tools round-trip through the editor so unsaved
  buffers are authoritative; `terminal/*` runs one-shot commands on the
  client's terminal.
- **MCP servers** — `new_session` accepts stdio `mcpServers`; their tools are
  registered under the `mcp__<server>__<tool>` prefix for the session.
  HTTP/SSE MCP transports fail loud at session creation (an honest gap, not
  a silent drop).

## Boundary

ACP is **automation-only**: it serves the standard protocol surface to
scripts, test runners, and editor clients. Private presentation data (plans,
titles, todos, terminal views, elicitations) never crosses ACP — the desktop
UI uses the sidecar's separate private method surface for those.

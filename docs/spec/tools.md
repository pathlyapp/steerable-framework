# Tools Spec

Tool interaction is modeled as two strict types:

- `ToolCall` — what the assistant asks to run
- `ToolResult` — what the runtime reports back

Plus one orthogonal classifier — `ToolMode` — that the harness uses to
decide whether a call needs explicit user consent.

## ToolCall

| Field        | Type                       | Required | Notes                                      |
| ------------ | -------------------------- | -------- | ------------------------------------------ |
| `id`         | `string`                   | yes      | Unique within a chat (use cuid2 or similar) |
| `name`       | `string`                   | yes      | Tool name registered with the runtime      |
| `arguments`  | `Record<string, unknown>`  | yes      | LLM-provided JSON args (validated by tool's schema) |

`additionalProperties` is **disabled** so tool envelopes stay
deterministic across versions. New per-call metadata should go through
the harness's `TraceSpan.attrs`, not into `ToolCall`.

## ToolResult

| Field           | Type                       | Required | Notes                                          |
| --------------- | -------------------------- | -------- | ---------------------------------------------- |
| `success`       | `boolean`                  | yes      | Hard distinction — `false` flips status to error |
| `terminal`      | `boolean`                  | no       | Explicitly mark the result as terminal         |
| `needsFollowup` | `boolean`                  | no       | Even on `success: false`, re-prompt the LLM    |
| `nextAction`    | `string`                   | no       | Machine-readable hint for the next operation   |
| `message`       | `string`                   | no       | User-facing text (rendered in the bubble)      |
| `error`         | `string`                   | no       | Debug-friendly error string (logged + shown)   |
| `data`          | `Record<string, unknown>`  | no       | Arbitrary structured payload                   |

`additionalProperties` is **enabled** for forward compatibility.

## ToolMode (harness classifier)

The harness's [`decide_tool_mode(name)`](../spec/architecture.md) returns
one of:

| Mode          | Meaning                              | Default UI treatment       |
| ------------- | ------------------------------------ | -------------------------- |
| `read`        | Pure inspection (no side effects)    | Auto-run, no consent       |
| `safe_write`  | Bounded mutation (e.g. update_event) | Auto-run with diff preview |
| `destructive` | Irreversible (delete_*, drop_*, …)   | Auto-run, undo affordance  |
| `local`       | Touches the user's machine           | **Requires consent**       |
| `external`    | Calls outside services               | Auto-run, log              |

Pattern rules (TypeScript regex equivalents in
`@steerable/agent-ui/useToolCallStatus`):

```
^get_  | ^list_  | ^read_  | ^search_   →  read
^create_ | ^update_ | ^add_ | ^set_     →  safe_write
^delete_ | ^remove_ | ^archive_ | ^drop_ →  destructive
^local_ | ^shell_ | ^exec_              →  local
```

You can override the inferred mode at registration time via the `@tool`
decorator's `mode=` kwarg.

## Exposure tiers

Every registered tool carries a `ToolExposure` tier
(`steerable_agent_runtime/tools.py`):

| Tier       | Offered list (`describe_model()`) | Dispatchable | `tool_search`-able |
| ---------- | --------------------------------- | ------------ | ------------------ |
| `direct`   | yes                               | yes          | n/a                |
| `deferred` | no                                | yes          | yes                |
| `hidden`   | no                                | yes          | no — also excluded from unknown-tool suggestions |

Dispatch never gates on exposure: a tool the model discovered (or a host
invoked directly) runs by name without being re-listed. `describe()` keeps
the full inventory for host introspection.

`tool_search` (`tool_search.py`) is the deferred tier's discovery seam: one
direct-tier tool that BM25-ranks the deferred inventory over name +
description (name tokens weigh double) and returns full schemas so a match
is callable the next round. Results default to 8 with a per-call ceiling of
20 — every match carries a schema, so the payload stays bounded. Ranking
has a relevance floor: a document containing no query term scores zero and
is dropped, so an off-vocabulary query returns an empty result rather than
irrelevant tools.

## `run_code` (programmatic tool calls)

Opt-in (`STEERABLE_RUN_CODE=1`). The model still sees native tools; `run_code`
is an extra tool whose arguments are `{code, description}`. `code` is the
body of a Python function. The program runs in a **child** interpreter
under the same layer-2 backend as bash (Seatbelt / bwrap / Landlock). The
sidecar process that holds the API key does not `exec` model Python.

The child talks JSON-over-stdio (`tools.call(name, **kwargs)` /
`tools.<name>(...)`). Nested calls go through the live executor (approval,
sandbox rewrite, host `tool.invoke`). Nested `run_code` is refused.
`import os` / `subprocess` / `socket` fail. No backend →
`error: sandbox_unavailable`. Default off; Harbor does not force it off
the way `--no-web-tools` omits fetch — leave the env unset unless the trial
wants it.

The `progressive` harness strategy (`harness.py`) builds on the tiers: the
offered list is the direct tier plus the `tool_search` descriptor. It needs
the run's `ToolRouter` — the entrypoint calls
`AssembledHarness.wire_tools(router)` before selection, which registers the
discovery tool. Selecting `progressive` without wiring raises: the model is
never offered a tool that cannot dispatch. Paths whose tools arrive over
the wire (the sidecar's host-tools chat path) have no router to bind and
must use `full` or `minimal`.

## Web tools (sidecar)

`web_search` and `web_fetch` (`steerable_sidecar/web_tools.py`) are the
network-read pair. One implementation serves every entry point:
headless/ACP get them through `workspace_tools_for_cwd`; the
desktop-spawned sidecar registers them on the RPC router at boot and the
host delegates execution over `tool.invoke` (the host router carries
schemas only, gated by a `tool.list` handshake so an unconfigured
deployment never advertises a broken tool). Single implementation → they
are deliberately **not** in `tool_contract.json`, which exists to keep
independently implemented capabilities from diverging: the
`bash`/`read_file`/`write_file`/`edit_file` pairs, and `tool_search`'s
ranking (`toolSearch`, scored against a fixed inventory — the desktop ports
BM25 in `tool-search-rank.ts` rather than delegating, since deferred tools
are registered host-side).

A caller whose task contract is offline declares that:
`workspace_tools_for_cwd(..., web_tools=False)`, surfaced as headless's
`--no-web-tools`. The Harbor eval runner passes it on every trial — TB 2.1
tasks are solved from the container, and the container has egress for the
LLM gateway, so an offered `web_fetch` would both let a trial answer from
outside the environment under test and confound a harness comparison with a
capability change. Every other surface keeps the pair.

Both register at the `direct` exposure tier in `read` mode: primary
capabilities, side-effect-free network reads. Approval gating is the
executor wrapper's job on interactive paths, not the registry's — the
harness classifier names `web_search` / `web_fetch` explicitly (exact
names, not a `web_` prefix, so a future write-flavored `web_*` tool does
not inherit the read posture).

### Provider seam

`web_search` goes through the `WebSearchProvider` protocol — the same grain
as `LLMProvider`: a protocol, a default factory
(`default_web_search_provider`), and explicit injection at registration, so
the backend changes without touching the tool. The shipped in-process
backend is Tavily (`POST {base_url}/search`, bearer key from
`STEERABLE_WEB_SEARCH_API_KEY` or `TAVILY_API_KEY` — never the brokered LLM
key: under credential-broker mode the sidecar must not hold the real chat
key, so search carries its own credential). The desktop settings page
persists that key in userData and injects it at sidecar spawn; an empty
key still leaves `web_search` **unregistered**.

`STEERABLE_WEB_SEARCH_PROVIDER=host` registers without a sidecar key so the
Electron host can execute hosted search with the existing chat credential
(OpenAI `api.openai.com` only). GLM, OpenRouter, and DeepSeek have no hosted
search here — they need the Tavily settings key. There is no DuckDuckGo
HTML scrape. Harbor keeps `--no-web-tools`. An unknown provider name
raises at resolve time.

### Bounds

Every bound is a validated `WebToolsConfig` field resolved from
`STEERABLE_WEB_*` env vars; invalid values raise at resolve time (headless
fails at load; the desktop sidecar logs the misconfiguration and serves
without the web pair, so a typo'd optional-feature var cannot brick chat).

| Field                 | Env var                              | Default   | Ceiling     |
| --------------------- | ------------------------------------ | --------- | ----------- |
| `fetch_timeout_ms`    | `STEERABLE_WEB_FETCH_TIMEOUT_MS`     | 30 000    | 600 000     |
| `fetch_max_bytes`     | `STEERABLE_WEB_FETCH_MAX_BYTES`      | 1 000 000 | 100 000 000 |
| `fetch_max_redirects` | `STEERABLE_WEB_FETCH_MAX_REDIRECTS`  | 5         | 20          |
| `search_timeout_ms`   | `STEERABLE_WEB_SEARCH_TIMEOUT_MS`    | 30 000    | 600 000     |
| `search_max_results`  | `STEERABLE_WEB_SEARCH_MAX_RESULTS`   | 8         | 20          |

The byte cap bounds what a page can push into the process; the
transcript-side bound is the existing spill hook (`SpillHooks`
externalizes oversized `data`), not a second truncation path. Redirects are
followed same-origin only and re-validated per hop; a cross-origin
redirect is reported (`redirect_to` in `data`), not followed, so the model
re-issues the call against the new origin and the approval prompt names
it. Non-text content types are refused with a pointer at `bash` + `curl`.

### SSRF policy

`web_fetch` takes a model-supplied URL — untrusted input crossing into the
host's network position. Every hop (initial URL and each redirect target)
is validated: http(s) only, no credentials-in-URL, URL length ≤ 2048, and
the host's DNS answers must ALL be globally reachable
(`ipaddress.is_global`), with IPv4-mapped and NAT64 (`64:ff9b::/96`) forms
unwrapped before the check — so loopback, private, link-local (including
`169.254.169.254`-style metadata endpoints), and reserved ranges are
refused. Residual gap, documented honestly: the policy check and httpx's
own connect resolve DNS twice, so a hostile authoritative server could
rotate answers between them (classic TOCTOU). httpx exposes no lookup hook
to pin the connection to the validated address, so per-hop re-validation
plus the short window is the mitigation.

### Egress-proxy interaction

When the desktop runs the per-host egress proxy (`STEERABLE_EGRESS_PROXY=1`,
see `safety.md`), the sidecar's outbound is confined to a proxy that only
tunnels the configured LLM provider endpoint. The desktop marks that
posture with `STEERABLE_EGRESS_CONFINED=1` in the sidecar env — set only on
the proxy-started path, never on the startup-failure fallback, so the
sidecar cannot believe it is confined when it is not. Both tools then fail
loud with an actionable error naming the remedies (restart without the
proxy, or extend the proxy's allow-list) instead of hanging behind a proxy
that 403/405s them.

## Completion semantics

`isTerminalResult(result)` (TS) /
`is_terminal_result(result.model_dump())` (Py) treats a result as
terminal when:

- `terminal == true`, **or**
- `success == false` **and** `needsFollowup != true`

Use `needsFollowup=True` on a failure to ask the LLM to self-heal (write
a different argument, try a different tool, etc.). Without it, a failed
call ends the run.

## Example pair

```json
// ToolCall
{"id":"c_42","name":"create_event","arguments":{"title":"Lunch","start":"2026-05-15T12:00:00Z"}}

// ToolResult (success)
{"success":true,"message":"Event created.","data":{"eventId":"e_777"}}

// ToolResult (recoverable failure)
{"success":false,"needsFollowup":true,"error":"Invalid date format","message":"Please retry with ISO-8601."}

// ToolResult (terminal failure)
{"success":false,"terminal":true,"error":"Calendar service unavailable"}
```

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

## Third-party tools (plugin runtime)

An installed Python package adds tools without any host code change by
declaring the `steerable.tools` entry-point group in its own packaging
metadata:

```toml
# pyproject.toml of the extension package
[project.entry-points."steerable.tools"]
my_tools = "my_package.tools:register"
```

```python
# my_package/tools.py
from steerable_agent_runtime import tool

def register(router):
    @tool(router=router, description="Greet by name")
    async def greet(name: str) -> str:
        return f"hello {name}"
```

The sidecar boots a `PluginRegistry` over the tool router and loads every
configured `PluginSource`: `EntryPointSource` (installed packages, as
above) plus `DirectorySource` on the directory named by
`STEERABLE_PLUGIN_DIR` (local development — each non-`_`-prefixed `.py`
file is one plugin with a top-level `register(router)`). The registry
tracks which tool names each plugin registered (via a recording proxy over
the router), so plugins have a lifecycle after boot:
`enable`/`disable`/`unload`/`reload`. Disable removes the plugin's tools
from the router; enable re-runs the register callable; reload re-imports
the plugin's module (`importlib.reload`) and swaps its registrations —
with the usual reload boundaries (references already imported from the
module elsewhere keep the old objects). Remote sources (a market) plug
into the `PluginSource` protocol; only discovery is reserved, no download
mechanism.

Failures fail loud and name the offender: a missing source directory, an
import failure, a missing/non-callable `register`, and tool-name conflicts
(first registrant wins; the later plugin's load raises `PluginLoadError`)
all abort the offending plugin — and, at boot, the sidecar — rather than
silently dropping an installed tool. A registration that fails mid-way
rolls back the tools already registered by that plugin. Lifecycle calls on
unknown or wrong-state plugins raise `PluginStateError`.

## `ask_user` (structured user questions)

Opt-in per request (`askUser: true` on `agent.chat.stream`). The model calls
`ask_user` with `{intro, questions[], outro?}` — field names mirror the
protocol's `AskUserQuestionsPayload`, so the desktop renders the arguments as
the question card unchanged. Each question is `select` / `text` / `password`
with optional `options` and `multiSelect`.

Models do not always follow the schema literally (observed: gpt-oss via
Ollama emits Inquirer-style `name` / `message` / `choices`). The tool
normalizes those aliases onto the canonical fields at the model-JSON
boundary, so hosts only ever render the canonical payload; a question still
missing a non-empty `id` or `text` after normalization fails the tool call
with an error naming the fix, and the model retries within the same turn.

The tool **blocks**: dispatch awaits the product-injected handler, and the
answers (`{questionId: value}`) return as the tool result, landing in the
durable record and the model's next context. On the desktop path the sidecar
routes it over the reverse channel (`ask_user.request`) to the host UI; an
unreachable host or a user cancel records an empty answers mapping rather
than hanging the turn. Under `toolsViaHost` the sidecar answers `ask_user`
locally from its own router (same interception as `run_code`) — the host's
`tool.invoke` surface has no `ask_user`, so forwarding it would fail with
`Unknown tool`. The framework seam is `make_ask_user_tool(handler)` in
`steerable_agent_runtime.ask_user` — a CLI or ACP embedder injects its own
handler (an ACP elicitation, a terminal prompt) instead of the host-routed
one.

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

**Child environment.** The child inherits only an allowlist (`PATH`, `HOME`,
`TMPDIR`/`TEMP`/`TMP`, `LANG`, `LC_*`, `PYTHONPATH`, Windows `SYSTEMROOT`/
`SYSTEMDRIVE`) plus `PYTHONDONTWRITEBYTECODE=1`. Everything else — including
`STEERABLE_API_KEY` and any `*_API_KEY` / `*_TOKEN` the sidecar holds — is
scrubbed, because the import guard blocks `import os` but not the
`__subclasses__` route to `os.environ`.

**Inheriting layer-1.** When the host already runs the sidecar under an OS
sandbox it sets `STEERABLE_SIDECAR_CONFINED=1`; a confined sidecar cannot
apply a *second* sandbox to its own child (macOS denies a nested
`sandbox_apply` once the outer profile allows outbound network). In that
posture `run_code` skips the layer-2 wrap and lets the child inherit the
layer-1 boundary; the result's `data._sandbox` reads
`{backend: "inherited", enforcement: "partial", via: "layer1"}` instead of
naming a dedicated backend.

The `progressive` harness strategy (`harness.py`) builds on the tiers: the
offered list is the direct tier plus the `tool_search` descriptor. It needs
the run's `ToolRouter` — the entrypoint calls
`AssembledHarness.wire_tools(router)` before selection, which registers the
discovery tool. Selecting `progressive` without wiring raises: the model is
never offered a tool that cannot dispatch. Paths whose tools arrive over
the wire (the sidecar's host-tools chat path) have no router to bind and
must use `full` or `minimal`.

## File tools: read-before-write state

`read_file` returns a `version` (SHA-256 of the full content) alongside the
(clipped) preview; `write_file` / `edit_file` accept an optional
`expectedVersion` that rejects the write when the file changed since. On top
of that explicit token, the workspace keeps a session-scoped
**readFileState** (path → version) that the tools maintain themselves:
`read_file` records it, and every successful `write_file` / `edit_file` /
`apply_patch` refreshes it to the post-write version (without the refresh, a
second write would reject against the pre-write version — iterative editing
is the norm). When the model passes no `expectedVersion`, writes and edits
automatically CAS against the tracked state, so a file modified outside the
session (another tool, a human, a crashed write's partial state) is rejected
with a conflict instead of silently overwritten. A file that vanished since
the read is a plain create, not a conflict; brand-new files are never gated.

The state lives in the tool-owning process (the `workspace_tools_for_cwd`
caller's dict, the desktop's `LocalExecutor`), not in the transcript — context
compaction cannot fold it away. The loss point is process restart + session
resume, so resume re-seeds it from the durable record, whose tool messages
carry the result JSON with `data.path` / `data.version` (`apply_patch`
carries `data.versions`): the sidecar pushes the rebuilt mapping to the host
over `read_state.seed` on a `toolsViaHost` resume, and the ACP adapter seeds
its session on hydration (`read_file_state_from_messages`). Unparseable
entries (spilled/folded bodies) are skipped — a missing entry means one fewer
CAS check, never a wrong write.

The hard gate is ON by default (CC `read-before-write` parity): overwriting
an existing file the session never read is rejected outright with an error
naming the fix ("read it first, then write with the read version"), and the
model recovers within the same turn. Creating a new file is never gated.
`STEERABLE_REQUIRE_READ_BEFORE_WRITE=0` opts out.

Two deliberate refinements over a plain mtime check:

- The CAS token is a **content hash**, not an mtime — a touch that leaves
  content identical does not false-positive, and a same-mtime content change
  does not false-negative.
- A read whose display was clipped at `_MAX_OUTPUT` is a **partial view**
  (CC `isPartialView` parity): the result carries `partial: true`, and a
  blind full-file `write_file` overwrite is rejected because the model never
  saw the tail it would destroy. A CAS-checked targeted `edit_file` stays
  allowed; a full (unclipped) read or an own write clears the flag. The
  desktop's `local_read_file` rejects oversized files instead of truncating,
  so partial views only arise from the sidecar's display clip.

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
search here — they need the Tavily settings key, or the explicit free
backend `STEERABLE_WEB_SEARCH_PROVIDER=ddg` (DuckDuckGo lite HTML; not a
silent fallback when the Tavily key is empty). Harbor keeps `--no-web-tools`.
An unknown provider name raises at resolve time.

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
| `session_search_cap`  | `STEERABLE_WEB_SESSION_SEARCH_CAP`   | 200       | 1 000 000   |
| `session_fetch_cap`   | `STEERABLE_WEB_SESSION_FETCH_CAP`    | 0 (off)   | 1 000 000   |

The byte cap bounds what a page can push into the process; the
transcript-side bound is the existing spill hook (`SpillHooks`
externalizes oversized `data`), not a second truncation path. Redirects are
followed same-origin only and re-validated per hop; a cross-origin
redirect is reported (`redirect_to` in `data`), not followed, so the model
re-issues the call against the new origin and the approval prompt names
it. Non-text content types are refused with a pointer at `bash` + `curl`.

The session caps are per-sidecar-process counters (one sidecar serves one
session); the search default of 200 mirrors Claude Code's per-session
WebSearch limit, and 0 disables a cap. Exceeding one fails the call with a
followup-able error naming the limit and its env var.

### Domain policy

`allowed_domains` / `blocked_domains` (comma-separated
`STEERABLE_WEB_ALLOWED_DOMAINS` / `STEERABLE_WEB_BLOCKED_DOMAINS`) are the
Claude Code WebSearch `allowed_domains`/`blocked_domains` parity knobs. An
entry matches its exact host and every subdomain (`example.com` covers
`docs.example.com`); blocked wins over allowed on a tie; an empty
allow-list allows every public host. `web_fetch` refuses a disallowed
target before any DNS or network work (redirects are same-origin, so the
initial check covers the chain). `web_search` both passes the lists to
providers with native support (Tavily's `include_domains` /
`exclude_domains`, so ranking happens inside the policy) and filters
returned hits post-hoc, so the policy holds for every provider.

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

When the desktop runs the per-host egress proxy (`STEERABLE_EGRESS_PROXY`,
default-on in the desktop since 2026-09-08; see `safety.md`), the sidecar's
outbound is confined to the proxy. The proxy's CONNECT allow-list covers the
configured LLM provider endpoint, the deployment's web domain list, and a
configured in-sidecar search backend's fixed API endpoint — one source
(`STEERABLE_WEB_ALLOWED_DOMAINS`) feeds both this module's application-layer
policy and the proxy's network-layer list. The desktop marks that posture
with `STEERABLE_EGRESS_CONFINED=1` in the sidecar env — set only on the
proxy-started path, never on the startup-failure fallback, so the sidecar
cannot believe it is confined when it is not — and points `HTTPS_PROXY` at
the proxy. Both tools then run *through* the proxy (httpx `trust_env`): the
domain policy and the SSRF pre-check are unchanged, and a target outside the
proxy's allow-list fails with an error naming the list. Two honest edges:
the proxy matches exact hosts, so a subdomain of an allowed domain passes
the app layer but is denied by the proxy unless listed separately; and an
empty `STEERABLE_WEB_ALLOWED_DOMAINS` (app-layer "any public domain") cannot
be expressed in a closed proxy list, so arbitrary fetches then fail at the
proxy. The marker without any proxy env is a misconfiguration and fails loud
with an actionable error instead of hanging behind an absent proxy.

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

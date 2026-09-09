# Framework Comparison

How Steerable compares to other agent frameworks and products — including
where we lag. Steerable is an early-stage project (`0.2.x`); this page is
written to help you decide, not to declare a winner.

!!! note "Honesty policy"
    Every claim below links to a spec page or names a shipped artifact.
    Where Steerable is behind, the table says so. If you find a stale row,
    [open an issue](https://github.com/pathlyapp/steerable-framework/issues) —
    this page is reviewed on every release.

!!! info "How the Claude Code column was sourced"
    Claude Code is closed source, so its column is not quoted from marketing
    copy: it was read out of the shipped artifact. The `2.1.263` npm package is
    an installer stub whose real payload is eight platform-specific native
    binaries; the `darwin-arm64` one embeds its full application bundle as
    plain-text JavaScript, and the package also publishes `sdk-tools.d.ts` —
    unminified TypeScript declarations for 40 built-in tool input/output types.
    Identifiers are minified, but string literals, environment variable names,
    error messages, tool schemas and system-prompt text are intact. Facts below
    are taken from that bundle at version `2.1.263`. The same bundle serves the
    CLI and the Agent SDK — it carries three system-prompt variants, one of
    which reads *"running within the Claude Agent SDK"* — so they share a
    column rather than pretending to be separate products.

## The short version

Most agent frameworks answer one of two questions: *"how do I orchestrate
agent logic?"* (LangGraph, OpenAI Agents SDK) or *"how do I ship a coding
agent product?"* (Claude Code, Codex, DeepSeek Harness, Pi). Steerable
answers a third: *"how do I ship the same agent loop into a desktop app and
a server, over one typed protocol, without rewriting the plumbing twice?"*
It is a layered library — protocol, harness, runtime/sidecar, UI — where
each tier is independently adoptable.

## At a glance

| Dimension | Steerable | OpenAI Codex | DeepSeek Harness | Pi | LangGraph | OpenAI Agents SDK | Claude Code / Agent SDK |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **What it is** | Layered library (4 tiers, independently published) | Product family: CLI/TUI, IDE, desktop, cloud — Rust core | Plugin-based harness (TS) on vendored Cordis; everything is a plugin | Minimal-core coding agent CLI (TS, 11 lockstep packages); omits MCP, subagents, and a permission system by design — extensions add them | Low-level orchestration library (Py/JS): state graphs | Lightweight agent framework (Py/TS) over the Responses API | Closed-source product shipped as one bundle behind both the CLI and the Agent SDK |
| **Loop control / steering** | `CoreLoop` single-agent step loop; structured `LoopEvent` taxonomy (13 kinds, 5 categories); `agent.chat.steer` RPC mid-turn; fork | Session-owned turn loop; interrupt/abort; 12-event hook engine | Inbox-driven ReactLoop; steer / inject / followup delivery | `agent-loop.ts` turn loop; steering and follow-up queues polled between turns; tool calls parallel by default, per-tool sequential opt-out | You design the graph; interrupts at node boundaries | Handoffs + guardrails; no mid-run steer | Mid-turn "fold" queue absorbs messages between tool rounds, with a separate follow-up queue when folding is suspended; `Stop`/`PostToolUse`/`PreToolUse` hooks can return `preventContinuation` to veto turn completion, capped at 8 consecutive blocks; `--max-turns` |
| **Tool execution** | `ToolRouter` + `@tool`; host **reverse channel** — desktop tools run in the host process (visible terminal); native stdio MCP client wired on sidecar (`chat.stream` `mcp`), headless (`--mcp`), and ACP paths. Programmatic tool calling ships two ways: `run_code` (single-shot Python function body, sandboxed child, nested tools over stdio) and a session-style JS PTC (`run_js` / `wait_js` — a long-lived Node worker with per-session `store`/`load` KV, `yield`/`wait` cells, and a Promise tool bridge). The desktop adds two product families on top: cross-turn background Tasks (`task_run` / `task_status` / `task_result` on a host task table, with a task panel UI) and git-worktree isolation (`worktree_create` / `worktree_list` / `worktree_remove`, composable with tasks and mergeable/discarded from the UI) | Unified exec (PTY), MCP, parallel gating | Concurrency-safe tool pool, MCP client | 8 built-in tools; extensions register tools at runtime with no reload; **no MCP** | `ToolNode` inside your graph | Function tools, MCP, hosted tools | 40 built-in tool types (`sdk-tools.d.ts`), lazy exposure via `ToolSearch` + `defer_loading`, tool concurrency capped at 10; MCP over stdio/SSE/HTTP with user/project/org scoping, OAuth, and a 25k-token output cap |
| **Safety model** | Two layers: OS sandbox for the sidecar (macOS Seatbelt with a deny-by-default write whitelist; Linux bwrap, falling back to Landlock; no Windows rewriter) + a command classifier with dozens of rules, consent gate, plan-mode hard block. **Egress is per-host by default**: a bundled CONNECT allow-list proxy (`steerable-egress-proxy`) starts on boot (auto-degrading to port-only Seatbelt when a system/ambient proxy is present), the sidecar and shell tools route HTTP(S) through it, and the web tools' domain allow-list and the proxy's CONNECT list are one source. With the proxy live, shell egress pins to the localhost proxy endpoint, so Seatbelt reports `full` enforcement and `requireFull` defaults on | Approval policies + ExecPolicy + platform sandbox (Seatbelt/Landlock) + Guardian second-pass review | `sandbox.confine` (bwrap/Landlock/Seatbelt), fail-closed | None built-in — tools run with host user permissions; project trust gate only; containerization documented externally | None built-in — your infrastructure | Guardrails; no sandbox | Six permission modes (`default`/`plan`/`acceptEdits`/`auto`/`dontAsk`/`bypassPermissions`), `allow`/`deny`/`ask` rules from eight sources, `defer` as a fourth per-call state, headless fail-closed deny; real Seatbelt/bwrap confinement with a domain allowlist, but **opt-in and fail-open** — `failIfUnavailable` defaults to false, so a missing backend runs commands unconfined | <!-- anchor: packages/sidecar/py/src/steerable_sidecar/sandbox.py :: Windows\w*(ExecBackend|Rewriter) -->
| **Protocol surface** | One JSON Schema → codegen TS types + Pydantic models, lockstep-released; sidecar JSON-RPC (23 methods); conformance suite keeps both SDKs byte-compatible | app-server JSON-RPC (v2) with generated TS types; single-language (Rust) core | JSON-RPC SDK + ACP server; typed session-event map | CBOR-framed `pi-protocol` (experimental server/client) plus `--mode rpc` JSONL over stdio; no cross-language codegen | LangGraph Platform REST/SDK | OpenAI Responses / Realtime APIs | `--print --input-format/--output-format stream-json` plus ~25 control-request subtypes, so a host can answer permission prompts (`can_use_tool`), interrupt, swap the model mid-session (`set_model`), and hot-reload plugins; typed via the published `sdk-tools.d.ts` (TypeScript only) |
| **Skills ecosystem** | Layered disclosure: eager base skills in the system prompt, catalog skills loaded on demand via a `skill` tool; `SKILL.md`-compatible frontmatter (`disable-model-invocation` interop) | Skill files (`.codex/skills`) | Skill provider registry + catalog/loader tool | Agent Skills (`SKILL.md`) from `~/.pi/agent/skills/` and `.pi/skills/`, exposed as `/skill:name` | None built-in | None built-in | Agent Skills plus a plugin runtime: a plugin contributes commands, skills, agents, hooks, MCP and LSP servers, output styles, themes, workflows and background monitors, from six install sources, hot-reloadable via `reload_plugins`, with a marketplace schema, a blocklist and an impersonation check |
| **Persistence / sessions** | Append-only JSONL record per session via `TraceRecorder` + resume projection; fork with seed provenance and cycle-guarded `lineage` walking (`fork_record` / `resolve_fork_seq` — regenerate forks at the last user turn, the old tail stays intact); `agent.session.tree` returns the full family tree (cousins and all, depth/node-capped) and the desktop renders it as a `/tree`-style modal branch view with single-hop switching to any node; `CompactionBoundary` carries pre/post token counts across compactions; cancelled turns still persist traces | Rollout files as source of truth; resume + fork | Event-sourced session log (SQLite); fork | JSONL session tree keyed by cwd; `-c` / `-r` / `--fork`; in-session `/tree` branch UI; optional SQLite backend on the library path | Checkpointers (SQLite/Postgres/…) | Sessions (memory) | JSONL transcript per session under `~/.claude/projects/<cwd>/`, `parentUuid` chain with `isSidechain` branches, `--fork-session`, `--resume-session-at`, and a `logical_parent_uuid` that survives compaction |
| **Deployment form** | **Dual form**: embeddable signed sidecar binary (desktop: Electron/Tauri/Wails) + in-process FastAPI (server) | Local CLI/desktop + hosted cloud | Library + headless/ACP binaries | npm packages + Bun standalone binaries; library SDK via `createAgentSession` | Self-host or LangGraph Platform | Your infra + OpenAI platform | Eight platform-specific native binaries (~200 MB each) behind an installer stub; no user-visible runtime to install |
| **Maturity** | `0.2.x`; one production consumer ([DeepPath](https://deeppath.cc)); small traffic | Massive real-world usage | Pre-release (`0.1.x` RC); internal use | Lockstep `0.85.1` across 11 packages; patch = fixes/additions, minor = breaking, no majors | Widely adopted in production | Production, OpenAI-tied | Production; Anthropic models only, routed across first-party, Bedrock, Vertex, Foundry and Gateway. Much of the surface sits behind server-side flags, so reading the binary tells you the default, not necessarily what is live for a given user |

## Where Steerable is genuinely different

1. **Dual-form deployment, one loop.** The same `CoreLoop` runs embedded in
   a desktop app (signed, notarized sidecar binary, OS-sandboxed) and in a
   server process. The wire protocol is identical in both — a desktop
   frontend and a FastAPI backend consume the same event stream.
2. **The loop yields structured events, not bytes.** The `LoopEvent`
   taxonomy (13 kinds in 5 categories) was derived from a production
   server's ~114 emission sites, then adopted by the desktop. Transports
   render wire formats — including byte-compatible rendering onto an
   existing frontend contract — instead of the loop printing SSE.
   See [CoreLoop spec](spec/core-loop.md) and the
   [API SSE drift survey](migration/api-sse-drift.md).
3. **Cross-language contract as codegen, not parallel implementation.**
   `spec/*.schema.json` is the single source of truth; TypeScript types and
   Pydantic models are generated and drift-checked in CI. The conformance
   suite replays the same event fixtures against both SDKs.
4. **The sidecar is a distribution unit.** A portable, signed CPython
   binary that speaks JSON-RPC over stdio — your users never install
   Python. On macOS it spawns under a Seatbelt profile with a
   deny-by-default write whitelist. See [Sidecar spec](spec/sidecar.md)
   and [Safety spec](spec/safety.md).
5. **Confinement is fail-closed.** When a sandbox backend is unavailable the
   sidecar refuses to run the command rather than running it unconfined, and
   the degradation is surfaced in the UI with guidance. This is the opposite
   default from Claude Code, whose sandbox is opt-in and whose
   `failIfUnavailable` setting defaults to false — a missing backend there
   prints a warning and proceeds. Our layer-1 confinement is narrower than
   theirs in platform reach; the difference here is which way it fails.
   See [Safety spec](spec/safety.md).

## Where Steerable lags — honestly

- **Production volume.** Codex serves massive daily traffic; LangGraph is
  deployed across the industry. Steerable has one production consumer and a
  fraction of the mileage.
- **Ecosystem.** LangGraph's integration catalog and community dwarf ours.
  The framework-native stdio MCP client (`McpStdioClient`) is now wired on
  the sidecar (`chat.stream` `mcp` param), headless (`--mcp`), and ACP
  paths, so MCP tools reach the `ToolRouter` directly; desktop hosts may
  still prefer the reverse channel. What we lack is LangGraph's breadth of
  prebuilt integrations, not the wiring.
- **Extension runtime: lifecycle + hot reload, two install sources.** A
  `PluginRegistry` tracks which tools each plugin registered (via a
  recording router proxy), and supports `enable` / `disable` / `unload` /
  `reload` per plugin — reload re-executes the module in place and swaps
  its tool registrations without a sidecar restart. Two install sources
  ship: the `steerable.tools` `importlib.metadata` entry point group and a
  local directory source (`STEERABLE_PLUGIN_DIR`, each `.py` a plugin with a
  top-level `register(router)`); the `PluginSource` protocol leaves room
  for a remote/marketplace source. Name collisions fail closed (first
  registration wins, the offender is named). What we still lack versus
  Claude Code is the marketplace, blocklist, and impersonation check — the
  runtime is there, the distribution trust layer is not.
- **Context compaction now ships four paths and both breakers** —
  pressure-triggered, overflow-reactive, periodic micro-compaction
  (tool-result pruning), and manual (`compact_now`, the host-command
  path). Two circuit breakers match Claude Code's pair: the failure
  breaker stops the pressure path after three consecutive ineffective
  compactions, and the rapid-refill breaker stops it after three
  consecutive compactions whose freed space refills within three rounds —
  the tripping round appends an actionable thrashing reminder (model- and
  UI-visible) telling the model to converge instead of re-reading folded
  output. `pre_tokens` / `post_tokens` estimates are recorded on every
  `CompactionBoundary` (the `compact_boundary` observability pattern), and
  a hysteresis margin (which CC does not have) keeps a borderline
  transcript from re-compacting every round. What we still lack is
  Claude Code's partial-compaction variant that preserves named
  conversation sections.
- **Structured questions reach the model end-to-end, at Claude Code's
  constraints.** The `ask_user` tool registers sidecar-side per request,
  the desktop answers over the reverse channel with a rendered question
  card (Electron and browser-server modes alike), and answers land back in
  the transcript as the tool result. The schema is tightened to Claude
  Code's `AskUserQuestion` spec: 1–4 questions, a `header` chip label (≤12
  chars, derived from the question text when the model omits it), 2–4
  options per select question, an explicit `multiSelect` (defaulted to
  single-select when omitted), and an automatic "Other" free-text escape
  the host appends. Out-of-range payloads fail closed at the tool boundary.
- **Write-conflict detection is now a default-on hard gate.** Both the
  framework file tools and the desktop local executor refuse a write or
  edit to a file the model has not read this session (opt-out env var for
  legacy flows), reject full-file writes when the model only saw a clipped
  view (`edit_file` stays allowed for targeted changes), and detect
  external modification between read and write by content hash — a
  stronger check than Claude Code's mtime compare, in the spirit of
  DeepSeek Harness's versioned-handle CAS.
- **Web tools carry the deployment policy knobs, with two first-party
  search backends.** `web_search` / `web_fetch` ship with domain
  allow/block lists (passed to Tavily's native
  `include_domains`/`exclude_domains` and enforced post-hoc for every
  provider, so the policy is provider-independent) and per-session call
  caps (search defaults to 200, Claude Code's per-session WebSearch limit
  parity). Two search providers ship: Tavily and Brave Search (an
  independent-index first-party backend — `STEERABLE_WEB_SEARCH_PROVIDER=brave`
  with `BRAVE_SEARCH_API_KEY`), plus the host-delegated path. What we still
  lack is a self-hosted usage-accounting service, and Harbor evals run
  `--no-web-tools` regardless.
- **Provider compatibility is data, now including per-model optimal
  parameters.** Vendor wire divergences are flag entries
  (`PROVIDER_COMPAT_HOSTS`), and a preset table (`llm.presets`) fills
  vendor-documented sampling optima for the open-weight families —
  DeepSeek (0.0 for coding, nothing for the fixed-1.0 reasoner), Qwen3
  (0.6 / 0.95 / `top_k` 20), GLM (1.0 / 0.95), Llama (0.6 / 0.9), gpt-oss
  (1.0 / 1.0 / effort `medium`), MiniMax (1.0 / 0.95 / `top_k` 40) — keyed
  by base-URL host and model leaf, applied only where the caller left the
  field unset, with compat flags still gating what may be sent. What we
  still lack is a third *protocol* family beyond OpenAI-compatible and
  Anthropic-native (Gemini-native), and a remote model catalog with
  ETag-cached updates like Codex's.
- **Sandbox coverage.** Layer-1 OS confinement covers macOS (Seatbelt) and
  Linux (bwrap, falling back to Landlock). Windows has no rewriter and relies <!-- anchor: packages/sidecar/py/src/steerable_sidecar/sandbox.py :: Windows\w*(ExecBackend|Rewriter) -->
  on the layer-2 classifier plus consent. Egress control is productized: the
  bundled `steerable-egress-proxy` (a CONNECT allow-list proxy) is on by
  default and holds the per-host allow-list outside the sandbox, so per-host
  enforcement survives sbpl's port-only limitation on macOS. On Linux the
  layer-1 backends have no per-host pinning (bwrap's `allowed_hosts` is
  interface-compatible only, Landlock has none), so per-host egress there is
  enforced by the proxy plus the app-layer domain list, not the namespace —
  the UI says so honestly. When the proxy is live, shell egress pins to its
  localhost endpoint, Seatbelt reports `full`, and `requireFull` defaults on.
- **No hosted offering.** No cloud, no managed platform, no live
  observability stream (post-hoc OTLP export only).
- **Multi-agent: one delegate tool on a shared pool.** The model-facing
  surface is a single `delegate_subagent` tool — depth-1 by construction,
  named `subagent_type` profiles with per-profile tool domains that fail
  closed (`tool_not_delegated`), per-profile models (via the host's
  provider factory), and opt-in concurrency. Underneath, delegations run
  on the framework's `AgentPool`, so concurrent profiles execute in
  parallel under one budget and child lifecycle lands as `agent.child`
  events hosts can render live. A six-tool orchestration family
  (`agent_spawn` / `agent_send` / `agent_wait` / `agent_close` /
  `agent_list` / `agent_interrupt`) remains available as an opt-in
  advanced mode for explicit coordination, sharing the same pool. The
  desktop additionally ships a cross-turn background Task family
  (`task_run` / `task_status` / `task_result` on a host-side task table,
  with a task panel UI) — tasks run on their own sidecar stream so they
  outlive the parent turn, and compose with git-worktree isolation. What
  stays out of scope by design in the framework: planning, DAGs, and
  groupchat. If you want batteries-included orchestration, LangGraph or
  the Agents SDK will get you there faster.

## Terminal-Bench 2.1

The score of record is **Steerable + GLM-5.3-Flash = 80.7%** on the 89-task catalog (six-run mean at tag `tb-8e260de`; see [Evals](evals.md)). That is a Flash-cost model in the same band as Claude Code + Opus 4.8 (78.9%) and Codex CLI + GPT-5.5 (83.1%) on the [public 2.1 board](https://snorkel.ai/leaderboard/terminal-bench-2-1/). Z.AI's own Claude Code run of GLM-5.3-Flash is 84.3% under a 6-hour timeout — we are behind that vendor protocol, and still in the usable band.

In our own controlled matrix — same model, same gateway account, same Harbor protocol — Claude Code scores 83.1% at **$0.162 per solved task** against our 80.7% at **$0.146**, and Pi scores 73.4% at $0.061. Pass rate and cost per solved task are tracked as co-equal metrics precisely because they can move in opposite directions. Read both numbers with the six-run spread in mind: our sample standard deviation is 2.9 points, wide enough to contain the 2.4-point gap.

## Choosing

**Reach for Steerable when** you need a typed cross-language contract
(TS + Python held byte-compatible by codegen), plan to ship to desktop /
on-prem / air-gapped environments, want the same loop in your Electron app
and your FastAPI backend, or want a headless React chat UI you can theme
without fighting markup opinions.

**Don't reach for Steerable when** your agent lives entirely inside one
Python process with no frontend (LangGraph or the Agents SDK are more
direct), you want a hosted platform with managed tracing and evals, you
need a large integration ecosystem today, or you want a finished product
rather than plumbing — Claude Code and Codex ship the product.

## Related

- [CoreLoop spec](spec/core-loop.md) — the loop and its event taxonomy
- [Safety spec](spec/safety.md) — the two-layer safety model
- [Sidecar spec](spec/sidecar.md) — the JSON-RPC method catalog
- [Architecture](spec/architecture.md) — the four-tier layering rule

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
| **Tool execution** | `ToolRouter` + `@tool`; host **reverse channel** — desktop tools run in the host process (visible terminal); native stdio MCP client wired on sidecar (`chat.stream` `mcp`), headless (`--mcp`), and ACP paths | Unified exec (PTY), MCP, parallel gating | Concurrency-safe tool pool, MCP client | 8 built-in tools; extensions register tools at runtime with no reload; **no MCP** | `ToolNode` inside your graph | Function tools, MCP, hosted tools | 40 built-in tool types (`sdk-tools.d.ts`), lazy exposure via `ToolSearch` + `defer_loading`, tool concurrency capped at 10; MCP over stdio/SSE/HTTP with user/project/org scoping, OAuth, and a 25k-token output cap |
| **Safety model** | Two layers: OS sandbox for the sidecar (macOS Seatbelt with a deny-by-default write whitelist; Linux bwrap, falling back to Landlock; no Windows rewriter) + a command classifier with dozens of rules, consent gate, plan-mode hard block | Approval policies + ExecPolicy + platform sandbox (Seatbelt/Landlock) + Guardian second-pass review | `sandbox.confine` (bwrap/Landlock/Seatbelt), fail-closed | None built-in — tools run with host user permissions; project trust gate only; containerization documented externally | None built-in — your infrastructure | Guardrails; no sandbox | Six permission modes (`default`/`plan`/`acceptEdits`/`auto`/`dontAsk`/`bypassPermissions`), `allow`/`deny`/`ask` rules from eight sources, `defer` as a fourth per-call state, headless fail-closed deny; real Seatbelt/bwrap confinement with a domain allowlist, but **opt-in and fail-open** — `failIfUnavailable` defaults to false, so a missing backend runs commands unconfined | <!-- anchor: packages/sidecar/py/src/steerable_sidecar/sandbox.py :: Windows\w*(ExecBackend|Rewriter) -->
| **Protocol surface** | One JSON Schema → codegen TS types + Pydantic models, lockstep-released; sidecar JSON-RPC (23 methods); conformance suite keeps both SDKs byte-compatible | app-server JSON-RPC (v2) with generated TS types; single-language (Rust) core | JSON-RPC SDK + ACP server; typed session-event map | CBOR-framed `pi-protocol` (experimental server/client) plus `--mode rpc` JSONL over stdio; no cross-language codegen | LangGraph Platform REST/SDK | OpenAI Responses / Realtime APIs | `--print --input-format/--output-format stream-json` plus ~25 control-request subtypes, so a host can answer permission prompts (`can_use_tool`), interrupt, swap the model mid-session (`set_model`), and hot-reload plugins; typed via the published `sdk-tools.d.ts` (TypeScript only) |
| **Skills ecosystem** | Layered disclosure: eager base skills in the system prompt, catalog skills loaded on demand via a `skill` tool; `SKILL.md`-compatible frontmatter (`disable-model-invocation` interop) | Skill files (`.codex/skills`) | Skill provider registry + catalog/loader tool | Agent Skills (`SKILL.md`) from `~/.pi/agent/skills/` and `.pi/skills/`, exposed as `/skill:name` | None built-in | None built-in | Agent Skills plus a plugin runtime: a plugin contributes commands, skills, agents, hooks, MCP and LSP servers, output styles, themes, workflows and background monitors, from six install sources, hot-reloadable via `reload_plugins`, with a marketplace schema, a blocklist and an impersonation check |
| **Persistence / sessions** | Append-only JSONL record per session via `TraceRecorder` + resume projection; fork with seed provenance and cycle-guarded `lineage` walking (`fork_record` / `resolve_fork_seq` — regenerate forks at the last user turn, the old tail stays intact); `CompactionBoundary` carries pre/post token counts across compactions; cancelled turns still persist traces | Rollout files as source of truth; resume + fork | Event-sourced session log (SQLite); fork | JSONL session tree keyed by cwd; `-c` / `-r` / `--fork`; in-session `/tree` branch UI; optional SQLite backend on the library path | Checkpointers (SQLite/Postgres/…) | Sessions (memory) | JSONL transcript per session under `~/.claude/projects/<cwd>/`, `parentUuid` chain with `isSidechain` branches, `--fork-session`, `--resume-session-at`, and a `logical_parent_uuid` that survives compaction |
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
- **Extension runtime is minimal.** Third-party tools register through the
  `steerable.tools` `importlib.metadata` entry point, loaded at sidecar boot
  into the `ToolRouter`. That is a chosen, smaller surface than Claude Code's
  plugin runtime (six install sources, marketplace, hot reload), Codex's
  contributor traits + marketplace, DeepSeek Harness's Cordis plugins, or
  Pi's runtime TypeScript extension loading — declare-and-register, no
  plugin lifecycle or hot reload.
- **Context compaction now ships four paths** — pressure-triggered,
  overflow-reactive, periodic micro-compaction (tool-result pruning), and
  manual (`compact_now`, the host-command path) — with a circuit breaker
  that stops the pressure path after three consecutive ineffective
  compactions and `pre_tokens` / `post_tokens` estimates recorded on every
  `CompactionBoundary` (the `compact_boundary` observability pattern).
  What we still lack is Claude Code's partial-compaction variant that
  preserves named conversation sections.
- **Structured questions reach the model end-to-end.** The `ask_user` tool
  (schema'd select / text / password questions with multi-select and an
  automatic custom-text path) registers sidecar-side per request, the
  desktop answers over the reverse channel with a rendered question card
  (Electron and browser-server modes alike), and answers land back in the
  transcript as the tool result. Claude Code's `AskUserQuestion` remains
  richer on constraints (1–4 questions, 2–4 options each, an automatic
  "Other" option) — ours validates and normalizes model-emitted fields at
  the tool boundary instead of constraining counts.
- **Write-conflict detection is now a default-on hard gate.** Both the
  framework file tools and the desktop local executor refuse a write or
  edit to a file the model has not read this session (opt-out env var for
  legacy flows), reject full-file writes when the model only saw a clipped
  view (`edit_file` stays allowed for targeted changes), and detect
  external modification between read and write by content hash — a
  stronger check than Claude Code's mtime compare, in the spirit of
  DeepSeek Harness's versioned-handle CAS.
- **Web tools carry the deployment policy knobs.** `web_search` /
  `web_fetch` ship with domain allow/block lists (passed to Tavily's
  native `include_domains`/`exclude_domains` and enforced post-hoc for
  every provider, so the policy is provider-independent) and per-session
  call caps (search defaults to 200, Claude Code's per-session WebSearch
  limit parity). What we still lack is a first-party server-side search
  backend with usage accounting — non-OpenAI providers still need a Tavily
  key, and Harbor evals run `--no-web-tools` regardless.
- **Sandbox coverage.** Layer-1 OS confinement covers macOS (Seatbelt) and
  Linux (bwrap, falling back to Landlock). Windows has no rewriter and relies <!-- anchor: packages/sidecar/py/src/steerable_sidecar/sandbox.py :: Windows\w*(ExecBackend|Rewriter) -->
  on the layer-2 classifier plus consent.
- **No hosted offering.** No cloud, no managed platform, no live
  observability stream (post-hoc OTLP export only).
- **Multi-agent: delegation yes, orchestration no.** The `SubagentExecutor`
  seam answers a `delegate_subagent` tool call with a bounded child
  CoreLoop — depth-1 by construction, per-profile tool domains that fail
  closed (`tool_not_delegated`), named `subagent_type` profiles with
  per-profile models (via the host's provider factory) and opt-in
  concurrency. What stays out of scope by design: planning, DAGs,
  groupchat, and background task systems — those live above CoreLoop as
  product concerns. If you want batteries-included orchestration,
  LangGraph or the Agents SDK will get you there faster.

## Terminal-Bench 2.1

The score of record is **Steerable + GLM-5.3-Flash = 80.7%** on the 89-task catalog (six-run mean at tag `tb-8e260de`; see [Evals](evals.md)). That is a Flash-cost model in the same band as Claude Code + Opus 4.8 (78.9%) and Codex CLI + GPT-5.5 (83.1%) on the [public 2.1 board](https://snorkel.ai/leaderboard/terminal-bench-2-1/). Z.AI's own Claude Code run of GLM-5.3-Flash is 84.3% under a 6-hour timeout — we are behind that vendor protocol, and still in the usable band.

In our own controlled matrix — same model, same gateway account, same Harbor protocol — Claude Code scores 83.1% at **$0.162 per solved task** against our 80.7% at **$0.146**, and Pi scores 73.4% at $0.061. Pass rate and cost per solved task are tracked as co-equal metrics precisely because they can move in opposite directions. Read both numbers with the six-run spread in mind: our sample standard deviation is 2.9 points, wide enough to contain the 2.4-point gap.

In our own controlled matrix — same model, same gateway account, same Harbor protocol — Claude Code scores 83.1% at **$0.162 per solved task** against our 81.7% at **$0.138**, and Pi scores 73.4% at $0.061. Pass rate and cost per solved task are tracked as co-equal metrics precisely because they can move in opposite directions. Read both numbers with the three-run spread in mind: our sample standard deviation is 4.3 points, wide enough to contain the 1.4-point gap.

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

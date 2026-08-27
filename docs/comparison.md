# Framework Comparison

How Steerable compares to other agent frameworks and products — including
where we lag. Steerable is an early-stage project (`0.2.x`); this page is
written to help you decide, not to declare a winner.

!!! note "Honesty policy"
    Every claim below links to a spec page or names a shipped artifact.
    Where Steerable is behind, the table says so. If you find a stale row,
    [open an issue](https://github.com/pathlyapp/steerable-framework/issues) —
    this page is reviewed on every release.

## The short version

Most agent frameworks answer one of two questions: *"how do I orchestrate
agent logic?"* (LangGraph, OpenAI Agents SDK, Claude Agent SDK) or *"how do
I ship a coding agent product?"* (Codex, DeepSeek Harness). Steerable
answers a third: *"how do I ship the same agent loop into a desktop app and
a server, over one typed protocol, without rewriting the plumbing twice?"*
It is a layered library — protocol, harness, runtime/sidecar, UI — where
each tier is independently adoptable.

## At a glance

| Dimension | Steerable | OpenAI Codex | DeepSeek Harness | LangGraph | OpenAI Agents SDK | Claude Agent SDK |
| --- | --- | --- | --- | --- | --- | --- |
| **What it is** | Layered library (4 tiers, independently published) | Product family: CLI/TUI, IDE, desktop, cloud — Rust core | Plugin-based harness (TS) on vendored Cordis; everything is a plugin | Low-level orchestration library (Py/JS): state graphs | Lightweight agent framework (Py/TS) over the Responses API | The Claude Code agent loop as a library (Py/TS) |
| **Loop control / steering** | `CoreLoop` single-agent step loop; structured `LoopEvent` taxonomy (13 kinds, 5 categories); `agent.chat.steer` RPC mid-turn; fork | Session-owned turn loop; interrupt/abort; 12-event hook engine | Inbox-driven ReactLoop; steer / inject / followup delivery | You design the graph; interrupts at node boundaries | Handoffs + guardrails; no mid-run steer | Steer via messages; hooks intercept tool calls |
| **Tool execution** | `ToolRouter` + `@tool`; host **reverse channel** — desktop tools run in the host process (visible terminal); MCP host-mediated today | Unified exec (PTY), MCP, parallel gating | Concurrency-safe tool pool, MCP client | `ToolNode` inside your graph | Function tools, MCP, hosted tools | Built-in file/bash/web tools, MCP |
| **Safety model** | Two layers: OS sandbox for the sidecar (macOS Seatbelt, deny-by-default write whitelist; Linux Landlock planned) + a command classifier with dozens of rules, consent gate, plan-mode hard block | Approval policies + ExecPolicy + platform sandbox (Seatbelt/Landlock) + Guardian second-pass review | `sandbox.confine` (bwrap/Landlock/Seatbelt), fail-closed | None built-in — your infrastructure | Guardrails; no sandbox | Permission modes + hooks; sandboxed bash in Claude Code |
| **Protocol surface** | One JSON Schema → codegen TS types + Pydantic models, lockstep-released; sidecar JSON-RPC (15 methods); conformance suite keeps both SDKs byte-compatible | app-server JSON-RPC (v2) with generated TS types; single-language (Rust) core | JSON-RPC SDK + ACP server; typed session-event map | LangGraph Platform REST/SDK | OpenAI Responses / Realtime APIs | Anthropic API; SDK spawns the Claude Code process |
| **Skills ecosystem** | Layered disclosure: eager base skills in the system prompt, catalog skills loaded on demand via a `skill` tool; `SKILL.md`-compatible frontmatter (`disable-model-invocation` interop) | Skill files (`.codex/skills`) | Skill provider registry + catalog/loader tool | None built-in | None built-in | Agent Skills (shared with Claude Code) |
| **Persistence / sessions** | `TraceRecorder` + resume projection + fork (`untilSequence` truncation); cancelled turns still persist traces | Rollout files as source of truth; resume + fork | Event-sourced session log (SQLite); fork | Checkpointers (SQLite/Postgres/…) | Sessions (memory) | Session resume |
| **Deployment form** | **Dual form**: embeddable signed sidecar binary (desktop: Electron/Tauri/Wails) + in-process FastAPI (server) | Local CLI/desktop + hosted cloud | Library + headless/ACP binaries | Self-host or LangGraph Platform | Your infra + OpenAI platform | Local / CI agents |
| **Maturity** | `0.2.x`; one production consumer ([DeepPath](https://deeppath.cc)); small traffic | Massive real-world usage | Pre-release (`0.1.x` RC); internal use | Widely adopted in production | Production, OpenAI-tied | Production (powers Claude Code), Anthropic-only |

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

## Where Steerable lags — honestly

- **Production volume.** Codex serves massive daily traffic; LangGraph is
  deployed across the industry. Steerable has one production consumer and a
  fraction of the mileage.
- **Ecosystem.** LangGraph's integration catalog and community dwarf ours.
  MCP support is currently host-mediated (tools execute in the host over
  the reverse channel); a framework-native MCP client is deliberately
  deferred.
- **Sandbox coverage.** Layer-1 OS confinement is macOS-only today
  (Seatbelt). Linux Landlock is a planned follow-up; Windows relies on the
  layer-2 classifier plus consent.
- **No hosted offering.** No cloud, no managed platform, no live
  observability stream (post-hoc OTLP export only).
- **Multi-agent orchestration is out of scope by design.** CoreLoop is the
  single-agent step loop; planning, DAGs, and groupchat live above it as
  product concerns. If you want batteries-included orchestration, LangGraph
  or the Agents SDK will get you there faster.

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
rather than plumbing — Codex and Claude Agent SDK ship the product.

## Related

- [CoreLoop spec](spec/core-loop.md) — the loop and its event taxonomy
- [Safety spec](spec/safety.md) — the two-layer safety model
- [Sidecar spec](spec/sidecar.md) — the JSON-RPC method catalog
- [Architecture](spec/architecture.md) — the four-tier layering rule

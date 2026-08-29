# Architecture Review & Roadmap (2026-08-28)

A three-way review of Steerable against OpenAI Codex, DeepSeek Harness, and
the 2026 framework landscape. It records what Steerable can defend, one
architectural inversion that costs more every release, and the order in
which the fixes have to land.

!!! note "Honesty policy"
    This page is not encouraging. Every claim cites a file and line, and
    the negative findings are the point — a roadmap that only lists wins
    is a marketing page. Where a decision has been made it is stated as a
    decision, not a suggestion.

## The differentiator: the model-quality layer

Steerable's defensible position is not the wire protocol and not the
sidecar. It is the set of mechanisms that make **weak, local, quantized,
or cheap models behave reliably**:

| Mechanism | What it does | Where |
| --- | --- | --- |
| Pseudo tool-call recovery | Recognises MiniMax XML, DeepSeek `<function=>`, and markdown `[Tool call:]` and *executes* the recovered calls; plus a streaming stripper so display stays clean | `pseudo.py:99`, `loop.py:635`, `:497` |
| `before_completion` veto | A hook that receives a completion *draft* and answers `accept` / `retry` / `narrate` | `hooks.py:85-98`, `loop.py:688-730` |
| Anti-hallucination judges | Data-need routing, deferred/claimed discipline retry, grounding judge, narration rounds | `antihallucination.py` |
| Empirical token calibration | Ratio-of-sums self-calibrating provider wrapper; auto-registers a per-model factor at 20 samples | `calibration.py` |
| Soft timeout → wrap-up | Round-boundary deadline that drops tool descriptors and asks for a final answer instead of killing the turn | `loop.py:425-441` |
| Duplicate-call dedup | Same-turn `(name, argsHash)` suppression with a soft feedback message | `loop.py:805-814` |
| Breaker-skip synthesis | Synthetic tool messages for calls the error breaker skipped, so providers do not reject the transcript | `loop.py:947`, `:1035` |

None of this is speculative. The `before_completion` veto has quantified
production evidence: 646 "no tool calls and no final response" hard
failures on the DeepPath API path are exactly the class this design
converts into retries. The `0.708` calibration factor came from 6,605
production buckets. Neither Codex nor DeepSeek Harness recovers pseudo
tool calls — DeepSeek Harness does not even parse the `<function=name>`
format DeepSeek models routinely emit, so those calls land in a text
block and never execute.

### Why the moat holds

- **Vendor SDKs cannot build this.** The OpenAI Agents SDK and Claude
  Agent SDK exist to make their own frontier models shine. Engineering
  effort spent making a quantized local model reliable runs against that
  commercial purpose.
- **LangGraph has nowhere to put it.** It is a substrate; you write the
  loop. A layer that intervenes mid-loop has no home.
- **Codex and DeepSeek Harness own a loop but target frontier models.**
  They assume structured `tool_calls` arrive.

The market that needs this layer — local and on-device, air-gapped,
cost-sensitive, regulated desktop software over proprietary data — is
exactly what the dual-form deployment (signed CPython sidecar in Electron
plus in-process FastAPI) serves. **The sidecar is the delivery channel
for the differentiator, not the differentiator itself.**

### Positioning consequence

The "Why Steerable" table in `README.md` now leads with the model-quality
layer, and the surrounding repositioning has landed: the tagline ("The
model-quality layer that makes local, quantized, and cheap models
behave"), the docs hero, and the landing-page feature cards were
rewritten in one pass across `README.md`, `docs/index.md`, and
`mkdocs.yml`'s `site_description` so the three agree. One change remains:

- **Add `docs/spec/model-quality.md`** as the reference page for the
  layer: each mechanism, the failure mode it addresses, and the
  production evidence. The mechanisms are currently documented only as
  module docstrings.

## The root finding: one inversion, five consequences

The model-visible transcript is a **mutable `list[LLMMessage]` that
`pre_step` hooks replace wholesale**, rather than a projection of a
durable append-only record.

`loop.py:320` builds a local `transcript` list and mutates it in place:
steer injection at `:374`, the grounding prompt at `:400`,
`_SOFT_TIMEOUT_NOTICE` at `:440`, `_DISCIPLINE_RETRY_NOTICE` at `:699`,
`_NARRATION_REQUEST` at `:726`. A hook may swap the whole list at
`:464-465`. Meanwhile `self.trajectory` accumulates structured events on
a separate path, and `resume.py:71 project_transcript` rebuilds a *third*
representation with its own flushing logic.

Nothing forces the three to agree, and they do not. Notices, the skill
catalog body, mid-turn steers, and compaction replacements are all absent
from the projection.

### 1. Resume infidelity

A resumed session feeds the model a history it never saw. This is slow
drift, not a crash — it presents as "the agent forgot it was told X".
Resume also defaults to a 300-character `resultPreview`
(`loop.py:871-873`), so the model record is downstream of the *display*
record.

### 2. Prompt-cache destruction

The most expensive consequence. Rewriting history invalidates the cached
prefix. Codex's `WorldStateSection` plus RFC 7386 merge-patch design
(`codex-rs/core/src/context/world_state/mod.rs:228-333`) exists
specifically so the prefix stays byte-stable forever and a state change
costs one small tail fragment.

Steerable's `recompact_margin` hysteresis (`compaction.py:83-88`) makes
prefix invalidation *cheaper* rather than preventing it. It is scar
tissue from the dogfood pathology recorded in `CORELOOP_TODO.md` (22
compactions across 5 traces). Cache reads cost roughly 10% of input
price and break even at about 2.3 reuses per hour, so this is the
highest-leverage cost lever available — and there is currently **no way
to measure it**: `cached_tokens` and `cache_control` have zero matches
across `packages/`, and `LLMUsage` carries only
prompt/completion/total (`llm/__init__.py:35-38`).

### 3. Injected context cannot be bounded

Nothing caps a skill catalog, a steer message, or third-party hook
output. `spill.py` is the right idea at the right hook point, but it is
opt-in and covers only tool results. Codex caps hook output at 2500
tokens and spills the remainder to disk
(`codex-rs/hooks/src/output_spill.rs:12`), which works because bounding
is applied at the one place items enter history.

### 4. MCP cannot land safely

MCP is the largest single source of unbounded, third-party, mutable
model-visible context in any agent system. See
[the ordering decision](#the-mcp-ordering-decision) below.

### 5. Nothing can be proven in tests

There is no recording provider, so no test asserts what the model was
actually shown. Codex holds its entire context discipline in place with
outbound-request assertions (`ResponseMock` / `ResponsesRequest`, plus
`core/tests/suite/prompt_cache_key.rs`). Without the equivalent, every
fix below gets written correctly and silently regresses.

### The type that blocks the fix

`LLMMessage.content: str` (`llm/__init__.py:28`) is the single most
expensive line in the codebase. It blocks multimodal input, structured
outputs, and `cache_control` — which is a *per-block* annotation, so
prompt caching cannot be added without changing this type. It is a Tier 1
breaking change that propagates to `resume.py`, `compaction.py`,
`spill.py`, `tokens.py`, the TypeScript codegen, and every conformance
fixture. It gets more expensive every release. It must land before 1.0.

## Safety: the sandbox confines the wrong process

[The safety spec](spec/safety.md) is honest that layer 1 confines the
sidecar and that tool execution is deliberately unconfined
(`docs/spec/safety.md:116`). But the sidecar is the *lower-risk*
process. The high-risk process is the one running shell commands against
the user's machine.

Worse, the Seatbelt profile grants open reads
(`docs/spec/safety.md:98`) and open `network-outbound`
(`:99`). That puts private data, untrusted content (tool output and web
results enter the transcript), and egress in one process — the lethal
trifecta, ranked first in the OWASP Top 10 for Agentic Applications 2026.
The 61-rule regex classifier does not address it, and the spec says as
much.

Staged fix:

1. **Egress allow-list (S).** Replace open `network-outbound` with an
   allow-list derived from the configured provider `baseUrl` plus
   explicit host config. The sidecar's only legitimate egress is the LLM
   provider. Roughly 30 lines in the profile generator, and it breaks the
   exfiltration leg for the process holding the API key.
2. **Subagent tool scoping (M).** `SubagentExecutor` dispatches the child
   to the parent's own executor (`subagent.py:107` — all of the parent's
   tools, or none). A read-only researcher subagent breaks the trifecta
   by construction, but that requires the child to take its own executor
   and tool advertisement. Today the seam has the delegation ergonomics
   without the isolation guarantee, and the child gets no separate trace.
3. **`SandboxedToolExecutor` port (L).** So tool execution can route
   through a real boundary: per-exec Seatbelt on the desktop, an
   E2B/Modal-style sandbox on the server. Model it on the OpenAI SDK's
   harness/compute split — the loop should not know which it got.

Independently: adopt DeepSeek Harness's `SandboxEnforcement: full |
partial | none` as a **returned value** rather than a log line
(`deepseek-harness/docs/subsystems/sandbox.md:30`), so a caller requiring
an absolute boundary can refuse. Steerable currently tells hosts to log
loudly and continue (`docs/spec/safety.md:109-112`); a log line is not
something a host can branch on or show in a settings panel.

## Gap scorecard

| Capability | Reference implementation | Steerable today | Severity |
| --- | --- | --- | --- |
| History representation | Append-only envelopes with per-item metadata; "no history rewrite" is a written rule (codex) | Mutable `list[LLMMessage]`, hooks replace wholesale (`loop.py:320`, `:464-465`) | **Blocking** |
| Injected context typed + self-identifying | `ContextualUserFragment` with markers and `matches_text` (`codex-rs/context-fragments/src/fragment.rs:64-119`) | None; skill catalog is a plain transcript rewrite | **Blocking** |
| Cache-stable incremental state | `WorldStateSection` + RFC 7386 merge patch; unchanged sections emit nothing | None; state changes rewrite the transcript | **Blocking** |
| Durable model-visible record | Rollout JSONL with model and display items as distinct variants, governed by an explicit policy (`codex-rs/rollout/src/policy.rs`) | One display stream; the model transcript is re-derived from it (`resume.py:71-167`) | **Blocking** |
| Model-visible ⟺ logged, enforced | `deriveMessages()` folds the log; a runtime invariant compares provider bytes to the fold (dsh `agent-loop/src/invariant.ts`) | `self.trajectory` runs parallel to a mutable transcript; notices never reach the projection | **Blocking** |
| Test harness asserting what the model saw | `ResponseMock` / `ResponsesRequest` body assertions | No recording provider; tests assert emitted `LoopEvent`s | **Blocking** (gates the rest) |
| Prompt-cache instrumentation | `cached_tokens` / `cache_read_input_tokens` parsed and surfaced | Zero matches across `packages/`; `LLMUsage` has three fields | **Significant** |
| Multimodal / content parts | Content-part unions with per-part `cache_control` | `LLMMessage.content: str` (`llm/__init__.py:28`) | **Significant** (Tier 1, pre-1.0) |
| Compaction checkpointing | `CompactedItem.replacement_history` + chained windows (`codex-rs/history/src/lib.rs:146-155`); dsh `replace` surface op shadowing cited seqs | In-memory rewrite in a hook; nothing durable records what it did | **Significant** |
| Per-item bounding as an invariant | `TruncationPolicy`, middle-out, shared budget, self-describing header | `spill.py` as an opt-in `post_tool_result` hook; nothing bounds other injections | **Significant** |
| Hook output bounding | 2500-token cap with spill-to-disk (`codex-rs/hooks/src/output_spill.rs:12`) | None — hooks return transcripts with no cap | **Significant** |
| Per-tool timeouts | Server and caller timeouts composed with `min()` | None; `soft_timeout_ms` is only checked at round boundaries (`loop.py:425-429`) | **Significant** |
| Tool exposure tiers | `Direct / Deferred / Hidden` — registration ≠ exposure (`codex-rs/tools/src/tool_executor.rs:51-70`) | All registered tools are exposed; `tools` is a flat list | **Significant** (blocks MCP at scale) |
| MCP | Full client with identity-keyed reuse, per-server catalog caps, name qualification, immutable per-step binding | None | **Significant** |
| Tool-execution sandbox | Per-exec Seatbelt / seccomp / restricted token driven by per-turn policy | `SandboxedToolExecutor` + `SeatbeltExecBackend` (Wave 3, shell/subprocess; enforcement reported as a value, `require_full` fails closed) | Closed for shell/subprocess; Linux Landlock pending |
| Egress control | Allow-listed | Open `network-outbound` (`docs/spec/safety.md:99`) | **Significant** |
| Approval algebra | 8-variant decision, 3 persistence scopes, `Denied{reason}` distinct from `Abort` | `require_consent` / `consent_granted` booleans | **Significant** |
| Declared RPC concurrency | `ClientRequestSerializationScope` per method (`codex-rs/app-server-protocol/src/protocol/common.rs:128-139`) | "ordered by their JSON-RPC `id`" (`docs/spec/sidecar.md:162-163`) — not an ordering guarantee | **Significant** |
| Subagent as privilege boundary | Child gets its own context window *and* its own tool set | Child inherits the parent executor (`subagent.py:107`) | **Significant** |
| Tool render intent in the protocol | Declared `presentCall` / `presentResult` as pure functions, persisted for replay | Inferred from regex on the tool name (`docs/spec/tools.md:50-58`) | **Significant** |
| Cursor pagination on list methods | `cursor`/`limit` → `data`/`next_cursor` everywhere | `trace.fetch` returns every event; no back-pressure (`docs/spec/sidecar.md:164-166`) | **Minor** now, **Significant** at scale |
| Log format versioning | `SESSION_FORMAT_VERSION` + per-event `ignorable`, required-on-read default | None | **Significant** (desktop users downgrade) |
| Cancelled-stream integrity | `interrupted: true` anchor + synthetic `ABORTED_BEFORE_DISPATCH` results | `_close_dangling_tool_calls` runs at projection time (`resume.py:170`), not record time | **Significant** |
| Workflow orchestration | `ctx.workflowEngine` (dsh) | None | **None** — correctly out of scope |

## Where Steerable is ahead

Beyond the model-quality layer, four things hold up against both
references:

1. **`hook_action` events** (`loop.py:89-91`). Emitting *why* the loop
   changed course, at the decision point, so offline analysis sees hook
   triggers. Neither reference has a uniform equivalent.
2. **Parallel batching with a barrier model.** Start events in call
   order, results in call order, unsafe calls form barriers — cleanly
   specified and deterministic (`loop.py:755-841`).
3. **Executor composition by plain decoration.** `RouterToolExecutor` /
   `HostToolExecutor` / `SubagentExecutor` / `SkillExecutor` chain in
   about 40 readable lines and achieve what DeepSeek Harness gets from a
   dependency-injection plugin runtime across ~150 packages.
4. **Cross-language contract as codegen.** One JSON Schema to TypeScript
   types and Pydantic models, drift-checked in CI. The *discipline* is
   real engineering value even where the envelope is not defensible (see
   [protocol positioning](#protocol-positioning-decided)).

## What will age badly

- **`LLMMessage.content: str`** — see [above](#the-type-that-blocks-the-fix).
- **`MODEL_CONTEXT_WINDOWS` is stale and points the wrong way.**
  `tokens.py:129` says `claude: 200_000` while Opus 4.6 ships 1M. The
  table mirrors a downstream product's data
  (`deeppath-api/app/core/models_config.py`), which inverts the
  dependency: the framework should not depend on a consumer's table.
  Compaction thresholds derive from it, so staleness silently
  mis-triggers compaction.
- **`docs/spec/events.md` has already drifted from the loop.** It
  documents `orchestration` (`:35`), `loader-hint` (`:36`), and
  `keepalive` (`:37`) variants that no `LoopEventKind` includes
  (`loop.py:71-92`), while `hook_action`, `steer`, `soft_timeout`,
  `reasoning_delta`, and `stage_complete` appear nowhere in the spec.
- **`docs/spec/sidecar.md:65-82` documents 13 methods; `sidecar.py:147-161`
  registers 15.** `agent.chat.steer` and `agent.chat.fork` exist in the
  implementation and not in the catalog.
- **`BudgetLimit` advertises three axes and delivers one.**
  `budget.py:7-10` declares `max_tokens`, `max_steps`, `max_tool_calls`;
  `loop.py:536` is the only call site and passes tokens. `maxRounds` is
  the real guard.
- **`otel.py` is non-conformant.** It emits `steerable.*` attributes
  (`:111-141`) and `coreloop.run` / `tool.<name>` span names. No
  dashboard, collector, or eval platform knows those. GenAI semconv is
  still Development, which is the argument for aligning cheaply now
  rather than committing further to a hand-rolled vocabulary.
- **Heuristic token estimation is a shrinking problem.** The calibration
  work is excellent engineering against a problem providers are absorbing
  server-side. Keep the machinery for local and OpenAI-compatible models;
  do not invest further.

## Roadmap

Ordered by dependency, not by appeal. Each wave assumes the one before it.

### Wave 0 — prerequisites (all S, do first)

1. **`RecordingProvider` + prompt assertions.** Wrap any `LLMProvider`,
   capture every outbound request, and ship two assertions:
   `assert_stable_prefix` (request *n*'s messages are a prefix of
   *n+1*'s, except at declared compaction boundaries — the executable
   form of "no history rewrite", and it will fail today) and
   `assert_bounded_items`. This is a prerequisite, not a nice-to-have:
   without it Wave 1 gets written correctly and silently regresses.
2. **Per-tool timeouts.** `soft_timeout_ms` is only checked at round
   boundaries (`loop.py:425-429`), so a hung tool hangs the turn. Return
   a failed `ToolResult` on timeout so the existing consecutive-error
   breaker handles it. Also a hard MCP prerequisite — a remote server
   *will* hang.
3. **The egress allow-list** from [the safety section](#safety-the-sandbox-confines-the-wrong-process).

### Wave 1 — the foundation (L, one project, not three) ✅ landed 2026-08-29

Typed append-only history: `HistoryItem` envelopes carrying ordinal, turn
id, content kind, and token estimate; a `ContextFragment` concept for
injected content with stable markers so a fragment can recognise its own
rendering in retained history (codex's `ContextualUserFragment`,
`codex-rs/context-fragments/src/fragment.rs:64-119`); `pre_step` hooks
become append-only with `ContextManager.replace_all` as the single
declared rewrite path; a durable model-visible record separate from the
display stream (codex's rollout with distinct variants and an explicit
persistence policy, `codex-rs/rollout/src/policy.rs`); and resume becomes
a reverse scan to the newest compaction checkpoint, O(tail).

**Land the `content: str` → content-parts change in this same wave.**
Both are Tier 1 breaking changes; doing them separately breaks consumers
twice.

This can land incrementally — introduce `HistoryItem` / `ContextManager`
behaviour-identically first, migrate skill injection to a fragment, then
compaction, then flip `PreStepAction` to append-only — but it is one
project with one migration.

**As landed** (`history.py`, `hooks.py`, `recording.py`, `resume.py`,
`storage/`): the record is one continuous append-only log per chat
(`record_id` = `chat_id`), persisted via `StorageAdapter.append_history`
at full fidelity; hooks declare `appends` / `rewrite` and the loop is the
only writer; fork/regenerate opens a fresh record seeded inline with a
`HistorySeed` entry carrying provenance; the tripwire is
`assert_requests_match_record` — every recorded request must equal a
projection of the record, with declared compaction boundaries aligning
automatically (no manual boundary indices). `LLMMessage.content` is
`list[ContentPart]` with `text_of()` / `content_text` covering the
text-only common case; the wire schema gained an additive optional
`parts` field with `content` retained as its plain-text projection.

### Wave 2 — the payoff ✅ landed 2026-08-29

Cache instrumentation → world-state diffing → tool exposure tiers → MCP.
The order is the argument:

1. **Cache instrumentation** ✅ landed 2026-08-29. `LLMUsage` gained
   `cached_prompt_tokens` / `cache_creation_tokens`, parsed from
   `prompt_tokens_details.cached_tokens` (OpenAI-compatible; DeepSeek's
   top-level `prompt_cache_hit_tokens` as fallback) and
   `cache_read_input_tokens` / `cache_creation_input_tokens` (Anthropic),
   surfaced on the existing `stage_complete` event so `TraceRecorder`
   persists it with no new plumbing. First, so diffing can be verified
   rather than assumed.
2. **World-state sections with RFC 7386 merge-patch diffing** ✅ landed
   2026-08-29 (`world_state.py`). An unchanged section costs zero tokens;
   a changed one costs a small tail patch. The full snapshot rides inside
   every fragment (base64url comment), so resume/fork diff against what
   the model actually saw with no side channel; compaction folding the
   last fragment self-heals into a full re-injection. Landing it surfaced
   a Wave 1 seeding gap: production hosts rebuild a lossy per-turn view
   (no tool rounds, no injected fragments, display-transformed assistant
   texts), which the strict prefix check misread as a `host_revision`
   every turn. Seeding is now record-aware — on continuation the run
   seeds from the record's projection plus the host's new tail (user/
   system compared exactly, assistant tolerant of host-appended display
   suffixes), so the model keeps its tool work across turns and the diff
   actually engages in production. This is what makes cache stability
   permanent instead of a tuning exercise.
3. **Tool exposure tiers** ✅ landed 2026-08-29. `RegisteredTool` carries
   `direct` / `deferred` / `hidden`; `describe_model()` lists only the
   direct tier while dispatch stays exposure-agnostic, so registration and
   exposure are orthogonal and the offered list stays bounded once tools
   are no longer authored in-house. The deferred tier is discoverable
   through the `tool_search` seam (`tool_search.py`): one direct-tier
   search tool over the deferred inventory, returning full schemas so a
   match is callable the next round. Hidden tools leak nowhere — not into
   search results, not into unknown-tool suggestions.
4. **MCP** ✅ landed 2026-08-29 (`mcp.py`), on the full foundation:
   per-tool timeouts (Wave 0), exposure tiers (item 3), plus the two rules
   the module owns — deterministic `mcp__<server>__<tool>` qualification
   (collisions impossible by construction; origin visible to model, trace,
   and policy) and per-server catalog caps that fail loud and atomically
   (never a half-registered or silently truncated catalog). Catalogs
   register deferred by default, so the model discovers MCP tools through
   `tool_search` instead of paying for every schema in every request.
   `McpStdioClient` (NDJSON JSON-RPC: initialize handshake, cursor-paginated
   `tools/list`, `tools/call`, per-request timeouts, method-not-found
   answers to server-initiated requests) serves hosts embedding the
   runtime directly; the desktop keeps the recorded architecture — servers
   launch host-side (Electron main) and arrive through
   `ToolRouter.register_remote`, whose invoker contract is identical to
   `register_mcp_catalog`'s.

#### The MCP ordering decision (resolved)

The 2026-07-28 MCP spec made the core stateless HTTP — no handshake, no
session id, self-describing requests — which retires the "sidecar becomes
a process supervisor" objection recorded in `CORELOOP_TODO.md`. The
ordering argument above held: MCP landed only after timeouts, exposure
tiers, catalog caps, and name qualification existed, so the largest
unbounded third-party context source arrived bounded, discoverable, and
cache-friendly from day one.

### Wave 3

1. **Approval algebra** ✅ landed 2026-08-29 (`approval.py`). The 8-variant
   `ApprovalKind` mirrors codex's `ReviewDecision` — allow/deny across
   request / session / durable scopes, with codex's policy-amendment variants
   generalized into the durable one. Enforcement is `ApprovalExecutor`, a
   `ToolExecutor` decorator, so the algebra stands in front of any dispatch
   path (router, host reverse channel, MCP) instead of living inside one
   registry; an allow verdict bridges into the router's `require_consent`
   gate via `ctx.consent_granted`. Deny variants return a failed
   `ToolResult` — the model sees `Denied{reason}` and the run continues —
   while `abort` raises `ApprovalAborted` and the loop ends the turn as
   failed after giving every tool_call in the batch a response (real results
   plus `loop.abort_skip` placeholders, no dangling calls). `timed_out`
   fails closed but keeps its variant name for observability. Session scope
   is a per-category `SessionApprovalCache`; durable scope is an
   `ApprovalStore` (`JsonApprovalStore` writes atomically) and wins over
   session. `AutoApprover` is the headless policy: per-category automatic
   allow/deny by tool mode, so a run with no human rejects instead of
   hanging. The sidecar wires it as `approval: {mode: "auto" | "host",
   timeoutMs, storePath}` on `chat.stream` — absent means no approval layer
   (legacy behavior); `host` mode asks the host UI over the reverse channel
   (`approval.request`) and fails closed when the host can't answer.
2. **Tool-execution sandbox** ✅ landed 2026-08-29 (`sandboxed.py` +
   `SeatbeltExecBackend` in the sidecar's `sandbox.py`), shell/subprocess
   only. `SandboxedToolExecutor` is a `ToolExecutor` decorator (the same
   seam as `ApprovalExecutor`): it rewrites a shell call's `command`
   argument into a sandboxed invocation and delegates, so it stands in
   front of any dispatch path — in the desktop deployment the rewritten
   command travels over the reverse channel and the host's shell spawns it
   confined, per-exec Seatbelt with zero sandbox mechanics in the host.
   The `SandboxBackend` protocol is pluggable (Seatbelt today, E2B-class
   remote sandboxes later); the Seatbelt backend reuses the layer-1 profile
   generator with tool-execution defaults (deny-by-default, no network
   unless declared, writes confined to declared roots plus system scratch)
   and carries the profile inline in the command string. Enforcement is a
   return value, not a log line (dsh's `SandboxEnforcement` lesson): the
   result's `data["_sandbox"]` marker records `{backend, enforcement}`
   (`full` / `partial` / `none`) in the transcript, and `require_full`
   denies a call before execution when the available enforcement is weaker
   than `full`. The sidecar wires it as `execSandbox: {enabled,
   writableRoots, network, allowedHosts, shell, tools, commandArg,
   requireFull}` on `chat.stream` — absent means unconfined (legacy
   behavior); the wrap order is base → sandbox → approval → subagent so
   the approver reviews the original command. Linux Landlock is the
   deliberate follow-up backend.
3. **AG-UI and ACP transports** ✅ landed 2026-08-29 (`ag_ui.py` +
   `acp_adapter.py` in the sidecar package). Both are peer transports over
   the unchanged LoopEvent taxonomy — the bespoke `stream.chunk` surface
   stays for DeepPath byte-compatibility. AG-UI: `AgUiRenderer` projects
   loop events onto the official `ag-ui-protocol` models (text/reasoning
   segments open and close around tool calls; results and errors ride
   TOOL_CALL_RESULT; framework observability events travel as lossless
   `steerable.*` CUSTOM events; completion maps to RUN_FINISHED/RUN_ERROR
   by status), with `encode_sse` rendering the canonical SSE bytes for the
   embedder's web tier. ACP: `SteerableAcpAgent` implements the stable
   `acp.Agent` core (initialize / new_session / prompt / cancel /
   close_session) on the official `agent-client-protocol` SDK, so any ACP
   editor drives a CoreLoop over stdio (`steerable-sidecar-acp`).
   Multi-turn reuses the loop's record-aware seeding — the adapter keeps
   only the host-view (user/assistant texts), the record projection
   restores tool rounds. Session loading/fork and the editor-terminal
   tool bridge are the recorded follow-ups.
4. A golden-trajectory eval gate reusing the existing `replay.py` fixtures.
   Public capability evals (Terminal-Bench 2.1 cheap-12 via Harbor
   `claude-code` / `codex` / `pi`) live in `evals/` and are a scheduled
   job, not a required merge check.

## Protocol positioning (decided)

The protocol and sidecar tiers are reinventing standards that consolidated
during 2026. AG-UI is first-party in Microsoft Agent Framework, Google
ADK, AWS Strands, Bedrock AgentCore, Mastra, and Pydantic AI. ACP —
JSON-RPC over stdio, editor↔agent — is precisely the sidecar's transport
and precisely its problem statement, with 25+ agents, JetBrains, Google,
GitHub, and an official Python SDK at stable v1.

**Decision: the planned `protocolVersion` 1.0.0 freeze of the bespoke
15-method sidecar surface is cancelled.** Freezing a bespoke surface as a
multi-vendor standard consolidates in the same slot is the wrong
direction. The [freeze scope proposed in the SSE drift
survey](migration/api-sse-drift.md#sidecar-protocol-v1-proposed-freeze-scope)
is superseded by the following.

1. **Fix the real concurrency bug.** Declare a serialization scope per
   RPC method (codex's `ClientRequestSerializationScope`,
   `codex-rs/app-server-protocol/src/protocol/common.rs:128-139`).
   `agent.chat.stream`, `steer`, `cancel`, and `fork` on one session have
   genuine ordering requirements; `docs/spec/sidecar.md:162-163` promises
   only ordering by JSON-RPC id, which is not an ordering guarantee. The
   dispatcher keys a per-scope lock and the table is testable without a
   server.
2. **Add AG-UI and ACP transports as peers** to the existing ones,
   keeping the bespoke `SSEEvent` path for DeepPath byte-compatibility.
   [The SSE drift survey](migration/api-sse-drift.md) already establishes
   that transports render wire formats; this is that rule applied
   outward. A second protocol consumer is also the only real test of
   whether the event taxonomy is genuinely transport-neutral.
3. **Reposition Tier 1's pitch** from "our envelope" to "the codegen
   conformance discipline, plus mapping into the ecosystem's envelopes".
   The discipline is defensible; the envelope is not.
4. **Adopt cursor pagination on list methods.** `trace.fetch` returning
   every event of a long session over a stdio pipe is a real hazard given
   there is no back-pressure (`docs/spec/sidecar.md:164-166`).

The spec drift listed under [what will age
badly](#what-will-age-badly) — `events.md` documenting variants the loop
does not emit, `sidecar.md` missing two live methods — is repaired as
part of this work rather than as part of a freeze.

## Explicitly out of scope

- **A Cordis-style plugin runtime.** The decorator-chained executors
  already give provider substitution in about 40 readable lines. Adopt
  the seam *discipline* — name the port, keep consumers off concrete
  providers — not the runtime.
- **Workflow orchestration.** DeepSeek Harness's own README lists no
  journaling, no resume, and foreground-only collection; it is the least
  finished seam there. Depth-1 delegation covers the case that ships.
- **Durable execution.** A desktop chat turn does not need Temporal.
  Revisit when a consumer asks; the prerequisite is idempotency keys on
  `ToolCall`, which is itself a Tier 1 pre-1.0 decision.
- **Further investment in heuristic token estimation** beyond the
  local-model case. Providers are absorbing it server-side.
- **Mandatory per-package prose sections.** DeepSeek Harness requires a
  "KV Cache effect" block in 60+ package READMEs, including ones whose
  honest answer is "None". The underlying idea — that a component should
  declare its effect on the cached prefix — is worth capturing for the
  two places it matters: system-prompt assembly and compaction.

## Related

- [Evals](evals.md) — Terminal-Bench 2.1 cheap-12 via Harbor
- [Framework Comparison](comparison.md) — where Steerable sits against the field
- [CoreLoop spec](spec/core-loop.md) — the loop and its event taxonomy
- [Safety spec](spec/safety.md) — the two-layer model this page critiques
- [Sidecar spec](spec/sidecar.md) — the JSON-RPC surface that is no longer being frozen
- [API SSE Drift Survey](migration/api-sse-drift.md) — the adoption-cost study whose freeze proposal this page supersedes

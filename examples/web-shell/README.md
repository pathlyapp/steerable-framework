# Steerable web-shell — reference app

The framework-owned reference application for `@steerable/agent-ui`. Bundles all
of the things that make the rest of the framework click together:

- the compound `ChatPanel` (header / messages / input / empty / streaming-status)
- the `ChatSessionProvider` (wave-4 context)
- all 14 rich cards from `@steerable/agent-ui/cards`
- canned fixtures sourced straight from `spec/blocks/fixtures/`
- a real sidecar transport (opt-in via env var)

## Run it

```bash
# default: fully offline, mock transport replays canned fixtures
pnpm --filter steerable-example-web-shell dev

# point at a real sidecar (start examples/sidecar-roundtrip first)
VITE_TRANSPORT=sidecar pnpm --filter steerable-example-web-shell dev

# optional: override the sidecar endpoint
VITE_TRANSPORT=sidecar VITE_SIDECAR_URL=http://127.0.0.1:5181/chat/stream pnpm --filter steerable-example-web-shell dev
```

The dev server listens on port `5180`. Static build outputs go to `dist/` and
are deployable as a flat folder (the framework's `docs.yml` GitHub Pages job
serves them at `/demo/`).

## Model selector

The input toolbar carries the framework's `ModelSelector`: a model picker
plus a per-model reasoning-effort picker, backed by the gateway catalog
contract (the sidecar's `models.list` RPC). In mock mode it serves a canned
listing; in sidecar mode it calls `GET {origin}/models` on the same origin
as `VITE_SIDECAR_URL`, expecting the host to proxy the `models.list` result
as `ModelCatalogResponse` JSON. If that endpoint is absent or unreachable
the selector shows a "catalog offline" badge and the composer keeps working
— the catalog is discovery, not a routing whitelist. The current selection
rides along on every send as `metadata.model` / `metadata.reasoningEffort`;
a host transport forwards them to `agent.chat.stream`, which validates an
unsupported effort strictly instead of silently dropping it.

## Card scenarios

Click any item in the left sidebar to see the matching rich card render, or
just type one of these keywords (case-insensitive, English or Chinese works):

| keyword | card |
| ------- | ---- |
| `编排` / `orchestration` | `OrchestrationPlanCard` |
| `测验` / `quiz` | `QuizCard` |
| `覆盖` / `coverage` | `CoverageReportCard` |
| `分析` / `analysis` | `AnalysisDocumentCard` |
| `研究` / `research` | `ResearchPlanCard` |
| `建议` / `suggest` | `SuggestedRepliesCard` |
| `问我` / `ask` | `AskUserQuestionsCard` |
| `思考` / `think` | `ThinkingProcessCard` |
| `步骤` / `steps` | `PlanStepsCard` |
| `方案` / `plans` | `PlanSelectorCard` |
| `来源` / `sources` | `SearchSourcesCard` |
| `摘要` / `summary` | `SummaryMessageCard` |
| `操作` / `actions` | `ActionSegmentCard` |
| `工具` / `tool` | `ToolExecutionCard` |

Every payload is the verbatim JSON the Ajv conformance test
(`tests/conformance/ts/blocks.test.ts`) validates against the schemas in
`spec/blocks/`. Schemas evolve, fixtures evolve, the web-shell automatically
picks up the new shapes on the next build.

## How does this stay independent?

- The mock transport is a thin wrapper around the framework's own
  `MockChatStreamTransport`. No backend service is required.
- The sidecar mode uses `SSEParser` + `bridgeLegacySSE` from the framework
  (same code that powers deeppath / deeppath-agent), pointed at any HTTP
  endpoint that streams SSE in the standard `data: {event}` framing.

So the assertion in the project's Definition-of-Done holds:

> `git clone steerable-framework && pnpm install && pnpm --filter
> steerable-example-web-shell dev` opens a working chat UI with no external
> services.

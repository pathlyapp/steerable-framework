# Steerable Framework Docs

Steerable is a spec-first agent framework extracted from the DeepPath ecosystem.
Its core design is:

- `spec/` is the source of truth for cross-language contracts.
- TypeScript and Python models are generated from JSON Schema.
- Harness utilities provide small, composable runtime helpers.

## What is in this docs milestone

- Protocol spec overview and key model pages (`events`, `tools`, `chat`, `safety`)
- A practical DeepPath migration note
- Minimal usage examples for TypeScript and Python in `examples/`

## Repo quickstart

```bash
pnpm install
uv sync
pnpm gen
pnpm test
```

## Package map

- `@steerable/agent-protocol` and `steerable-agent-protocol`
- `@steerable/agent-harness` and `steerable-agent-harness`

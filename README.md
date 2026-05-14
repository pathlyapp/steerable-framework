# Steerable Framework

Open-source agent framework extracted from DeepPath ecosystem.

## Monorepo packages

- `@steerable/agent-protocol` (TypeScript)
- `steerable-agent-protocol` (Python)
- `@steerable/agent-harness` (TypeScript)
- `steerable-agent-harness` (Python)

## Principles

- Spec-first: all cross-language contracts come from `spec/`.
- Lock-step release for protocol and harness npm/PyPI packages.
- Conformance tests verify TypeScript and Python behavior parity.

## Development

```bash
pnpm install
uv sync
pnpm gen
pnpm test
```

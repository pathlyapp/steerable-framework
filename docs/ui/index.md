---
title: UI Components
---

# UI Components — Storybook

`@steerable/agent-ui` ships its full component reference and hook documentation as a live Storybook bundled alongside this site.

<a href="../storybook/index.html" target="_blank" class="md-button md-button--primary">Open the Storybook ↗</a>

What you'll find inside:

* **Components** — `ChatPanel`, `MessageList`, `OrchestrationPlanCard`, `ToolCallRenderer`, `SSEStreamView`, with one story per state (default, streaming, error, edge cases).
* **Hooks** — `useChatStream`, `useToolCallStatus`, `useAgentSession` with copy-paste TypeScript examples and design notes.
* **a11y panel** — every story is auto-checked by axe-core; baseline-failures block PRs.
* **Tailwind preset reference** — every `--agent-*` design token, what it controls, and how to override it in your own consumer app.

## How it stays in sync

* Stories live next to the components they document (`packages/agent-ui/ts/src/components/*.stories.tsx`).
* The Storybook static bundle is rebuilt by [`.github/workflows/docs.yml`](https://github.com/steerable-org/steerable-framework/blob/main/.github/workflows/docs.yml) on every push to `main` and embedded under `/storybook/` of this docs site.
* Visual snapshots + a11y checks run on every PR via [`.github/workflows/storybook-quality.yml`](https://github.com/steerable-org/steerable-framework/blob/main/.github/workflows/storybook-quality.yml).

## Working on it locally

```bash
cd packages/agent-ui/ts
pnpm storybook         # http://localhost:6006

# A11y + mount smoke tests
pnpm storybook:test

# Visual regression (uses a fresh static build + headless chromium)
pnpm storybook:build
pnpm storybook:vrt

# Refresh local (darwin) baselines after intentional UI changes:
pnpm storybook:vrt:update

# Refresh CI (linux) baselines via Docker:
../../scripts/generate-vrt-baselines-linux.sh
```

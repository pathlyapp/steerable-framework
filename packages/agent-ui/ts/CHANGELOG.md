# @steerable/agent-ui

All notable changes to this package will be documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the package adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0](https://github.com/pathlyapp/steerable-framework/compare/agent-ui-v0.1.0...agent-ui-v0.2.0) (2026-05-15)


### Features

* add json-schema-to-typescript dependency and enhance agent protocol models ([7c20f4f](https://github.com/pathlyapp/steerable-framework/commit/7c20f4fea4f777583789c867653c8e9e5524d266))
* integrate Storybook for UI components and enhance documentation ([512eaed](https://github.com/pathlyapp/steerable-framework/commit/512eaed453d7e8da67138a917212b126d3823804))
* **release:** wire autonomous publish pipeline (npm + PyPI) ([d245304](https://github.com/pathlyapp/steerable-framework/commit/d24530439ae56dbdc8f0d4c27fc58136117b2d06))

## [0.1.0] — 2026-05-14

### Added

* Initial public release.
* Tailwind preset (`@steerable/agent-ui/tailwind-preset`) exposing the
  `--agent-*` design tokens used across `deeppath/apps/web`.
* Headless React hooks:
  * `useChatStream` — owns the in-flight chat lifecycle, reduces SSE events
    onto a `ChatMessage[]`. Supports content deltas, tool calls + results,
    inline error / budget-exhausted overlays, and an `onUnknownEvent` escape
    hatch for `loader-hint` / `agent` / `orchestration` events.
  * `useToolCallStatus` — derives `(status, mode, requiresApproval,
    isDestructive)` from a `(ToolCall, ToolResult?)` pair, mirroring the
    framework's `decide_tool_mode` taxonomy.
  * `useAgentSession` — transport-agnostic wrapper around create / resume /
    list with a value-keyed effect so inline `autoLoad` literals don't loop.
* Headless React components (rendered via Tailwind class strings only):
  * `MessageList` — auto-scrolling list with a `renderMessage` slot.
  * `ChatPanel` — minimal compose shell (header slot + list + input + stop).
  * `OrchestrationPlanCard` — multi-step plan with status icons + per-step
    click handler.
  * `ToolCallRenderer` — collapsible card with mode pill, status dot,
    approve / reject row for `local` tools, and overridable args / result
    sub-renderers.
  * `SSEStreamView` — debug log for raw SSE events with type filtering and
    optional verbose JSON pretty-print.
* 44 vitest tests covering the hooks (event reducer, mode inference,
  cancellation, autoLoad de-dup) and component render contracts.

### Deferred

* Storybook (moved to 0.2.0 / P8 docs site).
* Multi-agent group chat shell (`ChatTabs`, `AgentManagementModal`,
  `ChatAgentPill`), slash command palette, automation alerts, file upload,
  share-image modal, and the action renderer family — these exist in
  `deeppath/apps/web` today and will be ported once the foundational set is
  consumed in production by P7.

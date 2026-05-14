# @steerable/agent-ui

> Headless React components + hooks + Tailwind preset for building agent UIs on
> top of `@steerable/agent-protocol`. Drop-in compatible with `deeppath/apps/web`'s
> ChatPanel architecture; designed to be reused by `deeppath-agent` (Electron)
> after sidecar wiring lands.

## Install

```bash
pnpm add @steerable/agent-ui @steerable/agent-protocol react react-dom
```

`@steerable/agent-ui` ships only **types + hooks + headless components**. UI
styling is delegated to your Tailwind config via the `@steerable/agent-ui`
preset.

## Tailwind preset

```ts
// tailwind.config.ts
import preset from '@steerable/agent-ui/tailwind-preset';

export default {
  presets: [preset],
  content: [
    './src/**/*.{ts,tsx}',
    './node_modules/@steerable/agent-ui/dist/**/*.js',
  ],
};
```

The preset exposes design tokens (`bg-agent-canvas`, `text-agent-foreground`,
`border-agent-border`, …) that map to CSS variables. Override them in your own
`globals.css` to retheme without forking components.

## Architecture

```
@steerable/agent-protocol  (types: SSEEvent, ChatMessage, ToolCall, ...)
        ▲
        │ types-only
        │
@steerable/agent-ui
  ├─ hooks/           ← framework-agnostic state + side-effects
  │   ├─ useAgentSession   create / resume / list sessions
  │   ├─ useChatStream     consume an SSE stream of events into ChatMessage[]
  │   └─ useToolCallStatus tool call lifecycle (proposed → running → done)
  │
  ├─ components/      ← headless React (Tailwind class names only)
  │   ├─ MessageList            virtualized + auto-scroll list of ChatMessages
  │   ├─ ChatPanel              orchestrates header / list / input
  │   ├─ OrchestrationPlanCard  multi-step plan with progress
  │   ├─ ToolCallRenderer       safe (read) / proposed (write) / running / done
  │   └─ SSEStreamView          live JSON event log (debug + power-user)
  │
  └─ tailwind-preset.ts  ← shared design tokens
```

Each hook returns plain data; each component takes plain props. Nothing imports
React Router, Next.js, or any specific HTTP client — the consumer wires the
actual transport (fetch / SSE / IPC / sidecar).

## Status (0.1.0)

This is the **foundation release** of the framework UI. It ships:

* ✅ Tailwind preset with the design tokens used by `deeppath/apps/web`.
* ✅ Three foundational hooks listed in the architecture diagram.
* ✅ Five core components listed in the architecture diagram (intentionally
  the minimum for a usable chat UI). Each is fully type-safe against
  `@steerable/agent-protocol`.
* ✅ Vitest test suite covering hooks + render snapshots.
* ⏸ Storybook is **deferred** to 0.2.0 / P8 docs (per project decision).
* ⏸ Multi-agent group chat (ChatTabs / AgentManagementModal), slash command
  palette, automation alerts, file upload, share-image modal, etc. are tracked
  for 0.2.0 / 0.3.0 — they exist in `deeppath/apps/web` today and will be
  ported once the foundational set is consumed in production by P7.

## Usage example

```tsx
import {
  ChatPanel,
  MessageList,
  ToolCallRenderer,
  useChatStream,
} from '@steerable/agent-ui';
import type { SSEEvent } from '@steerable/agent-protocol';

function MyChat({ chatId }: { chatId: string }) {
  const { messages, isStreaming, sendUserMessage } = useChatStream({
    transport: {
      stream: async (input, onEvent) => {
        const res = await fetch('/api/v2/chats/' + chatId + '/stream', {
          method: 'POST',
          body: JSON.stringify(input),
        });
        const reader = res.body!.getReader();
        // … decode + dispatch onEvent(SSEEvent) …
      },
    },
  });

  return (
    <ChatPanel
      messages={messages}
      isStreaming={isStreaming}
      onSubmit={sendUserMessage}
      renderToolCall={(call) => <ToolCallRenderer call={call} />}
    />
  );
}
```

See `apps/web/src/app/goals/desktop/components/ChatPanel/ChatPanel.tsx` in the
`deeppath` repo for the production-grade caller (which P7 will rewrite to
import these primitives).

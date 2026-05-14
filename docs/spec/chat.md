# Chat Spec

The chat layer defines the message exchange and agent metadata used by
both the protocol-level wire stream and the UI's `ChatMessage[]` state.

## ChatMessage

| Field         | Type                        | Required | Notes                                                  |
| ------------- | --------------------------- | -------- | ------------------------------------------------------ |
| `id`          | `string`                    | yes      | Stable identity across re-renders                      |
| `role`        | `'user' \| 'assistant' \| 'tool' \| 'system'` | yes | UI-rendered role                                       |
| `content`     | `string`                    | yes      | Plain-text body (UI may render as Markdown)            |
| `createdAt`   | `string` (ISO 8601)         | yes      | UTC timestamp                                          |
| `chatId`      | `string`                    | no       | Owning chat                                            |
| `agentId`     | `string`                    | no       | Owning agent (multi-agent mode)                        |
| `toolCalls`   | `ToolCall[]`                | no       | Calls emitted by the assistant                         |
| `toolResult`  | `ToolResult`                | no       | Result attached inline (or use a separate `tool` row)  |
| `updatedAt`   | `string` (ISO 8601)         | no       | Last edit time                                         |

`additionalProperties` is **enabled**. UI consumers (e.g.
`deeppath/apps/web`) layer additional render-only fields on top via
intersection types — those don't go on the wire.

## ChatAgent

| Field           | Type                       | Required | Notes                                       |
| --------------- | -------------------------- | -------- | ------------------------------------------- |
| `id`            | `string`                   | yes      |                                             |
| `name`          | `string`                   | yes      | Display name                                |
| `createdAt`     | `string`                   | yes      |                                             |
| `updatedAt`     | `string`                   | yes      |                                             |
| `rolePrompt`    | `string`                   | no       | System-level role prompt                    |
| `forbiddenPrompt` | `string`                 | no       | Negative constraints                        |
| `skillIds`      | `string[]`                 | no       | Tool / skill allowlist                      |
| `allowExternalSkills` | `boolean`            | no       | Permit any registered skill                 |
| `isBuiltin`     | `boolean`                  | no       | Hidden from user-facing agent picker        |
| `isArchived`    | `boolean`                  | no       |                                             |
| `sortOrder`     | `number`                   | no       |                                             |
| `icon`          | `string`                   | no       | Emoji or icon URL                           |
| `color`         | `string`                   | no       | Hex color for the agent badge               |

The schema is intentionally broad — products layer their own routing,
billing, and access control on top via the `metadata` escape hatch (open
for forward-compatibility).

## Conversational ownership

Every `ChatMessage` belongs to **at most one** `ChatAgent`. In a
multi-agent orchestration run, the `agent` SSE event flips the
"currently speaking" agent identity, and downstream `content` events
inherit that ownership until the next `agent` event.

## Variants (UI extension)

The framework's protocol intentionally does **not** define
`activeVariantId` / `variantsCount` — those are UI-layer concepts
(ChatGPT-style "regenerate" branches) carried in
`@steerable/agent-ui`'s extended message type. They live in your
`ChatMessage` extension, not on the wire.

## Example exchange

```json
[
  {"id":"m1","role":"user","content":"Find me a Mars mission timeline","createdAt":"2026-05-14T10:00:00Z"},
  {"id":"m2","role":"assistant","agentId":"researcher","content":"Sure — searching…","createdAt":"2026-05-14T10:00:01Z",
   "toolCalls":[{"id":"c1","name":"web_search","arguments":{"q":"Mars mission timeline"}}],
   "toolResult":{"success":true,"data":{"results":[…]}}},
  {"id":"m3","role":"assistant","agentId":"researcher","content":"Here's a 2024-2030 plan: …","createdAt":"2026-05-14T10:00:05Z"}
]
```

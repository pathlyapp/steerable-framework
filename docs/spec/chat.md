# Chat Spec

The chat model layer defines message exchange and agent metadata.

## ChatMessage

Required:

- `id`
- `role` (`user`, `assistant`, `tool`, `system`)
- `content`
- `createdAt` (date-time)

Optional:

- `chatId`, `agentId`
- `toolCalls: ToolCall[]`
- `toolResult: ToolResult`
- `updatedAt` (date-time)

This lets one message carry both plain text and structured tool execution context.

## ChatAgent

Required:

- `id`, `name`, `createdAt`, `updatedAt`

Notable optional fields:

- prompt controls: `rolePrompt`, `forbiddenPrompt`
- capability controls: `skillIds`, `allowExternalSkills`
- lifecycle/UI metadata: `isBuiltin`, `isArchived`, `sortOrder`, `icon`, `color`

The model is intentionally broad so products can layer richer routing and policy.

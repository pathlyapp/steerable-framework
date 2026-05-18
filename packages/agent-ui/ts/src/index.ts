/**
 * @steerable/agent-ui
 *
 * Entry point. Re-exports the public hooks + components surface.
 *
 * The Tailwind preset is exposed via the `./tailwind-preset` subpath, not from
 * the root, so consumers don't pull a build-time dependency into their app
 * runtime bundle. See `tsconfig.json` `exports` map.
 */

export * from './hooks/index.js';
export * from './components/index.js';

// `state/` is the home for chat-session primitives (hooks + transports +
// SSE parsing) lifted out of deeppath / deeppath-agent. The package root
// re-exports the public surface for convenience; the `./state` subpath is the
// canonical entrypoint for tree-shaking.
export {
  SSEParser,
  parseSSEData,
  bridgeLegacySSE,
  MockChatStreamTransport,
  useChatComposer,
  useChatList,
  useChatSession,
  useToolCallStream,
  useScrollLock,
  ChatSessionProvider,
  useChatSessionContext,
  useOptionalChatSession,
  useChatSessionSlice,
} from './state/index.js';
export type {
  SSEFrame,
  SSEParserOptions,
  EnvelopeProfile,
  BridgeLegacySSEOptions,
  MockScript,
  MockScriptStep,
  MockChatStreamTransportOptions,
  UseChatComposerOptions,
  UseChatComposerReturn,
  ChatListTransport,
  UseChatListOptions,
  UseChatListReturn,
  UseChatSessionOptions,
  UseChatSessionReturn,
  ToolCallEntry,
  UseToolCallStreamOptions,
  UseToolCallStreamReturn,
  UseScrollLockOptions,
  UseScrollLockReturn,
  ChatSessionContextValue,
  ChatSessionProviderProps,
} from './state/index.js';

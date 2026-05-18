/**
 * `@steerable/agent-ui/state` — chat-state primitives lifted out of deeppath
 * and deeppath-agent. Composes with the existing `hooks/` exports.
 *
 * Subpath import recommended:
 *
 *   import { useChatSession, MockChatStreamTransport } from '@steerable/agent-ui/state';
 *
 * but the same identifiers are also re-exported from the package root for
 * back-compat.
 */

export { SSEParser, parseSSEData } from './parseSSE.js';
export type { SSEFrame, SSEParserOptions } from './parseSSE.js';

export { bridgeLegacySSE } from './bridgeLegacySSE.js';
export type { EnvelopeProfile, BridgeLegacySSEOptions } from './bridgeLegacySSE.js';

export { MockChatStreamTransport } from './MockChatStreamTransport.js';
export type {
  MockScript,
  MockScriptStep,
  MockChatStreamTransportOptions,
} from './MockChatStreamTransport.js';

export { useChatComposer } from './useChatComposer.js';
export type {
  UseChatComposerOptions,
  UseChatComposerReturn,
} from './useChatComposer.js';

export { useChatList } from './useChatList.js';
export type {
  ChatListTransport,
  UseChatListOptions,
  UseChatListReturn,
} from './useChatList.js';

export { useChatSession } from './useChatSession.js';
export type {
  UseChatSessionOptions,
  UseChatSessionReturn,
} from './useChatSession.js';

export {
  ChatSessionProvider,
  useChatSessionContext,
  useOptionalChatSession,
  useChatSessionSlice,
} from './ChatSessionProvider.js';
export type {
  ChatSessionContextValue,
  ChatSessionProviderProps,
} from './ChatSessionProvider.js';

export {
  useToolCallStream,
  useToolCallStatus,
} from './useToolCallStream.js';
export type {
  ToolCallEntry,
  UseToolCallStreamOptions,
  UseToolCallStreamReturn,
} from './useToolCallStream.js';

export { useScrollLock } from './useScrollLock.js';
export type {
  UseScrollLockOptions,
  UseScrollLockReturn,
} from './useScrollLock.js';

// Re-export the canonical chat-stream surface so callers can stay on a single
// import root if they don't want to think about subpaths.
export {
  useChatStream,
} from '../hooks/useChatStream.js';
export type {
  UseChatStreamOptions,
  UseChatStreamReturn,
  ChatStreamSendInput,
  ChatStreamTransport,
} from '../hooks/useChatStream.js';

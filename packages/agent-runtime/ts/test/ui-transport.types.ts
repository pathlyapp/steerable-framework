/**
 * Compile-time gate for 3.2.3: the transport adapters must be directly
 * consumable by `@steerable/agent-ui` hooks. The runtime package declares
 * its own structural copies of the hook interfaces (layering forbids a
 * runtime→UI dependency); this file asserts assignability in both
 * directions so the copies cannot drift from the real ones.
 *
 * Gated by `pnpm lint` (tsconfig.test.json includes test/**), not vitest —
 * type relations only exist at compile time.
 */
import type {
  AgentSessionTransport as UiAgentSessionTransport,
  ChatStreamTransport as UiChatStreamTransport,
} from '@steerable/agent-ui';
import type {
  AgentSessionTransport,
  ChatStreamTransport,
} from '../src/transports.js';

type AssertAssignable<T extends U, U> = true;

// Runtime → UI: an adapter built by this package satisfies the hooks.
type _ChatForward = AssertAssignable<ChatStreamTransport, UiChatStreamTransport>;
type _SessionForward = AssertAssignable<AgentSessionTransport, UiAgentSessionTransport>;

// UI → Runtime: a hand-rolled UI-conformant transport also satisfies ours
// (guards against our copies being *stricter* than the hooks).
type _ChatBackward = AssertAssignable<UiChatStreamTransport, ChatStreamTransport>;
type _SessionBackward = AssertAssignable<UiAgentSessionTransport, AgentSessionTransport>;

export const typeGate: [
  _ChatForward,
  _SessionForward,
  _ChatBackward,
  _SessionBackward,
] = [true, true, true, true];

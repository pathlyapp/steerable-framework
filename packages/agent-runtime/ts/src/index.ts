/**
 * `@steerable/agent-runtime` — the official TypeScript production runtime
 * for the Steerable framework. Owns the embedded Python sidecar lifecycle
 * and exposes the CoreLoop-level API; see `docs/spec/architecture.md`
 * ("Official TS production entry") and `docs/spec/sidecar.md`.
 */

export {
  JsonRpcPeer,
  JsonRpcRemoteError,
  JsonRpcTransportClosedError,
} from './jsonrpc.js';
export {
  SidecarNotReadyError,
  SidecarProcess,
  SidecarStartError,
  type SidecarProcessOptions,
  type SidecarReadyInfo,
  type SidecarRestartPolicy,
} from './sidecar.js';
export {
  SIDECAR_METHODS,
  SIDECAR_NOTIFICATIONS,
  type SidecarMethod,
  type SidecarNotification,
} from './methods.js';
export {
  AgentRuntime,
  type AgentRuntimeOptions,
  type ApplyEditsResult,
  type BranchFamily,
  type BranchPoint,
  type ChatStreamHandle,
  type ChatStreamParams,
  type ChatStreamStatus,
  type SkillInfo,
  type ToolDescriptor,
  type TraceExportResult,
  type TraceFetchResult,
} from './runtime.js';
export {
  createChatStreamTransport,
  createSessionTransport,
  type AgentSessionTransport,
  type ChatStreamSendInput,
  type ChatStreamTransport,
  type ChatStreamTransportOptions,
} from './transports.js';

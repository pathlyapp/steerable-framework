/**
 * Public API for the steerable-sidecar bridge.
 *
 * Usage from main.ts:
 *
 *   import { SidecarSupervisor } from './sidecar';
 *   const sidecar = await SidecarSupervisor.start();
 *   await sidecar.ping();
 *   const result = await sidecar.invokeTool('list_events', { limit: 10 });
 *   await sidecar.shutdown();
 */
export { SidecarSupervisor } from './supervisor.js';
export {
  SidecarBootError,
  SidecarMethodError,
  SidecarShutdownError,
  SidecarSandboxUnavailableError,
} from './errors.js';
export type {
  SidecarChatStreamHandlers,
  SidecarChatStreamRequest,
  SidecarChildEvent,
  SidecarHealthSnapshot,
  SidecarMethodOptions,
  SidecarRawChunk,
  SidecarSandboxPosture,
  SidecarSessionForkResult,
  SidecarStartOptions,
  SidecarStreamChunk,
  SidecarStreamDone,
  SidecarStreamError,
  SidecarToolResult,
} from './types.js';

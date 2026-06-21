/**
 * Public API for the steerable-sidecar bridge.
 *
 * Usage:
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
} from './errors.js';
export type {
  SidecarChatStreamHandlers,
  SidecarChatStreamRequest,
  SidecarHealthSnapshot,
  SidecarMethodOptions,
  SidecarStartOptions,
  SidecarStreamChunk,
  SidecarStreamDone,
  SidecarStreamError,
  SidecarToolResult,
} from './types.js';

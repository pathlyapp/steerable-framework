/**
 * JSON-RPC 2.0 notification frame (no id, no response). Used by the sidecar to push streaming SSE chunks, lifecycle events, and trace updates back to the host.
 */
export interface SidecarNotification {
  jsonrpc: "2.0";
  /**
   * Notification namespace. Reserved: 'stream.chunk', 'stream.done', 'lifecycle.ready', 'lifecycle.shutdown', 'trace.event', 'log.line'.
   */
  method: string;
  params?:
    | {
        [k: string]: any;
      }
    | any[]
    | null;
}

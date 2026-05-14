/**
 * JSON-RPC 2.0 request frame for the steerable-sidecar stdio transport.
 */
export interface SidecarRequest {
  jsonrpc: "2.0";
  /**
   * Request id; null is reserved for notifications.
   */
  id: string | number | null;
  /**
   * Method name. Reserved namespaces: 'system.*' (lifecycle), 'agent.*' (sessions/turns), 'tool.*' (tool router), 'stream.*' (incremental SSE chunks).
   */
  method: string;
  /**
   * Method-specific arguments. Object form preferred.
   */
  params?:
    | {
        [k: string]: any;
      }
    | any[]
    | null;
}

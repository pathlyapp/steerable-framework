export interface SidecarNotification {
  jsonrpc: "2.0";
  method: string;
  params?: Record<string, unknown> | unknown[] | null;
  [k: string]: unknown;
}

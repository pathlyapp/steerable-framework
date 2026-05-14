export interface SidecarRequest {
  jsonrpc: "2.0";
  id: string | number | null;
  method: string;
  params?: Record<string, unknown> | unknown[] | null;
  [k: string]: unknown;
}

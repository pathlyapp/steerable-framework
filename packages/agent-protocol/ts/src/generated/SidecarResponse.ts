import type { SidecarError } from "./SidecarError";

export interface SidecarResponse<T = unknown> {
  jsonrpc: "2.0";
  id: string | number | null;
  result?: T;
  error?: SidecarError;
  [k: string]: unknown;
}

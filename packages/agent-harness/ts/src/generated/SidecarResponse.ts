import type { SidecarError } from "./SidecarError.js";
/**
 * JSON-RPC 2.0 response frame. Exactly one of `result` or `error` MUST be present.
 */
export interface SidecarResponse {
  jsonrpc: "2.0";
  id: string | number | null;
  /**
   * Method-specific success payload.
   */
  result?: {
    [k: string]: any;
  };
  error?: SidecarError;
}

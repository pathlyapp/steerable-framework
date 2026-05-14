/**
 * JSON-RPC 2.0 error object with steerable-specific extensions.
 */
export interface SidecarError {
  /**
   * Numeric code. Reuses JSON-RPC reserved codes: -32700 ParseError, -32600 InvalidRequest, -32601 MethodNotFound, -32602 InvalidParams, -32603 InternalError. Steerable extension range -32099..-32000.
   */
  code: number;
  message: string;
  /**
   * Optional structured details (e.g. budget snapshot, tool stack trace, retry advice).
   */
  data?:
    | {
        [k: string]: any;
      }
    | any[]
    | string
    | number
    | boolean
    | null;
  /**
   * Steerable-specific failure category, parallel to numeric `code`.
   */
  kind?:
    | "parse"
    | "invalid_request"
    | "method_not_found"
    | "invalid_params"
    | "internal"
    | "budget_exhausted"
    | "policy_denied"
    | "tool_failed"
    | "transport_closed"
    | "timeout"
    | "cancelled";
}

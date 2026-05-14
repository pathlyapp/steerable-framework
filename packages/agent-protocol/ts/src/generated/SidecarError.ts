export type SidecarErrorKind =
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

export interface SidecarError {
  code: number;
  message: string;
  data?: unknown;
  kind?: SidecarErrorKind;
  [k: string]: unknown;
}

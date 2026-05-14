export type SidecarHealthStatus = "ok" | "degraded" | "starting" | "shutting_down";

export interface SidecarHealthCheck {
  status: "ok" | "warn" | "error";
  message?: string;
  latencyMs?: number;
  [k: string]: unknown;
}

export interface SidecarHealth {
  status: SidecarHealthStatus;
  version: string;
  protocolVersion?: string;
  uptimeMs: number;
  pid?: number;
  pythonVersion?: string;
  platform?: string;
  loadedProviders?: string[];
  loadedTools?: number;
  activeTraces?: number;
  checks?: Record<string, SidecarHealthCheck>;
  [k: string]: unknown;
}

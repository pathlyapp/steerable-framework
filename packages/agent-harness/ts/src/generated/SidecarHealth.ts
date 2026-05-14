/**
 * Result payload for the `system.ping` health-check method.
 */
export interface SidecarHealth {
  status: "ok" | "degraded" | "starting" | "shutting_down";
  /**
   * Sidecar package version (steerable-agent-runtime).
   */
  version: string;
  /**
   * Highest spec/sidecar protocol version this sidecar speaks (semver).
   */
  protocolVersion?: string;
  uptimeMs: number;
  pid?: number;
  pythonVersion?: string;
  /**
   * OS/arch tag (e.g. 'darwin-arm64', 'linux-x64', 'win32-x64').
   */
  platform?: string;
  /**
   * LLM provider ids currently registered.
   */
  loadedProviders?: string[];
  loadedTools?: number;
  activeTraces?: number;
  /**
   * Per-subsystem health snapshot.
   */
  checks?: {
    [k: string]: {
      status: "ok" | "warn" | "error";
      message?: string;
      latencyMs?: number;
    };
  };
}

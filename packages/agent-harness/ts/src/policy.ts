export type ToolMode = "read" | "safe_write" | "destructive" | "other";

export interface PolicyDecision {
  allowed: boolean;
  toolMode: ToolMode;
  reason: string;
}

// Side-effect-free network reads the prefix rules cannot reach. Exact
// names, not a "web_" prefix: a future web_deploy-style tool must not
// inherit the read posture. Kept in lockstep with the Python classifier
// (conformance case: tests/conformance/cases/policy/decide_tool_mode.yaml).
const READ_EXACT = new Set(["web_search", "web_fetch"]);

export function decideToolMode(toolName: string): ToolMode {
  const normalized = toolName.toLowerCase();
  if (READ_EXACT.has(normalized)) {
    return "read";
  }
  if (normalized.startsWith("get_") || normalized.startsWith("list_") || normalized.startsWith("read_")) {
    return "read";
  }
  if (
    normalized.startsWith("create_") ||
    normalized.startsWith("update_") ||
    normalized.startsWith("set_") ||
    normalized.startsWith("write_") ||
    normalized.startsWith("apply_")
  ) {
    return "safe_write";
  }
  if (
    normalized.startsWith("delete_") ||
    normalized.startsWith("drop_") ||
    normalized.startsWith("remove_") ||
    normalized.startsWith("destroy_")
  ) {
    return "destructive";
  }
  return "other";
}

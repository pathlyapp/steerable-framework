export type ToolMode = "read" | "safe_write" | "destructive" | "other";

export interface PolicyDecision {
  allowed: boolean;
  toolMode: ToolMode;
  reason: string;
}

export function decideToolMode(toolName: string): ToolMode {
  const normalized = toolName.toLowerCase();
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

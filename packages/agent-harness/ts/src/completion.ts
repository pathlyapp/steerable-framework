export function isTerminalResult(result: Record<string, unknown> | null | undefined): boolean {
  if (!result) return false;
  if (result.terminal === true) return true;
  if (result.success === false && result.needsFollowup !== true) return true;
  return false;
}

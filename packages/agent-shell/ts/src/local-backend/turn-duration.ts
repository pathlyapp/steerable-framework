/** Wall-clock of a CoreLoop turn from `turn_active.startedAt` to persist time. */
export function turnDurationMs(
  startedAtIso: string | undefined | null,
  endedAtMs: number,
): number | null {
  if (!startedAtIso) return null;
  const started = Date.parse(startedAtIso);
  if (!Number.isFinite(started)) return null;
  return Math.max(0, endedAtMs - started);
}

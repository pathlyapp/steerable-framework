/**
 * Codex-style compact elapsed time for the turn-process disclosure.
 * Examples: 0s, 12s, 1m 00s, 1m 23s, 1h 02m 03s.
 */

export function formatElapsedCompact(elapsedMs: number): string {
  const elapsedSecs = Math.max(0, Math.floor(elapsedMs / 1000));
  if (elapsedSecs < 60) return `${elapsedSecs}s`;
  if (elapsedSecs < 3600) {
    const minutes = Math.floor(elapsedSecs / 60);
    const seconds = elapsedSecs % 60;
    return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
  }
  const hours = Math.floor(elapsedSecs / 3600);
  const minutes = Math.floor((elapsedSecs % 3600) / 60);
  const seconds = elapsedSecs % 60;
  return `${hours}h ${String(minutes).padStart(2, '0')}m ${String(seconds).padStart(2, '0')}s`;
}

export function readPersistedDurationMs(
  metadataJson: string | null | undefined,
): number | undefined {
  if (!metadataJson) return undefined;
  try {
    const parsed = JSON.parse(metadataJson) as { durationMs?: unknown };
    if (typeof parsed.durationMs !== 'number' || !Number.isFinite(parsed.durationMs)) {
      return undefined;
    }
    return parsed.durationMs >= 0 ? parsed.durationMs : undefined;
  } catch {
    return undefined;
  }
}

/** Wall-clock from a user prompt timestamp to the assistant reply timestamp. */
export function inferDurationMs(
  startIso: string | undefined,
  endIso: string | undefined,
): number | undefined {
  if (!startIso || !endIso) return undefined;
  const start = Date.parse(startIso);
  const end = Date.parse(endIso);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return undefined;
  return end - start;
}

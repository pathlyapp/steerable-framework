/**
 * Optional sidecar handle. `main.ts` calls `setSidecarSupervisor()` after the
 * sidecar has booted. The sidecar path is default-on (2026-08-26): the
 * CoreLoop chat path and the LLM provider both route through it. Set
 * `STEERABLE_USE_SIDECAR=0` to fall back to the in-process providers — also
 * the automatic behavior when the sidecar failed to start.
 *
 * Kept in a dependency-light module (type-only import of the supervisor) so
 * RPC thin clients (`local-edit`, `skill-loader`) can read the handle without
 * pulling the LLM-service → storage → Electron graph into pure-Node test
 * contexts.
 */
import type { SidecarSupervisor } from './index.js';

let sidecarSupervisor: SidecarSupervisor | null = null;
let pendingSupervisor: Promise<SidecarSupervisor | null> | null = null;

export function setSidecarSupervisor(supervisor: SidecarSupervisor | null): void {
  sidecarSupervisor = supervisor;
}

/**
 * Register the in-flight boot promise so early callers (e.g. the renderer's
 * skills request racing sidecar boot) can await readiness instead of
 * observing a bare `null` and degrading. `main.ts` registers this
 * synchronously inside `maybeStartSidecar()`, before the first await.
 */
export function setSidecarSupervisorPending(
  pending: Promise<SidecarSupervisor | null> | null,
): void {
  pendingSupervisor = pending;
}

/** The supervised sidecar handle, when the sidecar path is active. */
export function getSidecarSupervisor(): SidecarSupervisor | null {
  return isSidecarEnabled() ? sidecarSupervisor : null;
}

export function isSidecarEnabled(): boolean {
  return process.env.STEERABLE_USE_SIDECAR !== '0' && sidecarSupervisor != null;
}

/**
 * Await the sidecar handle through boot: returns the live handle immediately
 * when already up, otherwise waits on the registered boot promise. Returns
 * null when the sidecar is disabled, no boot is in flight, boot failed, or
 * the wait exceeds `timeoutMs` (bounded so a hung python env degrades the
 * caller to its no-sidecar fallback instead of hanging the request).
 */
export async function whenSidecarSupervisor(timeoutMs = 20_000): Promise<SidecarSupervisor | null> {
  if (process.env.STEERABLE_USE_SIDECAR === '0') return null;
  const live = getSidecarSupervisor();
  if (live) return live;
  const pending = pendingSupervisor;
  if (!pending) return null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), timeoutMs);
  });
  // A rejected boot promise (python env broken etc.) is already logged by the
  // boot path; waiters degrade to their no-sidecar fallback instead of
  // crashing on someone else's boot failure.
  const settled = pending.catch(() => null);
  try {
    return await Promise.race([settled, timeout]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

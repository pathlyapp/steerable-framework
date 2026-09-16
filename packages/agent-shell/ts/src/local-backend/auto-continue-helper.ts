/**
 * Auto-continue policy for the CoreLoop turn in `router.ts`.
 *
 * `budget_exhausted` means the loop hit a wall, not that the model decided it
 * was done — a finished turn terminates `completed`. So the status alone is
 * the "unfinished" judgement; no separate goal verifier is needed to decide
 * whether continuing is warranted.
 *
 * Continuation goes through the W7-1 resume channel: the sidecar replays the
 * durable record's projection with fresh round and token budgets, so each
 * pass is a checkpoint, not a smaller retry of the one that hit the wall.
 *
 * The turn stops only when the task is actually over: `completed` (the
 * model's own finish), `failed`, `cancelled` (the user's Stop), or a pass
 * that made no progress — a pass that spent a whole budget without running
 * one tool or writing one character is spinning, and resuming from the
 * transcript that produced the spin spins again. There is no pass-count cap
 * by default: the round guardrail (`maxRounds`) bounds one pass, never the
 * task. `STEERABLE_AUTO_CONTINUE` opts back into a cap (`0` disables
 * continuation entirely).
 *
 * Pure so it is unit-testable: `storage/index.ts` can't load under plain
 * Node/vitest (`better-sqlite3` is compiled against Electron's ABI).
 */

/** Continuation policy when `STEERABLE_AUTO_CONTINUE` is unset: no cap. */
export const DEFAULT_AUTO_CONTINUE_MAX = Number.POSITIVE_INFINITY;

export interface AutoContinueInput {
  /** The pass's CoreLoop terminal status. */
  status: string;
  /** Continuation passes already spent on this turn (0 on the first pass). */
  continuationsUsed: number;
  /** Configured cap, from `resolveAutoContinueMax`. */
  max: number;
  /** The request's abort signal fired — the user pressed Stop. */
  aborted: boolean;
  /** The pass ran a tool or produced assistant text. */
  madeProgress: boolean;
}

/**
 * True when the turn should run another resume pass.
 *
 * Only `budget_exhausted` continues. `completed` is the model's own
 * finish, `failed` would just repeat the failure, and `cancelled` is the
 * user's explicit stop. A pass with no progress is spinning — stopping
 * there is the runaway guard now that the pass count is uncapped.
 */
export function shouldAutoContinue(input: AutoContinueInput): boolean {
  if (input.status !== 'budget_exhausted') return false;
  if (input.aborted) return false;
  if (!input.madeProgress) return false;
  return input.continuationsUsed < input.max;
}

/**
 * Parse `STEERABLE_AUTO_CONTINUE` into a continuation cap. `0` disables
 * auto-continue (stop at the first wall). Unset, unparseable, or negative
 * values select the default: no cap — the turn runs until the task ends.
 */
export function resolveAutoContinueMax(raw: string | undefined): number {
  if (raw === undefined || raw.trim() === '') return DEFAULT_AUTO_CONTINUE_MAX;
  const parsed = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(parsed) || parsed < 0) return DEFAULT_AUTO_CONTINUE_MAX;
  return parsed;
}

export interface AutoContinuePass {
  /** The pass's CoreLoop terminal status. */
  status: string;
}

export interface AutoContinueDriveOptions<P extends AutoContinuePass> {
  /** Continuation cap from `resolveAutoContinueMax`; `Infinity` runs until done. */
  max: number;
  /** The request's abort signal fired — the user pressed Stop. */
  isAborted(): boolean;
  /** Cumulative progress counter (tools run + assistant chars so far). */
  progressSnapshot(): number;
  /** Run one pass; `continuationsUsed === 0` is the initial, non-resume pass. */
  runPass(continuationsUsed: number): Promise<P>;
  /** After each pass (usage recording, trace collection). */
  afterPass?(pass: P, continuationsUsed: number): void;
  /** When another continuation pass is about to start. */
  onContinuation?(continuationsUsed: number, max: number): void;
}

/**
 * The router's continuation loop, extracted so the pass/resume/progress
 * wiring is unit-testable without the Electron-ABI storage module. Runs
 * passes until `shouldAutoContinue` says stop and returns the final pass.
 */
export async function driveWithAutoContinue<P extends AutoContinuePass>(
  opts: AutoContinueDriveOptions<P>,
): Promise<{ pass: P; continuations: number }> {
  let continuations = 0;
  for (;;) {
    // Progress is judged per pass: the counters accumulate across passes, so
    // the baseline is the snapshot before this pass ran.
    const progressBefore = opts.progressSnapshot();
    const pass = await opts.runPass(continuations);
    opts.afterPass?.(pass, continuations);
    const madeProgress = opts.progressSnapshot() > progressBefore;
    if (
      !shouldAutoContinue({
        status: pass.status,
        continuationsUsed: continuations,
        max: opts.max,
        aborted: opts.isAborted(),
        madeProgress,
      })
    ) {
      return { pass, continuations };
    }
    continuations += 1;
    opts.onContinuation?.(continuations, opts.max);
  }
}

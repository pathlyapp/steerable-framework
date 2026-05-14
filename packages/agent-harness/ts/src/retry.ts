export interface RetryPolicy {
  maxAttempts: number;
  baseDelayMs: number;
  maxDelayMs: number;
  jitter: boolean;
}

export function nextRetryDelayMs(policy: RetryPolicy, attempt: number): number {
  const safeAttempt = Math.max(attempt, 1);
  const raw = Math.min(policy.baseDelayMs * 2 ** (safeAttempt - 1), policy.maxDelayMs);
  if (!policy.jitter) return raw;
  return Math.max(0, Math.floor(raw * (0.8 + Math.random() * 0.4)));
}

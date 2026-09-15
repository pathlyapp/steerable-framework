import { describe, expect, it } from 'vitest';
import {
  formatElapsedCompact,
  inferDurationMs,
  readPersistedDurationMs,
} from './elapsed';

describe('formatElapsedCompact', () => {
  it('matches Codex compact elapsed', () => {
    expect(formatElapsedCompact(0)).toBe('0s');
    expect(formatElapsedCompact(999)).toBe('0s');
    expect(formatElapsedCompact(1_000)).toBe('1s');
    expect(formatElapsedCompact(12_000)).toBe('12s');
    expect(formatElapsedCompact(59_000)).toBe('59s');
    expect(formatElapsedCompact(60_000)).toBe('1m 00s');
    expect(formatElapsedCompact(61_000)).toBe('1m 01s');
    expect(formatElapsedCompact(83_000)).toBe('1m 23s');
    expect(formatElapsedCompact(3_605_000)).toBe('1h 00m 05s');
    expect(formatElapsedCompact(90_122_000)).toBe('25h 02m 02s');
  });
});

describe('readPersistedDurationMs', () => {
  it('reads a finite non-negative duration from assistant metadata', () => {
    expect(readPersistedDurationMs(JSON.stringify({ durationMs: 83000 }))).toBe(83000);
    expect(readPersistedDurationMs(JSON.stringify({ durationMs: 0 }))).toBe(0);
    expect(readPersistedDurationMs(JSON.stringify({}))).toBeUndefined();
    expect(readPersistedDurationMs(JSON.stringify({ durationMs: -1 }))).toBeUndefined();
    expect(readPersistedDurationMs('{')).toBeUndefined();
    expect(readPersistedDurationMs(null)).toBeUndefined();
  });
});

describe('inferDurationMs', () => {
  it('subtracts the user timestamp from the assistant timestamp', () => {
    expect(
      inferDurationMs('2026-09-08T12:00:00.000Z', '2026-09-08T12:01:23.000Z'),
    ).toBe(83_000);
    expect(inferDurationMs('2026-09-08T12:00:00.000Z', '2026-09-08T11:59:00.000Z')).toBeUndefined();
    expect(inferDurationMs(undefined, '2026-09-08T12:00:00.000Z')).toBeUndefined();
  });
});

import { describe, expect, it } from 'vitest';
import { turnDurationMs } from '../../src/local-backend/turn-duration.js';

describe('turnDurationMs', () => {
  it('returns elapsed milliseconds from turn_active.startedAt', () => {
    const started = '2026-09-08T12:00:00.000Z';
    const ended = Date.parse(started) + 83_000;
    expect(turnDurationMs(started, ended)).toBe(83_000);
  });

  it('rejects missing or invalid start timestamps', () => {
    expect(turnDurationMs(null, Date.now())).toBeNull();
    expect(turnDurationMs('not-a-date', Date.now())).toBeNull();
  });
});

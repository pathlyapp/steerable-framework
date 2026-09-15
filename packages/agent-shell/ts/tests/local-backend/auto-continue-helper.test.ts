import { describe, expect, it } from 'vitest';
import {
  DEFAULT_AUTO_CONTINUE_MAX,
  driveWithAutoContinue,
  resolveAutoContinueMax,
  shouldAutoContinue,
} from '../../src/local-backend/auto-continue-helper.js';

// 自动续跑的判据：只有「撞预算墙」才续，模型自己收尾、失败、用户取消都不续。
// 默认没有趟数上限——maxRounds 管的是一趟，不是任务；整趟零进展（打转）
// 是不续的唯一兜底。
describe('shouldAutoContinue', () => {
  const base = { continuationsUsed: 0, max: 3, aborted: false, madeProgress: true };

  it('continues a budget-exhausted pass — the wall stopped it, not the model', () => {
    expect(shouldAutoContinue({ ...base, status: 'budget_exhausted' })).toBe(true);
  });

  it('does not continue a completed pass — that is the model finishing', () => {
    expect(shouldAutoContinue({ ...base, status: 'completed' })).toBe(false);
  });

  it('does not continue a failed pass — another pass would just repeat the failure', () => {
    expect(shouldAutoContinue({ ...base, status: 'failed' })).toBe(false);
  });

  it('does not continue a cancelled pass', () => {
    expect(shouldAutoContinue({ ...base, status: 'cancelled' })).toBe(false);
  });

  it('stops once an explicit cap is spent', () => {
    expect(
      shouldAutoContinue({ ...base, status: 'budget_exhausted', continuationsUsed: 3 }),
    ).toBe(false);
    expect(
      shouldAutoContinue({ ...base, status: 'budget_exhausted', continuationsUsed: 2 }),
    ).toBe(true);
  });

  it('the default policy has no cap — a productive pass always continues', () => {
    expect(
      shouldAutoContinue({
        ...base,
        status: 'budget_exhausted',
        continuationsUsed: 50,
        max: DEFAULT_AUTO_CONTINUE_MAX,
      }),
    ).toBe(true);
  });

  it('honours a cap of 0 — auto-continue off, stop at the first wall', () => {
    expect(shouldAutoContinue({ ...base, status: 'budget_exhausted', max: 0 })).toBe(false);
  });

  it('does not continue after the user pressed Stop', () => {
    expect(
      shouldAutoContinue({ ...base, status: 'budget_exhausted', aborted: true }),
    ).toBe(false);
  });

  // 一趟花光整份预算却一个工具没跑、一个字没写，就是在原地打转；它续跑时
  // 回放的正是产生这次打转的 transcript，下一趟只会一样。趟数不设上限之后，
  // 这个「零进展不续」就是防失控的兜底。
  it('does not continue a pass that ran no tool and wrote nothing', () => {
    expect(
      shouldAutoContinue({
        ...base,
        status: 'budget_exhausted',
        madeProgress: false,
      }),
    ).toBe(false);
  });
});

describe('resolveAutoContinueMax', () => {
  it('defaults to no cap when unset or blank', () => {
    expect(resolveAutoContinueMax(undefined)).toBe(Number.POSITIVE_INFINITY);
    expect(resolveAutoContinueMax('   ')).toBe(Number.POSITIVE_INFINITY);
  });

  it('takes an explicit count, including 0 to disable', () => {
    expect(resolveAutoContinueMax('0')).toBe(0);
    expect(resolveAutoContinueMax('7')).toBe(7);
  });

  it('takes large explicit counts verbatim — the cap is opt-in, not clamped', () => {
    expect(resolveAutoContinueMax('9999')).toBe(9999);
  });

  it('falls back to the default on unparseable or negative values', () => {
    expect(resolveAutoContinueMax('lots')).toBe(Number.POSITIVE_INFINITY);
    expect(resolveAutoContinueMax('-2')).toBe(Number.POSITIVE_INFINITY);
  });
});

describe('driveWithAutoContinue', () => {
  interface FakePass {
    status: string;
    traceId?: string;
  }

  function fakeDriver(overrides: {
    max?: number;
    aborted?: () => boolean;
    script: (continuationsUsed: number) => { status: string; progress: number };
  }) {
    let progress = 0;
    const passIndexes: number[] = [];
    const continuationsLogged: number[] = [];
    const driver = driveWithAutoContinue<FakePass>({
      max: overrides.max ?? DEFAULT_AUTO_CONTINUE_MAX,
      isAborted: overrides.aborted ?? (() => false),
      progressSnapshot: () => progress,
      runPass: async (continuationsUsed) => {
        passIndexes.push(continuationsUsed);
        const step = overrides.script(continuationsUsed);
        progress += step.progress;
        return { status: step.status, traceId: `trace-${continuationsUsed}` };
      },
      onContinuation: (continuationsUsed) => continuationsLogged.push(continuationsUsed),
    });
    return { driver, passIndexes, continuationsLogged };
  }

  // 超长任务模拟：每趟打满 maxRounds 轮次墙（budget_exhausted），12 趟才做
  // 完——按每趟 80 轮计约 960 轮。旧的 3 趟上限会在第 4 趟后把任务截停；
  // 现在的策略必须一路续到 completed。
  it('runs until completed across more passes than the old 3-pass cap', async () => {
    const { driver, passIndexes, continuationsLogged } = fakeDriver({
      script: (used) => ({
        status: used === 11 ? 'completed' : 'budget_exhausted',
        progress: 80,
      }),
    });
    const result = await driver;
    expect(result.pass.status).toBe('completed');
    expect(result.continuations).toBe(11);
    expect(passIndexes).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
    expect(continuationsLogged).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
  });

  it('stops on a spinning pass even with the uncapped default', async () => {
    const { driver, passIndexes } = fakeDriver({
      script: () => ({ status: 'budget_exhausted', progress: 0 }),
    });
    const result = await driver;
    expect(result.pass.status).toBe('budget_exhausted');
    expect(result.continuations).toBe(0);
    expect(passIndexes).toEqual([0]);
  });

  it('respects an explicit cap from STEERABLE_AUTO_CONTINUE', async () => {
    const { driver, passIndexes } = fakeDriver({
      max: resolveAutoContinueMax('2'),
      script: () => ({ status: 'budget_exhausted', progress: 10 }),
    });
    const result = await driver;
    expect(result.continuations).toBe(2);
    expect(passIndexes).toEqual([0, 1, 2]);
  });

  it('stops when the user aborts mid-turn', async () => {
    let passes = 0;
    const { driver } = fakeDriver({
      aborted: () => passes > 0,
      script: () => {
        passes += 1;
        return { status: 'budget_exhausted', progress: 10 };
      },
    });
    const result = await driver;
    expect(result.continuations).toBe(0);
  });

  it('never continues completed, failed, or cancelled passes', async () => {
    for (const status of ['completed', 'failed', 'cancelled']) {
      const { driver, passIndexes } = fakeDriver({
        script: () => ({ status, progress: 10 }),
      });
      const result = await driver;
      expect(result.pass.status).toBe(status);
      expect(passIndexes).toEqual([0]);
    }
  });
});

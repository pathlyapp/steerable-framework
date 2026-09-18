/**
 * date-groups：侧栏会话分组的标签与排序权重。
 * 锁定分档边界（今天 / 昨天 / 2–3 天前 / 同年 M月D日 / 跨年 YYYY年M月D日）
 * 与 priority 的单调性（越小越新）。测试日期一律相对今天构造（正午锚定），
 * 避免硬编码日期随时间腐化或在午夜前后抖动。
 */
import { describe, expect, it } from 'vitest';
import { getDateGroupLabel, getDateGroupPriority } from './date-groups';

/** 构造 n 天前的日期（锚在正午，避开跨午夜的边界抖动）。 */
function daysAgo(n: number): Date {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  d.setDate(d.getDate() - n);
  return d;
}

describe('getDateGroupLabel 分档', () => {
  it('今天与昨天有专用标签', () => {
    expect(getDateGroupLabel(daysAgo(0))).toBe('今天');
    expect(getDateGroupLabel(daysAgo(1))).toBe('昨天');
  });

  it('2–3 天前折叠为「N天前」', () => {
    expect(getDateGroupLabel(daysAgo(2))).toBe('2天前');
    expect(getDateGroupLabel(daysAgo(3))).toBe('3天前');
  });

  it('4 天起按年份分档：同年 M月D日，跨年 YYYY年M月D日', () => {
    const d = daysAgo(10);
    const today = new Date();
    if (d.getFullYear() === today.getFullYear()) {
      expect(getDateGroupLabel(d)).toBe(`${d.getMonth() + 1}月${d.getDate()}日`);
    } else {
      expect(getDateGroupLabel(d)).toBe(
        `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`,
      );
    }
  });

  it('确定的过去年份一定走跨年格式', () => {
    expect(getDateGroupLabel(new Date(2000, 0, 5))).toBe('2000年1月5日');
  });
});

describe('getDateGroupPriority 排序权重', () => {
  it('各档位的权重值', () => {
    expect(getDateGroupPriority('今天')).toBe(1);
    expect(getDateGroupPriority('昨天')).toBe(2);
    expect(getDateGroupPriority('3天前')).toBe(5);
    expect(getDateGroupPriority('9月1日')).toBe(100);
    expect(getDateGroupPriority('2025年9月1日')).toBe(1000);
  });

  it('按权重排序后组序为 今天 → 昨天 → N天前 → 月日 → 跨年', () => {
    const labels = ['2024年3月2日', '9月1日', '3天前', '昨天', '2天前', '今天'];
    const sorted = [...labels].sort(
      (a, b) => getDateGroupPriority(a) - getDateGroupPriority(b),
    );
    expect(sorted).toEqual(['今天', '昨天', '2天前', '3天前', '9月1日', '2024年3月2日']);
  });
});

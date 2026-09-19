import { describe, expect, it } from 'vitest';
import { stripNextStepsTags } from './next-steps-tags';

describe('stripNextStepsTags', () => {
  it('去掉标签，保留里面的建议正文', () => {
    expect(
      stripNextStepsTags(
        'PPT 已完成。\n\n[next_steps]\n- 调整封面配色\n- 再加一页\n[/next_steps]',
      ),
    ).toBe('PPT 已完成。\n\n\n- 调整封面配色\n- 再加一页\n');
  });

  it('流式未写完的开标签不露出来', () => {
    expect(stripNextStepsTags('PPT 已完成。\n\n[next_st')).toBe('PPT 已完成。\n\n');
    expect(stripNextStepsTags('PPT 已完成。\n\n[next_steps]\n- 调整封面')).toBe(
      'PPT 已完成。\n\n\n- 调整封面',
    );
  });

  it('没有标签时原文不动', () => {
    expect(stripNextStepsTags('可以接着改封面配色。')).toBe('可以接着改封面配色。');
  });
});

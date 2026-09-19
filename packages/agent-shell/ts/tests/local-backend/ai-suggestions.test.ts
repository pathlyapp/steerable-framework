/**
 * 回合追问建议（local-backend/ai-suggestions.ts）行为测试。
 *
 * 钉住：只把 `[next_steps]` / 最后一段交给 LLM 判断，JSON / 编号列表解析，
 * 失败或超时返回空数组且永不抛错。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  generate: vi.fn(),
}));

vi.mock('../../src/llm/index.js', () => ({
  llmService: { generate: mocks.generate },
}));

import {
  cleanSuggestedReply,
  extractNextStepsSource,
  generateSuggestedReplies,
  parseSuggestedReplies,
} from '../../src/local-backend/ai-suggestions.js';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('cleanSuggestedReply / parseSuggestedReplies', () => {
  it('去掉编号、引号、尾标点并截断', () => {
    expect(cleanSuggestedReply('1. 调整封面配色。')).toBe('调整封面配色');
    expect(cleanSuggestedReply('"把个人简介写得更具体"')).toBe('把个人简介写得更具体');
    expect(cleanSuggestedReply('• 再加一页')).toBe('再加一页');
    expect(cleanSuggestedReply('x')).toBe('');
    expect(Array.from(cleanSuggestedReply('字'.repeat(60))).length).toBe(48);
  });

  it('解析 JSON 数组（含 markdown 代码块）', () => {
    expect(
      parseSuggestedReplies('["调整封面配色","把个人简介写得更具体","再加一页项目案例"]'),
    ).toEqual(['调整封面配色', '把个人简介写得更具体', '再加一页项目案例']);
    expect(
      parseSuggestedReplies('```json\n["A建议内容足够长","B建议内容足够长","C建议内容足够长"]\n```'),
    ).toEqual(['A建议内容足够长', 'B建议内容足够长', 'C建议内容足够长']);
  });

  it('解析编号列表，去重后不压成 3 条', () => {
    expect(
      parseSuggestedReplies(
        '1. 调整封面配色\n2. 调整封面配色\n3. 把第2页写具体\n4. 再加一页\n5. 换浅色背景',
      ),
    ).toEqual(['调整封面配色', '把第2页写具体', '再加一页', '换浅色背景']);
  });

  it('非法 JSON 落到分行', () => {
    expect(parseSuggestedReplies('不是数组\n- 调整封面配色\n- 补充项目经历\n- 改成简洁文案')).toEqual([
      '调整封面配色',
      '补充项目经历',
      '改成简洁文案',
    ]);
  });
});

describe('extractNextStepsSource', () => {
  it('优先取最后一段 [next_steps] 正文', () => {
    expect(
      extractNextStepsSource(
        'PPT 已完成。\n\n文件位置：桌面\n\n[next_steps]\n- 调整封面配色\n- 再加一页项目案例\n[/next_steps]',
      ),
    ).toBe('- 调整封面配色\n- 再加一页项目案例');
  });

  it('多段标签时取最后一段', () => {
    expect(
      extractNextStepsSource(
        '[next_steps]\n旧建议\n[/next_steps]\n\n正文\n\n[next_steps]\n新建议甲\n新建议乙\n[/next_steps]',
      ),
    ).toBe('新建议甲\n新建议乙');
  });

  it('没有标签时取最后一段', () => {
    expect(
      extractNextStepsSource(
        'PPT 已制作完成并已打开。\n\n**文件位置**：桌面\n**页数**：8 页\n\n可以接着改封面配色，或再加一页作品赏析。',
      ),
    ).toBe('可以接着改封面配色，或再加一页作品赏析。');
  });

  it('空回复得到空来源', () => {
    expect(extractNextStepsSource('')).toBe('');
    expect(extractNextStepsSource('   ')).toBe('');
  });
});

describe('generateSuggestedReplies', () => {
  const user = '制作自我介绍ppt';
  const assistant =
    'PPT 已生成并打开。\n\n[next_steps]\n- 把封面改成深蓝商务风\n- 第2页个人简介写具体\n[/next_steps]';

  it('把 next_steps 正文交给 LLM，合格 JSON 时 usedFallback=false', async () => {
    mocks.generate.mockResolvedValue({
      content: '["把封面改成深蓝商务风","第2页个人简介写具体","再加一页项目经历"]',
    });
    const result = await generateSuggestedReplies(user, assistant);
    expect(result.usedFallback).toBe(false);
    expect(result.suggestions).toEqual([
      '把封面改成深蓝商务风',
      '第2页个人简介写具体',
      '再加一页项目经历',
    ]);
    const prompt = mocks.generate.mock.calls[0][0].messages[1].content as string;
    expect(prompt).toContain('把封面改成深蓝商务风');
    expect(prompt).not.toContain('PPT 已生成并打开');
  });

  it('没有标签时只把最后一段当来源', async () => {
    mocks.generate.mockResolvedValue({
      content: '["调整封面配色","再加一页作品赏析"]',
    });
    await generateSuggestedReplies(
      '介绍杜甫',
      'PPT 已完成。\n\n**页数**：8 页\n\n可以接着改封面配色，或再加一页作品赏析。',
    );
    const prompt = mocks.generate.mock.calls[0][0].messages[1].content as string;
    expect(prompt).toContain('可以接着改封面配色，或再加一页作品赏析。');
    expect(prompt).not.toContain('**页数**：8 页');
  });

  it('模型判定没有下一步时返回空数组且不算兜底', async () => {
    mocks.generate.mockResolvedValue({ content: '[]' });
    const result = await generateSuggestedReplies(
      '介绍杜甫',
      'PPT 已完成。\n\n**文件位置**：桌面\n**页数**：8 页',
    );
    expect(result.usedFallback).toBe(false);
    expect(result.suggestions).toEqual([]);
  });

  it('没有来源时不调 LLM', async () => {
    const result = await generateSuggestedReplies('你好', '   ');
    expect(mocks.generate).not.toHaveBeenCalled();
    expect(result).toEqual({ suggestions: [], usedFallback: false });
  });

  it('LLM 空白 / 抛错时返回空数组', async () => {
    mocks.generate.mockResolvedValue({ content: '' });
    const empty = await generateSuggestedReplies(user, assistant);
    expect(empty.usedFallback).toBe(true);
    expect(empty.suggestions).toEqual([]);

    mocks.generate.mockRejectedValue(new Error('boom'));
    const failed = await generateSuggestedReplies(user, assistant);
    expect(failed.usedFallback).toBe(true);
    expect(failed.suggestions).toEqual([]);
  });

  it('超时返回空数组且不抛错', async () => {
    mocks.generate.mockImplementation(() => new Promise(() => {}));
    const result = await generateSuggestedReplies(user, assistant, { perAttemptTimeoutMs: 20 });
    expect(result.usedFallback).toBe(true);
    expect(result.suggestions).toEqual([]);
  });
});

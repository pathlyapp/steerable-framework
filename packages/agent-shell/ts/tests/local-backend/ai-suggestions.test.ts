/**
 * 回合追问建议（local-backend/ai-suggestions.ts）行为测试。
 *
 * 钉住：JSON / markdown / 编号列表解析、单条清洗、启发式兜底（PPT / 计划 / 代码 / 通用）、
 * LLM 成功替换兜底、失败/超时/空白走兜底且永不抛错。
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
  fallbackSuggestedReplies,
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
    expect(Array.from(cleanSuggestedReply('字'.repeat(50))).length).toBe(36);
  });

  it('解析 JSON 数组（含 markdown 代码块）', () => {
    expect(
      parseSuggestedReplies('["调整封面配色","把个人简介写得更具体","再加一页项目案例"]'),
    ).toEqual(['调整封面配色', '把个人简介写得更具体', '再加一页项目案例']);
    expect(
      parseSuggestedReplies('```json\n["A建议内容足够长","B建议内容足够长","C建议内容足够长"]\n```'),
    ).toEqual(['A建议内容足够长', 'B建议内容足够长', 'C建议内容足够长']);
  });

  it('解析编号列表，去重后最多 3 条', () => {
    expect(
      parseSuggestedReplies('1. 调整封面配色\n2. 调整封面配色\n3. 把第2页写具体\n4. 再加一页'),
    ).toEqual(['调整封面配色', '把第2页写具体', '再加一页']);
  });

  it('非法 JSON 落到分行', () => {
    expect(parseSuggestedReplies('不是数组\n- 调整封面配色\n- 补充项目经历\n- 改成简洁文案')).toEqual([
      '调整封面配色',
      '补充项目经历',
      '改成简洁文案',
    ]);
  });
});

describe('fallbackSuggestedReplies', () => {
  it('PPT 产物走封面/内容/加页', () => {
    expect(fallbackSuggestedReplies('制作自我介绍ppt', 'PPT 已生成 /tmp/自我介绍_PPT.pptx')).toEqual([
      '调整封面标题和配色',
      '把某一页内容写得更具体',
      '再加一页项目案例',
    ]);
  });

  it('助手邀请改内容/样式时把邀请具体化', () => {
    expect(
      fallbackSuggestedReplies(
        '做个 ppt',
        'PPT 已生成。如需修改内容或调整样式，请告诉我。',
      ),
    ).toEqual(['调整幻灯片的内容和文案', '调整配色和版式', '再加一页补充材料']);
  });

  it('代码回复走解释/测试/可读性', () => {
    expect(fallbackSuggestedReplies('修这个函数', '```ts\nexport function foo() {}\n```')).toEqual([
      '解释这段实现的思路',
      '帮我补上测试',
      '再优化一下可读性',
    ]);
  });

  it('其它走通用三条', () => {
    expect(fallbackSuggestedReplies('你好', '你好，需要帮忙吗？')).toEqual([
      '继续完善这份结果',
      '换一种呈现方式',
      '告诉我下一步怎么做',
    ]);
  });
});

describe('generateSuggestedReplies', () => {
  const user = '制作自我介绍ppt';
  const assistant = 'PPT 已生成并打开。包含 6 页幻灯片。如需修改内容或调整样式，请告诉我。';

  it('LLM 返回合格 JSON 时 usedFallback=false', async () => {
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
  });

  it('LLM 空白 / 抛错走启发式兜底', async () => {
    mocks.generate.mockResolvedValue({ content: '' });
    const empty = await generateSuggestedReplies(user, assistant);
    expect(empty.usedFallback).toBe(true);
    expect(empty.suggestions).toHaveLength(3);

    mocks.generate.mockRejectedValue(new Error('boom'));
    const failed = await generateSuggestedReplies(user, assistant);
    expect(failed.usedFallback).toBe(true);
    expect(failed.suggestions).toEqual(empty.suggestions);
  });

  it('超时走兜底且不抛错', async () => {
    mocks.generate.mockImplementation(() => new Promise(() => {}));
    const result = await generateSuggestedReplies(user, assistant, { perAttemptTimeoutMs: 20 });
    expect(result.usedFallback).toBe(true);
    expect(result.suggestions).toHaveLength(3);
  });
});

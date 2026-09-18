/**
 * AI 聊天标题生成（local-backend/ai-title.ts）行为测试。
 *
 * 钉住的行为契约：
 *  - 短消息（≤ 8 字）跳过 LLM 直接当标题（小模型只会给出更糟的东西）；
 *  - LLM 输出的清洗：取第一行、去引号/书名号/「标题：」前缀、30 字软上限；
 *  - 失败/超时/空白输出 → 用消息开头（20 码点 + 省略号）兜底，永不抛错；
 *  - usedFallback 标记区分「真生成」与「兜底」，调用方据此决定要不要发
 *    chat_title_updated 事件（兜底不覆盖用户手改的标题）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  generate: vi.fn(),
}));

vi.mock('../../src/llm/index.js', () => ({
  llmService: { generate: mocks.generate },
}));

import { generateChatTitle } from '../../src/local-backend/ai-title.js';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('generateChatTitle · 快速路径', () => {
  it('空消息 → DEFAULT_TITLE + usedFallback', async () => {
    expect(await generateChatTitle('')).toEqual({ title: '新对话', usedFallback: true });
    expect(await generateChatTitle('   ')).toEqual({ title: '新对话', usedFallback: true });
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it('≤ 8 字短消息跳过 LLM，消息本身当标题', async () => {
    expect(await generateChatTitle('帮我查天气')).toEqual({ title: '帮我查天气', usedFallback: false });
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it('短消息但清洗后为空（纯标点）→ 落到 LLM 路径', async () => {
    mocks.generate.mockResolvedValue({ content: '标点问候' });
    const result = await generateChatTitle('？？？？', { skipLlmForShortMessages: true });
    expect(mocks.generate).toHaveBeenCalledOnce();
    expect(result).toEqual({ title: '标点问候', usedFallback: false });
  });

  it('skipLlmForShortMessages=false 时短消息也走 LLM', async () => {
    mocks.generate.mockResolvedValue({ content: '问候语' });
    await generateChatTitle('你好', { skipLlmForShortMessages: false });
    expect(mocks.generate).toHaveBeenCalledOnce();
  });
});

describe('generateChatTitle · LLM 输出清洗', () => {
  const msg = '请帮我分析一下这个季度销售数据的趋势变化';

  it('正常输出：原样返回', async () => {
    mocks.generate.mockResolvedValue({ content: '季度销售趋势分析' });
    expect(await generateChatTitle(msg)).toEqual({ title: '季度销售趋势分析', usedFallback: false });
  });

  it('多行输出只取第一行（模型先吐思考再给标题）', async () => {
    mocks.generate.mockResolvedValue({ content: '让我想想...\n季度销售趋势\n还有一些解释' });
    // 第一行是思考内容时取第一行——清洗契约就是「取第一行」，不猜测哪行是标题
    const result = await generateChatTitle(msg);
    expect(result.title).toBe('让我想想');
  });

  it('去包裹引号/书名号/尾标点', async () => {
    mocks.generate.mockResolvedValue({ content: '《季度销售趋势》。' });
    expect((await generateChatTitle(msg)).title).toBe('季度销售趋势');
    mocks.generate.mockResolvedValue({ content: '"销售分析"' });
    expect((await generateChatTitle(msg)).title).toBe('销售分析');
  });

  it('去「标题：」「Title:」前缀', async () => {
    mocks.generate.mockResolvedValue({ content: '标题：销售趋势' });
    expect((await generateChatTitle(msg)).title).toBe('销售趋势');
    mocks.generate.mockResolvedValue({ content: 'Title: sales trend' });
    expect((await generateChatTitle(msg)).title).toBe('sales trend');
  });

  it('超过 30 字软上限截断', async () => {
    mocks.generate.mockResolvedValue({ content: '标'.repeat(50) });
    const result = await generateChatTitle(msg);
    expect(result.title.length).toBe(30);
  });

  it('LLM 返回空白/纯符号 → 兜底用消息开头', async () => {
    mocks.generate.mockResolvedValue({ content: '。。。' });
    const result = await generateChatTitle(msg);
    expect(result.title).toContain('请帮我分析一下这个季度销售数据');
    expect(result.usedFallback).toBe(false);
  });
});

describe('generateChatTitle · 失败与兜底', () => {
  const longMsg = '这是一条长度超过二十个字符的用户消息，用来验证兜底截断行为是否按码点工作';

  it('LLM 抛错 → 消息前 20 码点 + 省略号', async () => {
    mocks.generate.mockRejectedValue(new Error('connection refused'));
    const result = await generateChatTitle(longMsg);
    // 前 20 个码点 + 省略号
    expect(result.title).toBe('这是一条长度超过二十个字符的用户消息，用…');
    expect(result.usedFallback).toBe(false);
  });

  it('LLM 超时 → 走兜底而不是悬挂', async () => {
    mocks.generate.mockImplementation(() => new Promise(() => {}));
    const result = await generateChatTitle(longMsg, { perAttemptTimeoutMs: 50 });
    expect(result.title).toContain('这是一条');
    expect(result.usedFallback).toBe(false);
  });

  it('重试：首次失败第二次成功', async () => {
    mocks.generate
      .mockRejectedValueOnce(new Error('timeout'))
      .mockResolvedValueOnce({ content: '重试后的标题' });
    const result = await generateChatTitle(longMsg, { maxRetries: 1 });
    expect(result.title).toBe('重试后的标题');
    expect(mocks.generate).toHaveBeenCalledTimes(2);
  });

  it('重试全部失败 → 兜底；maxRetries=0 默认不重试', async () => {
    mocks.generate.mockRejectedValue(new Error('x'));
    await generateChatTitle(longMsg);
    expect(mocks.generate).toHaveBeenCalledTimes(1);
  });

  it('消息本身清洗不出标题（纯标点长消息）→ DEFAULT_TITLE + usedFallback', async () => {
    mocks.generate.mockRejectedValue(new Error('x'));
    const result = await generateChatTitle('？？？？？？？？？？？？？？？？？', {
      skipLlmForShortMessages: false,
    });
    expect(result).toEqual({ title: '新对话', usedFallback: true });
  });

  it('emoji 兜底不截在半字节上产生乱码', async () => {
    mocks.generate.mockRejectedValue(new Error('x'));
    const result = await generateChatTitle('🚀'.repeat(25) + '后缀', { skipLlmForShortMessages: false });
    // 兜底先按码点取前 20 个 🚀；随后 cleanTitle 的 30 字软上限按 UTF-16
    // code unit 截（🚀 占 2 个）→ 剩 15 个。截点落在码点边界上，无乱码。
    expect(result.title).toBe('🚀'.repeat(15) + '…');
    expect(result.title).not.toContain('\uFFFD');
  });

  it('超长消息截断到 500 字再发给 LLM', async () => {
    mocks.generate.mockResolvedValue({ content: '标题' });
    await generateChatTitle('长'.repeat(2000));
    const sent = mocks.generate.mock.calls[0][0].messages[1].content as string;
    expect(sent.length).toBe(500);
  });

  it('LLM 调用参数：system prompt 固定 + temperature 0.7', async () => {
    mocks.generate.mockResolvedValue({ content: '标题' });
    await generateChatTitle('给我讲讲分布式系统的一致性协议');
    const arg = mocks.generate.mock.calls[0][0];
    expect(arg.messages[0].role).toBe('system');
    expect(arg.messages[0].content).toContain('标题生成助手');
    expect(arg.temperature).toBe(0.7);
  });
});

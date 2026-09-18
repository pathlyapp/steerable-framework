/**
 * 回合追问建议（local-backend/ai-suggestions.ts）行为测试。
 *
 * 钉住：JSON / 编号列表解析、单条清洗、从用户技能/助手回复抽下一步（条数不固定）、
 * 启发式兜底（PPT / 计划 / 代码 / 通用）、LLM 成功替换、失败/超时走兜底且永不抛错。
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
  extractListedNextSteps,
  extractSkillNextSteps,
  fallbackSuggestedReplies,
  generateSuggestedReplies,
  parseSuggestedReplies,
  stripSkillInvocation,
} from '../../src/local-backend/ai-suggestions.js';

beforeEach(() => {
  vi.clearAllMocks();
});

const WELL_SKILL = `# 井密度交会图

按层位画井密度交会图。用户提到交会图 / 井密度时使用。

## 用法
用 {scripts}/invoke-crossplot.ps1。

## 下一步
1. 画接底层井密度交会图（先核对井位）
2. 再画一层井密度交会图
3. 统计本区有效厚度与净毛比
4. 输出气层厚度分层表
5. 绘制气层厚度等值线
6. 复核井位/层位完整性
7. 补画 CAL / CNL 缺失井
`;

const WELL_ASSISTANT = `本轮已完成接底层数据检查。

下一步：
1. 画接底层井密度交会图（先核对井位） & 'C:\\Users\\me\\AppData\\Roaming\\app\\skills\\plot-crossplot\\scripts\\invoke-crossplot.ps1' -Action plot -Projectpath 'D:\\data'
2. 再画一层井密度交会图 & 'C:\\Users\\me\\AppData\\Roaming\\app\\skills\\plot-crossplot\\scripts\\invoke-crossplot.ps1' -Action plot
3. 统计本区有效厚度与净毛比 & 'C:\\Users\\me\\AppData\\Roaming\\app\\skills\\analyze-reservoir-thickness\\scripts\\analyze.ps1'
4. 输出气层厚度分层表 & 'C:\\Users\\me\\AppData\\Roaming\\app\\skills\\report-fluid-layers\\scripts\\report.ps1'
5. 绘制气层厚度等值线 & 'C:\\Users\\me\\AppData\\Roaming\\app\\skills\\plot-contour-map\\scripts\\invoke-contour.ps1'
6. 复核井位/层位完整性 & 'C:\\Users\\me\\AppData\\Roaming\\app\\skills\\audit-well-data\\scripts\\audit.ps1'
7. 补画 CAL / CNL 缺失井
`;

describe('cleanSuggestedReply / parseSuggestedReplies', () => {
  it('去掉编号、引号、尾标点并截断', () => {
    expect(cleanSuggestedReply('1. 调整封面配色。')).toBe('调整封面配色');
    expect(cleanSuggestedReply('"把个人简介写得更具体"')).toBe('把个人简介写得更具体');
    expect(cleanSuggestedReply('• 再加一页')).toBe('再加一页');
    expect(cleanSuggestedReply('x')).toBe('');
    expect(Array.from(cleanSuggestedReply('字'.repeat(60))).length).toBe(48);
  });

  it('剥掉脚本调用只留标题', () => {
    expect(
      stripSkillInvocation(
        "画接底层井密度交会图 & 'C:\\\\skills\\\\plot-crossplot\\\\scripts\\\\invoke-crossplot.ps1' -Action plot",
      ),
    ).toBe('画接底层井密度交会图');
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

describe('extractSkillNextSteps / extractListedNextSteps', () => {
  it('从用户技能「下一步」节抽出全部条目', () => {
    expect(extractSkillNextSteps(WELL_SKILL)).toEqual([
      '画接底层井密度交会图（先核对井位）',
      '再画一层井密度交会图',
      '统计本区有效厚度与净毛比',
      '输出气层厚度分层表',
      '绘制气层厚度等值线',
      '复核井位/层位完整性',
      '补画 CAL / CNL 缺失井',
    ]);
  });

  it('助手按技能列出带脚本的下一步时抽出标题且不限 3 条', () => {
    expect(extractListedNextSteps(WELL_ASSISTANT)).toEqual([
      '画接底层井密度交会图（先核对井位）',
      '再画一层井密度交会图',
      '统计本区有效厚度与净毛比',
      '输出气层厚度分层表',
      '绘制气层厚度等值线',
      '复核井位/层位完整性',
      '补画 CAL / CNL 缺失井',
    ]);
  });

  it('没有下一步标题或脚本列表时不误抽页面大纲', () => {
    expect(
      extractListedNextSteps('PPT 已生成，包含：\n1. 封面\n2. 个人简介\n3. 项目经历'),
    ).toEqual([]);
  });

  it('带括号补充的「后续动作」标题也识别，列表不带脚本也能抽', () => {
    expect(
      extractListedNextSteps(
        '统计完成。\n\n后续动作（可点选，也可继续对话）\n1. 按层位画交会图\n2. 统计有效厚度\n3. 复核井位完整性\n4. 单位换算',
      ),
    ).toEqual(['按层位画交会图', '统计有效厚度', '复核井位完整性', '单位换算']);
  });

  it('普通句尾出现「建议」「下一步」不触发抽取', () => {
    expect(
      extractListedNextSteps('以上是我的建议\n1. 封面\n2. 个人简介\n3. 项目经历'),
    ).toEqual([]);
    expect(
      extractListedNextSteps('我不知道下一步\n- 封面\n- 个人简介'),
    ).toEqual([]);
  });

  it('短引导行带冒号时仍然识别', () => {
    expect(
      extractSkillNextSteps('完成后可以继续：\n1. 导出分层表\n2. 绘制等值线'),
    ).toEqual(['导出分层表', '绘制等值线']);
  });
});

describe('fallbackSuggestedReplies', () => {
  it('回复里已有技能下一步时采用那些条目', () => {
    expect(fallbackSuggestedReplies('继续分析这口井', WELL_ASSISTANT)).toEqual(
      extractListedNextSteps(WELL_ASSISTANT),
    );
  });

  it('技能正文有下一步时即使回复没列出也采用', () => {
    expect(
      fallbackSuggestedReplies('按技能继续', '本轮只做了数据检查。', { skillContents: [WELL_SKILL] }),
    ).toEqual(extractSkillNextSteps(WELL_SKILL));
  });

  it('本轮点到名的技能，其下一步排在其它技能前面', () => {
    const other = '# 导出井报告\n\n## 下一步\n1. 导出井报告 PDF\n2. 校对报告页眉';
    expect(
      fallbackSuggestedReplies('帮我用井密度交会图看看', '本轮只做了数据检查。', {
        skillContents: [other, WELL_SKILL],
      })[0],
    ).toBe('画接底层井密度交会图（先核对井位）');
  });

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

  it('用户技能有多条下一步时按实际条数保留', async () => {
    mocks.generate.mockResolvedValue({
      content: JSON.stringify([
        '画接底层井密度交会图',
        '再画一层井密度交会图',
        '统计本区有效厚度',
        '输出气层厚度分层表',
        '绘制气层厚度等值线',
        '复核井位完整性',
        '补画 CAL CNL 缺失井',
      ]),
    });
    const result = await generateSuggestedReplies('继续分析这口井', '数据检查完成。', {
      skillContents: [WELL_SKILL],
    });
    expect(result.usedFallback).toBe(false);
    expect(result.suggestions).toHaveLength(7);
    expect(mocks.generate.mock.calls[0][0].messages[1].content).toContain('用户技能中的下一步');
    expect(mocks.generate.mock.calls[0][0].messages[1].content).toContain('不要压成 3 条');
  });

  it('LLM 失败但技能已有下一步时仍返回那些条目', async () => {
    mocks.generate.mockRejectedValue(new Error('boom'));
    const result = await generateSuggestedReplies('继续', '检查完成。', {
      skillContents: [WELL_SKILL],
    });
    expect(result.usedFallback).toBe(false);
    expect(result.suggestions).toEqual(extractSkillNextSteps(WELL_SKILL));
  });

  it('LLM 空白 / 抛错且无技能下一步时走启发式兜底', async () => {
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

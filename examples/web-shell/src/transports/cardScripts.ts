/**
 * Card scripts for the web-shell mock transport.
 *
 * Each entry is a `(userPromptKeyword) -> MockScript` mapping: when the user
 * types e.g. "测验" the mock transport replays the quiz script so the chat
 * shows the full QuizCard render.
 *
 * Each script ends with a `message.complete` event so `useChatSession`
 * flips `isStreaming` back to false.
 */
import type { MockScript } from '@steerable/agent-ui';

// Pull fixtures straight from `spec/blocks/fixtures/` so they stay in lockstep
// with the Ajv conformance test (`tests/conformance/ts/blocks.test.ts`).
// Vite resolves these JSON imports at build time and inlines them.
import orchestrationFx from '../../../../spec/blocks/fixtures/OrchestrationPlanPayload.json';
import quizFx from '../../../../spec/blocks/fixtures/QuizPayload.json';
import coverageFx from '../../../../spec/blocks/fixtures/CoverageReportPayload.json';
import analysisFx from '../../../../spec/blocks/fixtures/AnalysisDocumentPayload.json';
import researchFx from '../../../../spec/blocks/fixtures/ResearchPlanPayload.json';
import suggestedFx from '../../../../spec/blocks/fixtures/SuggestedRepliesPayload.json';
import askFx from '../../../../spec/blocks/fixtures/AskUserQuestionsPayload.json';
import thinkingFx from '../../../../spec/blocks/fixtures/ThinkingProcessPayload.json';
import planStepsFx from '../../../../spec/blocks/fixtures/PlanStepsPayload.json';
import planSelectorFx from '../../../../spec/blocks/fixtures/PlanSelectorPayload.json';
import searchSourcesFx from '../../../../spec/blocks/fixtures/SearchSourcesPayload.json';
import summaryFx from '../../../../spec/blocks/fixtures/SummaryMessagePayload.json';
import actionSegmentFx from '../../../../spec/blocks/fixtures/ActionSegmentPayload.json';
import toolExecutionFx from '../../../../spec/blocks/fixtures/ToolExecutionPayload.json';

export const CARD_FIXTURES = {
  OrchestrationPlanPayload: orchestrationFx,
  QuizPayload: quizFx,
  CoverageReportPayload: coverageFx,
  AnalysisDocumentPayload: analysisFx,
  ResearchPlanPayload: researchFx,
  SuggestedRepliesPayload: suggestedFx,
  AskUserQuestionsPayload: askFx,
  ThinkingProcessPayload: thinkingFx,
  PlanStepsPayload: planStepsFx,
  PlanSelectorPayload: planSelectorFx,
  SearchSourcesPayload: searchSourcesFx,
  SummaryMessagePayload: summaryFx,
  ActionSegmentPayload: actionSegmentFx,
  ToolExecutionPayload: toolExecutionFx,
} as const;

export type CardKind = keyof typeof CARD_FIXTURES;

const STREAM_INTRO: Record<CardKind, string> = {
  OrchestrationPlanPayload: '收到，我已经把任务拆给三个研究子代理，并行收集中。',
  QuizPayload: '好的，给你一份知识点小测，做完后我会给出详细反馈。',
  CoverageReportPayload: '这是你本周的掌握度报告：',
  AnalysisDocumentPayload: '我整理了一份完整分析文档：',
  ResearchPlanPayload: '当前研究计划如下，准备进入下一轮。',
  SuggestedRepliesPayload: '你可以试试这些追问：',
  AskUserQuestionsPayload: '在继续之前我需要几个关键信息：',
  ThinkingProcessPayload: '这是我刚才的思考过程（展开查看）。',
  PlanStepsPayload: '我接下来会按这些步骤来：',
  PlanSelectorPayload: '我准备了三个候选方案，请挑一个：',
  SearchSourcesPayload: '我查阅了以下来源：',
  SummaryMessagePayload: '之前的会话我已经做了摘要，节省上下文。',
  ActionSegmentPayload: '我顺手做了以下两个操作：',
  ToolExecutionPayload: '我刚做了一次网络搜索：',
};

function streamLetters(text: string): MockScript {
  return text.split('').map((ch) => ({
    event: { type: 'content', content: ch } as any,
    delayMs: 18,
  }));
}

export function buildCardScript(kind: CardKind): MockScript {
  const intro = STREAM_INTRO[kind];
  const payload = CARD_FIXTURES[kind];
  return [
    ...streamLetters(intro),
    {
      event: {
        type: 'content',
        content: '',
        messageMetadata: { card: kind, payload },
      } as any,
      delayMs: 80,
    },
    { event: { type: 'done' } as any },
  ];
}

export const CARD_KEYWORDS: Record<string, CardKind> = {
  '编排': 'OrchestrationPlanPayload',
  'orchestration': 'OrchestrationPlanPayload',
  '测验': 'QuizPayload',
  'quiz': 'QuizPayload',
  '覆盖': 'CoverageReportPayload',
  'coverage': 'CoverageReportPayload',
  '分析': 'AnalysisDocumentPayload',
  'analysis': 'AnalysisDocumentPayload',
  '研究': 'ResearchPlanPayload',
  'research': 'ResearchPlanPayload',
  '建议': 'SuggestedRepliesPayload',
  'suggest': 'SuggestedRepliesPayload',
  '问我': 'AskUserQuestionsPayload',
  'ask': 'AskUserQuestionsPayload',
  '思考': 'ThinkingProcessPayload',
  'think': 'ThinkingProcessPayload',
  '步骤': 'PlanStepsPayload',
  'steps': 'PlanStepsPayload',
  '方案': 'PlanSelectorPayload',
  'plans': 'PlanSelectorPayload',
  '来源': 'SearchSourcesPayload',
  'sources': 'SearchSourcesPayload',
  '摘要': 'SummaryMessagePayload',
  'summary': 'SummaryMessagePayload',
  '操作': 'ActionSegmentPayload',
  'actions': 'ActionSegmentPayload',
  '工具': 'ToolExecutionPayload',
  'tool': 'ToolExecutionPayload',
};

export function detectCardKind(text: string): CardKind | null {
  const lower = text.toLowerCase();
  for (const [kw, kind] of Object.entries(CARD_KEYWORDS)) {
    if (text.includes(kw) || lower.includes(kw)) return kind;
  }
  return null;
}

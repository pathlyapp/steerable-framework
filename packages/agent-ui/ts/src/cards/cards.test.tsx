/**
 * Smoke tests for the 14 cards. We focus on render-without-crash + the
 * payload-driven branching that's most likely to regress (read-only vs
 * interactive, expand toggle, callbacks). Full visual coverage lives in
 * Storybook VRT.
 */
import * as React from 'react';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  OrchestrationPlanCard,
  QuizCard,
  CoverageReportCard,
  AnalysisDocumentCard,
  ResearchPlanCard,
  SuggestedRepliesCard,
  AskUserQuestionsCard,
  ThinkingProcessCard,
  PlanStepsCard,
  PlanSelectorCard,
  SearchSourcesCard,
  SummaryMessageCard,
  ActionSegmentCard,
  ToolExecutionCard,
} from './index.js';

afterEach(() => cleanup());

describe('SuggestedRepliesCard', () => {
  it('calls onSelect when a chip is clicked', () => {
    const onSelect = vi.fn();
    render(
      <SuggestedRepliesCard
        payload={{ suggestions: ['继续', '换个角度'] }}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByText('换个角度'));
    expect(onSelect).toHaveBeenCalledWith('换个角度');
  });

  it('renders nothing when suggestions is empty', () => {
    const { container } = render(
      <SuggestedRepliesCard payload={{ suggestions: [] }} onSelect={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe('PlanStepsCard', () => {
  it('renders steps separated by /', () => {
    render(<PlanStepsCard payload={{ steps: ['搜索', '汇总', '回复'] }} />);
    expect(screen.getByText('搜索')).toBeTruthy();
    expect(screen.getByText('汇总')).toBeTruthy();
    expect(screen.getByText('回复')).toBeTruthy();
  });

  it('hides itself with no steps and not streaming', () => {
    const { container } = render(<PlanStepsCard payload={{ steps: [] }} />);
    expect(container.firstChild).toBeNull();
  });
});

describe('OrchestrationPlanCard', () => {
  const payload = {
    rationale: '需要并行收集 3 个领域的资料',
    mode: 'parallel',
    tasks: [
      { id: 't1', agentId: 'researcher.a', prompt: '搜索 A' },
      { id: 't2', agentId: 'researcher.b', prompt: '搜索 B' },
    ],
  };

  it('shows task rows and counts', () => {
    render(
      <OrchestrationPlanCard
        payload={payload}
        taskStatuses={{ t1: 'ok', t2: 'running' }}
        agentNameFor={(id) => id.replace('researcher.', '')}
      />,
    );
    expect(screen.getByText('a')).toBeTruthy();
    expect(screen.getByText('b')).toBeTruthy();
    expect(screen.getByText('1/2')).toBeTruthy();
  });

  it('collapses on header click', () => {
    render(<OrchestrationPlanCard payload={payload} />);
    fireEvent.click(screen.getByText('编排计划'));
    expect(screen.queryByText('搜索 A')).toBeNull();
  });

  it('renders loading shell when loading=true', () => {
    render(
      <OrchestrationPlanCard
        payload={{ tasks: [] }}
        loading
        loadingHint="正在规划"
        headerLabel="协调员"
      />,
    );
    expect(screen.getByText('正在规划')).toBeTruthy();
    expect(screen.getByText('协调员')).toBeTruthy();
    expect(screen.queryByText('编排计划')).toBeNull();
  });

  it('renders failed shell with failure text', () => {
    render(
      <OrchestrationPlanCard
        payload={{ tasks: [] }}
        failed
        failure="超时"
        headerLabel="协调员"
      />,
    );
    expect(screen.getByText('规划失败')).toBeTruthy();
    expect(screen.getByText('超时')).toBeTruthy();
  });

  it('uses renderTaskRow override and hides mode + counts', () => {
    render(
      <OrchestrationPlanCard
        payload={payload}
        taskStatuses={{ t1: 'ok', t2: 'running' }}
        hideMode
        hideHeaderCounts
        headerLabel="协调员安排"
        headerSecondary="2 个子任务"
        renderTaskRow={(task, status) => (
          <li key={task.id} data-task={task.id} data-status={status}>
            {`row:${task.agentId}:${status}`}
          </li>
        )}
      />,
    );
    expect(screen.getByText('row:researcher.a:ok')).toBeTruthy();
    expect(screen.getByText('row:researcher.b:running')).toBeTruthy();
    expect(screen.queryByText('parallel')).toBeNull();
    expect(screen.getByText('2 个子任务')).toBeTruthy();
  });
});

describe('AskUserQuestionsCard', () => {
  it('submits selected answers', () => {
    const onSubmit = vi.fn();
    render(
      <AskUserQuestionsCard
        payload={{
          intro: '请回答',
          questions: [
            { id: 'q1', text: '你的目标？', type: 'select', options: ['学习', '工作'] },
          ],
        }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByText('学习'));
    fireEvent.click(screen.getByText('提交'));
    expect(onSubmit).toHaveBeenCalledWith({ q1: '学习' });
  });

  it('switches to read-only when payload.answers is filled', () => {
    render(
      <AskUserQuestionsCard
        payload={{
          intro: '请回答',
          questions: [
            { id: 'q1', text: '你的目标？', type: 'select', options: ['学习', '工作'] },
          ],
          answers: { q1: '学习' },
        }}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.queryByText('提交')).toBeNull();
  });

  it('exposes auto-continue button when onAutoContinue provided', () => {
    const onAutoContinue = vi.fn();
    render(
      <AskUserQuestionsCard
        payload={{
          intro: '请回答',
          questions: [
            { id: 'q1', text: '你的目标？', type: 'select', options: ['学习', '工作'] },
          ],
        }}
        onSubmit={vi.fn()}
        onAutoContinue={onAutoContinue}
        autoContinueLabel="交给我决定"
      />,
    );
    fireEvent.click(screen.getByText('交给我决定'));
    expect(onAutoContinue).toHaveBeenCalled();
  });

  it('sends free-text answer when allowCustomText + custom pill used', () => {
    const onSubmit = vi.fn();
    render(
      <AskUserQuestionsCard
        payload={{
          intro: '请回答',
          questions: [
            { id: 'q1', text: '其他想法？', type: 'select', options: ['学习', '工作'] },
          ],
        }}
        allowCustomText
        customLabel="自定义"
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByText('自定义'));
    const input = screen.getByPlaceholderText('输入你的回答...');
    fireEvent.change(input, { target: { value: '休息' } });
    fireEvent.click(screen.getByText('提交'));
    expect(onSubmit).toHaveBeenCalledWith({ q1: '休息' });
  });
});

describe('QuizCard', () => {
  it('submits selected choice', () => {
    const onSubmit = vi.fn();
    render(
      <QuizCard
        payload={{
          quizId: 'q',
          title: '测验',
          description: null,
          submitActionLabel: '提交',
          questions: [
            { id: 'q1', type: 'choice', stem: '一加一', options: ['1', '2'], allowMultiple: false, placeholder: null, points: null, knowledgePointId: null, difficulty: null },
          ],
        }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByText('2'));
    fireEvent.click(screen.getByText('提交'));
    expect(onSubmit).toHaveBeenCalledWith({ q1: '2' });
  });
});

describe('CoverageReportCard', () => {
  it('renders overall percentages', () => {
    render(
      <CoverageReportCard
        payload={{
          reportId: 'r',
          title: '覆盖度报告',
          overallCoverage: 0.75,
          overallMastery: 0.5,
          summary: null,
          sections: [],
          weakPoints: [],
          actions: { allowRemediateQuiz: false, remediateActionLabel: '' },
        }}
      />,
    );
    expect(screen.getByText('75%')).toBeTruthy();
    expect(screen.getByText('50%')).toBeTruthy();
  });
});

describe('AnalysisDocumentCard', () => {
  it('uses renderMarkdown slot', () => {
    render(
      <AnalysisDocumentCard
        payload={{ title: 't', body: '# hi', createdAt: null, modelId: null }}
        renderMarkdown={(body) => <span data-testid="md">{body}</span>}
      />,
    );
    expect(screen.getByTestId('md').textContent).toBe('# hi');
  });
});

describe('ResearchPlanCard', () => {
  it('shows topic and decision', () => {
    render(
      <ResearchPlanCard
        payload={{
          topic: '研究 X',
          round: 2,
          final: false,
          subQuestions: [
            { id: 's1', question: '什么是 X', kind: 'fact', status: 'evidenced_strong', evidenceCount: 3, note: null },
          ],
          decision: { next: 'expand', reason: '需要更多证据' },
        }}
      />,
    );
    expect(screen.getByText('研究 X')).toBeTruthy();
    expect(screen.getByText(/横向扩展/)).toBeTruthy();
  });
});

describe('ThinkingProcessCard', () => {
  it('toggles expanded', () => {
    render(<ThinkingProcessCard payload={{ body: 'reasoning', defaultExpanded: false }} />);
    expect(screen.queryByText('reasoning')).toBeNull();
    fireEvent.click(screen.getByText('思考过程'));
    expect(screen.getByText('reasoning')).toBeTruthy();
  });
});

describe('PlanSelectorCard', () => {
  it('emits planId when picked', () => {
    const onSelect = vi.fn();
    render(
      <PlanSelectorCard
        payload={{
          comparison: '',
          goalAttribution: { type: 'new', newGoalTitle: '新目标' },
          plans: [
            {
              id: 'p1',
              name: '稳健方案',
              summary: '走过验证路径',
              approach: 'a',
              bestFor: '稳健',
              metrics: { duration: '1周', effortLevel: 'low', riskLevel: 'low' },
              pros: ['稳定'],
              cons: ['慢'],
            },
          ],
        }}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByText('选择此方案'));
    expect(onSelect).toHaveBeenCalledWith('p1');
  });
});

describe('SearchSourcesCard', () => {
  it('shows source count and expand toggle', () => {
    const { container } = render(
      <SearchSourcesCard
        payload={{
          sources: [
            { url: 'https://example.com', title: 'Example', snippet: 'sn', favicon: null, publishedAt: null },
          ],
        }}
      />,
    );
    expect(screen.getByText(/1 个来源/)).toBeTruthy();
    const trigger = container.querySelector('button');
    if (!trigger) throw new Error('no toggle');
    fireEvent.click(trigger);
    expect(screen.getByText('Example')).toBeTruthy();
  });
});

describe('SummaryMessageCard', () => {
  it('reveals body when expanded', () => {
    render(
      <SummaryMessageCard
        payload={{ body: 'summary body', summarizedCount: 3, status: 'complete', type: null }}
      />,
    );
    fireEvent.click(screen.getByText(/历史摘要/));
    expect(screen.getByText('summary body')).toBeTruthy();
  });

  it('honours custom renderLabel slot', () => {
    render(
      <SummaryMessageCard
        payload={{ body: 'b', summarizedCount: 7, status: 'complete', type: null }}
        renderLabel={({ count }) => `已总结 ${count} 条`}
      />,
    );
    expect(screen.getByText('已总结 7 条')).toBeTruthy();
  });

  it('renders inline appearance without card chrome', () => {
    const { container } = render(
      <SummaryMessageCard
        payload={{ body: 'b', summarizedCount: 1, status: 'complete', type: null }}
        appearance="inline"
      />,
    );
    const root = container.firstElementChild as HTMLElement;
    expect(root.className.includes('rounded-lg')).toBe(false);
  });
});

describe('SearchSourcesCard appearance', () => {
  it('renders inline appearance with deeppath label', () => {
    render(
      <SearchSourcesCard
        payload={{
          sources: [
            { url: 'https://example.com', title: 'Example', snippet: null, favicon: null, publishedAt: null },
          ],
        }}
        appearance="inline"
      />,
    );
    expect(screen.getByText(/搜索了 1 个网站/)).toBeTruthy();
  });
});

describe('ResearchPlanCard progress', () => {
  it('uses totalRounds to compute progress bar width', () => {
    const { container } = render(
      <ResearchPlanCard
        payload={{
          topic: 'topic',
          round: 2,
          final: false,
          subQuestions: [
            { id: 's', question: 'q', kind: 'fact', status: 'pending', evidenceCount: 0, note: null },
          ],
          decision: { next: 'continue', reason: null },
        }}
        totalRounds={4}
      />,
    );
    const bar = container.querySelector('[style*="width"]') as HTMLElement | null;
    expect(bar?.style.width).toBe('50%');
  });
});

describe('CoverageReportCard selection mode', () => {
  it('emits selected ids when allowWeakPointSelection is on', () => {
    const onRemediate = vi.fn();
    render(
      <CoverageReportCard
        payload={{
          reportId: 'r',
          title: 't',
          overallCoverage: 0,
          overallMastery: 0,
          summary: null,
          sections: [],
          weakPoints: [
            { id: 'w1', name: 'wp1', sectionName: null, accuracy: 0.3, recommendation: 'do' },
            { id: 'w2', name: 'wp2', sectionName: null, accuracy: 0.5, recommendation: 'do' },
          ],
          actions: { allowRemediateQuiz: true, remediateActionLabel: '出题' },
        }}
        allowWeakPointSelection
        onRemediate={onRemediate}
      />,
    );
    const checkboxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    fireEvent.click(checkboxes[0]!);
    fireEvent.click(screen.getByText('出题'));
    expect(onRemediate).toHaveBeenCalledWith(['w1']);
  });
});

describe('ToolExecutionCard', () => {
  it('expands to show args/output and renders error', () => {
    render(
      <ToolExecutionCard
        payload={{
          id: 't',
          name: 'search.web',
          status: 'failed',
          summary: '搜索失败',
          args: { q: 'foo' },
          output: null,
          error: 'timeout',
          durationMs: 1200,
          icon: null,
          expandable: true,
        }}
      />,
    );
    fireEvent.click(screen.getByText('search.web'));
    expect(screen.getByText(/"q": "foo"/)).toBeTruthy();
    expect(screen.getByText('timeout')).toBeTruthy();
  });
});

describe('ActionSegmentCard', () => {
  it('renders each segment via ToolExecutionCard', () => {
    render(
      <ActionSegmentCard
        payload={{
          segments: [
            { id: 's1', kind: 'fetch', status: 'succeeded', label: '抓取', args: null, output: null, error: null, startedAt: null, finishedAt: null },
            { id: 's2', kind: 'parse', status: 'running', label: '解析', args: null, output: null, error: null, startedAt: null, finishedAt: null },
          ],
        }}
      />,
    );
    expect(screen.getByText('fetch')).toBeTruthy();
    expect(screen.getByText('parse')).toBeTruthy();
  });
});

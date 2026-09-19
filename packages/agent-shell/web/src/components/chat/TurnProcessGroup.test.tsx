import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  SHOW_THINKING_CONTENT_STORAGE_KEY,
  persistThinkingDisplay,
} from '@/lib/show-thinking-content';
import type { ThinkingDisplayMode } from '@/lib/show-thinking-content';
import type { TurnBlock } from './turn-timeline';

vi.mock('./Markdown', () => ({
  Markdown: ({ children }: { children: string }) => <span>{children}</span>,
}));

vi.mock('./ExecutedActionsCard', () => ({
  ToolsFlow: ({
    actions,
    defaultExpanded,
  }: {
    actions: Array<{ tool: string }>;
    defaultExpanded?: boolean;
  }) => (
    <div data-testid="tools-flow" data-expanded={defaultExpanded ? 'true' : 'false'}>
      {actions.map((action) => action.tool).join(',')}
    </div>
  ),
}));

const { TurnProcessGroup, THINKING_PEEK_HEIGHT } = await import('./TurnProcessGroup');

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  localStorage.removeItem(SHOW_THINKING_CONTENT_STORAGE_KEY);
});

const tool = (name: string, running = false): TurnBlock => ({
  type: 'tools',
  actions: [{ tool: name, arguments: {}, ...(running ? {} : { result: { success: true } }) }],
});

const blocks: TurnBlock[] = [
  { type: 'reasoning', content: '先读配置' },
  tool('csv_get_config'),
  { type: 'reasoning', content: '再查天气' },
  tool('web_fetch'),
  { type: 'text', content: '本地 CSV 配置' },
];

function processToggle() {
  return screen.getByTestId('turn-process-toggle');
}

function renderGroup(overrides: {
  blocks?: TurnBlock[];
  isStreaming?: boolean;
  startedAtMs?: number;
  durationMs?: number;
  thinkingDisplay?: ThinkingDisplayMode;
  showThinkingContent?: boolean;
} = {}) {
  return render(
    <TurnProcessGroup
      blocks={overrides.blocks ?? blocks}
      isStreaming={overrides.isStreaming ?? false}
      startedAtMs={overrides.startedAtMs}
      durationMs={overrides.durationMs}
      thinkingDisplay={overrides.thinkingDisplay}
      showThinkingContent={overrides.showThinkingContent}
      agents={[]}
      chats={[]}
      emptyFallback={<div>empty</div>}
      renderAnswer={(block) => <div data-testid="answer">{block.content}</div>}
    />,
  );
}

describe('TurnProcessGroup', () => {
  it('puts work and thinking chevrons after the title', () => {
    renderGroup({
      isStreaming: true,
      thinkingDisplay: 'full',
      blocks: blocks.slice(0, 1),
    });
    const work = processToggle();
    expect(work.firstElementChild?.tagName.toLowerCase()).toBe('span');
    expect(work.lastElementChild?.tagName.toLowerCase()).toBe('svg');
    const think = screen.getByTestId('turn-thinking').querySelector('button');
    expect(think?.firstElementChild?.tagName.toLowerCase()).toBe('span');
    expect(think?.lastElementChild?.tagName.toLowerCase()).toBe('svg');
  });

  it('collapses think+tool process after a finished summary, leaving the answer visible', () => {
    renderGroup({ isStreaming: false });

    const toggle = processToggle();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.textContent).toContain('思考 2 次 · 工具调用 2 次');
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.queryByTestId('tools-flow')).toBeNull();
    expect(screen.getByTestId('answer').textContent).toBe('本地 CSV 配置');
  });

  it('shows per-round thinking while streaming in peek mode', () => {
    const view = renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 4),
      showThinkingContent: false,
    });

    expect(processToggle().textContent).toBe('工作中');
    expect(processToggle().getAttribute('aria-expanded')).toBe('true');
    const folds = screen.getAllByTestId('turn-thinking');
    expect(folds).toHaveLength(2);
    expect(folds[0].querySelector('button')?.getAttribute('aria-expanded')).toBe('false');
    expect(folds[1].querySelector('button')?.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.queryByText('再查天气')).toBeNull();
    expect(screen.queryByTestId('thinking-peek')).toBeNull();
    expect(screen.getAllByTestId('tools-flow').map((el) => el.textContent)).toEqual([
      'csv_get_config',
      'web_fetch',
    ]);
    expect(screen.queryByTestId('answer')).toBeNull();

    view.rerender(
      <TurnProcessGroup
        blocks={blocks}
        isStreaming={false}
        showThinkingContent={false}
        agents={[]}
        chats={[]}
        emptyFallback={<div>empty</div>}
        renderAnswer={(block) => <div data-testid="answer">{block.content}</div>}
      />,
    );

    expect(processToggle().getAttribute('aria-expanded')).toBe('false');
    expect(processToggle().textContent).toContain('思考 2 次 · 工具调用 2 次');
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.queryByTestId('thinking-peek')).toBeNull();
    expect(screen.getByTestId('answer').textContent).toBe('本地 CSV 配置');
  });

  it('hides thinking body while streaming when mode is hidden', () => {
    renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 4),
      thinkingDisplay: 'hidden',
    });

    expect(processToggle().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('thinking-peek')).toBeNull();
    expect(screen.queryByText('再查天气')).toBeNull();
    expect(screen.queryByTestId('tools-flow')).toBeNull();
  });

  it('keeps the work line as 工作中 when thinking is hidden', () => {
    renderGroup({
      isStreaming: true,
      thinkingDisplay: 'hidden',
      startedAtMs: Date.parse('2026-09-08T12:00:00.000Z'),
      blocks: [{ type: 'reasoning', content: '先读配置先读配置先读配置先读配置' }],
    });

    expect(processToggle().textContent).toBe('工作中');
    expect(screen.queryByTestId('thinking-peek')).toBeNull();
    expect(screen.queryByText('先读配置先读配置先读配置先读配置')).toBeNull();
  });

  it('shows live thinking time on the 思考 fold', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-08T12:00:00.000Z'));
    renderGroup({
      isStreaming: true,
      thinkingDisplay: 'full',
      blocks: [{ type: 'reasoning', content: '先读配置先读配置先读配置先读配置' }],
    });
    expect(processToggle().textContent).toBe('工作中');
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    const fold = screen.getByTestId('turn-thinking').querySelector('button');
    expect(fold?.textContent).toContain('思考中');
    expect(fold?.textContent).toContain('1s');
    expect(fold?.textContent).not.toMatch(/tok\/s/);
  });

  it('shows a fixed 5-line thinking peek only on the live round, then folds work', () => {
    const view = renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 3),
      thinkingDisplay: 'peek',
    });

    const folds = screen.getAllByTestId('turn-thinking');
    expect(folds[0].querySelector('button')?.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('先读配置')).toBeNull();
    const peek = screen.getByTestId('thinking-peek');
    expect(peek.getAttribute('data-peek-lines')).toBe('5');
    expect(peek.style.height).toBe('');
    expect(peek.style.maxHeight).toBe(THINKING_PEEK_HEIGHT);
    expect(peek.textContent).toContain('再查天气');
    expect(screen.getAllByTestId('tools-flow').length).toBeGreaterThan(0);

    view.rerender(
      <TurnProcessGroup
        blocks={blocks}
        isStreaming={false}
        thinkingDisplay="peek"
        agents={[]}
        chats={[]}
        emptyFallback={<div>empty</div>}
        renderAnswer={(block) => <div data-testid="answer">{block.content}</div>}
      />,
    );

    expect(screen.queryByTestId('thinking-peek')).toBeNull();
    expect(screen.queryByText('再查天气')).toBeNull();
    expect(screen.getByTestId('answer').textContent).toBe('本地 CSV 配置');
  });

  it('collapses the thinking body once that round finishes', () => {
    const view = renderGroup({
      isStreaming: true,
      thinkingDisplay: 'peek',
      blocks: [{ type: 'reasoning', content: '先读配置' }],
    });
    expect(screen.getByTestId('thinking-peek').textContent).toContain('先读配置');

    view.rerender(
      <TurnProcessGroup
        blocks={[{ type: 'reasoning', content: '先读配置' }, tool('local_run_snippet', true)]}
        isStreaming
        thinkingDisplay="peek"
        agents={[]}
        chats={[]}
        emptyFallback={<div>empty</div>}
        renderAnswer={(block) => <div data-testid="answer">{block.content}</div>}
      />,
    );

    const fold = screen.getByTestId('turn-thinking');
    expect(fold.querySelector('button')?.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.getByTestId('tools-flow').textContent).toBe('local_run_snippet');
  });

  it('renders HTML-like reasoning as plain text in the peek', () => {
    renderGroup({
      isStreaming: true,
      blocks: [{ type: 'reasoning', content: '比较 a < b 再调用 <tool>' }],
      thinkingDisplay: 'peek',
    });
    const peek = screen.getByTestId('thinking-peek');
    expect(peek.textContent).toContain('比较 a < b 再调用 <tool>');
    expect(peek.style.height).toBe('');
    expect(peek.style.maxHeight).toBe(THINKING_PEEK_HEIGHT);
  });

  it('folds the work row after the summary even when 完整显示 is on', () => {
    const view = renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 4),
      thinkingDisplay: 'full',
    });

    expect(processToggle().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getAllByTestId('turn-thinking')).toHaveLength(2);
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.queryByTestId('thinking-peek')).toBeNull();

    view.rerender(
      <TurnProcessGroup
        blocks={blocks}
        isStreaming={false}
        thinkingDisplay="full"
        agents={[]}
        chats={[]}
        emptyFallback={<div>empty</div>}
        renderAnswer={(block) => <div data-testid="answer">{block.content}</div>}
      />,
    );

    expect(processToggle().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.getByTestId('answer').textContent).toBe('本地 CSV 配置');
  });

  it('collapses work after finish even if the row was toggled during the stream', async () => {
    const view = renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 4),
      thinkingDisplay: 'full',
    });
    fireEvent.click(processToggle());
    expect(processToggle().getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(processToggle());
    expect(processToggle().getAttribute('aria-expanded')).toBe('true');

    view.rerender(
      <TurnProcessGroup
        blocks={blocks}
        isStreaming={false}
        thinkingDisplay="full"
        agents={[]}
        chats={[]}
        emptyFallback={<div>empty</div>}
        renderAnswer={(block) => <div data-testid="answer">{block.content}</div>}
      />,
    );

    await waitFor(() =>
      expect(processToggle().getAttribute('aria-expanded')).toBe('false'),
    );
  });

  it('follows the settings switch without a prop override', async () => {
    persistThinkingDisplay('hidden');
    renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 1),
    });
    expect(processToggle().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('先读配置')).toBeNull();
    persistThinkingDisplay('peek');
    await waitFor(() =>
      expect(processToggle().getAttribute('aria-expanded')).toBe('true'),
    );
    expect(screen.getByText('先读配置')).toBeTruthy();
    expect(screen.getByTestId('thinking-peek')).toBeTruthy();
  });

  it('keeps a mid-turn response as 回应 when the next think starts', () => {
    renderGroup({
      isStreaming: true,
      thinkingDisplay: 'full',
      blocks: [
        { type: 'reasoning', content: '先想一下' },
        { type: 'text', content: '我先搜工具' },
        { type: 'reasoning', content: '再写脚本' },
      ],
    });

    const thinking = screen.getAllByTestId('turn-thinking');
    expect(thinking).toHaveLength(2);
    expect(thinking[0].textContent).toContain('思考');
    expect(thinking[0].textContent).not.toContain('先想一下');
    expect(thinking[1].textContent).toContain('再写脚本');
    const response = screen.getByTestId('turn-response');
    expect(response.textContent).not.toContain('回应');
    expect(response.textContent).toContain('我先搜工具');
    expect(screen.queryByTestId('answer')).toBeNull();
  });

  it('lets each 思考 block collapse without hiding 回应', () => {
    renderGroup({
      isStreaming: true,
      thinkingDisplay: 'full',
      blocks: [
        { type: 'reasoning', content: '先想一下' },
        { type: 'text', content: '我先搜工具' },
        { type: 'reasoning', content: '再写脚本' },
      ],
    });

    const folds = screen.getAllByTestId('turn-thinking');
    const liveToggle = folds[1].querySelector('button');
    expect(folds[0].querySelector('button')?.getAttribute('aria-expanded')).toBe('false');
    expect(liveToggle?.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(liveToggle!);
    expect(liveToggle?.getAttribute('aria-expanded')).toBe('false');
    expect(folds[1].textContent).not.toContain('再写脚本');
    expect(screen.getByTestId('turn-response').textContent).toContain('我先搜工具');
  });

  it('promotes only the last text to 结论 after the stream ends', () => {
    renderGroup({
      isStreaming: false,
      thinkingDisplay: 'full',
      blocks: [
        { type: 'reasoning', content: '先想一下' },
        { type: 'text', content: '我先搜工具' },
        { type: 'reasoning', content: '再写脚本' },
        { type: 'text', content: 'PPT 已生成' },
      ],
    });

    expect(screen.getByTestId('answer').textContent).toBe('PPT 已生成');
    fireEvent.click(processToggle());
    expect(screen.getByTestId('turn-response').textContent).toContain('我先搜工具');
    expect(screen.queryByText('PPT 已生成')?.closest('[data-testid="turn-response"]')).toBeNull();
  });

  it('peek fold shows 5 lines when expanded and hides when collapsed', () => {
    renderGroup({ isStreaming: false, thinkingDisplay: 'peek' });
    fireEvent.click(processToggle());
    const fold = screen.getAllByTestId('turn-thinking')[0].querySelector('button');
    expect(fold?.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(fold!);
    expect(fold?.getAttribute('aria-expanded')).toBe('true');
    const peek = screen.getAllByTestId('thinking-peek')[0];
    expect(peek.getAttribute('data-peek-lines')).toBe('5');
    expect(peek.style.height).toBe('');
    expect(peek.style.maxHeight).toBe(THINKING_PEEK_HEIGHT);
    expect(peek.textContent).toContain('先读配置');
    fireEvent.click(fold!);
    expect(fold?.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('先读配置')).toBeNull();
  });

  it('expands the process again when the disclosure is clicked', () => {
    renderGroup({ isStreaming: false });
    fireEvent.click(processToggle());
    expect(processToggle().getAttribute('aria-expanded')).toBe('true');
    const folds = screen.getAllByTestId('turn-thinking');
    expect(folds).toHaveLength(2);
    expect(folds[1].querySelector('button')?.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(folds[1].querySelector('button')!);
    expect(folds[1].querySelector('button')?.getAttribute('aria-expanded')).toBe('true');
    const peek = folds[1].querySelector('[data-testid="thinking-peek"]');
    expect(peek?.getAttribute('data-peek-lines')).toBe('5');
    expect((peek as HTMLElement).style.height).toBe('');
    expect((peek as HTMLElement).style.maxHeight).toBe(THINKING_PEEK_HEIGHT);
    expect(peek?.textContent).toContain('再查天气');
    fireEvent.click(folds[1].querySelector('button')!);
    expect(folds[1].querySelector('button')?.getAttribute('aria-expanded')).toBe('false');
    expect(folds[1].querySelector('[data-testid="thinking-peek"]')).toBeNull();
    expect(screen.queryByText('再查天气')).toBeNull();
    expect(screen.getAllByTestId('tools-flow')).toHaveLength(2);
  });

  it('hides thinking body when the setting is 隐藏, even after expanding work', () => {
    renderGroup({ isStreaming: false, thinkingDisplay: 'hidden' });
    fireEvent.click(processToggle());
    expect(screen.queryByTestId('turn-thinking')).toBeNull();
    expect(screen.queryByTestId('thinking-peek')).toBeNull();
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.getAllByTestId('tools-flow')).toHaveLength(2);
  });

  it('folds a tools-only turn when the setting is off', () => {
    renderGroup({
      isStreaming: false,
      blocks: [tool('csv_get_config')],
      showThinkingContent: false,
    });
    expect(processToggle().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('tools-flow')).toBeNull();
    expect(processToggle().textContent).toContain('工具调用 1 次');
  });

  it('folds a finished tools-only turn even when 完整显示 is on', () => {
    renderGroup({
      isStreaming: false,
      blocks: [tool('csv_get_config')],
      showThinkingContent: true,
    });
    expect(processToggle().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('tools-flow')).toBeNull();
    expect(processToggle().textContent).toContain('工具调用 1 次');
  });

  it('keeps the work line as 工作中 while a tool is running', () => {
    renderGroup({
      isStreaming: true,
      blocks: [{ type: 'reasoning', content: '先读配置' }, tool('local_run_snippet', true)],
      showThinkingContent: false,
    });
    expect(processToggle().textContent).toBe('工作中');
    const fold = screen.getByTestId('turn-thinking');
    expect(fold.querySelector('button')?.getAttribute('aria-expanded')).toBe('false');
    expect(fold.textContent).not.toContain('先读配置');
    expect(screen.getByTestId('tools-flow').textContent).toBe('local_run_snippet');
  });

  it('puts live thinking time on the 思考 fold, not the work line', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-08T12:00:00.000Z'));
    renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 1),
      startedAtMs: Date.parse('2026-09-08T12:00:00.000Z'),
      thinkingDisplay: 'full',
    });
    expect(processToggle().textContent).toBe('工作中');
    act(() => {
      vi.advanceTimersByTime(12_000);
    });
    expect(processToggle().textContent).toBe('工作中');
    expect(screen.getByTestId('turn-thinking').querySelector('button')?.textContent).toContain('12s');
  });

  it('keeps thinking duration on the 思考 fold after the turn finishes', () => {
    renderGroup({
      isStreaming: false,
      thinkingDisplay: 'full',
      blocks: [
        { type: 'reasoning', content: '先读配置', durationMs: 8000 },
        { type: 'text', content: '本地 CSV 配置' },
      ],
    });
    fireEvent.click(processToggle());
    expect(screen.getByTestId('turn-thinking').querySelector('button')?.textContent).toBe(
      '思考 · 8s',
    );
  });

  it('appends worked duration after the turn finishes', () => {
    renderGroup({ isStreaming: false, durationMs: 83_000 });
    expect(processToggle().textContent).toContain(
      '工作了 1m 23s · 思考 2 次 · 工具调用 2 次',
    );
  });

  it('omits worked duration for sub-second finished turns', () => {
    renderGroup({ isStreaming: false, durationMs: 400 });
    const label = processToggle().textContent ?? '';
    expect(label).toContain('思考 2 次 · 工具调用 2 次');
    expect(label).not.toContain('工作了');
  });

  it('freezes live elapsed when the stream ends before durationMs lands', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-08T12:00:12.000Z'));
    const startedAtMs = Date.parse('2026-09-08T12:00:00.000Z');
    const view = renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 4),
      startedAtMs,
      showThinkingContent: false,
    });
    view.rerender(
      <TurnProcessGroup
        blocks={blocks}
        isStreaming={false}
        showThinkingContent={false}
        agents={[]}
        chats={[]}
        emptyFallback={<div>empty</div>}
        renderAnswer={(block) => <div data-testid="answer">{block.content}</div>}
      />,
    );
    expect(processToggle().textContent).toContain(
      '工作了 12s · 思考 2 次 · 工具调用 2 次',
    );
  });
});

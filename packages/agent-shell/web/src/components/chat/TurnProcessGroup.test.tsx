import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  SHOW_THINKING_CONTENT_STORAGE_KEY,
  persistShowThinkingContent,
} from '@/lib/show-thinking-content';
import type { TurnBlock } from './turn-timeline';

vi.mock('./Markdown', () => ({
  Markdown: ({ children }: { children: string }) => <span>{children}</span>,
}));

vi.mock('./ExecutedActionsCard', () => ({
  ToolsFlow: ({ actions }: { actions: Array<{ tool: string }> }) => (
    <div data-testid="tools-flow">{actions.map((action) => action.tool).join(',')}</div>
  ),
}));

const { TurnProcessGroup } = await import('./TurnProcessGroup');

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

function renderGroup(overrides: {
  blocks?: TurnBlock[];
  isStreaming?: boolean;
  startedAtMs?: number;
  durationMs?: number;
  showThinkingContent?: boolean;
} = {}) {
  return render(
    <TurnProcessGroup
      blocks={overrides.blocks ?? blocks}
      isStreaming={overrides.isStreaming ?? false}
      startedAtMs={overrides.startedAtMs}
      durationMs={overrides.durationMs}
      showThinkingContent={overrides.showThinkingContent}
      agents={[]}
      chats={[]}
      emptyFallback={<div>empty</div>}
      renderAnswer={(block) => <div data-testid="answer">{block.content}</div>}
    />,
  );
}

describe('TurnProcessGroup', () => {
  it('collapses think+tool process after a finished summary, leaving the answer visible', () => {
    renderGroup({ isStreaming: false });

    const toggle = screen.getByRole('button');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.textContent).toContain('2 次工具调用 · 已思考');
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.queryByTestId('tools-flow')).toBeNull();
    expect(screen.getByTestId('answer').textContent).toBe('本地 CSV 配置');
  });

  it('keeps the process collapsed while streaming unless the setting is on', () => {
    const view = renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 4),
      showThinkingContent: false,
    });

    expect(screen.getByRole('button').textContent).toContain('思考中');
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('tools-flow')).toBeNull();
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

    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false');
    expect(screen.getByRole('button').textContent).toContain('2 次工具调用 · 已思考');
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.queryByTestId('thinking-peek')).toBeNull();
    expect(screen.getByTestId('answer').textContent).toBe('本地 CSV 配置');
  });

  it('shows a fixed 7-line thinking peek while streaming, then folds it', () => {
    const view = renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 4),
      showThinkingContent: false,
    });

    const peek = screen.getByTestId('thinking-peek');
    expect(peek.getAttribute('data-peek-lines')).toBe('7');
    expect(peek.className).toContain('h-[7lh]');
    expect(screen.getByText('再查天气')).toBeTruthy();
    expect(screen.queryByTestId('tools-flow')).toBeNull();

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

    expect(screen.queryByTestId('thinking-peek')).toBeNull();
    expect(screen.queryByText('再查天气')).toBeNull();
    expect(screen.getByTestId('answer').textContent).toBe('本地 CSV 配置');
  });

  it('expands while streaming when 显示思考内容 is on, then auto-collapses', () => {
    const view = renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 4),
      showThinkingContent: true,
    });

    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('先读配置')).toBeTruthy();
    expect(screen.queryByTestId('thinking-peek')).toBeNull();

    view.rerender(
      <TurnProcessGroup
        blocks={blocks}
        isStreaming={false}
        showThinkingContent={true}
        agents={[]}
        chats={[]}
        emptyFallback={<div>empty</div>}
        renderAnswer={(block) => <div data-testid="answer">{block.content}</div>}
      />,
    );

    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.getByTestId('answer').textContent).toBe('本地 CSV 配置');
  });

  it('follows the settings switch without a prop override', async () => {
    renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 1),
    });
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false');
    persistShowThinkingContent(true);
    await waitFor(() =>
      expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true'),
    );
    expect(screen.getByText('先读配置')).toBeTruthy();
  });

  it('expands the process again when the disclosure is clicked', () => {
    renderGroup({ isStreaming: false });
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('再查天气')).toBeTruthy();
    expect(screen.getAllByTestId('tools-flow')).toHaveLength(2);
  });

  it('folds a tools-only turn when the setting is off', () => {
    renderGroup({
      isStreaming: false,
      blocks: [tool('csv_get_config')],
      showThinkingContent: false,
    });
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('tools-flow')).toBeNull();
    expect(screen.getByRole('button').textContent).toContain('1 次工具调用');
  });

  it('does not fold a tools-only turn when the setting is on', () => {
    renderGroup({
      isStreaming: false,
      blocks: [tool('csv_get_config')],
      showThinkingContent: true,
    });
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByTestId('tools-flow').textContent).toBe('csv_get_config');
  });

  it('names a running tool on the status line', () => {
    renderGroup({
      isStreaming: true,
      blocks: [{ type: 'reasoning', content: '先读配置' }, tool('local_run_snippet', true)],
      showThinkingContent: false,
    });
    expect(screen.getByRole('button').textContent).toContain('调用工具中 · local_run_snippet');
    expect(screen.getByTestId('thinking-peek')).toBeTruthy();
    expect(screen.queryByTestId('tools-flow')).toBeNull();
  });

  it('shows elapsed while streaming', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-08T12:00:12.000Z'));
    renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 1),
      startedAtMs: Date.parse('2026-09-08T12:00:00.000Z'),
      showThinkingContent: false,
    });
    expect(screen.getByRole('button').textContent).toContain('思考中');
    expect(screen.getByRole('button').textContent).toContain('12s');
  });

  it('appends worked duration after the turn finishes', () => {
    renderGroup({ isStreaming: false, durationMs: 83_000 });
    expect(screen.getByRole('button').textContent).toContain(
      '2 次工具调用 · 已思考 · 工作了 1m 23s',
    );
  });

  it('omits worked duration for sub-second finished turns', () => {
    renderGroup({ isStreaming: false, durationMs: 400 });
    const label = screen.getByRole('button').textContent ?? '';
    expect(label).toContain('2 次工具调用 · 已思考');
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
    expect(screen.getByRole('button').textContent).toContain(
      '2 次工具调用 · 已思考 · 工作了 12s',
    );
  });
});

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
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
});

const tool = (name: string): TurnBlock => ({
  type: 'tools',
  actions: [{ tool: name, arguments: {} }],
});

const blocks: TurnBlock[] = [
  { type: 'reasoning', content: '先读配置' },
  tool('cflog_get_config'),
  { type: 'reasoning', content: '再查天气' },
  tool('web_fetch'),
  { type: 'text', content: '本地 CIFLog 配置' },
];

function renderGroup(overrides: {
  blocks?: TurnBlock[];
  isStreaming?: boolean;
  startedAtMs?: number;
  durationMs?: number;
} = {}) {
  return render(
    <TurnProcessGroup
      blocks={overrides.blocks ?? blocks}
      isStreaming={overrides.isStreaming ?? false}
      startedAtMs={overrides.startedAtMs}
      durationMs={overrides.durationMs}
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
    expect(screen.getByTestId('answer').textContent).toBe('本地 CIFLog 配置');
  });

  it('keeps the process expanded while streaming, then auto-collapses when the summary lands', () => {
    const view = renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 4),
    });

    expect(screen.getByRole('button').textContent).toContain('正在执行…');
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('先读配置')).toBeTruthy();
    expect(screen.queryByTestId('answer')).toBeNull();

    view.rerender(
      <TurnProcessGroup
        blocks={blocks}
        isStreaming={false}
        agents={[]}
        chats={[]}
        emptyFallback={<div>empty</div>}
        renderAnswer={(block) => <div data-testid="answer">{block.content}</div>}
      />,
    );

    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false');
    expect(screen.getByRole('button').textContent).toContain('2 次工具调用 · 已思考');
    expect(screen.queryByText('先读配置')).toBeNull();
    expect(screen.getByTestId('answer').textContent).toBe('本地 CIFLog 配置');
  });

  it('expands the process again when the disclosure is clicked', () => {
    renderGroup({ isStreaming: false });
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('再查天气')).toBeTruthy();
    expect(screen.getAllByTestId('tools-flow')).toHaveLength(2);
  });

  it('does not fold a tools-only turn that never produced a summary', () => {
    renderGroup({
      isStreaming: false,
      blocks: [tool('cflog_get_config')],
    });
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByTestId('tools-flow').textContent).toBe('cflog_get_config');
    expect(screen.queryByTestId('answer')).toBeNull();
  });

  it('shows Codex-style elapsed while streaming', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-08T12:00:12.000Z'));
    renderGroup({
      isStreaming: true,
      blocks: blocks.slice(0, 4),
      startedAtMs: Date.parse('2026-09-08T12:00:00.000Z'),
    });
    expect(screen.getByRole('button').textContent).toContain('正在执行… 12s');
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
    });
    view.rerender(
      <TurnProcessGroup
        blocks={blocks}
        isStreaming={false}
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

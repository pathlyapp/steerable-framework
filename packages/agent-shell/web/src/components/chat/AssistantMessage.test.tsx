/**
 * AssistantMessage 时间戳行：分享对话的入口从标题栏挪到最近一条助手回复
 * 时间戳行上的分享、复制、重新生成共用悬停显隐。
 *
 * 回合产物列表：turnFiles 在回合收尾后渲染到回答气泡之下（流式中不渲染）。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '@steerable/agent-protocol';
import { AssistantMessage } from './AssistantMessage';

vi.mock('@/lib/local-api', () => ({
  openLocalPath: vi.fn(async () => ({ success: true })),
}));

afterEach(cleanup);

const MESSAGE: ChatMessage = {
  id: 'm1',
  role: 'assistant',
  content: '问好完成。',
  createdAt: '2026-09-13T09:03:00.000Z',
};

describe('AssistantMessage 分享', () => {
  it('没有 onShare 时不画分享按钮', () => {
    render(
      <AssistantMessage
        message={MESSAGE}
        isStreaming={false}
        agents={[]}
        currentAgent={null}
      />,
    );
    expect(screen.queryByRole('button', { name: '分享对话截图' })).toBeNull();
  });

  it('流式中不画分享按钮', () => {
    render(
      <AssistantMessage
        message={MESSAGE}
        isStreaming
        agents={[]}
        currentAgent={null}
        onShare={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: '分享对话截图' })).toBeNull();
  });

  it('点分享会调用 onShare，成功后提示已复制', async () => {
    const onShare = vi.fn().mockResolvedValue(true);
    render(
      <AssistantMessage
        message={MESSAGE}
        isStreaming={false}
        agents={[]}
        currentAgent={null}
        onShare={onShare}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '分享对话截图' }));
    expect(screen.getByRole('button', { name: '分享对话截图' }).className).toContain(
      'group-hover/message:opacity-100',
    );
    await waitFor(() => expect(onShare).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '分享对话截图' }).getAttribute('title')).toBe(
        '截图已复制到剪贴板',
      ),
    );
  });
});

describe('AssistantMessage token 速度', () => {
  it('结束后把模型请求 tok/s 画在时间戳旁边', () => {
    render(
      <AssistantMessage
        message={MESSAGE}
        isStreaming={false}
        agents={[]}
        currentAgent={null}
        llmSpeed={{
          tokens: 13,
          elapsedMs: 1000,
          live: false,
          closedMs: 1000,
          requestStartedAt: null,
        }}
      />,
    );
    expect(screen.getByTestId('turn-token-speed').textContent).toBe('13 tok/s');
  });

  it('没有用时或几乎没产出时不画速度', () => {
    render(
      <AssistantMessage
        message={MESSAGE}
        isStreaming={false}
        agents={[]}
        currentAgent={null}
      />,
    );
    expect(screen.queryByTestId('turn-token-speed')).toBeNull();
  });
});

describe('AssistantMessage 回合产物列表', () => {
  it('turnFiles 非空且非流式时渲染在回答之下', () => {
    render(
      <AssistantMessage
        message={MESSAGE}
        isStreaming={false}
        agents={[]}
        currentAgent={null}
        turnFiles={[{ path: '/proj/自我介绍.pptx', kind: 'created', size: 2048 }]}
      />,
    );
    expect(screen.getByText('自我介绍.pptx')).toBeTruthy();
    expect(screen.getByText('本轮产生了 1 个文件，点击打开')).toBeTruthy();
  });

  it('流式中不渲染产物列表（数据要等回合收尾）', () => {
    render(
      <AssistantMessage
        message={MESSAGE}
        isStreaming
        agents={[]}
        currentAgent={null}
        turnFiles={[{ path: '/proj/a.md', kind: 'created' }]}
      />,
    );
    expect(screen.queryByText('a.md')).toBeNull();
  });

  it('没有 turnFiles 时不渲染产物卡', () => {
    const { container } = render(
      <AssistantMessage
        message={MESSAGE}
        isStreaming={false}
        agents={[]}
        currentAgent={null}
      />,
    );
    expect(container.querySelector('[data-turn-files]')).toBeNull();
  });
});

/**
 * AssistantMessage 时间戳行：分享对话的入口从标题栏挪到最近一条助手回复
 * 时间戳行上的分享、复制、重新生成共用悬停显隐。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '@steerable/agent-protocol';
import { AssistantMessage } from './AssistantMessage';

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

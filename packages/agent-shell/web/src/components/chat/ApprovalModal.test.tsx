/**
 * ApprovalModalHost 的网络出口（network_egress）分支契约（W-egress-ask）：
 *   - category=network_egress 的请示以「请求访问外网」标题 + host:port
 *     展示，并带仿冒域名警示；
 *   - 「始终」变体对网络出口隐藏（代理白名单是会话寿命的进程级状态，
 *     持久放行要走设置的出网白名单，不是这个模态）；
 *   - 普通工具调用的 7 变体行为不回归。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ApprovalPromptRequest } from '@/lib/electron-bridge';
import { ApprovalPromptMenu } from './ApprovalModal';

afterEach(() => {
  cleanup();
});

const EGRESS_REQUEST: ApprovalPromptRequest = {
  requestId: 'req-egress',
  toolName: 'network_egress',
  arguments: { host: 'cdn.example.com', port: 443, url: 'https://cdn.example.com/x.js' },
  mode: 'other',
  category: 'network_egress',
  round: 0,
};

const SHELL_REQUEST: ApprovalPromptRequest = {
  requestId: 'req-shell',
  toolName: 'bash',
  arguments: { command: 'rm -rf /tmp/x' },
  mode: 'destructive',
  category: 'bash',
  round: 0,
};

describe('ApprovalModalHost 网络出口分支（W-egress-ask）', () => {
  it('以 host:port 标题 + 仿冒警示展示，并隐藏「始终」变体', () => {
    render(
      <ApprovalPromptMenu request={EGRESS_REQUEST} pendingCount={0} onDecide={vi.fn()} />,
    );

    expect(screen.getByText('Agent 请求访问外网')).toBeTruthy();
    expect(screen.getByText('cdn.example.com:443')).toBeTruthy();
    expect(screen.getByText(/仿冒域名/)).toBeTruthy();
    // 会话档保留，「始终」档隐藏。
    expect(screen.getByText('允许一次')).toBeTruthy();
    expect(screen.getByText('本次会话允许')).toBeTruthy();
    expect(screen.queryByText('始终允许')).toBeNull();
    expect(screen.queryByText('始终拒绝')).toBeNull();
  });

  it('普通工具调用的 7 变体不回归', () => {
    const onDecide = vi.fn();
    render(
      <ApprovalPromptMenu request={SHELL_REQUEST} pendingCount={1} onDecide={onDecide} />,
    );

    expect(screen.getByText('Agent 请求执行')).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText('还有 1 个待审批')).toBeTruthy();
    expect(screen.getByText('始终允许')).toBeTruthy();
    expect(screen.getByText('始终拒绝')).toBeTruthy();
    expect(screen.getByText(/工作区沙箱/)).toBeTruthy();
    fireEvent.click(screen.getByText('允许一次'));
    expect(onDecide).toHaveBeenCalledWith('allow_once');
  });
});

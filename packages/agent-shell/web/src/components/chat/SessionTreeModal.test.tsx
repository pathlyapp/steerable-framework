/**
 * SessionTreeModal 的接线契约：
 *   - 打开时经 localBackend 拉 GET /branches/tree，DFS 渲染缩进树，
 *     activeRecordId 行高亮带 ✓；
 *   - 点击（或 Enter）非 active 节点 → POST /branches/activate →
 *     onBranchSwitched + onClose；
 *   - 树只有当前记录一个节点时显示空态文案；
 *   - Escape 关闭。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ChatBranchTreeResponse } from '@/lib/local-api';
import { SessionTreeModal } from './SessionTreeModal';

afterEach(() => {
  cleanup();
  delete (window as { electron?: unknown }).electron;
});

const TREE_RESPONSE: ChatBranchTreeResponse = {
  activeRecordId: 'chat_1:r2',
  nodeCount: 4,
  truncated: false,
  tree: {
    recordId: 'chat_1',
    sourceRecordId: null,
    sourceUntilSeq: null,
    label: 'root',
    depth: 0,
    children: [
      {
        recordId: 'chat_1:r2',
        sourceRecordId: 'chat_1',
        sourceUntilSeq: 5,
        label: 'question 1',
        depth: 1,
        children: [
          {
            recordId: 'chat_1:r2:x',
            sourceRecordId: 'chat_1:r2',
            sourceUntilSeq: 2,
            label: 'variant question',
            depth: 2,
            children: [],
          },
        ],
      },
      {
        recordId: 'chat_1:r3',
        sourceRecordId: 'chat_1',
        sourceUntilSeq: 3,
        label: 'question 0',
        depth: 1,
        children: [],
      },
    ],
  },
};

function installBridge(treeResponse: ChatBranchTreeResponse | null = TREE_RESPONSE) {
  const request = vi.fn((input: { method: string; path: string; body?: unknown }) => {
    if (input.method === 'GET' && input.path.endsWith('/branches/tree')) {
      return Promise.resolve(treeResponse);
    }
    if (input.method === 'POST' && input.path.endsWith('/branches/activate')) {
      return Promise.resolve({ activeRecordId: 'x', messageCount: 3 });
    }
    return Promise.reject(new Error(`unexpected request: ${input.method} ${input.path}`));
  });
  (window as { electron?: unknown }).electron = { localBackend: { request } };
  return { request };
}

describe('SessionTreeModal', () => {
  it('渲染全树：缩进行 + active 节点高亮', async () => {
    installBridge();
    render(<SessionTreeModal chatId="chat_1" onClose={() => {}} />);

    await waitFor(() => expect(screen.queryByText('正在加载分支…')).toBeNull());

    const rows = screen.getAllByRole('button').filter((b) => b.hasAttribute('data-tree-row'));
    expect(rows).toHaveLength(4);
    // DFS 顺序：root → r2 → r2:x → r3；label 按行展示（label 在 truncate span 里）。
    const labels = rows.map(
      (r) => (r.querySelector('span.truncate') as HTMLElement | null)?.textContent,
    );
    expect(labels).toEqual(['root', 'question 1', 'variant question', 'question 0']);
    // 连接线前缀：根无前缀，孙节点带 │ 缩进。
    const prefixes = rows.map(
      (r) => (r.querySelector('span.font-mono') as HTMLElement | null)?.textContent ?? '',
    );
    expect(prefixes).toEqual(['', '├─ ', '│  └─ ', '└─ ']);
    // active 标记在 r2 行。
    expect(rows[1].getAttribute('data-active')).toBe('true');
    expect(rows[0].getAttribute('data-active')).toBeNull();
  });

  it('点击堂兄弟节点：activate + onBranchSwitched + 关闭', async () => {
    const { request } = installBridge();
    const onClose = vi.fn();
    const onBranchSwitched = vi.fn();
    render(
      <SessionTreeModal chatId="chat_1" onClose={onClose} onBranchSwitched={onBranchSwitched} />,
    );
    await waitFor(() => screen.getByText('question 0'));

    fireEvent.click(screen.getByText('question 0'));

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(request).toHaveBeenCalledWith({
      method: 'POST',
      path: '/api/v2/chats/chat_1/branches/activate',
      body: { recordId: 'chat_1:r3' },
    });
    expect(onBranchSwitched).toHaveBeenCalledTimes(1);
  });

  it('点击 active 节点不触发切换', async () => {
    const { request } = installBridge();
    render(<SessionTreeModal chatId="chat_1" onClose={() => {}} />);
    await waitFor(() => screen.getByText('question 1'));

    fireEvent.click(screen.getByText('question 1'));

    expect(
      request.mock.calls.filter(([input]) => (input as { method: string }).method === 'POST'),
    ).toHaveLength(0);
  });

  it('单节点树显示空态', async () => {
    installBridge({
      activeRecordId: 'chat_1',
      nodeCount: 1,
      truncated: false,
      tree: {
        recordId: 'chat_1',
        sourceRecordId: null,
        sourceUntilSeq: null,
        label: 'root',
        depth: 0,
        children: [],
      },
    });
    render(<SessionTreeModal chatId="chat_1" onClose={() => {}} />);

    await waitFor(() => screen.getByText(/重新生成回复后，旧版本会保留在这里/));
    expect(screen.queryAllByRole('button').filter((b) => b.hasAttribute('data-tree-row')))
      .toHaveLength(0);
  });

  it('Escape 关闭模态', async () => {
    const onClose = vi.fn();
    installBridge();
    render(<SessionTreeModal chatId="chat_1" onClose={onClose} />);
    await waitFor(() => screen.getByText('question 0'));

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

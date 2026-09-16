import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { UseChatsAndAgentsResult } from '@/hooks/useChatsAndAgents';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';

vi.mock('@/lib/electron-bridge', () => ({
  isElectron: () => false,
  getElectronBridge: () => null,
}));

vi.mock('@/brand', () => ({
  BRAND_NAME: 'CIFLog智能助手',
  getBrandLogoUrl: () => 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"/>',
}));

const { AgentSidebar } = await import('./AgentSidebar');

afterEach(() => cleanup());

const agent: LocalChatAgent = {
  id: 'local-assistant',
  slug: 'local-assistant',
  name: '电脑操作员',
  icon: null,
  color: '#4f46e5',
  description: null,
  rolePrompt: null,
  isBuiltin: true,
};

const existingChat: LocalChat = {
  id: 'chat-with-content',
  projectId: null,
  userId: 'local',
  title: '已经聊过的对话',
  agentId: agent.id,
  createdAt: '2026-01-01T00:00:00.000Z',
  updatedAt: '2026-01-01T00:01:00.000Z',
  isPinned: false,
  systemPrompt: null,
  pinnedRefs: null,
};

function LocationProbe() {
  const loc = useLocation();
  return (
    <div data-testid="loc">
      {loc.pathname}
      {loc.search}
    </div>
  );
}

function renderSidebar(
  initialEntry: string,
  createChat = vi.fn(),
) {
  const data: UseChatsAndAgentsResult = {
    chats: [existingChat],
    agents: [agent],
    isLoading: false,
    error: null,
    selectedAgentId: agent.id,
    setSelectedAgentId: vi.fn(),
    refreshChats: vi.fn(async () => {}),
    refreshAgents: vi.fn(async () => {}),
    createChat,
    deleteChat: vi.fn(async () => true),
    patchChatTitle: vi.fn(),
    isLoadingMoreChats: false,
    hasMoreChats: false,
    loadMoreChats: vi.fn(async () => {}),
  };

  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          element={
            <>
              <AgentSidebar
                data={data}
                rightPanel={null}
                onToggleRightPanel={() => {}}
                chatSlots={[]}
                onCollapse={() => {}}
              />
              <LocationProbe />
              <Outlet />
            </>
          }
        >
          <Route path="/" element={<div />} />
          <Route path="/agent" element={<div />} />
          <Route path="/agent/:chatId" element={<div />} />
          <Route path="/settings" element={<div />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );

  return { createChat };
}

describe('AgentSidebar 新对话不落库', () => {
  it('打开落地页且不调用 createChat', () => {
    const { createChat } = renderSidebar('/agent/chat-with-content');
    fireEvent.click(screen.getByTestId('sidebar-new-chat'));
    expect(createChat).not.toHaveBeenCalled();
    expect(screen.getByTestId('loc').textContent).toBe('/agent');
  });

  it('连点两次也不会多出侧栏行', () => {
    renderSidebar('/agent');
    const before = screen.getAllByTestId('sidebar-chat-row').length;
    fireEvent.click(screen.getByTestId('sidebar-new-chat'));
    fireEvent.click(screen.getByTestId('sidebar-new-chat'));
    expect(screen.getAllByTestId('sidebar-chat-row')).toHaveLength(before);
  });

  it('打开智能体管理页', () => {
    renderSidebar('/agent');
    fireEvent.click(screen.getByTestId('sidebar-agent-settings'));
    expect(screen.getByTestId('loc').textContent).toBe('/settings?section=agents');
  });
});

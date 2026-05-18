/**
 * Tests for `useChatList`. The generics are deliberately unconstrained, so
 * here we instantiate with simple `{id,title}`-shaped chats and agents to
 * keep the suite readable.
 */

import { describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import {
  useChatList,
  type ChatListTransport,
} from './useChatList';

interface ChatDouble {
  id: string;
  title: string;
}

interface AgentDouble {
  id: string;
}

function makeTransport(): {
  transport: ChatListTransport<ChatDouble, AgentDouble>;
  listChats: ReturnType<typeof vi.fn>;
  listAgents: ReturnType<typeof vi.fn>;
  createChat: ReturnType<typeof vi.fn>;
  deleteChat: ReturnType<typeof vi.fn>;
} {
  const listChats = vi.fn(async ({ page }: { page: number }) => ({
    chats:
      page === 1
        ? [
            { id: 'c1', title: 'First' },
            { id: 'c2', title: 'Second' },
          ]
        : [{ id: 'c3', title: 'Third' }],
    hasMore: page === 1,
  }));
  const listAgents = vi.fn(async () => [{ id: 'a1' }, { id: 'a2' }]);
  const createChat = vi.fn(async () => ({ id: 'c-new', title: 'New' }));
  const deleteChat = vi.fn(async () => true);
  return {
    transport: {
      listChats,
      listAgents,
      createChat,
      deleteChat,
      getChatId: (c) => c.id,
      setChatTitle: (c, title) => ({ ...c, title }),
      getAgentId: (a) => a.id,
    },
    listChats,
    listAgents,
    createChat,
    deleteChat,
  };
}

describe('useChatList', () => {
  it('loads chats + agents on mount', async () => {
    const { transport } = makeTransport();
    const { result } = renderHook(() => useChatList({ transport, pageSize: 50 }));

    await waitFor(() => {
      expect(result.current.chats.length).toBe(2);
      expect(result.current.agents.length).toBe(2);
    });
    expect(result.current.hasMoreChats).toBe(true);
  });

  it('loadMoreChats appends page 2 results', async () => {
    const { transport } = makeTransport();
    const { result } = renderHook(() => useChatList({ transport, pageSize: 50 }));

    await waitFor(() => expect(result.current.chats.length).toBe(2));

    await act(async () => {
      await result.current.loadMoreChats();
    });

    expect(result.current.chats.map((c) => c.id)).toEqual(['c1', 'c2', 'c3']);
    expect(result.current.hasMoreChats).toBe(false);
  });

  it('patchChatTitle updates without re-fetching', async () => {
    const { transport, listChats } = makeTransport();
    const { result } = renderHook(() => useChatList({ transport, pageSize: 50 }));
    await waitFor(() => expect(result.current.chats.length).toBe(2));

    act(() => {
      result.current.patchChatTitle('c1', 'Renamed');
    });

    expect(result.current.chats[0].title).toBe('Renamed');
    expect(listChats).toHaveBeenCalledTimes(1);
  });

  it('createChat triggers refresh + returns id', async () => {
    const { transport } = makeTransport();
    const { result } = renderHook(() => useChatList({ transport, pageSize: 50 }));
    await waitFor(() => expect(result.current.chats.length).toBe(2));

    let id: string | null = null;
    await act(async () => {
      id = await result.current.createChat();
    });
    expect(id).toBe('c-new');
  });

  it('deleteChat optimistically removes the chat', async () => {
    const { transport } = makeTransport();
    const { result } = renderHook(() => useChatList({ transport, pageSize: 50 }));
    await waitFor(() => expect(result.current.chats.length).toBe(2));

    await act(async () => {
      await result.current.deleteChat('c1');
    });

    expect(result.current.chats.map((c) => c.id)).toEqual(['c2']);
  });

  it('selectedAgentId is settable and persists across renders', async () => {
    const { transport } = makeTransport();
    const { result, rerender } = renderHook(() =>
      useChatList({ transport, pageSize: 50 }),
    );
    await waitFor(() => expect(result.current.agents.length).toBe(2));

    act(() => result.current.setSelectedAgentId('a2'));
    expect(result.current.selectedAgentId).toBe('a2');
    rerender();
    expect(result.current.selectedAgentId).toBe('a2');
  });
});

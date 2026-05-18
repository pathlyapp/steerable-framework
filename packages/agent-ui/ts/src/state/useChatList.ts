/**
 * `useChatList` — paginated chat-summary + agent-catalog state for the chat
 * sidebar. Generic in the chat and agent shapes so cloud and local apps reuse
 * the same hook (the cloud schema has extra fields on `Chat`, the local
 * schema has fewer).
 *
 * Lifted from deeppath-agent's `useChatsAndAgents.ts` and deeppath's
 * `useChatUI` chat-list subset. The hook owns:
 *   - paginated chat list with "load more" and refresh
 *   - the agent catalog (single page for now)
 *   - the agent picked for the next new chat
 *   - in-place title patching (used by `chat_title_updated` SSE events to
 *     avoid clobbering pagination state with a full refresh)
 *   - create / delete chat actions, with optimistic patching of the local
 *     list.
 *
 * The transport is provided by the consumer so the same hook serves Electron
 * IPC, the cloud REST client, or any other backend.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react';

export interface ChatListTransport<TChat, TAgent> {
  listChats: (input: { page: number; pageSize: number }) => Promise<{
    chats: TChat[];
    hasMore: boolean;
  }>;
  listAgents: () => Promise<TAgent[]>;
  createChat?: (input: { agentId?: string }) => Promise<TChat | string>;
  deleteChat?: (chatId: string) => Promise<boolean>;
  /** Tell the hook how to read a chat's id and title. */
  getChatId: (chat: TChat) => string;
  setChatTitle?: (chat: TChat, title: string) => TChat;
  /** Tell the hook how to read an agent's id (for `selectedAgentId`). */
  getAgentId: (agent: TAgent) => string;
}

export interface UseChatListOptions<TChat, TAgent> {
  transport: ChatListTransport<TChat, TAgent>;
  /** Defaults to 50. */
  pageSize?: number;
  /** Skip the mount-time fetch (e.g. SSR contexts). */
  skipInitialLoad?: boolean;
}

export interface UseChatListReturn<TChat, TAgent> {
  chats: TChat[];
  agents: TAgent[];
  isLoading: boolean;
  isLoadingMoreChats: boolean;
  hasMoreChats: boolean;
  error: Error | null;
  selectedAgentId: string | null;
  /**
   * Accepts either a plain value or a React-style updater function so
   * adapters can patch the selection without re-reading the current value.
   */
  setSelectedAgentId: Dispatch<SetStateAction<string | null>>;
  refreshChats: () => Promise<void>;
  refreshAgents: () => Promise<void>;
  loadMoreChats: () => Promise<void>;
  createChat: (input?: { agentId?: string }) => Promise<string | null>;
  deleteChat: (chatId: string) => Promise<boolean>;
  /** In-place title patcher — does not hit the server. */
  patchChatTitle: (chatId: string, title: string) => void;
}

export function useChatList<TChat, TAgent>(
  options: UseChatListOptions<TChat, TAgent>,
): UseChatListReturn<TChat, TAgent> {
  const pageSize = options.pageSize ?? 50;
  const transportRef = useRef(options.transport);
  transportRef.current = options.transport;

  const [chats, setChats] = useState<TChat[]>([]);
  const [agents, setAgents] = useState<TAgent[]>([]);
  const [isLoading, setLoading] = useState(!options.skipInitialLoad);
  const [isLoadingMoreChats, setLoadingMore] = useState(false);
  const [hasMoreChats, setHasMore] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const pageRef = useRef(1);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refreshChats = useCallback(async () => {
    setLoading(true);
    try {
      const res = await transportRef.current.listChats({ page: 1, pageSize });
      if (!mountedRef.current) return;
      setChats(res.chats);
      setHasMore(res.hasMore);
      pageRef.current = 1;
      setError(null);
    } catch (err) {
      if (mountedRef.current) setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [pageSize]);

  const refreshAgents = useCallback(async () => {
    try {
      const list = await transportRef.current.listAgents();
      if (mountedRef.current) setAgents(list);
    } catch (err) {
      if (mountedRef.current) setError(err instanceof Error ? err : new Error(String(err)));
    }
  }, []);

  const loadMoreChats = useCallback(async () => {
    if (!hasMoreChats || isLoadingMoreChats) return;
    setLoadingMore(true);
    try {
      const next = pageRef.current + 1;
      const res = await transportRef.current.listChats({ page: next, pageSize });
      if (!mountedRef.current) return;
      setChats((prev) => prev.concat(res.chats));
      setHasMore(res.hasMore);
      pageRef.current = next;
    } catch (err) {
      if (mountedRef.current) setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      if (mountedRef.current) setLoadingMore(false);
    }
  }, [hasMoreChats, isLoadingMoreChats, pageSize]);

  const createChat = useCallback(
    async (input?: { agentId?: string }) => {
      const t = transportRef.current;
      if (!t.createChat) return null;
      const created = await t.createChat({ agentId: input?.agentId ?? selectedAgentId ?? undefined });
      // Either a chat object or a bare id.
      const id = typeof created === 'string' ? created : t.getChatId(created);
      await refreshChats();
      return id;
    },
    [refreshChats, selectedAgentId],
  );

  const deleteChat = useCallback(
    async (chatId: string) => {
      const t = transportRef.current;
      if (!t.deleteChat) return false;
      const ok = await t.deleteChat(chatId);
      if (ok && mountedRef.current) {
        setChats((prev) => prev.filter((c) => t.getChatId(c) !== chatId));
      }
      return ok;
    },
    [],
  );

  const patchChatTitle = useCallback((chatId: string, title: string) => {
    const t = transportRef.current;
    if (!t.setChatTitle) return;
    setChats((prev) =>
      prev.map((c) => (t.getChatId(c) === chatId ? t.setChatTitle!(c, title) : c)),
    );
  }, []);

  useEffect(() => {
    if (options.skipInitialLoad) return;
    void refreshChats();
    void refreshAgents();
    // Run once on mount; downstream callers use refresh* explicitly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return useMemo<UseChatListReturn<TChat, TAgent>>(
    () => ({
      chats,
      agents,
      isLoading,
      isLoadingMoreChats,
      hasMoreChats,
      error,
      selectedAgentId,
      setSelectedAgentId,
      refreshChats,
      refreshAgents,
      loadMoreChats,
      createChat,
      deleteChat,
      patchChatTitle,
    }),
    [
      chats,
      agents,
      isLoading,
      isLoadingMoreChats,
      hasMoreChats,
      error,
      selectedAgentId,
      refreshChats,
      refreshAgents,
      loadMoreChats,
      createChat,
      deleteChat,
      patchChatTitle,
    ],
  );
}

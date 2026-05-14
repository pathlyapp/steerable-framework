/**
 * `useAgentSession` — light-weight wrapper around the session lifecycle.
 *
 * Sessions are created lazily on first message in most production setups, but
 * the desktop / web clients still need a deterministic handle to fetch /
 * resume / list. The hook exposes a transport-agnostic API matching the
 * methods on the framework's storage adapter (`InMemoryStorage`,
 * `SqlAlchemyStorage`) so consumers can wire any backend.
 *
 * Stability contract:
 *   The hook tolerates inline-object literals for `autoLoad`. We re-run the
 *   `list` effect only when the **values** in `autoLoad` change, not when the
 *   object identity changes (otherwise an unmemoised consumer would loop).
 *   `transport` is held by ref so swapping the implementation mid-session
 *   doesn't trigger a refetch — call `refresh()` explicitly if you need that.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { AgentSession } from '@steerable/agent-protocol';

export interface AgentSessionTransport {
  create: (input: {
    chatId: string;
    userId: string;
    projectId?: string | null;
    scenario?: string;
    stageData?: Record<string, unknown> | null;
  }) => Promise<AgentSession>;
  resume: (sessionId: string) => Promise<AgentSession>;
  list: (filter: {
    userId?: string;
    chatId?: string;
    activeOnly?: boolean;
  }) => Promise<AgentSession[]>;
}

export interface UseAgentSessionOptions {
  transport: AgentSessionTransport;
  /** Optional auto-load filter; when set the hook calls `list` on mount. */
  autoLoad?: { userId?: string; chatId?: string; activeOnly?: boolean };
}

export interface UseAgentSessionReturn {
  /** Active sessions for the current filter. */
  sessions: AgentSession[];
  /** The session created/resumed most recently via this hook. */
  current: AgentSession | null;
  isLoading: boolean;
  error: Error | null;
  create: AgentSessionTransport['create'];
  resume: AgentSessionTransport['resume'];
  refresh: () => Promise<void>;
  /** Set the current session pointer without re-fetching. */
  setCurrent: (session: AgentSession | null) => void;
}

function autoLoadKey(
  autoLoad: UseAgentSessionOptions['autoLoad'],
): string | null {
  if (!autoLoad) return null;
  // Stable, deterministic key — values are primitives so JSON.stringify is fine.
  return JSON.stringify({
    userId: autoLoad.userId ?? null,
    chatId: autoLoad.chatId ?? null,
    activeOnly: autoLoad.activeOnly ?? null,
  });
}

export function useAgentSession(
  options: UseAgentSessionOptions,
): UseAgentSessionReturn {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [current, setCurrent] = useState<AgentSession | null>(null);
  const [isLoading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const mountedRef = useRef(true);
  const transportRef = useRef(options.transport);
  transportRef.current = options.transport;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const autoLoadKeyVal = autoLoadKey(options.autoLoad);

  const refresh = useCallback(async () => {
    if (!options.autoLoad) return;
    setLoading(true);
    try {
      const list = await transportRef.current.list(options.autoLoad);
      if (mountedRef.current) {
        setSessions(list);
        setError(null);
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err : new Error(String(err)));
      }
    } finally {
      if (mountedRef.current) setLoading(false);
    }
    // We intentionally key on the serialised filter, not the object identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoLoadKeyVal]);

  useEffect(() => {
    if (autoLoadKeyVal === null) return;
    void refresh();
  }, [autoLoadKeyVal, refresh]);

  const create = useCallback<AgentSessionTransport['create']>(
    async (input) => {
      setLoading(true);
      try {
        const session = await transportRef.current.create(input);
        if (mountedRef.current) {
          setCurrent(session);
          setSessions((prev) => [
            session,
            ...prev.filter((s) => s.sessionId !== session.sessionId),
          ]);
          setError(null);
        }
        return session;
      } catch (err) {
        if (mountedRef.current) {
          setError(err instanceof Error ? err : new Error(String(err)));
        }
        throw err;
      } finally {
        if (mountedRef.current) setLoading(false);
      }
    },
    [],
  );

  const resume = useCallback<AgentSessionTransport['resume']>(
    async (sessionId) => {
      setLoading(true);
      try {
        const session = await transportRef.current.resume(sessionId);
        if (mountedRef.current) {
          setCurrent(session);
          setError(null);
        }
        return session;
      } catch (err) {
        if (mountedRef.current) {
          setError(err instanceof Error ? err : new Error(String(err)));
        }
        throw err;
      } finally {
        if (mountedRef.current) setLoading(false);
      }
    },
    [],
  );

  return useMemo<UseAgentSessionReturn>(
    () => ({
      sessions,
      current,
      isLoading,
      error,
      create,
      resume,
      refresh,
      setCurrent,
    }),
    [sessions, current, isLoading, error, create, resume, refresh],
  );
}

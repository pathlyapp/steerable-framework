import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { getElectronBridge, type AskUserPromptRequest } from '@/lib/electron-bridge';

type AnswerValue = string | string[];

interface AskUserPromptContextValue {
  current: AskUserPromptRequest | null;
  pendingCount: number;
  answer: (answers: Record<string, AnswerValue>) => void;
}

const AskUserPromptContext = createContext<AskUserPromptContextValue | null>(null);

/**
 * Keeps structured user prompts alive across chat navigation while allowing
 * the active composer to present the current prompt.
 */
export function AskUserPromptProvider({ children }: { children: ReactNode }) {
  const [queue, setQueue] = useState<AskUserPromptRequest[]>([]);
  const answeredRequestIds = useRef(new Set<string>());

  useEffect(() => {
    const bridge = getElectronBridge();
    if (!bridge?.askUser) return;
    let active = true;
    const addRequests = (
      requests: AskUserPromptRequest[],
      placement: 'append' | 'prepend' = 'append',
    ) => {
      setQueue((previous) => {
        const known = new Set(previous.map(({ requestId }) => requestId));
        const additions = requests.filter(
          ({ requestId }) =>
            !known.has(requestId) && !answeredRequestIds.current.has(requestId),
        );
        if (additions.length === 0) return previous;
        return placement === 'prepend'
          ? [...additions, ...previous]
          : [...previous, ...additions];
      });
    };
    const unsubscribe = bridge.askUser.onRequest((request) => {
      addRequests([request]);
    });
    void bridge.askUser
      .pending()
      .then((requests) => {
        if (active) addRequests(requests, 'prepend');
      })
      .catch((error) => {
        console.error('恢复待回答问题失败:', error);
      });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  const current = queue[0] ?? null;
  const answer = useCallback(
    (answers: Record<string, AnswerValue>) => {
      if (!current) return;
      const bridge = getElectronBridge();
      answeredRequestIds.current.add(current.requestId);
      setQueue((previous) => previous.slice(1));
      void bridge?.askUser?.answer({ requestId: current.requestId, answers });
    },
    [current],
  );

  const value = useMemo(
    () => ({
      current,
      pendingCount: Math.max(0, queue.length - 1),
      answer,
    }),
    [answer, current, queue.length],
  );

  return (
    <AskUserPromptContext.Provider value={value}>
      {children}
    </AskUserPromptContext.Provider>
  );
}

/** Returns the structured prompt currently assigned to the chat composer. */
export function useAskUserPrompt(): AskUserPromptContextValue | null {
  return useContext(AskUserPromptContext);
}

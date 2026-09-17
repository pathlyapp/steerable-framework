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
import {
  getElectronBridge,
  type ApprovalDecisionKind,
  type ApprovalPromptRequest,
} from '@/lib/electron-bridge';

interface ApprovalPromptContextValue {
  current: ApprovalPromptRequest | null;
  pendingCount: number;
  decide: (kind: ApprovalDecisionKind) => void;
}

const ApprovalPromptContext = createContext<ApprovalPromptContextValue | null>(null);

/** Keeps approval prompts alive while the active composer presents them. */
export function ApprovalPromptProvider({ children }: { children: ReactNode }) {
  const [queue, setQueue] = useState<ApprovalPromptRequest[]>([]);
  const decidedRequestIds = useRef(new Set<string>());

  useEffect(() => {
    const bridge = getElectronBridge();
    if (!bridge?.approval) return;
    let active = true;
    const addRequests = (
      requests: ApprovalPromptRequest[],
      placement: 'append' | 'prepend' = 'append',
    ) => {
      setQueue((previous) => {
        const known = new Set(previous.map(({ requestId }) => requestId));
        const additions = requests.filter(
          ({ requestId }) =>
            !known.has(requestId) && !decidedRequestIds.current.has(requestId),
        );
        if (additions.length === 0) return previous;
        return placement === 'prepend'
          ? [...additions, ...previous]
          : [...previous, ...additions];
      });
    };
    const unsubscribe = bridge.approval.onRequest((request) => {
      addRequests([request]);
    });
    void bridge.approval
      .pending()
      .then((requests) => {
        if (active) addRequests(requests, 'prepend');
      })
      .catch((error) => {
        console.error('恢复待审批请求失败:', error);
      });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  const current = queue[0] ?? null;
  const decide = useCallback(
    (kind: ApprovalDecisionKind) => {
      if (!current) return;
      const bridge = getElectronBridge();
      decidedRequestIds.current.add(current.requestId);
      setQueue((previous) => previous.slice(1));
      void bridge?.approval?.decide({ requestId: current.requestId, kind });
    },
    [current],
  );

  const value = useMemo(
    () => ({
      current,
      pendingCount: Math.max(0, queue.length - 1),
      decide,
    }),
    [current, decide, queue.length],
  );

  return (
    <ApprovalPromptContext.Provider value={value}>
      {children}
    </ApprovalPromptContext.Provider>
  );
}

/** Returns the approval prompt currently assigned to the chat composer. */
export function useApprovalPrompt(): ApprovalPromptContextValue | null {
  return useContext(ApprovalPromptContext);
}

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { LuArrowDown, LuLoaderCircle, LuTerminal } from 'react-icons/lu';
import { DockHeaderButton, DockPanelHeader } from '@/components/DockPanelHeader';
import { getTaskProcess } from '@/lib/local-api';
import { getElectronBridge } from '@/lib/electron-bridge';
import { Markdown } from '@/components/chat/Markdown';
import { TurnProcessGroup } from '@/components/chat/TurnProcessGroup';
import { parseTurnBlocks, type TurnBlock } from '@/components/chat/turn-timeline';

export interface InspectedTask {
  id: string;
  chatId: string;
  title: string;
}

const NEAR_BOTTOM_THRESHOLD_PX = 100;

/**
 * 右侧栏：后台任务的模型推理过程（与主对话 TurnProcessGroup 同形）。
 * 数据来自 GET /tasks/:id/process，live 增量走 task-process 广播。
 */
export function TaskProcessPanel({
  inspected,
  onClose,
  onShowTerminal,
}: {
  inspected: InspectedTask;
  onClose: () => void;
  onShowTerminal?: () => void;
}) {
  const [blocks, setBlocks] = useState<TurnBlock[]>([]);
  const [live, setLive] = useState(false);
  const [stale, setStale] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);

  const checkAtBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const atBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_THRESHOLD_PX;
    setIsAtBottom(atBottom);
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const el = containerRef.current;
    if (!el) return;
    if (behavior === 'smooth') {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
      return;
    }
    el.scrollTop = el.scrollHeight;
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    el.addEventListener('scroll', checkAtBottom, { passive: true });
    return () => {
      el.removeEventListener('scroll', checkAtBottom);
    };
  }, [checkAtBottom]);

  // 换任务 / 首屏：没有「之前在不在底部」可言，直接钉到底。
  useLayoutEffect(() => {
    scrollToBottom('instant' as ScrollBehavior);
    setIsAtBottom(true);
  }, [inspected.id, scrollToBottom]);

  // live 增量只在用户还贴着底部时跟滚——往上翻看前面的工具调用时不要拽走。
  const timelineSig = blocks
    .map((block) =>
      block.type === 'tools' ? `t${block.actions.length}` : `c${block.content.length}`,
    )
    .join('|');
  useEffect(() => {
    if (!isAtBottom) return;
    scrollToBottom('smooth');
  }, [timelineSig, live, isAtBottom, scrollToBottom]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const snapshot = await getTaskProcess(inspected.id);
        if (cancelled) return;
        setBlocks(parseTurnBlocks(snapshot.timeline) ?? []);
        setLive(snapshot.live);
        setStale(snapshot.stale);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
        setBlocks([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [inspected.id]);

  useEffect(() => {
    const bridge = getElectronBridge();
    if (!bridge?.onTaskProcess) return;
    return bridge.onTaskProcess((payload) => {
      if (payload.taskId !== inspected.id) return;
      const next = parseTurnBlocks(payload.timeline);
      if (next) setBlocks(next);
      else if (Array.isArray(payload.timeline) && payload.timeline.length === 0) {
        setBlocks([]);
      }
      setLive(payload.live);
      if (payload.live) setStale(false);
    });
  }, [inspected.id]);

  const statusLabel = live
    ? '正在推理'
    : stale
      ? '进程已中断（表上仍是运行中）'
      : '已结束';

  return (
    <div
      className="flex h-full w-full flex-col bg-agent-canvas text-agent-foreground"
      data-testid="task-process-panel"
    >
      <DockPanelHeader
        title={`后台推理 · ${statusLabel}`}
        onClose={onClose}
        closeLabel="关闭过程面板"
        actions={
          onShowTerminal && (
            <DockHeaderButton
              icon={<LuTerminal className="h-3 w-3" />}
              label="终端"
              title="切换到终端"
              onClick={onShowTerminal}
            />
          )
        }
      />
      <div className="border-b border-agent-border/60 px-3 py-2">
        <div className="line-clamp-3 text-xs leading-relaxed text-agent-foreground">
          {inspected.title}
        </div>
      </div>
      <div className="relative min-h-0 flex-1 overflow-hidden">
        <div
          ref={containerRef}
          className="h-full overflow-y-auto p-3"
          data-testid="task-process-scroll"
        >
          {loading ? (
            <div className="flex items-center gap-2 text-xs text-agent-muted-foreground">
              <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
              正在加载推理过程…
            </div>
          ) : error ? (
            <div className="text-xs text-red-600 dark:text-red-300">{error}</div>
          ) : (
            <TurnProcessGroup
              blocks={blocks}
              isStreaming={live}
              showThinkingContent
              agents={[]}
              chats={[]}
              emptyFallback={
                <div className="text-xs text-agent-muted-foreground">
                  {stale
                    ? '任务流已随进程结束，没有留下可回放的推理记录。'
                    : live
                      ? '任务已启动，推理过程会显示在这里。'
                      : '没有可显示的推理过程。'}
                </div>
              }
              renderAnswer={(block) => (
                <div className="text-sm leading-relaxed text-agent-foreground">
                  <Markdown agents={[]} chats={[]}>{block.content}</Markdown>
                </div>
              )}
            />
          )}
        </div>
        {!isAtBottom && !loading && (
          <button
            type="button"
            onClick={() => {
              setIsAtBottom(true);
              scrollToBottom('smooth');
            }}
            className="absolute bottom-3 right-3 flex h-8 w-8 items-center justify-center rounded-full border border-agent-border bg-agent-canvas text-agent-foreground shadow-md transition-colors hover:bg-agent-foreground/5"
            title="回到底部"
            aria-label="回到底部"
          >
            <LuArrowDown className="h-4 w-4" />
          </button>
        )}
      </div>
    </div>
  );
}

export default TaskProcessPanel;

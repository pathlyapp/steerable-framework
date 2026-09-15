import { useCallback, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  LuCheck,
  LuChevronDown,
  LuChevronRight,
  LuClock,
  LuGitBranch,
  LuGitMerge,
  LuListTodo,
  LuLoaderCircle,
  LuTrash2,
  LuX,
} from 'react-icons/lu';
import {
  discardTaskWorktree,
  listChatTasks,
  mergeTaskWorktree,
  type LocalTask,
} from '@/lib/local-api';
import { getElectronBridge } from '@/lib/electron-bridge';

/**
 * TaskPanelModal — 跨 turn 后台任务面板（4.6a/4.6c）。
 *
 * 数据来自 listChatTasks（host SQLite 任务表）。任务在独立 sidecar 流里
 * 跑，终态经主进程 `task-updated` 广播推到这里刷新——面板打开期间任务
 * 跑完会实时翻状态，不需要重开。
 *
 * worktree 任务（worktreeState=pending）在完成后提供「合并到主仓」/
 * 「丢弃」操作；两者都是幂等目标态，失败（如合并冲突）以错误条显示，
 * 任务保持 pending 可重试。
 */

interface TaskPanelModalProps {
  chatId: string;
  onClose: () => void;
  /** 点击任务行：关闭面板并在右侧展示该任务的推理过程。 */
  onInspect?: (task: LocalTask) => void;
  /** 打开时就展开的任务行——header 角标直达唯一待处理的 worktree 任务。 */
  initialExpandedId?: string;
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function TaskPanelModal({
  chatId,
  onClose,
  onInspect,
  initialExpandedId,
}: TaskPanelModalProps) {
  const [tasks, setTasks] = useState<LocalTask[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(
    initialExpandedId ?? null,
  );
  /** 正在执行合并/丢弃的任务 id——按钮防重入。 */
  const [actingId, setActingId] = useState<string | null>(null);
  /** 合并/丢弃失败（如冲突）的提示，按任务 id 挂。 */
  const [actionError, setActionError] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    try {
      const res = await listChatTasks(chatId);
      setTasks(res.tasks);
    } catch {
      setTasks(null);
    } finally {
      setLoading(false);
    }
  }, [chatId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 任务终态推送 → 重拉列表。preload 只透传 chatId/taskId/status，
  // 记录本体以 SQLite 为准。
  useEffect(() => {
    const bridge = getElectronBridge();
    if (!bridge?.onTaskUpdated) return;
    return bridge.onTaskUpdated((payload) => {
      if (payload.chatId === chatId) void refresh();
    });
  }, [chatId, refresh]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [onClose]);

  const runAction = async (task: LocalTask, action: 'merge' | 'discard') => {
    if (actingId) return;
    setActingId(task.id);
    setActionError((prev) => {
      const next = { ...prev };
      delete next[task.id];
      return next;
    });
    try {
      if (action === 'merge') {
        await mergeTaskWorktree(task.id);
      } else {
        await discardTaskWorktree(task.id);
      }
      await refresh();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setActionError((prev) => ({ ...prev, [task.id]: message }));
    } finally {
      setActingId(null);
    }
  };

  const runningCount = tasks?.filter((t) => t.status === 'running').length ?? 0;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="后台任务"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      data-task-panel-modal
    >
      <div className="flex max-h-[85vh] w-full max-w-xl flex-col overflow-hidden rounded-agent-lg border border-agent-border bg-agent-canvas shadow-xl">
        <div className="flex items-center gap-2 border-b border-agent-border px-4 py-2.5">
          <LuListTodo className="h-4 w-4 shrink-0 text-agent-muted-foreground" />
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-agent-foreground">
            后台任务
            {tasks && tasks.length > 0 && (
              <span className="ml-1.5 text-xs font-normal text-agent-muted-foreground">
                {tasks.length} 个任务{runningCount > 0 ? `，${runningCount} 个运行中` : ''}
              </span>
            )}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground"
            aria-label="关闭"
          >
            <LuX className="h-3.5 w-3.5" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-2">
          {loading ? (
            <div className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-agent-muted-foreground">
              <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
              正在加载任务…
            </div>
          ) : !tasks || tasks.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-agent-muted-foreground">
              暂无后台任务 — 让助手用 task_run 启动一个跨轮任务后会出现在这里。
            </div>
          ) : (
            tasks.map((task) => {
              const expanded = expandedId === task.id;
              const acting = actingId === task.id;
              return (
                <div
                  key={task.id}
                  className="mb-1 rounded border border-agent-border/60 px-2.5 py-2"
                  data-task-row
                  data-status={task.status}
                >
                  <div className="flex items-center gap-2">
                    {task.status === 'running' ? (
                      <LuLoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin text-sky-600 dark:text-sky-400" />
                    ) : task.status === 'blocked' ? (
                      <LuClock className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
                    ) : task.status === 'completed' ? (
                      <LuCheck className="h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
                    ) : (
                      <LuX className="h-3.5 w-3.5 shrink-0 text-red-600 dark:text-red-400" />
                    )}
                    <button
                      type="button"
                      onClick={() => onInspect?.(task)}
                      className="min-w-0 flex-1 truncate text-left text-xs font-medium text-agent-foreground hover:underline"
                      title="在右侧查看推理过程"
                    >
                      {task.task}
                    </button>
                    {task.worktreeBranch && (
                      <span
                        className="flex shrink-0 items-center gap-1 rounded bg-agent-foreground/5 px-1.5 py-0.5 font-mono text-[10px] text-agent-muted-foreground"
                        title={task.worktreePath ?? undefined}
                      >
                        <LuGitBranch className="h-3 w-3" />
                        {task.worktreeBranch}
                        {task.worktreeState === 'merged'
                          ? ' · 已合并'
                          : task.worktreeState === 'discarded'
                            ? ' · 已丢弃'
                            : ''}
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={() => setExpandedId(expanded ? null : task.id)}
                      className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground"
                      aria-label={expanded ? '收起' : '展开'}
                      aria-expanded={expanded}
                    >
                      {expanded ? (
                        <LuChevronDown className="h-3.5 w-3.5" />
                      ) : (
                        <LuChevronRight className="h-3.5 w-3.5" />
                      )}
                    </button>
                  </div>

                  <div className="mt-1 pl-5 text-[10px] text-agent-muted-foreground/80">
                    {task.status === 'running'
                      ? '运行中'
                      : task.status === 'blocked'
                        ? '等待依赖'
                        : task.status === 'completed'
                          ? '已完成'
                          : '失败'}
                    {' · '}
                    {formatTime(task.updatedAt)}
                  </div>

                  {expanded && (
                    <div className="mt-2 border-t border-agent-border/60 pt-2 pl-5">
                      {task.error && (
                        <div className="mb-2 rounded bg-red-500/10 px-2 py-1.5 text-xs whitespace-pre-wrap text-red-700 dark:text-red-300">
                          {task.error}
                        </div>
                      )}
                      {task.answer ? (
                        <div className="max-h-64 overflow-auto text-xs whitespace-pre-wrap text-agent-foreground">
                          {task.answer}
                        </div>
                      ) : (
                        !task.error && (
                          <div className="text-xs text-agent-muted-foreground">
                            {task.status === 'running'
                              ? '任务还在运行，完成后结果会出现在这里。'
                              : task.status === 'blocked'
                                ? '等待依赖任务完成，就绪后会自动开始。'
                                : '无结果。'}
                          </div>
                        )
                      )}
                      {task.worktreeState === 'pending' && task.status === 'completed' && (
                        <div className="mt-2 flex items-center gap-2">
                          <button
                            type="button"
                            disabled={actingId !== null}
                            onClick={() => void runAction(task, 'merge')}
                            className="flex items-center gap-1 rounded bg-agent-foreground/10 px-2 py-1 text-[11px] font-medium text-agent-foreground transition-colors hover:bg-agent-foreground/15 disabled:opacity-50"
                            data-task-merge
                          >
                            {acting ? (
                              <LuLoaderCircle className="h-3 w-3 animate-spin" />
                            ) : (
                              <LuGitMerge className="h-3 w-3" />
                            )}
                            合并到主仓
                          </button>
                          <button
                            type="button"
                            disabled={actingId !== null}
                            onClick={() => void runAction(task, 'discard')}
                            className="flex items-center gap-1 rounded px-2 py-1 text-[11px] text-agent-muted-foreground transition-colors hover:bg-red-500/10 hover:text-red-600 disabled:opacity-50 dark:hover:text-red-400"
                            data-task-discard
                          >
                            <LuTrash2 className="h-3 w-3" />
                            丢弃
                          </button>
                        </div>
                      )}
                      {actionError[task.id] && (
                        <div className="mt-2 rounded bg-red-500/10 px-2 py-1.5 text-xs whitespace-pre-wrap text-red-700 dark:text-red-300">
                          {actionError[task.id]}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        <div className="border-t border-agent-border px-4 py-2 text-[10px] text-agent-muted-foreground/80">
          任务由后台智能体独立执行。点任务名可在右侧看推理过程；Esc 关闭。
        </div>
      </div>
    </div>,
    document.body,
  );
}

export default TaskPanelModal;

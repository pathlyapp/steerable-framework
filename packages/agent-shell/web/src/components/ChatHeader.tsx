import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { LuCheck, LuGitBranch, LuListTodo, LuListTree, LuLoaderCircle } from 'react-icons/lu';
import type { LocalChat, LocalChatAgent, LocalTask } from '@/lib/local-api';
import {
  activateChatBranch,
  getChatBranches,
  type ChatBranchesResponse,
} from '@/lib/local-api';
import { SessionTreeModal } from './chat/SessionTreeModal';
import { TaskPanelModal } from './chat/TaskPanelModal';
import { actionableTasks, summarizeTasks, type ChatTaskSummary } from './chat/useChatTasks';

interface ChatHeaderProps {
  chat: LocalChat | null;
  agent: LocalChatAgent | null;
  /**
   * W1.2.1: called after the active branch switches — the page re-hydrates
   * the message list from the re-projected store. When provided (and the
   * chat has branches), a branch picker appears in the header.
   */
  onBranchSwitched?: () => void;
  /** 点击后台任务行：在右侧栏打开该任务的推理过程。 */
  onInspectTask?: (task: { id: string; chatId: string; title: string }) => void;
  /** 本对话的后台任务（AgentPage 的 `useChatTasks` 单一订阅源）。 */
  tasks?: LocalTask[];
}

/**
 * ChatHeader — slim title bar above ChatPanel. Shows:
 *   - chat title (完整显示，长标题换行不截断)
 *   - bound agent name + icon (read-only here; switching is done from the
 *     sidebar's expert team list)
 *   - shortened chat id (for support / debugging)
 *
 * Terminal / 场景包调试窗 / 本地模型设置 USED to live here, but per the
 * P1 parity contract (`match_old`) they've been moved to the AgentSidebar
 * bottom strip. 刷新按钮已下线（消息流有 SSE 自动同步）。分享在最后一条
 * 助手消息的时间戳行，不占标题栏。
 */
export function ChatHeader({
  chat,
  agent,
  onBranchSwitched,
  onInspectTask,
  tasks = [],
}: ChatHeaderProps) {
  const [branchMenuOpen, setBranchMenuOpen] = useState(false);
  const [treeModalOpen, setTreeModalOpen] = useState(false);
  const [taskPanelOpen, setTaskPanelOpen] = useState(false);
  const [branches, setBranches] = useState<ChatBranchesResponse | null>(null);
  const [switching, setSwitching] = useState(false);
  const [menuBox, setMenuBox] = useState<{ top: number; left: number; width: number } | null>(
    null,
  );
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const branchCount = branches ? branches.lineage.length + branches.children.length : 0;

  const taskSummary = useMemo(() => summarizeTasks(tasks), [tasks]);
  const taskBadge = describeTaskBadge(taskSummary);
  const taskShortcut = useMemo(
    () => describeTaskShortcut(tasks, taskSummary),
    [tasks, taskSummary],
  );

  const openBranchMenu = async () => {
    if (!chat || branchMenuOpen) {
      setBranchMenuOpen(false);
      return;
    }
    try {
      const data = await getChatBranches(chat.id);
      setBranches(data);
      setBranchMenuOpen(true);
    } catch {
      setBranches(null);
    }
  };

  const switchBranch = async (recordId: string) => {
    if (!chat || !branches || switching || recordId === branches.activeRecordId) return;
    setSwitching(true);
    try {
      await activateChatBranch(chat.id, recordId);
      setBranchMenuOpen(false);
      onBranchSwitched?.();
    } catch {
      // 切换失败（分支族外记录 / sidecar 离线）——菜单保持打开，用户可重试。
    } finally {
      setSwitching(false);
    }
  };

  useLayoutEffect(() => {
    if (!branchMenuOpen) {
      setMenuBox(null);
      return;
    }
    const button = buttonRef.current;
    if (!button) return;
    const update = () => setMenuBox(placeBranchMenu(button));
    update();
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    return () => {
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
    };
  }, [branchMenuOpen]);

  useEffect(() => {
    if (!branchMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (buttonRef.current?.contains(t) || menuRef.current?.contains(t)) return;
      setBranchMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [branchMenuOpen]);

  const branchMenu =
    branchMenuOpen && menuBox
      ? createPortal(
          <div
            ref={menuRef}
            className="fixed z-[80] rounded-lg border border-agent-border bg-agent-canvas p-1 shadow-lg"
            style={{ top: menuBox.top, left: menuBox.left, width: menuBox.width }}
            data-branch-menu
          >
            {!branches || branchCount === 0 ? (
              <div className="px-3 py-2 text-xs text-agent-muted-foreground">
                暂无分支 — 重新生成回复后，旧版本会保留在这里。
              </div>
            ) : (
              <>
                {branches.lineage.map((point) => (
                  <BranchRow
                    key={point.recordId}
                    label={point.label}
                    active={point.recordId === branches.activeRecordId}
                    disabled={switching}
                    onSelect={() => void switchBranch(point.recordId)}
                  />
                ))}
                {branches.children.map((point) => (
                  <BranchRow
                    key={point.recordId}
                    label={point.label}
                    active={point.recordId === branches.activeRecordId}
                    disabled={switching}
                    onSelect={() => void switchBranch(point.recordId)}
                  />
                ))}
                {/* 全树入口：下拉只有 lineage + 直接子节点，堂兄弟等更远的
                    家族成员在树视图里切换。 */}
                <div className="mt-1 border-t border-agent-border pt-1">
                  <button
                    type="button"
                    onClick={() => {
                      setBranchMenuOpen(false);
                      setTreeModalOpen(true);
                    }}
                    className="flex w-full items-center gap-2 rounded px-3 py-1.5 text-left text-xs text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground"
                    data-action="branch-tree"
                  >
                    <LuListTree className="h-3 w-3 shrink-0" />
                    查看完整分支树
                  </button>
                </div>
              </>
            )}
          </div>,
          document.body,
        )
      : null;

  return (
    <header className="flex w-full min-w-0 items-center gap-3 bg-agent-canvas px-3 py-2">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-0.5">
        <span
          className="break-words text-sm font-medium text-agent-foreground"
          title={chat?.title}
        >
          {chat?.title ?? '未选择对话'}
        </span>
        <span className="shrink-0 text-xs text-agent-muted-foreground">
          {agent ? (
            <>
              <span className="mr-0.5">{agent.icon || '🤖'}</span>
              {agent.name}
            </>
          ) : chat?.agentId ? (
            <span className="font-mono">{chat.agentId}</span>
          ) : (
            <span className="italic">no agent</span>
          )}
        </span>
        {chat && (
          <span className="hidden shrink-0 font-mono text-[10px] text-agent-muted-foreground/70 md:inline">
            {shortenId(chat.id)}
          </span>
        )}
      </div>
      {chat && onBranchSwitched && (
        <div className="shrink-0">
          <button
            ref={buttonRef}
            type="button"
            onClick={() => void openBranchMenu()}
            className="flex h-7 items-center gap-1 rounded-full px-2 text-xs text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground"
            title="会话分支（重新生成产生的分叉）"
            aria-label="会话分支"
            data-action="branches"
          >
            <LuGitBranch className="h-3.5 w-3.5" />
            {branchCount > 0 && <span>{branchCount}</span>}
          </button>
          {branchMenu}
        </div>
      )}
      {chat && (
        <button
          type="button"
          onClick={() => {
            if (taskShortcut?.kind === 'process' && onInspectTask) {
              onInspectTask({
                id: taskShortcut.task.id,
                chatId: taskShortcut.task.chatId,
                title: taskShortcut.task.task,
              });
              return;
            }
            setTaskPanelOpen(true);
          }}
          className={`flex h-7 shrink-0 items-center gap-1 rounded-full transition-colors ${
            taskBadge
              ? `px-2 ${taskBadge.className}`
              : 'w-7 justify-center text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground'
          }`}
          title={taskButtonTitle(taskBadge, taskShortcut)}
          aria-label={taskBadge ? `后台任务：${taskBadge.title}` : '后台任务'}
          data-action="tasks"
          data-task-state={taskBadge?.state}
          data-task-shortcut={taskShortcut?.kind}
        >
          {taskSummary.running > 0 ? (
            <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <LuListTodo className="h-3.5 w-3.5" />
          )}
          {taskBadge && <span className="text-xs">{taskBadge.count}</span>}
        </button>
      )}
      {chat && taskPanelOpen && (
        <TaskPanelModal
          chatId={chat.id}
          onClose={() => setTaskPanelOpen(false)}
          initialExpandedId={
            taskShortcut?.kind === 'expand' ? taskShortcut.taskId : undefined
          }
          onInspect={
            onInspectTask
              ? (task) => {
                  onInspectTask({
                    id: task.id,
                    chatId: task.chatId,
                    title: task.task,
                  });
                  setTaskPanelOpen(false);
                }
              : undefined
          }
        />
      )}
      {chat && treeModalOpen && (
        <SessionTreeModal
          chatId={chat.id}
          onClose={() => setTreeModalOpen(false)}
          onBranchSwitched={onBranchSwitched}
        />
      )}
    </header>
  );
}

interface TaskBadge {
  /** 驱动角标配色的那一类任务，也写进 `data-task-state` 供测试断言。 */
  state: 'running' | 'review' | 'failed' | 'idle';
  count: number;
  className: string;
  title: string;
}

/**
 * 任务按钮的角标。只有一个状态能上色，按「用户该不该现在看一眼」排序：
 * 运行中（正在发生）> 待合并（等用户动手）> 失败（需要知道）> 全部跑完。
 * 计数跟着状态走——显示 3 个任务里那 1 个待合并的，比显示总数 3 更有用。
 */
function describeTaskBadge(summary: ChatTaskSummary): TaskBadge | null {
  if (summary.total === 0) return null;

  const detail = [
    summary.running > 0 ? `${summary.running} 个运行中` : null,
    summary.blocked > 0 ? `${summary.blocked} 个等依赖` : null,
    summary.needsReview > 0 ? `${summary.needsReview} 个待合并` : null,
    summary.failed > 0 ? `${summary.failed} 个失败` : null,
  ]
    .filter((part) => part !== null)
    .join('，');
  const title = `共 ${summary.total} 个后台任务${detail ? `（${detail}）` : ''}`;

  // 等依赖的任务也算在推进中：依赖跑完它会自动点火，用户不用做任何事。
  const active = summary.running + summary.blocked;
  if (active > 0) {
    return {
      state: 'running',
      count: active,
      className:
        'bg-sky-500/10 text-sky-600 hover:bg-sky-500/15 dark:bg-sky-500/20 dark:text-sky-400',
      title,
    };
  }
  if (summary.needsReview > 0) {
    return {
      state: 'review',
      count: summary.needsReview,
      className:
        'bg-amber-500/10 text-amber-600 hover:bg-amber-500/15 dark:bg-amber-500/20 dark:text-amber-400',
      title,
    };
  }
  if (summary.failed > 0) {
    return {
      state: 'failed',
      count: summary.failed,
      className:
        'bg-red-500/10 text-red-600 hover:bg-red-500/15 dark:bg-red-500/20 dark:text-red-400',
      title,
    };
  }
  return {
    state: 'idle',
    count: summary.total,
    className: 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
    title,
  };
}

/**
 * 角标的直达目标——只有一件事等着处理时跳过任务列表这一层。失败任务直接
 * 开推理过程（要看的是为什么挂的），待合并的 worktree 任务直接展开它在弹层
 * 里的行（合并/丢弃按钮在那儿）。有多件事等着、或还有任务在跑时仍然先给
 * 列表：那时候用户得先挑一个。
 */
type TaskShortcut =
  | { kind: 'process'; task: LocalTask }
  | { kind: 'expand'; taskId: string };

function describeTaskShortcut(
  tasks: LocalTask[],
  summary: ChatTaskSummary,
): TaskShortcut | null {
  if (summary.running + summary.blocked > 0) return null;
  const pending = actionableTasks(tasks);
  if (pending.length !== 1) return null;
  const [task] = pending;
  return task.worktreeState === 'pending'
    ? { kind: 'expand', taskId: task.id }
    : { kind: 'process', task };
}

function taskButtonTitle(
  badge: TaskBadge | null,
  shortcut: TaskShortcut | null,
): string {
  const base = badge?.title ?? '后台任务（跨轮运行，可查看结果与合并 worktree）';
  if (!shortcut) return base;
  return shortcut.kind === 'process'
    ? `${base} · 点击查看推理过程`
    : `${base} · 点击处理 worktree`;
}

function shortenId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 6)}…${id.slice(-4)}`;
}

/** Pin the branch menu to the trigger, clamped inside the chat panel so
 *  `overflow-hidden` ancestors cannot clip the left side of a `w-72` sheet. */
function placeBranchMenu(button: HTMLElement): { top: number; left: number; width: number } {
  const pad = 8;
  const panel = button.closest('.chat-panel-container');
  const bounds = (panel ?? document.documentElement).getBoundingClientRect();
  const btn = button.getBoundingClientRect();
  const width = Math.min(288, Math.max(160, bounds.width - pad * 2));
  const minLeft = bounds.left + pad;
  const maxLeft = bounds.right - width - pad;
  const left = Math.min(Math.max(btn.right - width, minLeft), Math.max(minLeft, maxLeft));
  return { top: btn.bottom + 4, left, width };
}

function BranchRow({
  label,
  active,
  disabled,
  onSelect,
}: {
  label: string;
  active: boolean;
  disabled: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? 'true' : undefined}
      aria-disabled={disabled || active || undefined}
      className={`flex w-full items-center gap-2 rounded px-3 py-1.5 text-left text-xs transition-colors ${
        active
          ? 'cursor-default bg-agent-foreground/5 font-medium text-agent-foreground'
          : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground'
      } ${disabled ? 'pointer-events-none opacity-60' : ''}`}
      data-branch-row
      data-active={active || undefined}
    >
      <span className="min-w-0 flex-1 truncate">{label || '(空分支)'}</span>
      {active && (
        <LuCheck className="h-3 w-3 shrink-0 text-emerald-600 dark:text-emerald-400" />
      )}
    </button>
  );
}

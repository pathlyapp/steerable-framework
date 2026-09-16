/**
 * 后台任务终态通知卡（消息列尾部）。
 *
 * 后台任务跑在主对话之外，结果只有在模型自己调 `task_status` / `task_result`
 * 时才会被提起——模型不调就没人提，失败原因要经「角标 → 任务弹层 → 任务名」
 * 三层才看得到。本卡片把终态带回用户正在看的地方：说清哪个任务结束了、成败
 * 与一句摘要，再给一个直达推理过程的入口。
 *
 * 只渲染本次挂载期间到达终态的任务（`useChatTasks` 的 `finished`），「忽略」
 * 不写库。worktree 的合并/丢弃仍归任务弹层——卡片只通知，不重复那套操作。
 */
import { LuCheck, LuGitBranch, LuX } from 'react-icons/lu';
import type { LocalTask } from '@/lib/local-api';

/** 同时挤在消息列尾部的卡片上限；更多的只留一行汇总指回角标。 */
const MAX_CARDS = 3;

export interface TaskOutcomeCardsProps {
  /** 刚跑完的任务，新→旧。 */
  tasks: LocalTask[];
  /** 在右侧栏打开该任务的推理过程。 */
  onInspect: (task: LocalTask) => void;
  /** 隐藏这条通知（本次挂载内）。 */
  onDismiss: (taskId: string) => void;
}

export function TaskOutcomeCards({ tasks, onInspect, onDismiss }: TaskOutcomeCardsProps) {
  if (tasks.length === 0) return null;
  const shown = tasks.slice(0, MAX_CARDS);
  const overflow = tasks.length - shown.length;

  return (
    <div className="mt-1.5 space-y-1.5" data-testid="task-outcome-cards">
      {shown.map((task) => (
        <TaskOutcomeCard
          key={task.id}
          task={task}
          onInspect={() => onInspect(task)}
          onDismiss={() => onDismiss(task.id)}
        />
      ))}
      {overflow > 0 && (
        <div className="px-1 text-[11px] text-agent-muted-foreground">
          另有 {overflow} 个后台任务已结束 — 见标题栏的任务按钮。
        </div>
      )}
    </div>
  );
}

function TaskOutcomeCard({
  task,
  onInspect,
  onDismiss,
}: {
  task: LocalTask;
  onInspect: () => void;
  onDismiss: () => void;
}) {
  const failed = task.status === 'failed';
  const detail = failed ? task.error : task.answer;
  const needsReview = task.worktreeState === 'pending';

  return (
    <div
      role="status"
      data-task-outcome={task.id}
      data-status={task.status}
      className={`flex items-start gap-2 rounded-agent-md border px-3 py-2 text-xs ${
        failed
          ? 'border-red-400/40 bg-red-400/10'
          : 'border-emerald-400/40 bg-emerald-400/10'
      }`}
    >
      {failed ? (
        <LuX className="mt-0.5 h-4 w-4 shrink-0 text-red-600 dark:text-red-400" />
      ) : (
        <LuCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
      )}

      <div className="min-w-0 flex-1">
        <div
          className={
            failed
              ? 'text-red-700 dark:text-red-300'
              : 'text-emerald-700 dark:text-emerald-300'
          }
        >
          {failed ? '后台任务失败：' : '后台任务已完成：'}
          <span className="font-medium">{task.task}</span>
        </div>
        {detail && (
          <div className="mt-1 line-clamp-3 whitespace-pre-wrap text-agent-muted-foreground">
            {detail}
          </div>
        )}
        {needsReview && (
          <div className="mt-1 flex items-center gap-1 text-agent-muted-foreground">
            <LuGitBranch className="h-3 w-3 shrink-0" />
            <span className="min-w-0 truncate font-mono text-[10px]">
              {task.worktreeBranch}
            </span>
            <span>待合并 — 在标题栏的任务按钮里处理。</span>
          </div>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        <button
          type="button"
          onClick={onDismiss}
          className="rounded px-1.5 py-0.5 text-agent-muted-foreground transition-colors hover:text-agent-foreground"
        >
          忽略
        </button>
        <button
          type="button"
          onClick={onInspect}
          className="rounded-full bg-agent-foreground px-3 py-1 font-medium text-agent-canvas transition hover:opacity-90"
        >
          查看过程
        </button>
      </div>
    </div>
  );
}

export default TaskOutcomeCards;

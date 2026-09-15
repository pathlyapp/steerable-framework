/**
 * `useChatTasks` — 本对话后台任务的单一订阅源（host SQLite 任务表 + 主进程
 * `task-updated` 广播）。header 角标、任务弹层的直达入口和消息列尾部的终态
 * 卡都读它，三者因此永远看到同一份列表、同一个瞬间。
 *
 * `finished` 只收本次挂载期间跑到终态的任务：终态卡是「刚刚发生了什么」的
 * 通知，重开会话后由角标承担持久状态——否则几天前跑完的任务会在每次打开
 * 对话时重新弹一遍。首次加载只播种 `seenRef`，不产出通知。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { listChatTasks, type LocalTask } from '@/lib/local-api';
import { getElectronBridge, isElectron } from '@/lib/electron-bridge';

export interface ChatTaskSummary {
  total: number;
  running: number;
  /** 等依赖任务完成后自动点火。 */
  blocked: number;
  /** 跑完的 worktree 任务，等用户合并或丢弃——唯一需要用户动手的状态。 */
  needsReview: number;
  failed: number;
}

export interface ChatTasksState {
  tasks: LocalTask[];
  summary: ChatTaskSummary;
  /** 本次挂载期间跑完、用户还没忽略的任务（新→旧）。 */
  finished: LocalTask[];
  /** 忽略一条终态通知。仅本次挂载生效，不写库。 */
  dismissFinished: (taskId: string) => void;
}

/** 空数组用同一个引用，重置时 setState 才能在值没变时跳过重渲染。 */
const NO_TASKS: LocalTask[] = [];
const NO_IDS: string[] = [];

function isTerminal(status: LocalTask['status']): boolean {
  return status === 'completed' || status === 'failed';
}

export function summarizeTasks(tasks: LocalTask[]): ChatTaskSummary {
  return {
    total: tasks.length,
    running: tasks.filter((t) => t.status === 'running').length,
    blocked: tasks.filter((t) => t.status === 'blocked').length,
    needsReview: tasks.filter(
      (t) => t.status === 'completed' && t.worktreeState === 'pending',
    ).length,
    failed: tasks.filter((t) => t.status === 'failed').length,
  };
}

/** 等用户处理的任务：worktree 待合并（要动手）或失败（要知道原因）。 */
export function actionableTasks(tasks: LocalTask[]): LocalTask[] {
  return tasks.filter(
    (t) =>
      (t.status === 'completed' && t.worktreeState === 'pending') ||
      t.status === 'failed',
  );
}

export function useChatTasks(chatId: string | null): ChatTasksState {
  const [tasks, setTasks] = useState<LocalTask[]>(NO_TASKS);
  const [finishedIds, setFinishedIds] = useState<string[]>(NO_IDS);
  const [dismissedIds, setDismissedIds] = useState<string[]>(NO_IDS);
  /** 上一次见到的每个任务的状态；null = 还没播种。 */
  const seenRef = useRef<Map<string, LocalTask['status']> | null>(null);

  const refresh = useCallback(async () => {
    if (!chatId || !isElectron()) {
      setTasks(NO_TASKS);
      return;
    }
    let next: LocalTask[];
    try {
      next = (await listChatTasks(chatId)).tasks;
    } catch {
      // 任务表读不到（sidecar 离线 / 对话刚删）——按「没有任务」处理。
      setTasks(NO_TASKS);
      return;
    }
    const seen = seenRef.current;
    seenRef.current = new Map(next.map((t) => [t.id, t.status]));
    setTasks(next);
    if (!seen) return;
    // 首次见到就已是终态的任务也算刚跑完：它在两次刷新之间从建到完，
    // 用户同样没看见过它运行。
    const justFinished = next
      .filter((t) => isTerminal(t.status) && seen.get(t.id) !== t.status)
      .map((t) => t.id);
    if (justFinished.length === 0) return;
    setFinishedIds((prev) => [
      ...justFinished.filter((id) => !prev.includes(id)),
      ...prev,
    ]);
  }, [chatId]);

  useEffect(() => {
    seenRef.current = null;
    setFinishedIds(NO_IDS);
    setDismissedIds(NO_IDS);
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!chatId) return;
    const bridge = getElectronBridge();
    if (!bridge?.onTaskUpdated) return;
    return bridge.onTaskUpdated((payload) => {
      if (payload.chatId === chatId) void refresh();
    });
  }, [chatId, refresh]);

  const dismissFinished = useCallback((taskId: string) => {
    setDismissedIds((prev) =>
      prev.includes(taskId) ? prev : [...prev, taskId],
    );
  }, []);

  const summary = useMemo(() => summarizeTasks(tasks), [tasks]);
  const finished = useMemo(
    () =>
      finishedIds
        .filter((id) => !dismissedIds.includes(id))
        .map((id) => tasks.find((t) => t.id === id))
        .filter((t): t is LocalTask => t !== undefined),
    [finishedIds, dismissedIds, tasks],
  );

  return { tasks, summary, finished, dismissFinished };
}

export default useChatTasks;

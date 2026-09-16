import type { OrchestrationTaskStatus } from '@steerable/agent-ui/cards';
import type { OrchestrationPlanPayload } from '@steerable/agent-protocol';

/**
 * Pure model half of OrchestrationChildrenCard — no React and no runtime
 * framework imports (type-only, erased at build), so the desktop's node
 * vitest suite can exercise the mapping without the agent-ui dist build.
 *
 * The sidecar emits `agent.child` lifecycle notifications for orchestration
 * children AND for `delegate_subagent` delegations (delegate-on-pool); the
 * router forwards them as `orchestration_child` SSE events and AgentPage
 * accumulates them into the `ChildInfo` list below.
 */

export interface ChildInfo {
  childId: string;
  /** Spawn task text (only `child_spawned` carries it). */
  task?: string;
  depth?: number;
  /** Resolved subagent profile (delegate children; `general-purpose` when
   * the delegation named no subagent_type). Rendered as the task's agent. */
  profile?: string;
  /** running | completed | failed | cancelled | interrupted */
  status: string;
}

const STATUS_MAP: Record<string, OrchestrationTaskStatus> = {
  running: 'running',
  completed: 'ok',
  failed: 'failed',
  cancelled: 'skipped',
  // Interrupted = paused-but-resumable; renders as pending (not terminal).
  interrupted: 'pending',
};

/**
 * Pure projection of the live child list onto the card model — the card
 * itself is click-tested in `@steerable/agent-ui`.
 */
export function childrenToCardModel(children: ChildInfo[]): {
  payload: OrchestrationPlanPayload;
  taskStatuses: Record<string, OrchestrationTaskStatus>;
} {
  return {
    payload: {
      mode: 'parallel',
      tasks: children.map((c) => ({
        id: c.childId,
        // 委派子代理显示其 profile 名(如 researcher / general-purpose);
        // 编排六件套的子代理没有 profile,退回 lineage childId。
        agentId: c.profile ?? c.childId,
        prompt: c.task ?? '',
      })),
    },
    taskStatuses: Object.fromEntries(
      children.map((c) => [c.childId, STATUS_MAP[c.status] ?? 'pending']),
    ),
  };
}

/**
 * Fold the accumulated raw sidecar `agent.child` lifecycle events (as
 * forwarded by the backend's `orchestration_child` SSE payload) back into a
 * `ChildInfo[]`. Used to rebuild the in-flight child list when re-attaching
 * to a running turn (where the original event stream was consumed by a now
 * unmounted view).
 */
export function foldOrchestrationChildEvents(
  events: ReadonlyArray<Record<string, unknown>>,
): ChildInfo[] {
  const list: ChildInfo[] = [];
  for (const ev of events) {
    if (!ev || typeof ev !== 'object') continue;
    const kind = typeof ev.kind === 'string' ? ev.kind : '';
    const childId = typeof ev.childId === 'string' ? ev.childId : '';
    if (!kind || !childId) continue;
    const idx = list.findIndex((c) => c.childId === childId);
    if (kind === 'child_spawned') {
      if (idx >= 0) continue;
      list.push({
        childId,
        task: typeof ev.task === 'string' ? ev.task : undefined,
        depth: typeof ev.depth === 'number' ? ev.depth : undefined,
        status: 'running',
      });
      continue;
    }
    if (idx < 0) continue;
    const status =
      kind === 'child_completed'
        ? 'completed'
        : kind === 'child_failed'
          ? 'failed'
          : kind === 'child_cancelled'
            ? 'cancelled'
            : kind === 'child_interrupted'
              ? 'interrupted'
              : kind === 'child_resumed'
                ? 'running'
                : list[idx].status;
    list[idx] = { ...list[idx], status };
  }
  return list;
}

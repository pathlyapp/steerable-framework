import { useMemo } from 'react';
import { OrchestrationPlanCard } from '@steerable/agent-ui/cards';
import {
  childrenToCardModel,
  type ChildInfo,
} from './orchestration-children-model';

/**
 * OrchestrationChildrenCard — the desktop surface of the framework's P3.1
 * multi-agent orchestration. Adapts the live `ChildInfo` list (accumulated
 * from `orchestration_child` SSE events) onto the framework's
 * `OrchestrationPlanCard`: one row per child, status dot per row. The card
 * payload is synthesized from spawn events, so there is no separate "plan"
 * step: the plan IS the set of spawned children.
 *
 * The mapping logic lives in `./orchestration-children-model` (pure,
 * runtime-dependency-free) so node-side tests do not pull the agent-ui
 * dist build.
 */

export type { ChildInfo } from './orchestration-children-model';

export function OrchestrationChildrenCard({ children }: { children: ChildInfo[] }) {
  const { payload, taskStatuses } = useMemo(
    () => childrenToCardModel(children),
    [children],
  );

  if (children.length === 0) return null;
  return (
    <OrchestrationPlanCard
      payload={payload}
      taskStatuses={taskStatuses}
      agentNameFor={(id) => id}
      defaultExpanded
    />
  );
}

export default OrchestrationChildrenCard;

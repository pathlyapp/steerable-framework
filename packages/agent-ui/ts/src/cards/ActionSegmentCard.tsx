/**
 * `<ActionSegmentCard />`
 *
 * Horizontal strip of inline tool / action invocations within a single
 * assistant message. Each segment is rendered with `<ToolExecutionCard />` so
 * the visual behaviour matches the standalone tool-execution card.
 */
import * as React from 'react';
import type { ActionSegmentPayload, ToolExecutionPayload } from '@steerable/agent-protocol';
import { ToolExecutionCard } from './ToolExecutionCard.js';

export interface ActionSegmentCardProps {
  payload: ActionSegmentPayload;
  className?: string;
  renderArgs?: (args: unknown) => React.ReactNode;
  renderOutput?: (output: unknown) => React.ReactNode;
}

function segmentToTool(segment: ActionSegmentPayload['segments'][number]): ToolExecutionPayload {
  return {
    id: segment.id,
    name: segment.kind,
    status: segment.status,
    summary: segment.label ?? null,
    args: segment.args,
    output: segment.output,
    error: segment.error ?? null,
    durationMs:
      segment.startedAt && segment.finishedAt
        ? Math.max(0, Date.parse(segment.finishedAt) - Date.parse(segment.startedAt))
        : null,
    icon: null,
    expandable: true,
  };
}

export const ActionSegmentCard: React.FC<ActionSegmentCardProps> = ({
  payload,
  className,
  renderArgs,
  renderOutput,
}) => {
  const segments = payload.segments ?? [];
  if (segments.length === 0) return null;

  return (
    <div className={['steerable-action-segment space-y-1.5', className].filter(Boolean).join(' ')}>
      {segments.map((s) => (
        <ToolExecutionCard
          key={s.id}
          payload={segmentToTool(s)}
          renderArgs={renderArgs}
          renderOutput={renderOutput}
        />
      ))}
    </div>
  );
};

export default ActionSegmentCard;

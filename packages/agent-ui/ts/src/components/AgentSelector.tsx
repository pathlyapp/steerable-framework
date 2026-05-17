import { useMemo } from 'react';
import type { ChatAgent } from '@steerable/agent-protocol';
import { cn } from './cn.js';

export interface AgentSelectorProps {
  agents: ChatAgent[];
  selectedId: string;
  onSelect: (id: string) => void;
  renderAgent?: (agent: ChatAgent, state: { selected: boolean }) => React.ReactNode;
  className?: string;
  disabled?: boolean;
  loading?: boolean;
}

function defaultRenderAgent(agent: ChatAgent) {
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      {agent.icon ? (
        <span aria-hidden className="text-sm leading-none">
          {agent.icon}
        </span>
      ) : null}
      <span className="truncate">{agent.name}</span>
    </span>
  );
}

/**
 * AgentSelector
 *
 * Keyboard support:
 * - ArrowLeft / ArrowUp: move selection to previous agent
 * - ArrowRight / ArrowDown: move selection to next agent
 * - Home / End: jump to first / last agent
 * - Enter / Space: select focused agent (native button behavior)
 */
export function AgentSelector(props: AgentSelectorProps) {
  const {
    agents,
    selectedId,
    onSelect,
    renderAgent = defaultRenderAgent,
    className,
    disabled = false,
    loading = false,
  } = props;

  const selectedIndex = useMemo(
    () => agents.findIndex((a) => a.id === selectedId),
    [agents, selectedId],
  );

  if (loading) {
    return (
      <div
        className={cn(
          'inline-flex h-9 items-center rounded-agent-md border border-agent-border bg-agent-canvas px-3 text-sm text-agent-muted-foreground',
          className,
        )}
        aria-live="polite"
      >
        Loading agents…
      </div>
    );
  }

  if (agents.length === 0) {
    return (
      <div
        className={cn(
          'inline-flex h-9 items-center rounded-agent-md border border-agent-border bg-agent-canvas px-3 text-sm text-agent-muted-foreground',
          className,
        )}
      >
        No agents
      </div>
    );
  }

  return (
    <div
      role="radiogroup"
      aria-label="Agent selector"
      className={cn(
        'inline-flex max-w-full items-center gap-1 rounded-agent-lg border border-agent-border bg-agent-canvas p-1',
        className,
      )}
    >
      {agents.map((agent, index) => {
        const selected = agent.id === selectedId;
        const tabIndex =
          selected
            ? 0
            : selectedIndex >= 0
              ? -1
              : index === 0
                ? 0
                : -1;

        return (
          <button
            key={agent.id}
            type="button"
            role="radio"
            aria-checked={selected}
            aria-label={agent.name}
            tabIndex={tabIndex}
            disabled={disabled}
            onClick={() => onSelect(agent.id)}
            onKeyDown={(event) => {
              if (disabled) return;

              let nextIndex = index;
              if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
                nextIndex = Math.min(agents.length - 1, index + 1);
              } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
                nextIndex = Math.max(0, index - 1);
              } else if (event.key === 'Home') {
                nextIndex = 0;
              } else if (event.key === 'End') {
                nextIndex = agents.length - 1;
              } else {
                return;
              }

              if (nextIndex !== index) {
                event.preventDefault();
                const next = agents[nextIndex];
                onSelect(next.id);
              }
            }}
            className={cn(
              'inline-flex h-7 min-w-0 max-w-[220px] items-center rounded-agent-md px-2.5 text-sm transition-colors',
              selected
                ? 'bg-agent-accent text-agent-accent-foreground'
                : 'text-agent-foreground hover:bg-agent-muted',
              disabled && 'cursor-not-allowed opacity-50',
            )}
            title={agent.name}
          >
            {renderAgent(agent, { selected })}
          </button>
        );
      })}
    </div>
  );
}

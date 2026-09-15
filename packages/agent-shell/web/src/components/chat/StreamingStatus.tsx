import { motion } from 'framer-motion';

/**
 * StreamingStatus — informative placeholder for an assistant bubble that has
 * been created but doesn't have content tokens yet (or is between rounds).
 *
 * Local-backend's SSE stream gives us these signals while a turn is running:
 *   • token chunks   (`{content: "..."}`)   — handled by useChatStream
 *   • round_end      (`completion: executing`) — bumps `round` counter
 *   • executed_actions — fires after each round's tool calls
 *   • completion (completed/failed) — stream end
 *
 * So the meaningful "thinking" states we can actually display are:
 *   1. Round 1, no actions yet         → "正在思考..."
 *   2. Round 1+, just executed tools   → "已调用 N 个工具，正在分析..."
 *   3. Round ≥ 2, no actions this round → "Round N · 继续推理..."
 *
 * If the backend ever starts emitting real reasoning chunks, we can extend
 * `label` with a streaming text channel and progress dots.
 */
interface StreamingStatusProps {
  /** Current round number (1-based). `executions_actions` per round, framework appends content tokens between rounds. */
  round: number;
  /** Number of tool calls executed in this turn so far (across all rounds). */
  actionCount: number;
  /**
   * True when the assistant bubble has at least one content token already.
   * Pass through from the parent — when content is non-empty we render
   * nothing (the content + cursor blink is enough).
   */
  hasContent: boolean;
}

export function StreamingStatus({
  round,
  actionCount,
  hasContent,
}: StreamingStatusProps) {
  // Once tokens are flowing the parent already has plenty of visual feedback
  // (the streaming cursor); a separate status line would be noise.
  if (hasContent) return null;

  const label = selectLabel(round, actionCount);

  return (
    <motion.div
      key={label}
      initial={{ opacity: 0, y: 2 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
      // aria-live=polite + aria-atomic=true so AT clients (VoiceOver,
      // NVDA, JAWS) announce the new status text on each transition. We
      // chose `polite` over `assertive` because the change is informational,
      // not actionable — `assertive` would barge in on whatever the user is
      // currently reading.
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className="my-2 inline-flex items-center gap-2 text-xs text-agent-muted-foreground"
    >
      <Dots />
      <span>{label}</span>
      {round > 1 && (
        <span className="rounded-full border border-agent-border bg-agent-muted/40 px-1.5 py-[1px] font-mono text-[10px] text-agent-muted-foreground">
          round {round}
        </span>
      )}
    </motion.div>
  );
}

function selectLabel(round: number, actionCount: number): string {
  if (round >= 2 && actionCount === 0) {
    return `Round ${round} · 继续推理...`;
  }
  if (actionCount > 0) {
    return `已调用 ${actionCount} 个工具，正在分析...`;
  }
  return '正在思考...';
}

function Dots() {
  // Three pulsing dots — matches the inline indicator used inside
  // AssistantMessage when content === '', so the visual hands off cleanly
  // once tokens start streaming.
  return (
    <span className="inline-flex items-center gap-1">
      <span className="h-1 w-1 animate-pulse rounded-full bg-agent-foreground/40 [animation-delay:0ms]" />
      <span className="h-1 w-1 animate-pulse rounded-full bg-agent-foreground/40 [animation-delay:150ms]" />
      <span className="h-1 w-1 animate-pulse rounded-full bg-agent-foreground/40 [animation-delay:300ms]" />
    </span>
  );
}

export default StreamingStatus;

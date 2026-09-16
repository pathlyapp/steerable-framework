/**
 * UnifiedDiff — render a unified diff string (from local_edit_file's
 * `result.diff`) with per-line colouring. Host-layer renderer plugged into
 * ToolExecutionCard's `renderOutput` slot; the framework card stays generic.
 */
export function UnifiedDiff({ diff }: { diff: string }) {
  const lines = diff.split('\n');
  return (
    <pre className="overflow-x-auto rounded bg-agent-muted/40 px-2 py-1.5 font-mono text-[11px] leading-relaxed">
      {lines.map((line, i) => {
        let cls = 'text-agent-foreground';
        if (line.startsWith('@@')) {
          cls = 'text-agent-muted-foreground';
        } else if (line.startsWith('+++') || line.startsWith('---')) {
          cls = 'font-semibold text-agent-muted-foreground';
        } else if (line.startsWith('+')) {
          cls = 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400';
        } else if (line.startsWith('-')) {
          cls = 'bg-rose-500/10 text-rose-700 dark:text-rose-400';
        }
        return (
          <div key={i} className={cls}>
            {line === '' ? ' ' : line}
          </div>
        );
      })}
    </pre>
  );
}

export default UnifiedDiff;

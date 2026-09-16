import { LuCircle, LuCircleCheck, LuListChecks } from 'react-icons/lu';

/**
 * PlanTodoList — renders the ```plan fenced block that the plan-mode skill
 * instructs the model to emit, as a visual todolist card instead of a raw
 * code block.
 *
 * Expected fence content (see src/local-backend/skills/70-plan-mode):
 *
 *   # 一句话计划标题
 *   - [ ] 第一步……
 *   - [x] 已完成的步骤（执行回顾时可能出现）
 *
 * The parser is intentionally forgiving: numbered lists (`1. …`), plain
 * dashes (`- …`) and bare `[ ] …` lines all count as steps, so a slightly
 * off-spec local model still gets a proper todolist instead of a wall of
 * monospace text. Unclosed fences during streaming parse the same way, so
 * steps appear progressively as tokens arrive.
 */

interface PlanItem {
  text: string;
  done: boolean;
}

interface ParsedPlan {
  title: string | null;
  items: PlanItem[];
  notes: string[];
}

const CHECKBOX_RE = /^(?:[-*]\s*)?\[([ xX])\]\s*(.*)$/;
const BULLET_RE = /^[-*]\s+(.*)$/;
const NUMBERED_RE = /^\d+[.、)]\s*(.*)$/;

export function parsePlanContent(raw: string): ParsedPlan {
  const plan: ParsedPlan = { title: null, items: [], notes: [] };
  for (const line of raw.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;

    if (trimmed.startsWith('#')) {
      const title = trimmed.replace(/^#+\s*/, '');
      if (title && plan.title === null) plan.title = title;
      continue;
    }

    const checkbox = trimmed.match(CHECKBOX_RE);
    if (checkbox) {
      const text = checkbox[2].trim();
      if (text) plan.items.push({ text, done: checkbox[1] !== ' ' });
      continue;
    }

    const bullet = trimmed.match(BULLET_RE) ?? trimmed.match(NUMBERED_RE);
    if (bullet) {
      const text = bullet[1].trim();
      if (text) plan.items.push({ text, done: false });
      continue;
    }

    plan.notes.push(trimmed);
  }
  return plan;
}

export function PlanTodoList({ content }: { content: string }) {
  const { title, items, notes } = parsePlanContent(content);

  return (
    <div
      className="my-2 overflow-hidden rounded-agent-lg border border-amber-400/50 bg-amber-400/5 not-italic dark:border-amber-500/30"
      data-testid="plan-todo-list"
    >
      <div className="flex items-center gap-2 border-b border-amber-400/30 bg-amber-400/10 px-3 py-2 dark:border-amber-500/20">
        <LuListChecks className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
        <span className="flex-1 truncate text-[13px] font-semibold text-amber-800 dark:text-amber-200">
          {title || '执行计划'}
        </span>
        {items.length > 0 && (
          <span className="shrink-0 rounded-full bg-amber-400/20 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300">
            {items.filter((i) => i.done).length}/{items.length}
          </span>
        )}
      </div>
      {items.length > 0 && (
        <ol className="m-0 list-none space-y-1 px-3 py-2">
          {items.map((item, idx) => (
            <li key={idx} className="flex items-start gap-2 text-[13px] leading-relaxed">
              {item.done ? (
                <LuCircleCheck className="mt-[3px] h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
              ) : (
                <LuCircle className="mt-[3px] h-3.5 w-3.5 shrink-0 text-amber-500/60" />
              )}
              <span
                className={
                  item.done
                    ? 'text-agent-muted-foreground line-through'
                    : 'text-agent-foreground'
                }
              >
                {item.text}
              </span>
            </li>
          ))}
        </ol>
      )}
      {notes.length > 0 && (
        <div className="border-t border-amber-400/20 px-3 py-1.5 text-[12px] text-agent-muted-foreground">
          {notes.map((note, idx) => (
            <p key={idx} className="my-0.5">
              {note}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

export default PlanTodoList;

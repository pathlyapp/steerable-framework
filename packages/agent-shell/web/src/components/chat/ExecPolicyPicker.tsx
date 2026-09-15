import { useEffect, useRef, useState } from 'react';
import { LuChevronDown, LuFolderLock, LuLockOpen } from 'react-icons/lu';
import type { ExecPolicy } from '@/lib/exec-policy';

/**
 * ExecPolicyPicker — 输入框上的命令沙箱切换（类 Codex 底部权限档）。
 *
 * 切档立刻记住，下一轮才生效：当前回合的 `execSandbox` 已经随流发出。
 * 工作区 = Seatbelt/bwrap 只允许写项目根；完整权限 = 不下发命令沙箱，
 * 本机路径（如 Downloads）不再被 Operation not permitted 拦住。
 */

const OPTIONS: Array<{
  id: ExecPolicy;
  label: string;
  description: string;
}> = [
  {
    id: 'workspace',
    label: '工作区',
    description: '命令只能写入当前项目；项目外路径会被系统拒绝。',
  },
  {
    id: 'full',
    label: '完整权限',
    description: '关闭命令沙箱，可写本机任意路径。工具审批仍然生效。',
  },
];

export function ExecPolicyPicker({
  policy,
  disabled,
  onChange,
}: {
  policy: ExecPolicy;
  disabled: boolean;
  onChange: (policy: ExecPolicy) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const current = OPTIONS.find((option) => option.id === policy) ?? OPTIONS[0];
  const isFull = policy === 'full';

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open]);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="命令沙箱"
        data-testid="exec-policy-picker"
        title={current.description}
        onClick={() => setOpen((next) => !next)}
        className={[
          'inline-flex h-7 max-w-[140px] items-center gap-1 rounded-full border px-2 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-70',
          isFull
            ? 'border-amber-400/50 bg-amber-400/10 text-amber-700 dark:text-amber-400'
            : 'border-agent-border bg-agent-canvas text-agent-foreground hover:bg-agent-foreground/5',
        ].join(' ')}
      >
        {isFull ? (
          <LuLockOpen className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <LuFolderLock className="h-3.5 w-3.5 shrink-0" />
        )}
        <span className="truncate">{current.label}</span>
        <LuChevronDown className="h-3 w-3 shrink-0 text-agent-muted-foreground" />
      </button>
      {open && (
        <div
          role="listbox"
          aria-label="命令沙箱"
          className="absolute bottom-full left-0 z-30 mb-1 w-64 overflow-hidden rounded-agent-md border border-agent-border bg-agent-canvas py-1 shadow-sm"
        >
          {OPTIONS.map((option) => {
            const selected = option.id === policy;
            return (
              <button
                key={option.id}
                type="button"
                role="option"
                aria-selected={selected}
                data-testid={`exec-policy-${option.id}`}
                onClick={() => {
                  onChange(option.id);
                  setOpen(false);
                }}
                className={[
                  'flex w-full flex-col items-start gap-0.5 px-3 py-2 text-left transition-colors',
                  selected
                    ? 'bg-agent-foreground/10'
                    : 'hover:bg-agent-foreground/5',
                ].join(' ')}
              >
                <span className="text-xs font-medium text-agent-foreground">
                  {option.label}
                </span>
                <span className="text-[11px] leading-snug text-agent-muted-foreground">
                  {option.description}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

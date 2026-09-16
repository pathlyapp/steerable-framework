import type { ReactNode } from 'react';
import { LuX } from 'react-icons/lu';

/**
 * 右侧 dock（终端 / 后台推理）的统一头部 chrome。
 *
 * 两个面板的正文配色不同——终端固定深色，过程栏跟主题——但头部保持同一
 * 套几何与字号，切换视图时标题栏不跳动。
 */
export function DockPanelHeader({
  title,
  actions,
  onClose,
  closeLabel,
}: {
  /** 左侧标题，超长截断。 */
  title: string;
  /** 右侧动作区（视图切换按钮等），排在关闭按钮之前。 */
  actions?: ReactNode;
  /** 缺省时不渲染关闭按钮。 */
  onClose?: () => void;
  /** 关闭按钮的 title / aria-label。 */
  closeLabel?: string;
}) {
  return (
    <div className="flex items-center gap-2 border-b border-agent-border bg-agent-canvas px-3 py-1.5">
      <span className="min-w-0 flex-1 truncate text-[11px] text-agent-muted-foreground">
        {title}
      </span>
      {actions}
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground"
          title={closeLabel}
          aria-label={closeLabel}
        >
          <LuX className="h-3 w-3" />
        </button>
      )}
    </div>
  );
}

/** dock 头部的视图切换按钮（图标 + 文字），与关闭按钮同高。 */
export function DockHeaderButton({
  icon,
  label,
  title,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  title?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex h-5 shrink-0 items-center gap-1 rounded px-1.5 text-[11px] text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground"
      title={title ?? label}
    >
      {icon}
      {label}
    </button>
  );
}

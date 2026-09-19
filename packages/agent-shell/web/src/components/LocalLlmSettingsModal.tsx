import { useEffect } from 'react';
import { createPortal } from 'react-dom';
import { LuX } from 'react-icons/lu';
import type { LlmSettings } from '@/lib/local-api';
import { LlmSettingsPanel } from '@/components/settings/LlmSettingsPanel';

interface LocalLlmSettingsModalProps {
  open: boolean;
  onClose: () => void;
  onSaved?: (settings: LlmSettings) => void;
}

/**
 * 输入区齿轮的快捷入口：弹窗只承载本地模型表单。
 * 侧栏「设置」走 `/settings` 页（模型 + 洞察 / 遥测 / 用量 / 搜索 / 安全）。
 */
export function LocalLlmSettingsModal({ open, onClose, onSaved }: LocalLlmSettingsModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;
  if (typeof document === 'undefined') return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 backdrop-blur-sm"
      onMouseDown={(e) => {
        // 只认点在遮罩上的按下。面板内拖选/下拉选项后在阴影上弹起，
        // 合成 click 会落到遮罩，不能当关闭。
        if (e.target === e.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      data-testid="llm-settings-dialog"
    >
      <div
        className="flex h-[560px] max-h-[90vh] w-[540px] max-w-[92vw] flex-col overflow-hidden rounded-agent-lg border border-agent-border bg-agent-canvas shadow-2xl animate-fade-in"
      >
        <div className="flex h-9 flex-shrink-0 items-center justify-between border-b border-agent-border px-3">
          <h2 className="text-xs font-semibold text-agent-foreground">本地模型设置</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full p-1 text-agent-muted-foreground transition-colors hover:bg-agent-muted hover:text-agent-foreground"
            aria-label="关闭"
          >
            <LuX className="h-4 w-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-3">
          <LlmSettingsPanel onSaved={onSaved} />
        </div>
        <div className="flex h-9 flex-shrink-0 items-center justify-end border-t border-agent-border px-3 bg-agent-muted/10">
          <button
            type="button"
            onClick={onClose}
            className="h-7 rounded-full px-3 text-xs font-medium text-agent-muted-foreground transition-colors hover:bg-agent-muted hover:text-agent-foreground"
          >
            关闭
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

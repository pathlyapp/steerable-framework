/**
 * W7-1 崩溃恢复入口卡片。
 *
 * 上一轮回复因应用退出/异常中断而没有完成时（local-backend 在 messages
 * 响应里下发 `interrupted: true`——签名是 settings_kv 里残留的
 * turn_active 标记，而非文案截断的启发式），在消息列表尾部展示本卡片：
 * 「继续上次回复」走后端的 resume 通道（sidecar 回放 durable record 的
 * 投影作为循环种子，用户无需重发请求）；「忽略」仅在本次挂载内隐藏卡片，
 * 不写库——下次打开会话若标记仍在会再次提示。
 *
 * 用户主动停止（completionStatus: cancelled）与失败（failed）都是活进程
 * 写下的终态，不会进入本卡片：前者是用户的明确意图，后者已有错误展示。
 */
import { LuTriangleAlert } from 'react-icons/lu';

export interface InterruptedTurnCardProps {
  /** 点击「继续上次回复」——触发 resume 流（不再追加用户消息）。 */
  onContinue: () => void;
  /** 点击「忽略」——本次挂载内隐藏卡片（不落库）。 */
  onDismiss: () => void;
}

export function InterruptedTurnCard({ onContinue, onDismiss }: InterruptedTurnCardProps) {
  return (
    <div
      role="status"
      className="mt-1.5 flex items-center justify-between gap-3 rounded-agent-md border border-amber-400/40 bg-amber-400/10 px-3 py-2 text-xs"
    >
      <div className="flex items-center gap-2 text-amber-700 dark:text-amber-300">
        <LuTriangleAlert className="h-4 w-4 shrink-0" />
        <span>上次回复因应用退出或异常中断，未能完成。</span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <button
          type="button"
          onClick={onDismiss}
          className="rounded px-1.5 py-0.5 text-amber-700/80 transition-colors hover:text-amber-900 dark:text-amber-300/80 dark:hover:text-amber-100"
        >
          忽略
        </button>
        <button
          type="button"
          onClick={onContinue}
          className="inline-flex items-center gap-1 rounded-full bg-agent-foreground px-3 py-1 font-medium text-agent-canvas transition hover:opacity-90"
        >
          继续上次回复
        </button>
      </div>
    </div>
  );
}

export default InterruptedTurnCard;

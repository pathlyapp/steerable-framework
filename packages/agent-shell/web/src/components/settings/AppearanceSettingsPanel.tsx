import {
  THINKING_DISPLAY_OPTIONS,
  persistThinkingDisplay,
  useThinkingDisplay,
} from '@/lib/show-thinking-content';

/**
 * 界面偏好：思考过程正文按「隐藏 / 显示5行 / 完整显示」三档。
 * 立即写入 localStorage，不走设置页顶部的模型保存按钮。
 */
export function AppearanceSettingsPanel() {
  const mode = useThinkingDisplay();
  const current = THINKING_DISPLAY_OPTIONS.find((item) => item.mode === mode);

  return (
    <div
      className="space-y-2 rounded-agent-md border border-agent-border bg-agent-card p-2.5"
      data-testid="appearance-settings-panel"
    >
      <div>
        <p className="text-xs font-medium text-agent-foreground">显示思考内容</p>
        <p className="mt-1 text-xs leading-relaxed text-agent-muted-foreground">
          {current?.hint} 工作行只显示「工作中」或结束后的思考次数、工具次数和用时；底部 tok/s 是整段模型请求的生成速度（思考+回复），不含工具等待。
        </p>
      </div>
      <div
        className="flex w-full rounded-full border border-agent-border bg-agent-canvas p-0.5"
        role="radiogroup"
        aria-label="显示思考内容"
        data-testid="thinking-display-toggle"
      >
        {THINKING_DISPLAY_OPTIONS.map((item) => {
          const selected = mode === item.mode;
          return (
            <button
              key={item.mode}
              type="button"
              role="radio"
              aria-checked={selected}
              aria-label={item.label}
              data-testid={`thinking-display-${item.mode}`}
              onClick={() => persistThinkingDisplay(item.mode)}
              className={[
                'h-7 flex-1 rounded-full px-2 text-xs transition-colors',
                selected
                  ? 'bg-agent-foreground/10 text-agent-foreground'
                  : 'text-agent-muted-foreground hover:text-agent-foreground',
              ].join(' ')}
            >
              {item.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

import { persistShowThinkingContent, useShowThinkingContent } from '@/lib/show-thinking-content';

/**
 * 界面偏好：思考过程正文默认折叠，状态行仍显示思考中 / 工具名 / 速度 / 时间。
 * 立即写入 localStorage，不走设置页顶部的模型保存按钮。
 */
export function AppearanceSettingsPanel() {
  const showThinkingContent = useShowThinkingContent();

  return (
    <div
      className="space-y-3 rounded-agent-md border border-agent-border bg-agent-card p-4"
      data-testid="appearance-settings-panel"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm font-medium text-agent-foreground">显示思考内容</p>
          <p className="mt-1 text-xs leading-relaxed text-agent-muted-foreground">
            关闭后流式中只露出 7 行思考，结束后自动折叠。状态行仍显示思考中、工具名、速度和用时。
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={showThinkingContent}
          aria-label="显示思考内容"
          data-testid="show-thinking-content-toggle"
          onClick={() => persistShowThinkingContent(!showThinkingContent)}
          className={[
            'relative mt-0.5 inline-flex h-5 w-9 shrink-0 items-center rounded-full px-0.5 transition-colors',
            showThinkingContent ? 'bg-agent-foreground' : 'bg-agent-foreground/25',
          ].join(' ')}
        >
          <span
            className={[
              'h-4 w-4 rounded-full bg-agent-canvas shadow-sm transition-transform',
              showThinkingContent ? 'translate-x-4' : 'translate-x-0',
            ].join(' ')}
          />
        </button>
      </div>
    </div>
  );
}

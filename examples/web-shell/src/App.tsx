/**
 * Web-shell App — the framework's reference application.
 *
 * Anatomy:
 *   1. Picks a transport: `mock` (default, fully offline) or `sidecar`
 *      (talks to `examples/sidecar-roundtrip`) based on `VITE_TRANSPORT`.
 *   2. Wraps the chat in `<ChatSessionProvider value={useChatSession({…})}/>`
 *      so child components (the custom card renderer, prompt buttons) can
 *      access the running session without prop-drilling.
 *   3. Renders the compound `ChatPanel`. The card renderer detects payloads
 *      carried in `message.messageMetadata` and routes to the matching rich
 *      card from `@steerable/agent-ui/cards`.
 *
 * No styling overrides: the framework's `agent-*` Tailwind tokens are
 * defined in `styles.css` and the cards' CSS variables fall back to the
 * same palette.
 */
import * as React from 'react';
import {
  ChatSessionProvider,
  ChatPanel,
  useChatSession,
  type ChatStreamTransport,
} from '@steerable/agent-ui';
import { createMockTransport } from './transports/mock.js';
import { createSidecarTransport } from './transports/sidecar.js';
import { CardMessageRenderer } from './components/CardMessageRenderer.js';

const TRANSPORT_MODE = ((import.meta.env.VITE_TRANSPORT as string | undefined) ?? 'mock').toLowerCase();

const SIDEBAR_DEMOS: Array<{ label: string; prompt: string }> = [
  { label: '编排计划', prompt: '帮我看一下当前的编排计划' },
  { label: '研究子代理', prompt: '当前研究计划进展如何？' },
  { label: '候选方案', prompt: '给我准备几个备选方案' },
  { label: '生成测验', prompt: '出一份小测验' },
  { label: '覆盖度报告', prompt: '看一下本周的覆盖度' },
  { label: '思考过程', prompt: '展开看一下你的思考过程' },
  { label: '行动步骤', prompt: '列出你接下来的行动步骤' },
  { label: '工具执行', prompt: '看一下刚才那次工具调用' },
  { label: '已执行操作', prompt: '看一下刚刚的两个操作' },
  { label: '检索来源', prompt: '把你查过的来源列出来' },
  { label: '建议回复', prompt: '给我几个追问建议' },
  { label: '历史摘要', prompt: '给我看一下历史摘要' },
  { label: '深度分析', prompt: '我需要一份分析文档' },
  { label: '问我几个问题', prompt: '帮我问几个澄清问题' },
];

function App() {
  const transport: ChatStreamTransport = React.useMemo(
    () => (TRANSPORT_MODE === 'sidecar' ? createSidecarTransport() : createMockTransport()),
    [],
  );
  const session = useChatSession({ transport });
  (window as any).session = session;
  const [darkMode, setDarkMode] = React.useState(false);

  React.useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode);
  }, [darkMode]);

  const onPrompt = (text: string) => {
    void session.sendUserMessage({ content: text });
  };

  return (
    <ChatSessionProvider value={session}>
      <div className={`h-full grid grid-cols-[260px_1fr] ${darkMode ? 'dark' : ''}`}>
        <aside className="flex h-full flex-col border-r border-agent-border bg-agent-canvas">
          <div className="border-b border-agent-border px-4 py-3 text-sm">
            <div className="font-semibold text-agent-foreground">Steerable web-shell</div>
            <div className="text-xs text-agent-muted-foreground">
              transport: <code className="rounded bg-agent-muted px-1 py-0.5 text-[11px]">{TRANSPORT_MODE}</code>
            </div>
          </div>
          <nav className="flex-1 overflow-y-auto p-3">
            <p className="mb-2 text-[11px] uppercase tracking-wider text-agent-muted-foreground">
              卡片场景
            </p>
            <ul className="space-y-1">
              {SIDEBAR_DEMOS.map((d) => (
                <li key={d.label}>
                  <button
                    type="button"
                    onClick={() => onPrompt(d.prompt)}
                    className="w-full rounded px-2 py-1.5 text-left text-xs text-agent-foreground hover:bg-agent-muted"
                  >
                    {d.label}
                  </button>
                </li>
              ))}
            </ul>
          </nav>
          <footer className="border-t border-agent-border p-3 text-xs">
            <label className="flex items-center gap-2 text-agent-muted-foreground">
              <input
                type="checkbox"
                checked={darkMode}
                onChange={(e) => setDarkMode(e.target.checked)}
              />
              暗色模式
            </label>
            <p className="mt-2 text-[10px] text-agent-muted-foreground/80">
              VITE_TRANSPORT=sidecar 启用真后端
            </p>
          </footer>
        </aside>
        <main className="flex h-full flex-col">
          <ChatPanel.Connected
            header={
              <div className="flex w-full items-center justify-between">
                <span className="font-medium text-agent-foreground">Live demo · 14 rich cards</span>
                <span className="text-xs text-agent-muted-foreground">
                  在左侧点一个场景，或直接在输入框输入关键词
                </span>
              </div>
            }
            emptyTitle="试一个卡片场景"
            emptyDescription="在左侧挑一个，或者直接输入「测验」/「分析」/「编排」等关键词。"
            emptyPrompts={['测验', '编排计划', '研究计划', '候选方案', '覆盖度报告']}
            renderMessage={CardMessageRenderer}
            inputPlaceholder="问点什么…  (Cmd/Ctrl+Enter 发送)"
          />
        </main>
      </div>
    </ChatSessionProvider>
  );
}

export default App;

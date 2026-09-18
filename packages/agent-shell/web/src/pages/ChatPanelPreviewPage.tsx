import { useMemo, useState } from 'react';
import type { ChatMessage } from '@steerable/agent-protocol';
import { LocalChatPanel } from '@/components/chat/LocalChatPanel';
import { ChatHeader } from '@/components/ChatHeader';
import type { ExecutedAction } from '@/components/chat/ExecutedActionsCard';
import type { TurnBlock } from '@/components/chat/turn-timeline';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';

/**
 * ChatPanelPreviewPage — dev-only visual harness for `LocalChatPanel`.
 *
 * Mounted at `/preview/chat` in dev. Produces deterministic mock chat data so
 * we can iterate on the message renderer, executed-actions card, copy buttons
 * etc. without needing local-backend to be wired up.
 *
 * Production builds keep the route around (tree-shake'd only when truly dead),
 * but the dataset is small enough (a few hundred lines of mock content) that
 * it's not worth gating behind `import.meta.env.DEV`. If we ever need to,
 * wrap the router entry in `main.tsx` instead of guarding here.
 */

const MOCK_AGENT: LocalChatAgent = {
  id: 'agent_local_default',
  slug: 'local-coder',
  name: '本地 Coder',
  icon: '🤖',
  color: '#0ea5e9',
  description: '本地代码助手 — 跑 shell / 工具演示 / 简单读写',
  rolePrompt: null,
  isBuiltin: true,
};

const MOCK_CHAT: LocalChat = {
  id: 'preview-chat',
  projectId: null,
  userId: 'preview-user',
  title: '预览：检查服务连通性 + 跑测试套件',
  agentId: MOCK_AGENT.id,
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
  isPinned: false,
  systemPrompt: null,
  pinnedRefs: null,
};

const MOCK_MESSAGES: ChatMessage[] = [
  {
    id: 'm1',
    role: 'user',
    content: '帮我看下本地服务是否连通，再跑一下本目录的测试套件。',
    createdAt: new Date(Date.now() - 1000 * 60 * 5).toISOString(),
  },
  {
    id: 'm2',
    role: 'assistant',
    agentId: MOCK_AGENT.id,
    content: `好的，先检查服务连通性，然后跑测试。

\`\`\`bash
$ check_connection
✓ Mock service @ 127.0.0.1:8765 reachable (latency 4ms)
\`\`\`

连接正常。接下来跑测试套件：

\`\`\`bash
$ npm test
PASS  src/components/chat/ChatInput.test.tsx
PASS  src/components/chat/MessageList.test.tsx
FAIL  src/components/chat/AssistantMessage.test.tsx
  ✗ renders agent badge when agentId present (5 ms)
    Expected color "#0ea5e9", got "#7c3aed"

Tests: 1 failed, 2 passed, 3 total
\`\`\`

有 1 个失败：\`AssistantMessage\` 的默认 agent 色没有从 props 传过来。建议在
\`agentInitial\` 之前先 \`a.color ?? '#7c3aed'\`，让 fallback 优先于硬编码。

要我帮你直接改这个文件吗？`,
    createdAt: new Date(Date.now() - 1000 * 60 * 3).toISOString(),
  },
];

const MOCK_ACTIONS: ExecutedAction[] = [
  {
    tool: 'check_connection',
    arguments: { host: '127.0.0.1', port: 8765 },
    result: { success: true, latencyMs: 4 },
  },
  {
    tool: 'shell_run',
    arguments: { command: 'npm test', cwd: '/home/me/proj' },
    result: {
      success: false,
      exitCode: 1,
      stdout: '...test output truncated...',
      stderr: 'Tests: 1 failed, 2 passed, 3 total',
    },
  },
  {
    tool: 'fs_read_file',
    arguments: { path: 'src/components/chat/AssistantMessage.tsx' },
    result: { success: true, bytes: 4096 },
  },
];

const MOCK_TIMELINE: TurnBlock[] = [
  { type: 'reasoning', content: '先检查服务连通性，再跑测试套件。' },
  { type: 'tools', actions: [MOCK_ACTIONS[0]] },
  { type: 'text', content: '连接正常。接下来跑测试套件。' },
  { type: 'tools', actions: [MOCK_ACTIONS[1]] },
  { type: 'reasoning', content: '测试失败，读一下失败文件再给建议。' },
  { type: 'tools', actions: [MOCK_ACTIONS[2]] },
  { type: 'text', content: MOCK_MESSAGES[1].content ?? '' },
];

/**
 * Three scenarios to flip between with the top-right pill buttons:
 *   • `static`     — finished conversation; verifies copy buttons, action
 *     cards, markdown rendering on history.
 *   • `thinking`   — in-flight assistant bubble before any content; verifies
 *     StreamingStatus's "正在思考..." baseline.
 *   • `tools-run`  — in-flight bubble, round 2, 2 tools just ran; verifies
 *     StreamingStatus's "已调用 N 个工具" + "Round N" badge interaction.
 */
type PreviewScene = 'static' | 'thinking' | 'tools-run';

const SCENES: { id: PreviewScene; label: string }[] = [
  { id: 'static', label: '静态历史' },
  { id: 'thinking', label: '流式 · 空内容' },
  { id: 'tools-run', label: '流式 · 工具已跑 · round 2' },
];

export function ChatPanelPreviewPage() {
  const [scene, setScene] = useState<PreviewScene>('static');

  // Explicit struct type so TS doesn't try to narrow `actionsByMsgId` to the
  // union of {} | { m2: ... } — both shapes are valid `Record<string, ...>`
  // but the inferred type rejects each side mutually.
  interface SceneConfig {
    messages: ChatMessage[];
    isStreaming: boolean;
    actionsByMsgId: Record<string, ExecutedAction[]>;
    currentTurnActions: ExecutedAction[];
    timelineByMsgId: Record<string, TurnBlock[]>;
    currentTurnTimeline?: TurnBlock[];
    currentTurnStartedAtMs?: number;
    durationByMessageId?: Record<string, number>;
    currentRound: number;
    suggestedReplies?: string[];
  }
  const config = useMemo<SceneConfig>(() => {
    switch (scene) {
      case 'thinking':
        return {
          messages: [
            MOCK_MESSAGES[0],
            {
              id: 'm-streaming',
              role: 'assistant',
              agentId: MOCK_AGENT.id,
              content: '',
              createdAt: new Date().toISOString(),
            },
          ],
          isStreaming: true,
          actionsByMsgId: {} as Record<string, ExecutedAction[]>,
          currentTurnActions: [],
          timelineByMsgId: {} as Record<string, TurnBlock[]>,
          currentTurnTimeline: [],
          currentRound: 1,
        };
      case 'tools-run':
        return {
          messages: [
            MOCK_MESSAGES[0],
            {
              id: 'm-streaming',
              role: 'assistant',
              agentId: MOCK_AGENT.id,
              content: '',
              createdAt: new Date().toISOString(),
            },
          ],
          isStreaming: true,
          actionsByMsgId: {} as Record<string, ExecutedAction[]>,
          currentTurnActions: MOCK_ACTIONS.slice(0, 2),
          timelineByMsgId: {} as Record<string, TurnBlock[]>,
          currentTurnTimeline: [
            {
              type: 'reasoning',
              content: [
                '先检查服务是否还能连上。',
                '如果端口通了，再跑本目录的测试套件。',
                '失败的话只读失败文件，不要整仓扫一遍。',
                '断言颜色对不上时优先看 AssistantMessage 的默认色。',
                '改完再复跑一次，确认没有带出新的失败。',
                '工具调用保持最少：连通性检查、跑测试、必要时读文件。',
                '最后用一两句说清楚结果和下一步。',
                '如果还在思考，后面的句子会被 7 行窗口裁掉。',
              ].join('\n'),
            },
            {
              type: 'tools',
              actions: [MOCK_ACTIONS[0], { ...MOCK_ACTIONS[1], result: undefined }],
            },
          ],
          currentTurnStartedAtMs: Date.now() - 12_000,
          currentRound: 2,
        };
      case 'static':
      default:
        return {
          messages: MOCK_MESSAGES,
          isStreaming: false,
          actionsByMsgId: { m2: MOCK_ACTIONS },
          currentTurnActions: [],
          timelineByMsgId: { m2: MOCK_TIMELINE },
          durationByMessageId: { m2: 83_000 },
          currentRound: 1,
          suggestedReplies: ['修一下失败的断言颜色', '把测试再跑一遍', '解释这次失败的原因'],
        };
    }
  }, [scene]);

  return (
    <div className="flex h-full w-full flex-col bg-agent-muted/30">
      <div className="flex shrink-0 items-center gap-2 border-b border-agent-border bg-agent-canvas px-3 py-2 text-xs">
        <span className="text-agent-muted-foreground">
          /preview/chat (dev harness)
        </span>
        <div className="flex gap-1">
          {SCENES.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => setScene(s.id)}
              className={`rounded-full border px-2 py-0.5 text-[11px] transition-colors ${
                scene === s.id
                  ? 'border-agent-foreground/40 bg-agent-foreground/10 text-agent-foreground'
                  : 'border-agent-border bg-agent-canvas text-agent-muted-foreground hover:bg-agent-foreground/5'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>
      <div className="m-2 flex flex-1 overflow-hidden rounded-agent-lg bg-agent-canvas shadow-sm">
        <LocalChatPanel
          messages={config.messages}
          isStreaming={config.isStreaming}
          onSubmit={async ({ content }) => {
            console.log('[preview] submit:', content);
          }}
          onCancel={() => {}}
          className="flex-1"
          header={
            <ChatHeader chat={MOCK_CHAT} />
          }
          inputPlaceholder="预览模式 — 输入不会真的发送…"
          agents={[MOCK_AGENT]}
          currentAgent={MOCK_AGENT}
          executedActionsByMessageId={config.actionsByMsgId}
          currentTurnActions={config.currentTurnActions}
          timelineByMessageId={config.timelineByMsgId}
          currentTurnTimeline={config.currentTurnTimeline}
          currentTurnStartedAtMs={config.currentTurnStartedAtMs}
          durationByMessageId={config.durationByMessageId}
          currentRound={config.currentRound}
          suggestedReplies={config.suggestedReplies}
          onSelectSuggestion={(text) => console.log('[preview] suggestion:', text)}
          onOpenSettings={() =>
            console.log('[preview] open llm settings (noop in harness)')
          }
        />
      </div>
    </div>
  );
}

export default ChatPanelPreviewPage;

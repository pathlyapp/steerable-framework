/**
 * Mock transport for the web-shell. Wraps the framework's
 * `MockChatStreamTransport` with the `cardScripts` registry so that when the
 * user types a keyword (e.g. "测验" or "quiz") the mock streams a fixture-
 * backed card payload back as the next assistant message.
 *
 * No external services are required to use this transport; everything is
 * inlined at build time. This is what makes `pnpm dev` work right after
 * `pnpm install` with zero ceremony.
 */
import { MockChatStreamTransport, type ChatStreamTransport, type ChatStreamSendInput } from '@steerable/agent-ui';
import { buildCardScript, detectCardKind } from './cardScripts.js';

const fallbackScript = [
  ...'我是 web-shell 中的 mock agent。试试问「测验」、「编排」、「方案」、「分析」、「研究」、「来源」、「思考」、「步骤」、「操作」、「工具」、「摘要」、「问我」、「建议」、「覆盖」中任意一个，就能看到对应卡片的 fixture。'
    .split('')
    .map((ch) => ({ event: { type: 'content' as const, content: ch } as any, delayMs: 18 })),
  { event: { type: 'done' as const } as any },
];

export function createMockTransport(): ChatStreamTransport {
  return {
    async stream(input: ChatStreamSendInput, onEvent) {
      const kind = detectCardKind(input.content);
      const script = kind ? buildCardScript(kind) : fallbackScript;
      const inner = new MockChatStreamTransport({ scripts: [script] });
      return await inner.stream(input, onEvent);
    },
  };
}

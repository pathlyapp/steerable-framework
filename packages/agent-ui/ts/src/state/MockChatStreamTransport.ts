/**
 * `MockChatStreamTransport` — canned-script transport for stories, tests, and
 * the framework's runnable web-shell example.
 *
 * Replays a deterministic list of `SSEEvent` per call to `stream()`. Optional
 * per-event delays make streaming feel real in demos. Loop the same script
 * forever, or vary by index — the constructor accepts either an array (one
 * script per turn) or a function `(turn: number) => SSEEvent[]`.
 *
 *   const transport = new MockChatStreamTransport({
 *     scripts: [
 *       [{ type: 'content', content: 'Hello, ' }, { type: 'content', content: 'world!' }, { type: 'done' }],
 *     ],
 *     defaultDelayMs: 25,
 *   });
 *
 *   const { messages, sendUserMessage } = useChatStream({ transport });
 */

import type { SSEEvent } from '@steerable/agent-protocol';
import type {
  ChatStreamSendInput,
  ChatStreamTransport,
} from '../hooks/useChatStream.js';

export type MockScriptStep =
  | SSEEvent
  | { event: SSEEvent; delayMs?: number };

export type MockScript = MockScriptStep[];

export interface MockChatStreamTransportOptions {
  /**
   * A list of scripts (one per turn), or a callback that returns a script for
   * the current turn. Required.
   */
  scripts: MockScript[] | ((turn: number, input: ChatStreamSendInput) => MockScript);
  /** Delay between events when the step doesn't specify one. Defaults to 0. */
  defaultDelayMs?: number;
  /**
   * What to do when there are fewer scripts than turns:
   *   - 'cycle' (default): wrap modulo `scripts.length`.
   *   - 'last': stay on the last script.
   *   - 'empty': emit only a `done` event.
   */
  exhaustionPolicy?: 'cycle' | 'last' | 'empty';
}

export class MockChatStreamTransport implements ChatStreamTransport {
  private turn = 0;
  /** Set of in-flight cancel signals, exposed for tests. */
  readonly cancelled = new Set<number>();

  constructor(private readonly options: MockChatStreamTransportOptions) {}

  /** Reset the turn counter — useful between fixture-driven test cases. */
  reset(): void {
    this.turn = 0;
    this.cancelled.clear();
  }

  async stream(
    input: ChatStreamSendInput,
    onEvent: (event: SSEEvent) => void,
  ): Promise<() => void> {
    const turn = this.turn++;
    const script = this.resolveScript(turn, input);
    let cancelled = false;
    const cancel = () => {
      cancelled = true;
      this.cancelled.add(turn);
    };
    void this.run(script, onEvent, () => cancelled);
    // Return synchronously so the framework hook can capture the cancel handle
    // without waiting for the full script.
    return cancel;
  }

  private resolveScript(turn: number, input: ChatStreamSendInput): MockScript {
    const { scripts, exhaustionPolicy = 'cycle' } = this.options;
    if (typeof scripts === 'function') return scripts(turn, input);
    if (scripts.length === 0) return [{ type: 'done' }];
    if (turn < scripts.length) return scripts[turn];
    switch (exhaustionPolicy) {
      case 'last':
        return scripts[scripts.length - 1];
      case 'empty':
        return [{ type: 'done' }];
      case 'cycle':
      default:
        return scripts[turn % scripts.length];
    }
  }

  private async run(
    script: MockScript,
    onEvent: (event: SSEEvent) => void,
    isCancelled: () => boolean,
  ): Promise<void> {
    const defaultDelay = this.options.defaultDelayMs ?? 0;
    for (const step of script) {
      if (isCancelled()) return;
      const { event, delayMs } = normaliseStep(step, defaultDelay);
      if (delayMs > 0) await sleep(delayMs);
      if (isCancelled()) return;
      onEvent(event);
    }
  }
}

function normaliseStep(
  step: MockScriptStep,
  defaultDelayMs: number,
): { event: SSEEvent; delayMs: number } {
  if ('event' in step && step.event && typeof step.event === 'object') {
    return { event: step.event, delayMs: step.delayMs ?? defaultDelayMs };
  }
  return { event: step as SSEEvent, delayMs: defaultDelayMs };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Work-row summary vs per-thinking-fold live stats.
 * Footer tok/s is the model HTTP-stream generation speed across every
 * output stage (reasoning + reply), excluding tool-wait gaps.
 */

import { formatElapsedCompact } from './elapsed';
import {
  countProcessReasoning,
  countProcessTools,
  type TurnBlock,
} from './turn-timeline';

/** Fallback thinking duration for legacy timelines that never stored durationMs. */
export function estimateReasoningDurationMs(content: string): number | undefined {
  const tokens = estimateTextTokens(content);
  if (tokens < 4) return undefined;
  return Math.max(1000, Math.round((tokens / 35) * 1000));
}

export function estimateTextTokens(text: string): number {
  if (!text) return 0;
  let cjk = 0;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (
      (code >= 0x4e00 && code <= 0x9fff) ||
      (code >= 0x3000 && code <= 0x303f) ||
      (code >= 0xff00 && code <= 0xffef)
    ) {
      cjk++;
    }
  }
  return Math.ceil(cjk * 0.6 + (text.length - cjk) * 0.25);
}

export function formatTokenSpeed(tokens: number, elapsedMs: number): string | null {
  if (tokens < 1 || elapsedMs < 400) return null;
  const perSec = tokens / (elapsedMs / 1000);
  if (perSec < 0.5) return null;
  const shown = perSec >= 10 ? Math.round(perSec) : Math.round(perSec * 10) / 10;
  return `${shown} tok/s`;
}

export function lastToolsAreRunning(process: TurnBlock[]): boolean {
  const last = process[process.length - 1];
  if (last?.type !== 'tools' || last.actions.length === 0) return false;
  return last.actions.some((action) => action.result == null);
}

export function activeToolNames(process: TurnBlock[]): string[] {
  for (let i = process.length - 1; i >= 0; i -= 1) {
    const block = process[i];
    if (block.type !== 'tools' || block.actions.length === 0) continue;
    const running = block.actions.filter((action) => action.result == null);
    const source = running.length > 0 ? running : block.actions;
    return source.slice(-2).map((action) => action.tool);
  }
  return [];
}

function formatElapsedPart(
  isLive: boolean,
  elapsedMs: number | undefined,
): string | null {
  if (elapsedMs == null) return null;
  if (!isLive && elapsedMs < 1000) return null;
  return formatElapsedCompact(elapsedMs);
}

/** Work disclosure: live is only 「工作中」; finished is a counts + duration summary. */
export function processStatusLabel(input: {
  process: TurnBlock[];
  isStreaming: boolean;
  elapsedMs?: number;
}): string {
  const { process, isStreaming, elapsedMs } = input;
  if (isStreaming) return '工作中';

  const parts: string[] = [];
  const thinks = countProcessReasoning(process);
  const tools = countProcessTools(process);
  const elapsed = formatElapsedPart(false, elapsedMs);
  if (elapsed) parts.push(`工作了 ${elapsed}`);
  if (thinks > 0) parts.push(`思考 ${thinks} 次`);
  if (tools > 0) parts.push(`工具调用 ${tools} 次`);
  if (parts.length === 0) return '执行过程';
  return parts.join(' · ');
}

/** Per-round 思考 fold: live time, or frozen duration after that round ends. */
export function thinkingFoldLabel(input: {
  content: string;
  isLive: boolean;
  elapsedMs?: number;
}): string {
  const elapsed = formatElapsedPart(input.isLive, input.elapsedMs);
  if (input.isLive) {
    return elapsed ? `思考中 · ${elapsed}` : '思考中';
  }
  return elapsed ? `思考 · ${elapsed}` : '思考';
}

/** Reasoning + reply tokens for the message-footer tok/s. */
export function estimateTurnTokens(blocks: TurnBlock[], fallbackContent = ''): number {
  if (blocks.length === 0) return estimateTextTokens(fallbackContent);
  let tokens = 0;
  for (const block of blocks) {
    if (block.type === 'reasoning' || block.type === 'text') {
      tokens += estimateTextTokens(block.content);
    }
  }
  return tokens;
}

/** Snapshot of model-request generation speed (all LLM output stages). */
export type LlmSpeedSnapshot = {
  tokens: number;
  elapsedMs: number;
  live: boolean;
  closedMs: number;
  requestStartedAt: number | null;
};

export function parseLlmSpeedPayload(payload: unknown): LlmSpeedSnapshot | null {
  if (!payload || typeof payload !== 'object') return null;
  const value = payload as Record<string, unknown>;
  if (typeof value.tokens !== 'number' || typeof value.elapsedMs !== 'number') {
    return null;
  }
  return {
    tokens: value.tokens,
    elapsedMs: value.elapsedMs,
    live: value.live === true,
    closedMs: typeof value.closedMs === 'number' ? value.closedMs : value.elapsedMs,
    requestStartedAt:
      typeof value.requestStartedAt === 'number' ? value.requestStartedAt : null,
  };
}

export function llmRequestElapsedMs(
  snap: LlmSpeedSnapshot | null | undefined,
  now: number,
): number {
  if (!snap) return 0;
  if (snap.live && snap.requestStartedAt != null) {
    return snap.closedMs + Math.max(0, now - snap.requestStartedAt);
  }
  return snap.elapsedMs;
}

/**
 * Accumulates tokens + elapsed only while the model is streaming.
 * `endRequest` freezes the current burst so tool waits do not dilute tok/s.
 */
export class LlmRequestSpeedTracker {
  private requestStartedAt: number | null = null;
  private requestText = '';
  private closedText = '';
  private closedMs = 0;

  noteOutput(delta: string, now = Date.now()): LlmSpeedSnapshot {
    if (delta) {
      if (this.requestStartedAt == null) this.requestStartedAt = now;
      this.requestText += delta;
    }
    return this.snapshot(now);
  }

  endRequest(now = Date.now()): LlmSpeedSnapshot {
    if (this.requestStartedAt != null) {
      this.closedText += this.requestText;
      this.closedMs += Math.max(0, now - this.requestStartedAt);
      this.requestStartedAt = null;
      this.requestText = '';
    }
    return this.snapshot(now);
  }

  snapshot(now = Date.now()): LlmSpeedSnapshot {
    const tokens = estimateTextTokens(this.closedText + this.requestText);
    let elapsedMs = this.closedMs;
    if (this.requestStartedAt != null) {
      elapsedMs += Math.max(0, now - this.requestStartedAt);
    }
    return {
      tokens,
      elapsedMs,
      live: this.requestStartedAt != null,
      closedMs: this.closedMs,
      requestStartedAt: this.requestStartedAt,
    };
  }
}

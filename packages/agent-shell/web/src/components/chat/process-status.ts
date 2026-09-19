/**
 * Work-row summary vs per-thinking-fold live stats.
 * Token speed is a coarse estimate from reasoning text (same CJK/other
 * weights as the desktop compactor).
 */

import { formatElapsedCompact } from './elapsed';
import {
  countProcessReasoning,
  countProcessTools,
  type TurnBlock,
} from './turn-timeline';

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
  if (thinks > 0) parts.push(`思考 ${thinks} 次`);
  if (tools > 0) parts.push(`工具调用 ${tools} 次`);
  const elapsed = formatElapsedPart(false, elapsedMs);
  if (elapsed) parts.push(`工作了 ${elapsed}`);
  if (parts.length === 0) return '执行过程';
  return parts.join(' · ');
}

/** Per-round 思考 fold: live speed + time, or frozen duration after that round ends. */
export function thinkingFoldLabel(input: {
  content: string;
  isLive: boolean;
  elapsedMs?: number;
}): string {
  const elapsed = formatElapsedPart(input.isLive, input.elapsedMs);
  if (input.isLive) {
    const parts = ['思考中'];
    const speed = formatTokenSpeed(estimateTextTokens(input.content), input.elapsedMs ?? 0);
    if (speed) parts.push(speed);
    if (elapsed) parts.push(elapsed);
    return parts.join(' · ');
  }
  return elapsed ? `思考 · ${elapsed}` : '思考';
}

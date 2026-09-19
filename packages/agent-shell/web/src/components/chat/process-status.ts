/**
 * Turn-process status line: live 「思考中 / 调用工具中」 plus finished
 * 「N 次工具调用 · 已思考 · 工作了 …」. Token speed is a coarse estimate
 * from reasoning text (same CJK/other weights as the desktop compactor).
 */

import { formatElapsedCompact } from './elapsed';
import {
  countProcessTools,
  processHasReasoning,
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

export function estimateProcessReasoningTokens(process: TurnBlock[]): number {
  let tokens = 0;
  for (const block of process) {
    if (block.type === 'reasoning') tokens += estimateTextTokens(block.content);
  }
  return tokens;
}

function speedElapsedMs(
  reasoningElapsedMs: number | undefined,
  elapsedMs: number | undefined,
): number {
  if (reasoningElapsedMs != null && reasoningElapsedMs >= 400) return reasoningElapsedMs;
  return elapsedMs ?? reasoningElapsedMs ?? 0;
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
  isStreaming: boolean,
  elapsedMs: number | undefined,
): string | null {
  if (elapsedMs == null) return null;
  if (!isStreaming && elapsedMs < 1000) return null;
  return formatElapsedCompact(elapsedMs);
}

export function processStatusLabel(input: {
  process: TurnBlock[];
  isStreaming: boolean;
  elapsedMs?: number;
  reasoningElapsedMs?: number;
}): string {
  const { process, isStreaming, elapsedMs, reasoningElapsedMs } = input;
  const elapsed = formatElapsedPart(isStreaming, elapsedMs);

  if (isStreaming) {
    const parts: string[] = [];
    const last = process[process.length - 1];
    if (lastToolsAreRunning(process)) {
      parts.push('调用工具中');
      const names = activeToolNames(process);
      if (names.length > 0) parts.push(names.join('、'));
    } else {
      parts.push('思考中');
      const tokens =
        last?.type === 'reasoning'
          ? estimateTextTokens(last.content)
          : estimateProcessReasoningTokens(process);
      const speed = formatTokenSpeed(
        tokens,
        speedElapsedMs(
          last?.type === 'reasoning' ? reasoningElapsedMs : undefined,
          elapsedMs,
        ),
      );
      if (speed) parts.push(speed);
    }
    if (elapsed) parts.push(elapsed);
    return parts.join(' · ');
  }

  const parts: string[] = [];
  const tools = countProcessTools(process);
  if (tools > 0) parts.push(`${tools} 次工具调用`);
  if (processHasReasoning(process)) parts.push('已思考');
  if (elapsed) parts.push(`工作了 ${elapsed}`);
  if (parts.length === 0) return '执行过程';
  return parts.join(' · ');
}

/**
 * Assistant-turn display timeline: reasoning, tool cards, and reply text
 * in call order (think → act → observe → reply), not as two stacked
 * groups.
 *
 * Live streams reconstruct this from SSE order. Persisted assistant
 * metadata may carry the same `timeline` array so history reloads keep
 * the interleaving. Messages without it fall back to tools-then-text.
 */

import type { ExecutedAction } from './ExecutedActionsCard';

export type TurnBlock =
  | { type: 'reasoning'; content: string }
  | { type: 'text'; content: string }
  | { type: 'tools'; actions: ExecutedAction[] };

export function appendDelta(
  blocks: TurnBlock[],
  type: 'text' | 'reasoning',
  delta: string,
): TurnBlock[] {
  if (!delta) return blocks;
  const last = blocks[blocks.length - 1];
  if (last && last.type === type) {
    const next = blocks.slice();
    next[next.length - 1] = { type, content: last.content + delta };
    return next;
  }
  return [...blocks, { type, content: delta }];
}

export function syncTools(
  blocks: TurnBlock[],
  actions: ExecutedAction[],
): TurnBlock[] {
  let placed = 0;
  for (const block of blocks) {
    if (block.type === 'tools') placed += block.actions.length;
  }

  const next: TurnBlock[] = blocks.map((block) =>
    block.type === 'tools' ? { type: 'tools', actions: [...block.actions] } : block,
  );

  let idx = 0;
  for (const block of next) {
    if (block.type !== 'tools') continue;
    for (let i = 0; i < block.actions.length; i += 1) {
      if (idx < actions.length) {
        block.actions[i] = actions[idx];
        idx += 1;
      }
    }
  }

  const added = actions.slice(placed);
  if (added.length === 0) return next;

  const last = next[next.length - 1];
  if (last && last.type === 'tools') {
    next[next.length - 1] = { type: 'tools', actions: [...last.actions, ...added] };
  } else {
    next.push({ type: 'tools', actions: added });
  }
  return next;
}

/** Legacy history: all tools, then the concatenated reply. */
export function fallbackTimeline(
  content: string,
  actions: ExecutedAction[] | undefined,
): TurnBlock[] {
  const blocks: TurnBlock[] = [];
  if (actions && actions.length > 0) {
    blocks.push({ type: 'tools', actions });
  }
  if (content) {
    blocks.push({ type: 'text', content });
  }
  return blocks;
}

export function parseTurnBlocks(raw: unknown): TurnBlock[] | null {
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const blocks: TurnBlock[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') return null;
    const rec = item as Record<string, unknown>;
    if (rec.type === 'text' || rec.type === 'reasoning') {
      if (typeof rec.content !== 'string') return null;
      blocks.push({ type: rec.type, content: rec.content });
      continue;
    }
    if (rec.type === 'tools') {
      if (!Array.isArray(rec.actions)) return null;
      blocks.push({ type: 'tools', actions: rec.actions as ExecutedAction[] });
      continue;
    }
    return null;
  }
  return blocks;
}

/**
 * Trailing text is the final summary. Reasoning, tools, and any narration
 * that happened before that last answer stay in the foldable process group
 * (Codex / DeepSeek turn-process).
 */
export function splitTurnProcess(blocks: TurnBlock[]): {
  process: TurnBlock[];
  answer: Extract<TurnBlock, { type: 'text' }>[];
} {
  let split = blocks.length;
  while (split > 0 && blocks[split - 1].type === 'text') split -= 1;
  return {
    process: blocks.slice(0, split),
    answer: blocks.slice(split) as Extract<TurnBlock, { type: 'text' }>[],
  };
}

export function countProcessTools(process: TurnBlock[]): number {
  let n = 0;
  for (const block of process) {
    if (block.type === 'tools') n += block.actions.length;
  }
  return n;
}

export function processHasReasoning(process: TurnBlock[]): boolean {
  return process.some((block) => block.type === 'reasoning' && block.content.trim().length > 0);
}

/**
 * Call-order display blocks persisted on assistant message metadata.
 * Keep this JSON compatible with apps/web `turn-timeline.ts`.
 */

export type PersistedTurnBlock =
  | { type: 'reasoning'; content: string; sealed?: boolean }
  | { type: 'text'; content: string; sealed?: boolean }
  | { type: 'tools'; actions: Array<Record<string, unknown>> };

export function appendTimelineDelta(
  blocks: PersistedTurnBlock[],
  type: 'text' | 'reasoning',
  delta: string,
): void {
  if (!delta) return;
  const last = blocks[blocks.length - 1];
  if (last && last.type === type && !last.sealed) {
    last.content += delta;
    return;
  }
  blocks.push({ type, content: delta });
}

/**
 * Close the current reasoning/text segment so the next same-kind delta
 * starts a new block. Mirrors apps/web `sealLastBlock`.
 */
export function sealLastTimelineBlock(blocks: PersistedTurnBlock[]): void {
  const last = blocks[blocks.length - 1];
  if (!last || last.type === 'tools' || last.sealed) return;
  last.sealed = true;
}

export function syncTimelineTools(
  blocks: PersistedTurnBlock[],
  actions: Array<Record<string, unknown>>,
): void {
  let placed = 0;
  for (const block of blocks) {
    if (block.type === 'tools') placed += block.actions.length;
  }

  let idx = 0;
  for (const block of blocks) {
    if (block.type !== 'tools') continue;
    for (let i = 0; i < block.actions.length; i += 1) {
      if (idx < actions.length) {
        block.actions[i] = actions[idx];
        idx += 1;
      }
    }
  }

  const added = actions.slice(placed);
  if (added.length === 0) return;
  const last = blocks[blocks.length - 1];
  if (last && last.type === 'tools') {
    last.actions.push(...added);
  } else {
    blocks.push({ type: 'tools', actions: added });
  }
}

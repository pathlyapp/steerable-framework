/**
 * Pure helper for the `/branches` routes in `router.ts` (W1.2.1).
 *
 * Branch activation is fail-closed: the target record must belong to the
 * chat's current branch family — the full tree rooted at the family's
 * first record, so cousins and deeper descendants are switchable, not
 * just the active record's lineage and direct children (the session-tree
 * view exposes exactly this set). Activating an unrelated record would
 * re-project the chat onto a conversation the user never saw in this
 * chat, so the route rejects it rather than trusting the caller. This
 * module only computes membership; storage and sidecar I/O stay in
 * `router.ts` (better-sqlite3 can't load under plain vitest).
 */

export interface BranchTreeNodeLike {
  recordId: string;
  children?: BranchTreeNodeLike[];
}

export type BranchActivation =
  | { ok: true }
  | { ok: false; reason: 'not_in_family' };

export function resolveBranchActivation(
  activeRecordId: string,
  tree: BranchTreeNodeLike | null,
  targetRecordId: string,
): BranchActivation {
  if (targetRecordId === activeRecordId) return { ok: true };
  if (tree && treeContains(tree, targetRecordId)) return { ok: true };
  return { ok: false, reason: 'not_in_family' };
}

/** DFS membership check. Family trees are depth-capped at 32 on the
 *  sidecar, so recursion stays shallow. */
function treeContains(node: BranchTreeNodeLike, targetRecordId: string): boolean {
  if (node.recordId === targetRecordId) return true;
  return (node.children ?? []).some((child) => treeContains(child, targetRecordId));
}

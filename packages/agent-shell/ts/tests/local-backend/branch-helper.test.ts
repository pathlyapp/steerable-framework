import { describe, expect, it } from 'vitest';
import {
  resolveBranchActivation,
  type BranchTreeNodeLike,
} from '../../src/local-backend/branch-helper.js';

describe('resolveBranchActivation (W1.2.1 + session tree)', () => {
  // chat_1 ─┬─ a ── c
  //         └─ b
  const tree: BranchTreeNodeLike = {
    recordId: 'chat_1',
    children: [
      { recordId: 'a', children: [{ recordId: 'c', children: [] }] },
      { recordId: 'b', children: [] },
    ],
  };

  it('allows the active record itself (no-op switch)', () => {
    expect(resolveBranchActivation('a', tree, 'a')).toEqual({ ok: true });
  });

  it('allows lineage ancestors and direct children', () => {
    expect(resolveBranchActivation('a', tree, 'chat_1')).toEqual({ ok: true });
    expect(resolveBranchActivation('a', tree, 'c')).toEqual({ ok: true });
  });

  it('allows cousins and deeper descendants anywhere in the family tree', () => {
    // The session-tree relaxation: a → b (uncle) and b → c (niece) are
    // single-hop activations now.
    expect(resolveBranchActivation('a', tree, 'b')).toEqual({ ok: true });
    expect(resolveBranchActivation('b', tree, 'c')).toEqual({ ok: true });
  });

  it('rejects records outside the branch family (fail-closed)', () => {
    expect(resolveBranchActivation('a', tree, 'rec_other_chat')).toEqual({
      ok: false,
      reason: 'not_in_family',
    });
  });

  it('rejects everything but the active record when the tree is missing (sidecar off)', () => {
    expect(resolveBranchActivation('chat_1', null, 'chat_1')).toEqual({ ok: true });
    expect(resolveBranchActivation('chat_1', null, 'rec_x')).toEqual({
      ok: false,
      reason: 'not_in_family',
    });
  });
});

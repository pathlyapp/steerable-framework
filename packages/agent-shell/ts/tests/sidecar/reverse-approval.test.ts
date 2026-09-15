import { describe, expect, it, vi } from 'vitest';
import {
  APPROVAL_DECISION_KINDS,
  createApprovalBridge,
  type ApprovalPromptRequest,
} from '../../src/sidecar/reverse-approval.js';

function makeBridge(overrides: {
  hasWindow?: boolean;
  broadcast?: (channel: 'approval:request', payload: ApprovalPromptRequest) => void;
}) {
  const sent: ApprovalPromptRequest[] = [];
  const bridge = createApprovalBridge({
    hasWindow: () => overrides.hasWindow ?? true,
    broadcast:
      overrides.broadcast ??
      ((_channel, payload) => {
        sent.push(payload);
      }),
  });
  return { bridge, sent };
}

describe('createApprovalBridge', () => {
  it('prompts the renderer and resolves with its decision', async () => {
    const { bridge, sent } = makeBridge({});

    const pending = bridge.handler({
      toolName: 'local_exec_shell',
      arguments: { command: 'rm -rf build' },
      mode: 'destructive',
      category: 'local_exec_shell',
      round: 2,
    });

    expect(sent).toHaveLength(1);
    expect(sent[0].toolName).toBe('local_exec_shell');
    expect(sent[0].mode).toBe('destructive');
    expect(sent[0].requestId).toBeTruthy();

    const ack = bridge.decide({
      requestId: sent[0].requestId,
      kind: 'allow_for_session',
      reason: 'user clicked',
    });
    expect(ack).toEqual({ ok: true });

    await expect(pending).resolves.toEqual({
      kind: 'allow_for_session',
      reason: 'user clicked',
    });
  });

  it('fails closed (deny_once) when no renderer window exists', async () => {
    const { bridge, sent } = makeBridge({ hasWindow: false });
    const decision = (await bridge.handler({
      toolName: 'local_write_file',
      arguments: {},
      mode: 'safe_write',
      category: 'local_write_file',
      round: 0,
    })) as { kind: string };
    expect(decision.kind).toBe('deny_once');
    expect(sent).toHaveLength(0);
  });

  it('fails closed on a malformed request (no toolName)', async () => {
    const { bridge } = makeBridge({});
    const decision = (await bridge.handler({})) as { kind: string };
    expect(decision.kind).toBe('deny_once');
  });

  it('an invalid decision kind degrades to deny_once', async () => {
    const { bridge, sent } = makeBridge({});
    const pending = bridge.handler({
      toolName: 'local_exec_shell',
      arguments: { command: 'ls' },
      mode: 'destructive',
      category: 'local_exec_shell',
      round: 0,
    });
    const ack = bridge.decide({ requestId: sent[0].requestId, kind: 'allow_forever' });
    expect(ack).toEqual({ ok: true });
    const decision = (await pending) as { kind: string; reason: string };
    expect(decision.kind).toBe('deny_once');
    expect(decision.reason).toContain('invalid');
  });

  it('drops decisions for unknown requestIds (late/stale answers)', async () => {
    const { bridge } = makeBridge({});
    expect(bridge.decide({ requestId: 'nope', kind: 'allow_once' })).toEqual({ ok: false });
  });

  it('serializes concurrent prompts FIFO', async () => {
    const { bridge, sent } = makeBridge({});
    const first = bridge.handler({ toolName: 'a', arguments: {}, mode: 'read', category: 'a', round: 0 });
    const second = bridge.handler({ toolName: 'b', arguments: {}, mode: 'read', category: 'b', round: 0 });
    expect(sent).toHaveLength(2);
    bridge.decide({ requestId: sent[0].requestId, kind: 'allow_once' });
    bridge.decide({ requestId: sent[1].requestId, kind: 'deny_once' });
    await expect(first).resolves.toMatchObject({ kind: 'allow_once' });
    await expect(second).resolves.toMatchObject({ kind: 'deny_once' });
  });

  it('covers exactly the 7 UI-decidable variants', () => {
    expect(APPROVAL_DECISION_KINDS).toEqual([
      'allow_once',
      'allow_for_session',
      'allow_always',
      'deny_once',
      'deny_for_session',
      'deny_always',
      'abort',
    ]);
  });
});

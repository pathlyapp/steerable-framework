/**
 * W4-1 reverse channel: serve the sidecar-hosted CoreLoop's
 * `approval.request` calls by asking the desktop user (Electron renderer
 * modal) and returning the 7-variant decision.
 *
 * Fail-closed by construction: no window, no listener, a renderer error,
 * or an invalid reply all become `deny_once` — never a hang, never an
 * auto-allow. The sidecar additionally bounds the wait (`timeoutMs` on the
 * approval config), so a wedged renderer degrades to `timed_out` there.
 *
 * The renderer answers via the `approval:decide` invoke with the requestId
 * echoed back; decisions are validated against the algebra's kind set
 * before crossing back to the sidecar.
 */

import { randomUUID } from 'node:crypto';
import type { SidecarReverseHandler } from './types.js';

/**
 * The approval algebra's decision variants (mirrors
 * `steerable_agent_runtime.approval.APPROVAL_KINDS`). `timed_out` is
 * synthesized by the sidecar on timeout, never by the UI.
 */
export const APPROVAL_DECISION_KINDS = [
  'allow_once',
  'allow_for_session',
  'allow_always',
  'deny_once',
  'deny_for_session',
  'deny_always',
  'abort',
] as const;

export type ApprovalDecisionKind = (typeof APPROVAL_DECISION_KINDS)[number];

export interface ApprovalPromptRequest {
  /** Correlation id echoed back by the renderer's `approval:decide`. */
  requestId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  mode: string;
  category: string;
  round: number;
}

interface PendingApproval {
  resolve: (decision: { kind: ApprovalDecisionKind; reason: string }) => void;
}

export interface ApprovalBridgeDeps {
  /**
   * Push the prompt to all renderer windows (main.ts wires
   * `webContents.send('approval:request', …)`). Returning false/undefined
   * means no window was available → fail closed.
   */
  broadcast: (channel: 'approval:request', payload: ApprovalPromptRequest) => void;
  /** True when at least one renderer window can receive the prompt. */
  hasWindow: () => boolean;
  onLog?: (line: string) => void;
}

export interface ApprovalBridge {
  /** Reverse-channel handler for the sidecar's `approval.request`. */
  handler: SidecarReverseHandler;
  /**
   * IPC entry point for the renderer's answer (`ipcMain.handle(
   * 'approval:decide')`). Unknown requestIds are dropped (late answer to an
   * already-settled prompt).
   */
  decide: (payload: unknown) => { ok: boolean };
}

export function createApprovalBridge(deps: ApprovalBridgeDeps): ApprovalBridge {
  const pending = new Map<string, PendingApproval>();

  const deny = (reason: string) => ({ kind: 'deny_once' as const, reason });

  return {
    handler: async (params) => {
      const p = (params ?? {}) as Partial<ApprovalPromptRequest>;
      const toolName = typeof p.toolName === 'string' ? p.toolName : '';
      if (!toolName) {
        return { kind: 'deny_once', reason: 'malformed approval request (no toolName)' };
      }
      if (!deps.hasWindow()) {
        deps.onLog?.(`approval: no renderer window; denying ${toolName} once`);
        return deny('no renderer window available to approve the call');
      }
      const requestId = randomUUID();
      const prompt: ApprovalPromptRequest = {
        requestId,
        toolName,
        arguments:
          p.arguments && typeof p.arguments === 'object'
            ? (p.arguments as Record<string, unknown>)
            : {},
        mode: typeof p.mode === 'string' ? p.mode : 'other',
        category: typeof p.category === 'string' ? p.category : toolName,
        round: typeof p.round === 'number' ? p.round : 0,
      };
      deps.onLog?.(`approval: prompting for ${toolName} (${prompt.mode}/${prompt.category})`);
      return await new Promise((resolve) => {
        pending.set(requestId, { resolve });
        try {
          deps.broadcast('approval:request', prompt);
        } catch (err) {
          pending.delete(requestId);
          resolve(deny(`approval prompt failed: ${err instanceof Error ? err.message : String(err)}`));
        }
      });
    },

    decide: (payload) => {
      const p = (payload ?? {}) as {
        requestId?: unknown;
        kind?: unknown;
        reason?: unknown;
      };
      const requestId = typeof p.requestId === 'string' ? p.requestId : '';
      const entry = pending.get(requestId);
      if (!entry) return { ok: false };
      pending.delete(requestId);
      const kind = APPROVAL_DECISION_KINDS.find((k) => k === p.kind);
      if (!kind) {
        entry.resolve(deny(`renderer returned an invalid decision kind: ${String(p.kind)}`));
        return { ok: true };
      }
      entry.resolve({
        kind,
        reason: typeof p.reason === 'string' ? p.reason : '',
      });
      return { ok: true };
    },
  };
}

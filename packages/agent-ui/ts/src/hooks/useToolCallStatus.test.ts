/**
 * Tests for `useToolCallStatus`.
 *
 * The status hook combines two axes — lifecycle (pending/running/done/error)
 * and mode (read/safe_write/destructive/local/…) — and exposes the booleans
 * (`isDestructive`, `requiresApproval`) the renderer cares about. We verify
 * both the explicit `mode` override path and the name-pattern fallback that
 * mirrors the framework's `decide_tool_mode`.
 */

import { renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useToolCallStatus } from './useToolCallStatus';

const baseCall = { id: 'c1', name: 'noop', arguments: {} };

describe('useToolCallStatus', () => {
  it('returns pending when no result has arrived', () => {
    const { result } = renderHook(() =>
      useToolCallStatus({ call: { ...baseCall } }),
    );
    expect(result.current.status).toBe('pending');
    expect(result.current.mode).toBe('unknown');
  });

  it('returns running when runningHint=true and no result yet', () => {
    const { result } = renderHook(() =>
      useToolCallStatus({ call: { ...baseCall }, runningHint: true }),
    );
    expect(result.current.status).toBe('running');
  });

  it('returns done when result.success=true', () => {
    const { result } = renderHook(() =>
      useToolCallStatus({
        call: { ...baseCall },
        result: { success: true },
      }),
    );
    expect(result.current.status).toBe('done');
  });

  it('returns error when result.success=false', () => {
    const { result } = renderHook(() =>
      useToolCallStatus({
        call: { ...baseCall },
        result: { success: false, error: 'nope' },
      }),
    );
    expect(result.current.status).toBe('error');
  });

  it.each([
    ['get_user', 'read'],
    ['list_files', 'read'],
    ['search_messages', 'read'],
    ['create_event', 'safe_write'],
    ['update_task', 'safe_write'],
    ['delete_chat', 'destructive'],
    ['archive_project', 'destructive'],
    ['local_run_script', 'local'],
    ['shell_exec', 'local'],
    ['something_unrecognised', 'unknown'],
  ])('infers mode for %s -> %s', (name, expected) => {
    const { result } = renderHook(() =>
      useToolCallStatus({ call: { id: 'x', name, arguments: {} } }),
    );
    expect(result.current.mode).toBe(expected);
  });

  it('honours an explicit mode override even if the name suggests something else', () => {
    const { result } = renderHook(() =>
      useToolCallStatus({
        call: { id: 'x', name: 'delete_everything', arguments: {} },
        mode: 'safe_write',
      }),
    );
    expect(result.current.mode).toBe('safe_write');
    expect(result.current.isDestructive).toBe(false);
  });

  it('marks local-mode calls as requiring approval and destructive calls as destructive', () => {
    const local = renderHook(() =>
      useToolCallStatus({
        call: { id: 'x', name: 'local_write_file', arguments: {} },
      }),
    );
    expect(local.result.current.requiresApproval).toBe(true);

    const destr = renderHook(() =>
      useToolCallStatus({
        call: { id: 'x', name: 'delete_event', arguments: {} },
      }),
    );
    expect(destr.result.current.isDestructive).toBe(true);
    // Destructive does not auto-flip requiresApproval; that's a separate decision.
    expect(destr.result.current.requiresApproval).toBe(false);
  });
});

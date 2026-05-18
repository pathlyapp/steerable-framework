/**
 * Tests for `useChatComposer`. The hook is intentionally tiny so the suite is
 * focused — keyboard semantics, the streaming guard, and the trim/clear
 * contract are what consumers actually pin behaviour against.
 */

import { describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useChatComposer } from './useChatComposer';

function makeKey(key: string, extras: Partial<KeyboardEvent> = {}) {
  // Construct a minimal React keyboard event matching what the hook uses.
  return {
    key,
    shiftKey: extras.shiftKey ?? false,
    metaKey: extras.metaKey ?? false,
    ctrlKey: extras.ctrlKey ?? false,
    preventDefault: vi.fn(),
  } as unknown as React.KeyboardEvent<HTMLElement>;
}

describe('useChatComposer', () => {
  it('submits the trimmed value and clears the draft', async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() =>
      useChatComposer({ onSend, initialValue: '  hello  ' }),
    );

    await act(async () => {
      await result.current.submit();
    });

    expect(onSend).toHaveBeenCalledWith('hello');
    expect(result.current.value).toBe('');
  });

  it('does not submit empty / whitespace-only drafts', async () => {
    const onSend = vi.fn();
    const { result } = renderHook(() =>
      useChatComposer({ onSend, initialValue: '   ' }),
    );

    await act(async () => {
      await result.current.submit();
    });

    expect(onSend).not.toHaveBeenCalled();
  });

  it('Enter submits, Shift+Enter inserts a newline (default)', async () => {
    const onSend = vi.fn();
    const { result } = renderHook(() => useChatComposer({ onSend, initialValue: 'hi' }));

    const submit = makeKey('Enter');
    await act(async () => {
      result.current.handleKeyDown(submit);
    });
    expect(submit.preventDefault).toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith('hi');

    const shift = makeKey('Enter', { shiftKey: true });
    await act(async () => {
      result.current.handleKeyDown(shift);
    });
    expect(shift.preventDefault).not.toHaveBeenCalled();
  });

  it('isStreaming flips Enter into a cancel', async () => {
    const onSend = vi.fn();
    const onCancel = vi.fn();
    const { result } = renderHook(() =>
      useChatComposer({ onSend, onCancel, isStreaming: true, initialValue: 'x' }),
    );

    const ev = makeKey('Enter');
    await act(async () => {
      result.current.handleKeyDown(ev);
    });
    expect(onCancel).toHaveBeenCalledOnce();
    expect(onSend).not.toHaveBeenCalled();
  });

  it('Escape cancels while streaming', async () => {
    const onCancel = vi.fn();
    const { result } = renderHook(() =>
      useChatComposer({ onSend: vi.fn(), onCancel, isStreaming: true }),
    );
    const ev = makeKey('Escape');
    await act(async () => {
      result.current.handleKeyDown(ev);
    });
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it('enterToSubmit=false requires Cmd/Ctrl+Enter', async () => {
    const onSend = vi.fn();
    const { result } = renderHook(() =>
      useChatComposer({ onSend, enterToSubmit: false, initialValue: 'hi' }),
    );

    const plain = makeKey('Enter');
    await act(async () => {
      result.current.handleKeyDown(plain);
    });
    expect(plain.preventDefault).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();

    const cmd = makeKey('Enter', { metaKey: true });
    await act(async () => {
      result.current.handleKeyDown(cmd);
    });
    expect(cmd.preventDefault).toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith('hi');
  });

  it('disabled state suppresses both submit and key handling', async () => {
    const onSend = vi.fn();
    const { result } = renderHook(() =>
      useChatComposer({ onSend, disabled: true, initialValue: 'hi' }),
    );

    await act(async () => {
      await result.current.submit();
    });
    const ev = makeKey('Enter');
    await act(async () => {
      result.current.handleKeyDown(ev);
    });
    expect(onSend).not.toHaveBeenCalled();
    expect(ev.preventDefault).not.toHaveBeenCalled();
    expect(result.current.canSubmit).toBe(false);
  });

  it('clear() empties the draft without sending', async () => {
    const onSend = vi.fn();
    const { result } = renderHook(() =>
      useChatComposer({ onSend, initialValue: 'hi' }),
    );
    await act(async () => {
      result.current.clear();
    });
    expect(result.current.value).toBe('');
    expect(onSend).not.toHaveBeenCalled();
  });
});

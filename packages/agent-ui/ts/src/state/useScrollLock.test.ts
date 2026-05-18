/**
 * Tests for `useScrollLock`. happy-dom doesn't compute scroll distance
 * automatically, so we manually mutate `scrollTop` and dispatch scroll events.
 */

import { describe, expect, it } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useScrollLock } from './useScrollLock';

function makeScrollContainer(
  options: { startAtBottom?: boolean } = {},
): HTMLDivElement {
  const el = document.createElement('div');
  Object.defineProperty(el, 'scrollHeight', { value: 1000, writable: true });
  Object.defineProperty(el, 'clientHeight', { value: 400, writable: true });
  // happy-dom doesn't implement scrollTo; emulate the clamping browsers do.
  el.scrollTo = ((opts: ScrollToOptions) => {
    if (typeof opts === 'object' && typeof opts.top === 'number') {
      const max = el.scrollHeight - el.clientHeight;
      el.scrollTop = Math.min(Math.max(0, opts.top), max);
    }
  }) as typeof el.scrollTo;
  if (options.startAtBottom !== false) {
    el.scrollTop = el.scrollHeight - el.clientHeight;
  }
  return el;
}

describe('useScrollLock', () => {
  it('auto-scrolls to bottom when revalidate keys change and user is anchored', () => {
    let keys: ReadonlyArray<unknown> = [0];
    const { result, rerender } = renderHook(() =>
      useScrollLock({ revalidateKeys: keys }),
    );
    const el = makeScrollContainer();
    act(() => result.current.setRef(el));
    expect(result.current.isAnchored).toBe(true);

    keys = [1];
    rerender();
    expect(el.scrollTop).toBe(el.scrollHeight - el.clientHeight);
  });

  it('releases the lock when the user scrolls up past the threshold', () => {
    const { result } = renderHook(() => useScrollLock({ threshold: 24 }));
    const el = makeScrollContainer();
    act(() => result.current.setRef(el));
    expect(result.current.isAnchored).toBe(true);

    el.scrollTop = 0;
    act(() => {
      el.dispatchEvent(new Event('scroll'));
    });
    expect(result.current.isAnchored).toBe(false);
  });

  it('re-anchors once the user scrolls back to the bottom', () => {
    const { result } = renderHook(() => useScrollLock({ threshold: 24 }));
    const el = makeScrollContainer();
    act(() => result.current.setRef(el));

    el.scrollTop = 0;
    act(() => el.dispatchEvent(new Event('scroll')));
    expect(result.current.isAnchored).toBe(false);

    el.scrollTop = el.scrollHeight - el.clientHeight;
    act(() => el.dispatchEvent(new Event('scroll')));
    expect(result.current.isAnchored).toBe(true);
  });

  it('scrollToBottom is callable imperatively', () => {
    const { result } = renderHook(() => useScrollLock());
    const el = makeScrollContainer({ startAtBottom: false });
    act(() => result.current.setRef(el));
    expect(el.scrollTop).toBe(0);

    act(() => result.current.scrollToBottom());

    expect(el.scrollTop).toBe(el.scrollHeight - el.clientHeight);
  });

  it('does not auto-scroll when the user is unanchored', () => {
    let keys: ReadonlyArray<unknown> = [0];
    const { result, rerender } = renderHook(() =>
      useScrollLock({ revalidateKeys: keys, threshold: 24 }),
    );
    const el = makeScrollContainer();
    act(() => result.current.setRef(el));

    el.scrollTop = 0;
    act(() => el.dispatchEvent(new Event('scroll')));
    expect(result.current.isAnchored).toBe(false);

    keys = [1];
    rerender();
    // Lock released → no auto-scroll, scrollTop stays at user's position.
    expect(el.scrollTop).toBe(0);
  });
});

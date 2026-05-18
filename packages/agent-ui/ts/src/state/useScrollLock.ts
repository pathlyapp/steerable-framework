/**
 * `useScrollLock` — sticky-bottom scrolling for streaming message lists.
 *
 * The hook returns a callback ref to attach to the scroll container and a
 * `scrollToBottom` imperative API. Behaviour:
 *   - When the container's scroll position is within `threshold` px of the
 *     bottom, the hook treats the user as "anchored" and re-runs
 *     `scrollToBottom` whenever the dependency array `revalidateKeys` changes.
 *   - As soon as the user scrolls up past `threshold`, the lock releases —
 *     the list will no longer auto-scroll until the user scrolls back to the
 *     bottom (or `scrollToBottom` is called explicitly, e.g. on send).
 *
 * Uses a callback ref so the listener attaches the moment the element mounts,
 * not after a second effect pass. Matches deeppath's MessageList scroll
 * handling minus the inline MutationObserver+IntersectionObserver soup.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export interface UseScrollLockOptions {
  /** Px from the bottom that still counts as "anchored". Default 24. */
  threshold?: number;
  /**
   * Dependencies that, when changed, will trigger an auto-scroll if the user
   * is currently anchored. Pass e.g. `[messages.length, lastMessageContent]`.
   */
  revalidateKeys?: ReadonlyArray<unknown>;
  /** Scroll smooth vs jump. Defaults to 'auto' (instant). */
  behavior?: ScrollBehavior;
}

export interface UseScrollLockReturn<E extends HTMLElement = HTMLDivElement> {
  /** Callback ref — pass directly to `ref={...}` on the scroll container. */
  setRef: (el: E | null) => void;
  isAnchored: boolean;
  scrollToBottom: (behavior?: ScrollBehavior) => void;
}

function maxScrollTop(el: HTMLElement): number {
  return Math.max(0, el.scrollHeight - el.clientHeight);
}

export function useScrollLock<E extends HTMLElement = HTMLDivElement>(
  options: UseScrollLockOptions = {},
): UseScrollLockReturn<E> {
  const threshold = options.threshold ?? 24;
  const elRef = useRef<E | null>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  const [isAnchored, setAnchored] = useState(true);
  const anchoredRef = useRef(true);
  anchoredRef.current = isAnchored;

  const scrollToBottom = useCallback(
    (behavior?: ScrollBehavior) => {
      const el = elRef.current;
      if (!el) return;
      el.scrollTo({
        top: maxScrollTop(el),
        behavior: behavior ?? options.behavior ?? 'auto',
      });
    },
    [options.behavior],
  );

  const setRef = useCallback(
    (el: E | null) => {
      if (cleanupRef.current) {
        cleanupRef.current();
        cleanupRef.current = null;
      }
      elRef.current = el;
      if (!el) return;
      const handler = () => {
        const distance = maxScrollTop(el) - el.scrollTop;
        const anchored = distance <= threshold;
        if (anchored !== anchoredRef.current) {
          anchoredRef.current = anchored;
          setAnchored(anchored);
        }
      };
      el.addEventListener('scroll', handler, { passive: true });
      cleanupRef.current = () => el.removeEventListener('scroll', handler);
      // Run once to seed `isAnchored` from the current position.
      handler();
    },
    [threshold],
  );

  useEffect(() => {
    return () => {
      if (cleanupRef.current) {
        cleanupRef.current();
        cleanupRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (anchoredRef.current) scrollToBottom();
    // Consumer-provided dependency array + the scroll callback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, options.revalidateKeys ?? []);

  return { setRef, isAnchored, scrollToBottom };
}

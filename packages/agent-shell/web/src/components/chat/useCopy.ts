import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * `useCopy(text)` — copy-button helper for message bubbles.
 *
 * Returns `{ copied, copy }`. `copy()` writes `text` to clipboard and flips
 * `copied` to true for `resetMs` milliseconds, then back to false so the
 * button can revert to its idle icon.
 *
 * Why not just inline this in each component:
 *   - UserMessage + AssistantMessage + (eventually) tool-result cards all
 *     need the same "copied / not copied" state machine + clipboard
 *     fallback. Factoring keeps the timer-clearing logic in one place.
 *   - `navigator.clipboard.writeText` rejects on insecure origins or
 *     unsupported browsers (older Electron); we fall back to a hidden
 *     textarea + `document.execCommand('copy')` so the chat stays
 *     interactive even when permissions are odd.
 */
export interface UseCopyResult {
  /** True for `resetMs` after a successful copy. */
  copied: boolean;
  /** Trigger a clipboard write. Idempotent if `copied` is already true. */
  copy: () => Promise<void>;
}

export function useCopy(text: string, resetMs = 1500): UseCopyResult {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const copy = useCallback(async () => {
    if (!text) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for older Electron / non-secure contexts.
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      setCopied(true);
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setCopied(false), resetMs);
    } catch {
      // Swallow; user can still highlight + ⌘C the bubble manually.
    }
  }, [text, resetMs]);

  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );

  return { copied, copy };
}

/**
 * Version-agnostic helpers shared by the cards.
 */
import type * as React from 'react';

/**
 * Return type for `renderMarkdown` / `renderLabel` / etc. slots.
 *
 * We deliberately accept `unknown` so the prop type doesn't anchor consumers
 * to a specific `@types/react` major (`React.ReactNode` is nominally
 * different between v18 and v19 even though structurally compatible -- a
 * consumer on React 19 cannot pass a function declared to return
 * `React.ReactNode` (v19) into a framework prop that wants v18's
 * `React.ReactNode`). The card then casts back to `React.ReactNode` at the
 * render site, where TypeScript only cares about runtime shape and `unknown`
 * widens harmlessly.
 *
 * For consumers, this just means: "return any valid React child".
 */
export type RenderSlotResult = unknown;

/**
 * Cast helper for use inside cards. Keeps render sites tidy.
 */
export function asReactNode(value: RenderSlotResult): React.ReactNode {
  return value as React.ReactNode;
}

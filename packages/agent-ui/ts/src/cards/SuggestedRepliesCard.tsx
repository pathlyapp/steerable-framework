/**
 * `<SuggestedRepliesCard />`
 *
 * Quick-reply chips rendered below the composer. The card is pure presentation:
 * the parent decides where to mount it and what `onSelect` does (typically:
 * push the chosen text into the composer and submit).
 */
import * as React from 'react';
import type { SuggestedRepliesPayload } from '@steerable/agent-protocol';

export interface SuggestedRepliesCardProps {
  payload: SuggestedRepliesPayload;
  onSelect: (text: string) => void;
  className?: string;
  buttonClassName?: string;
}

export const SuggestedRepliesCard: React.FC<SuggestedRepliesCardProps> = ({
  payload,
  onSelect,
  className,
  buttonClassName,
}) => {
  const suggestions = payload.suggestions ?? [];
  if (suggestions.length === 0) return null;

  return (
    <div className={['steerable-suggested-replies flex flex-wrap gap-2 py-2', className].filter(Boolean).join(' ')}>
      {suggestions.map((text, idx) => (
        <button
          key={`${idx}-${text}`}
          type="button"
          onClick={() => onSelect(text)}
          className={[
            'rounded-full border px-3 py-1.5 text-sm transition-colors duration-150',
            'border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#f9fafb)]',
            'text-[var(--agent-foreground,#111827)] hover:bg-[var(--agent-accent,#e5e7eb)]',
            buttonClassName,
          ].filter(Boolean).join(' ')}
        >
          {text}
        </button>
      ))}
    </div>
  );
};

export default SuggestedRepliesCard;

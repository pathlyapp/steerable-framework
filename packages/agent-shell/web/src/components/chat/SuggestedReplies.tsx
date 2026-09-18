/**
 * WorkBuddy-style follow-up chips: next-turn user inputs rendered under the
 * latest assistant reply. Count follows the turn (skill next steps), not a
 * fixed three. Clicking one sends that text immediately.
 */
export function SuggestedReplies({
  suggestions,
  onSelect,
}: {
  suggestions: string[];
  onSelect: (text: string) => void;
}) {
  if (suggestions.length === 0) return null;

  return (
    <div
      className="mx-auto flex w-full max-w-[var(--chat-input-box-width)] flex-col items-start gap-1.5 px-1 pt-1"
      data-testid="suggested-replies"
    >
      {suggestions.map((text) => (
        <button
          key={text}
          type="button"
          data-testid="suggested-reply"
          onClick={() => onSelect(text)}
          className="max-w-full rounded-full border border-agent-border bg-agent-muted/40 px-3 py-1.5 text-left text-[13px] leading-snug text-agent-foreground transition-colors hover:bg-agent-foreground/5"
        >
          {text}
        </button>
      ))}
    </div>
  );
}

export default SuggestedReplies;

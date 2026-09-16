/**
 * EmptyChat — empty-state component shown when a chat has no messages.
 * Currently returns null to remove suggestion prompts per user request.
 */

interface EmptyChatProps {
  /**
   * Called when the user clicks one of the prompt chips. The enclosing chat
   * panel should set its input value to `prompt` and move focus there.
   */
  onSelectPrompt: (prompt: string) => void;
}

export function EmptyChat(_props: EmptyChatProps) {
  return null;
}

export default EmptyChat;

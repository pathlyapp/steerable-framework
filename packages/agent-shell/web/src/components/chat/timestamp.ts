/**
 * Friendly timestamp formatter shared by `UserMessage` / `AssistantMessage`.
 * Today → `HH:MM`, other days → `MM-DD HH:MM`.
 *
 * Direct port of the helper from
 * `deeppath/apps/web/src/app/goals/desktop/components/ChatPanel/MessageList/UserMessage.tsx`.
 */
export function getFriendlyDate(date: Date): string {
  const now = new Date();
  const isToday = now.toDateString() === date.toDateString();
  if (isToday) {
    return date.toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    });
  }
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  const time = date.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
  return `${month}-${day} ${time}`;
}

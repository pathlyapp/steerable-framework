/**
 * Chat title parser — mirrors `deeppath/apps/web/src/types/chat.ts`'s
 * `parseChatTitle`. The local-backend re-uses the same `[自动化] ` prefix
 * convention as the cloud backend (deeppath-api/app/services/cron/executors/
 * automation.py:_AUTOMATION_TITLE_PREFIX), so the same parser works for both.
 */

export const AUTOMATION_TITLE_PREFIX = '[自动化] ';
const BARE_BRACKET_PREFIX = '[自动化]';

export interface ParsedChatTitle {
  displayTitle: string;
  isAutomation: boolean;
}

export function parseChatTitle(
  rawTitle: string | null | undefined,
): ParsedChatTitle {
  const title = rawTitle ?? '';
  if (title.startsWith(AUTOMATION_TITLE_PREFIX)) {
    return {
      displayTitle: title.slice(AUTOMATION_TITLE_PREFIX.length),
      isAutomation: true,
    };
  }
  if (title.startsWith(BARE_BRACKET_PREFIX)) {
    return {
      displayTitle: title.slice(BARE_BRACKET_PREFIX.length).trimStart(),
      isAutomation: true,
    };
  }
  return { displayTitle: title, isAutomation: false };
}

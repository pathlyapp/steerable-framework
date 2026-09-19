/**
 * 助手回复里的自定义 `[next_steps]...[/next_steps]` 标签：
 * 给追问建议当判断来源，气泡里只显示标签内的正文，不显示标签本身。
 */

const COMPLETE_BLOCK_RE = /\[next_steps\]([\s\S]*?)\[\/next_steps\]/gi;
const OPEN_TAG = 'next_steps]';
const CLOSE_TAG = '/next_steps]';

function isIncompleteTagSuffix(suffix: string): boolean {
  const body = suffix.slice(1).toLowerCase();
  if (!body) return false;
  return OPEN_TAG.startsWith(body) || CLOSE_TAG.startsWith(body);
}

function stripIncompleteTrailingTag(text: string): string {
  const idx = text.lastIndexOf('[');
  if (idx < 0) return text;
  const suffix = text.slice(idx);
  return isIncompleteTagSuffix(suffix) ? text.slice(0, idx) : text;
}

/** 去掉自定义标签，保留里面的建议正文。 */
export function stripNextStepsTags(text: string): string {
  let out = (text ?? '').replace(COMPLETE_BLOCK_RE, '$1');
  out = out.replace(/\[\/?next_steps\]/gi, '');
  return stripIncompleteTrailingTag(out);
}

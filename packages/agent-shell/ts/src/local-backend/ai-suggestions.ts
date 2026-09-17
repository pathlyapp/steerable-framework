/**
 * 回合结束后生成 3 条「下一轮用户输入」建议（WorkBuddy 式快捷追问）。
 *
 * 主路径走一次短 LLM 调用，根据本轮用户请求 + 助手回复写成用户口吻的短句。
 * 失败时用启发式兜底（PPT / 计划 / 代码 / 通用），永不抛错。
 *
 * 调用方应该 fire-and-forget：先把兜底三条推上 UI，LLM 成功后再替换。
 * 不要挂在 SSE 主流程里阻塞 `[DONE]`。
 */

import { llmService } from '../llm/index.js';

export const SUGGESTED_REPLY_COUNT = 3;
const MAX_SUGGESTION_CHARS = 36;
const USER_TEXT_LIMIT = 500;
const ASSISTANT_TEXT_LIMIT = 1600;

const SYSTEM_PROMPT = `你是对话追问建议助手。根据用户上一轮请求和助手刚刚完成的回复，生成 3 条用户可以点一下就发出去的下一轮输入。

要求：
1. 正好 3 条，彼此不重复
2. 每条 8-28 个字，用用户口吻，像用户会打的话
3. 紧扣刚刚完成的工作：改内容、改样式、继续深入、换方向；不要空泛的「再详细说说」
4. 如果助手已经邀请后续操作（例如「如需修改内容或调整样式」），把那些邀请写成具体可执行的短句
5. 不要编号、不要引号、不要解释
6. 只输出 JSON 字符串数组，例如 ["调整封面配色","把个人简介写得更具体","再加一页项目案例"]`;

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  if (timeoutMs <= 0) return promise;
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`${label} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

function clip(text: string, limit: number): string {
  const chars = Array.from((text ?? '').trim());
  if (chars.length <= limit) return chars.join('');
  return `${chars.slice(0, limit).join('')}…`;
}

/** 单条建议清洗：去编号/引号、压空白、截断。不合格返回空串。 */
export function cleanSuggestedReply(raw: string): string {
  let text = (raw ?? '').trim();
  text = text.replace(/^[\s"'“”‘’「」『』《》【】]+|[\s"'“”‘’「」『』《》【】。.！!?？，,；;:：]+$/g, '');
  text = text.replace(/^(?:\d+[\.\)、]|[-*•])\s*/, '');
  text = text.replace(/\s+/g, ' ').trim();
  if (text.length < 2) return '';
  const chars = Array.from(text);
  if (chars.length > MAX_SUGGESTION_CHARS) {
    text = chars.slice(0, MAX_SUGGESTION_CHARS).join('').trim();
  }
  return text;
}

function uniqueThree(candidates: string[], extras: string[] = []): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of [...candidates, ...extras]) {
    const cleaned = cleanSuggestedReply(raw);
    if (!cleaned || seen.has(cleaned)) continue;
    seen.add(cleaned);
    out.push(cleaned);
    if (out.length === SUGGESTED_REPLY_COUNT) break;
  }
  return out;
}

/**
 * 从模型原文抽出最多 3 条建议。接受 JSON 数组、markdown 代码块、或编号/项目列表。
 */
export function parseSuggestedReplies(raw: string): string[] {
  const trimmed = (raw ?? '').trim();
  if (!trimmed) return [];

  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidate = (fenced?.[1] ?? trimmed).trim();
  const start = candidate.indexOf('[');
  const end = candidate.lastIndexOf(']');
  if (start >= 0 && end > start) {
    try {
      const parsed = JSON.parse(candidate.slice(start, end + 1)) as unknown;
      if (Array.isArray(parsed)) {
        return uniqueThree(parsed.filter((item): item is string => typeof item === 'string'));
      }
    } catch {
      // 落到分行解析。
    }
  }

  const lines = candidate
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !/^```/.test(line));
  const listLines = lines.filter((line) => /^(?:\d+[\.\)、]|[-*•])\s+/.test(line));
  return uniqueThree(listLines.length > 0 ? listLines : lines);
}

const PPT_FALLBACK = ['调整封面标题和配色', '把某一页内容写得更具体', '再加一页项目案例'];
const PLAN_FALLBACK = ['按这个计划开始执行', '先改第 2 步再执行', '把计划写得更细一点'];
const CODE_FALLBACK = ['解释这段实现的思路', '帮我补上测试', '再优化一下可读性'];
const GENERIC_FALLBACK = ['继续完善这份结果', '换一种呈现方式', '告诉我下一步怎么做'];
const PPT_OFFER_FALLBACK = ['调整幻灯片的内容和文案', '调整配色和版式', '再加一页补充材料'];

/**
 * 不调模型的启发式三条。PPT / 计划 / 代码有专用句子，其它走通用。
 * 始终返回正好 3 条（输入全空时仍给通用三条）。
 */
export function fallbackSuggestedReplies(userText: string, assistantText: string): string[] {
  const blob = `${userText}\n${assistantText}`;
  if (/\.pptx\b|幻灯片|演示文稿|\bppt\b/i.test(blob)) {
    if (/修改内容|调整样式/.test(assistantText)) {
      return uniqueThree(PPT_OFFER_FALLBACK, PPT_FALLBACK);
    }
    return uniqueThree(PPT_FALLBACK, GENERIC_FALLBACK);
  }
  if (/标准工作流程|待办清单|\bTODO\b|先制定计划/.test(blob) || /^\s*计划已/.test(assistantText)) {
    return uniqueThree(PLAN_FALLBACK, GENERIC_FALLBACK);
  }
  if (/```|单元测试|函数实现|补测试/.test(blob)) {
    return uniqueThree(CODE_FALLBACK, GENERIC_FALLBACK);
  }
  return uniqueThree(GENERIC_FALLBACK);
}

export interface GenerateSuggestedRepliesResult {
  suggestions: string[];
  /** true = LLM 失败或输出不合格，suggestions 来自启发式兜底。 */
  usedFallback: boolean;
}

/**
 * 生成本轮 3 条追问建议。永不抛错，始终返回 3 条。
 */
export async function generateSuggestedReplies(
  userText: string,
  assistantText: string,
  opts: { perAttemptTimeoutMs?: number } = {},
): Promise<GenerateSuggestedRepliesResult> {
  const fallback = fallbackSuggestedReplies(userText, assistantText);
  const user = clip(userText, USER_TEXT_LIMIT);
  const assistant = clip(assistantText, ASSISTANT_TEXT_LIMIT);
  if (!user && !assistant) {
    return { suggestions: fallback, usedFallback: true };
  }

  const perAttemptTimeoutMs = opts.perAttemptTimeoutMs ?? 12_000;
  try {
    const result = await withTimeout(
      llmService.generate({
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          {
            role: 'user',
            content: `用户请求：\n${user || '（空）'}\n\n助手回复：\n${assistant || '（空）'}`,
          },
        ],
        temperature: 0.6,
      }),
      perAttemptTimeoutMs,
      'ai-suggestions',
    );
    const parsed = parseSuggestedReplies(result.content ?? '');
    const suggestions = uniqueThree(parsed, fallback);
    if (suggestions.length === SUGGESTED_REPLY_COUNT && parsed.length > 0) {
      return { suggestions, usedFallback: false };
    }
  } catch (err) {
    console.warn('[ai-suggestions] LLM 追问建议失败，用启发式兜底', err);
  }
  return { suggestions: fallback, usedFallback: true };
}

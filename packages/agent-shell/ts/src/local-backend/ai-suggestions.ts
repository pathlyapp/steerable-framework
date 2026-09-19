/**
 * 回合结束后生成「下一轮用户输入」建议。
 *
 * 只走一次短 LLM 调用。判断来源只有助手给用户的下一步：
 * `[next_steps]...[/next_steps]` 标签，或回复最后一段里的建议。
 * 不做规则抽取、不按 PPT/计划/代码套模板。永不抛错。
 *
 * 调用方应该 fire-and-forget，且只广播一次最终结果。
 * 不要挂在 SSE 主流程里阻塞 `[DONE]`。
 */

import { llmService } from '../llm/index.js';

export const SUGGESTED_REPLY_MAX = 8;
const MAX_SUGGESTION_CHARS = 48;
const USER_TEXT_LIMIT = 500;
const SOURCE_LIMIT = 2000;

const NEXT_STEPS_BLOCK_RE = /\[next_steps\]([\s\S]*?)\[\/next_steps\]/gi;

const SYSTEM_PROMPT = `你是对话追问建议助手。根据「下一步来源」判断用户点一下就能发出去的下一轮输入。

下一步来源只有两种：
- [next_steps]...[/next_steps] 标签里的内容
- 否则是助手回复的最后一段

要求：
1. 只把「用户可以接着做的下一步」改写成用户口吻的短指令。
2. 本轮已完成的汇报（文件位置、页数、设计风格、内容结构、摘要、目录、生平/作品列表）不是下一步。来源不是给用户的下一步时，输出空数组 []。
3. 不要编造，不要用「继续完善这份结果」「告诉我下一步怎么做」这类套话凑数。
4. 有几条真实下一步就给几条，最少 0 条，最多 8 条。
5. 每条 6-40 个字。不要编号、不要引号、不要解释、不要附带命令行或绝对路径。
6. 只输出 JSON 字符串数组，例如 ["把封面改成深蓝商务风","第2页个人简介写具体"]`;

const LIST_ITEM_RE = /^(?:\d+[.)、]|[-*•])\s+(\S.*)$/;

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

function lastParagraph(text: string): string {
  const trimmed = (text ?? '').trim();
  if (!trimmed) return '';
  const parts = trimmed.split(/\n\s*\n/);
  return (parts[parts.length - 1] ?? '').trim();
}

/**
 * 建议芯片的判断来源：有 `[next_steps]` 用最后一段标签正文，否则用回复最后一段。
 */
export function extractNextStepsSource(assistantText: string): string {
  const text = assistantText ?? '';
  const matches = [...text.matchAll(new RegExp(NEXT_STEPS_BLOCK_RE.source, 'gi'))];
  if (matches.length > 0) {
    return (matches[matches.length - 1][1] ?? '').trim();
  }
  return lastParagraph(text);
}

function isListItem(line: string): boolean {
  return LIST_ITEM_RE.test(line.trim());
}

/** 单条建议清洗：去编号/引号、压空白、截断。不合格返回空串。 */
export function cleanSuggestedReply(raw: string): string {
  let text = (raw ?? '').trim();
  text = text.replace(/`([^`]+)`/g, '$1');
  text = text.replace(/\s+[&|]\s+.+$/s, '');
  text = text.replace(/\s+[A-Za-z]:\\[^\s].*$/, '');
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

function uniqueSuggestions(candidates: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of candidates) {
    const cleaned = cleanSuggestedReply(raw);
    if (!cleaned || seen.has(cleaned)) continue;
    seen.add(cleaned);
    out.push(cleaned);
    if (out.length === SUGGESTED_REPLY_MAX) break;
  }
  return out;
}

function sliceJsonArray(raw: string): unknown | undefined {
  const trimmed = (raw ?? '').trim();
  if (!trimmed) return undefined;
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidate = (fenced?.[1] ?? trimmed).trim();
  const start = candidate.indexOf('[');
  const end = candidate.lastIndexOf(']');
  if (start < 0 || end <= start) return undefined;
  try {
    return JSON.parse(candidate.slice(start, end + 1)) as unknown;
  } catch {
    return undefined;
  }
}

/**
 * 从模型原文抽出建议。接受 JSON 数组、markdown 代码块、或编号/项目列表。
 * 最多 {@link SUGGESTED_REPLY_MAX} 条。
 */
export function parseSuggestedReplies(raw: string): string[] {
  const parsed = sliceJsonArray(raw);
  if (Array.isArray(parsed)) {
    return uniqueSuggestions(parsed.filter((item): item is string => typeof item === 'string'));
  }

  const trimmed = (raw ?? '').trim();
  if (!trimmed) return [];
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidate = (fenced?.[1] ?? trimmed).trim();
  const lines = candidate
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !/^```/.test(line));
  const listLines = lines.filter((line) => isListItem(line));
  return uniqueSuggestions(listLines.length > 0 ? listLines : lines);
}

export interface GenerateSuggestedRepliesResult {
  suggestions: string[];
  /** true = LLM 失败、超时或输出不合格；suggestions 为空。空数组本身算有效判断。 */
  usedFallback: boolean;
}

export interface GenerateSuggestedRepliesOptions {
  perAttemptTimeoutMs?: number;
}

/**
 * 生成本轮追问建议。永不抛错；没有下一步来源或模型判定没有下一步时返回空数组。
 */
export async function generateSuggestedReplies(
  userText: string,
  assistantText: string,
  opts: GenerateSuggestedRepliesOptions = {},
): Promise<GenerateSuggestedRepliesResult> {
  const source = extractNextStepsSource(assistantText);
  const user = clip(userText, USER_TEXT_LIMIT);
  const sourceClip = clip(source, SOURCE_LIMIT);
  if (!sourceClip) {
    return { suggestions: [], usedFallback: false };
  }

  const perAttemptTimeoutMs = opts.perAttemptTimeoutMs ?? 12_000;
  try {
    const result = await withTimeout(
      llmService.generate({
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          {
            role: 'user',
            content: `用户请求：\n${user || '（空）'}\n\n下一步来源：\n${sourceClip}`,
          },
        ],
        temperature: 0.4,
      }),
      perAttemptTimeoutMs,
      'ai-suggestions',
    );
    const raw = result.content ?? '';
    const parsed = parseSuggestedReplies(raw);
    if (parsed.length > 0) {
      return { suggestions: parsed, usedFallback: false };
    }
    if (Array.isArray(sliceJsonArray(raw))) {
      return { suggestions: [], usedFallback: false };
    }
    return { suggestions: [], usedFallback: true };
  } catch (err) {
    console.warn('[ai-suggestions] LLM 追问建议失败，不展示建议', err);
    return { suggestions: [], usedFallback: true };
  }
}

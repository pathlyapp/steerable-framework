/**
 * 回合结束后生成「下一轮用户输入」建议（WorkBuddy 式快捷追问）。
 *
 * 主路径走一次短 LLM 调用。优先消化用户/工作区技能和助手回复里已经写明的
 * 下一步（编号列表、脚本调用、推荐后续），条数跟真实下一步走，不凑 3 条。
 * 没有可执行下一步时，才用启发式兜底（PPT / 计划 / 代码 / 通用）。永不抛错。
 *
 * 调用方应该 fire-and-forget，且只广播一次最终结果。
 * 回复/技能里已经列出下一步时不要再调 LLM 改写，否则芯片会先出一版再换一版。
 * 不要挂在 SSE 主流程里阻塞 `[DONE]`。
 */

import { llmService } from '../llm/index.js';

export const SUGGESTED_REPLY_MAX = 8;
const MAX_SUGGESTION_CHARS = 48;
const USER_TEXT_LIMIT = 500;
const ASSISTANT_HEAD_LIMIT = 800;
const ASSISTANT_TAIL_LIMIT = 2000;
const SKILL_EXCERPT_LIMIT = 4000;

const SYSTEM_PROMPT = `你是对话追问建议助手。根据用户上一轮请求、助手刚刚完成的回复，以及用户技能里写明的下一步，生成用户点一下就能发出去的下一轮输入。

要求：
1. 条数不固定：有几条真实的下一步就给几条，最少 1 条，最多 8 条。不要为了凑数编造，也不要无故压成 3 条。
2. 优先使用「用户技能中的下一步」和助手回复末尾已经列出的后续动作（编号列表、脚本调用、推荐后续）。把它们改写成用户口吻的短指令，保留层位、对象、参数等关键信息，不要丢掉技能里的具体动作。
3. 助手本轮已经做完的步骤不要再建议；技能里写了但还没做的后续必须出现。
4. 每条 6-40 个字，像用户会打的话。不要空泛的「再详细说说」「告诉我下一步怎么做」。
5. 不要编号、不要引号、不要解释、不要附带命令行或绝对路径。
6. 只输出 JSON 字符串数组，例如 ["画接底层的井密度交会图","统计这口井的有效厚度","补一张气层厚度等值线"]`;

const NEXT_STEP_WORDS =
  '下一步(?:动作|操作|工作)?|后续(?:步骤|工作|动作|操作|可做)?|建议(?:的)?(?:下一步|继续|操作)?|可以继续|推荐(?:的)?(?:后续|操作|下一步)?|next\\s*steps?';

/** 整行即「下一步」类标题：可带 #/** 前缀、括号补充说明（如「后续动作（可点选）」）、结尾冒号。 */
const NEXT_STEP_HEADING_RE = new RegExp(
  `^(?:#{1,6}\\s*)?(?:\\*\\*)?(?:${NEXT_STEP_WORDS})(?:\\s*[（(][^）)]*[）)])?\\s*(?:\\*\\*)?\\s*[:：]?$`,
  'i',
);

/**
 * 短引导行，如「完成后可以继续：」。必须以关键词加冒号收尾且整行够短——
 * 否则「以上是我的建议」这类普通句尾会被当成标题，把后面无关的列表抽成建议。
 */
const NEXT_STEP_LEAD_IN_RE = new RegExp(`(?:${NEXT_STEP_WORDS})\\s*[:：]$`, 'i');
const NEXT_STEP_LEAD_IN_MAX_CHARS = 16;

// 列表标记后必须有空白，否则 PowerShell 参数（-Action）会被当成列表项。
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

function clipHeadTail(text: string, headLimit: number, tailLimit: number): string {
  const chars = Array.from((text ?? '').trim());
  if (chars.length <= headLimit + tailLimit) return chars.join('');
  return `${chars.slice(0, headLimit).join('')}…\n…${chars.slice(-tailLimit).join('')}`;
}

function isNextStepHeading(line: string): boolean {
  return NEXT_STEP_HEADING_RE.test(line.trim());
}

function isNextStepLeadIn(line: string): boolean {
  const trimmed = line.trim();
  if (isNextStepHeading(trimmed)) return true;
  return (
    Array.from(trimmed).length <= NEXT_STEP_LEAD_IN_MAX_CHARS &&
    NEXT_STEP_LEAD_IN_RE.test(trimmed)
  );
}

function isMarkdownHeading(line: string): boolean {
  return /^\s*#{1,6}\s+\S/.test(line);
}

/** 列表项正文；不是列表项返回 null。 */
function listItemBody(line: string): string | null {
  const matched = line.trim().match(LIST_ITEM_RE);
  return matched ? matched[1].trim() : null;
}

function isListItem(line: string): boolean {
  return listItemBody(line) !== null;
}

function hasScriptInvocation(text: string): boolean {
  return (
    /(?:^|\s)[&|]\s+/.test(text) ||
    /\.(?:ps1|py|js|mjs|sh|bat|cmd)\b/i.test(text) ||
    /\{scripts\}/i.test(text) ||
    /(?:^|[\s`])(?:powershell|pwsh)\b/i.test(text) ||
    /[A-Za-z]:\\/.test(text) ||
    /[/\\]skills[/\\]/.test(text)
  );
}

/** 去掉脚本调用、绝对路径，留下用户能点的短标题。 */
export function stripSkillInvocation(raw: string): string {
  let text = (raw ?? '').trim();
  text = text.replace(/`([^`]+)`/g, '$1');
  text = text.replace(/\s+[&|]\s+.+$/s, '');
  text = text.replace(/\s+[A-Za-z]:\\[^\s].*$/, '');
  text = text.replace(/\s+(?:powershell(?:\.exe)?|pwsh)\b.*$/i, '');
  text = text.replace(/\s+\{scripts\}.*$/i, '');
  return text.trim();
}

/** 单条建议清洗：去编号/引号、压空白、截断。不合格返回空串。 */
export function cleanSuggestedReply(raw: string): string {
  let text = stripSkillInvocation(raw ?? '');
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

function uniqueSuggestions(candidates: string[], extras: string[] = []): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of [...candidates, ...extras]) {
    const cleaned = cleanSuggestedReply(raw);
    if (!cleaned || seen.has(cleaned)) continue;
    seen.add(cleaned);
    out.push(cleaned);
    if (out.length === SUGGESTED_REPLY_MAX) break;
  }
  return out;
}

function collectListAfter(lines: string[], start: number): string[] {
  const items: string[] = [];
  for (let i = start; i < lines.length; i += 1) {
    const trimmed = lines[i].trim();
    // 列表项之间常有空行，空行不终止收集。
    if (!trimmed) continue;
    if (isMarkdownHeading(trimmed) || isNextStepHeading(trimmed)) break;
    const body = listItemBody(trimmed);
    if (body === null) {
      if (items.length > 0) break;
      continue;
    }
    const title = stripSkillInvocation(body);
    if (title) items.push(title);
  }
  return items;
}

/**
 * 从技能正文抽出「下一步 / 后续 / Next steps」节里的列表项。
 * 兼容用户自建技能：标题不固定，脚本调用行只保留标题。
 */
export function extractSkillNextSteps(content: string): string[] {
  const lines = (content ?? '').split(/\r?\n/);
  const collected: string[] = [];
  for (let i = 0; i < lines.length; i += 1) {
    if (!isNextStepLeadIn(lines[i])) continue;
    collected.push(...collectListAfter(lines, i + 1));
  }
  return uniqueSuggestions(collected);
}

function extractHeadingNextSteps(text: string): string[] {
  const lines = (text ?? '').split(/\r?\n/);
  let lastLeadIn = -1;
  for (let i = 0; i < lines.length; i += 1) {
    if (isNextStepLeadIn(lines[i])) lastLeadIn = i;
  }
  if (lastLeadIn < 0) return [];
  return uniqueSuggestions(collectListAfter(lines, lastLeadIn + 1));
}

function extractTrailingScriptedList(text: string): string[] {
  const lines = (text ?? '').split(/\r?\n/);
  let best: string[] = [];
  let i = 0;
  while (i < lines.length) {
    if (!isListItem(lines[i])) {
      i += 1;
      continue;
    }
    const rawItems: string[] = [];
    const titles: string[] = [];
    while (i < lines.length) {
      const trimmed = lines[i].trim();
      if (!trimmed) {
        i += 1;
        continue;
      }
      const body = listItemBody(trimmed);
      if (body === null) break;
      rawItems.push(body);
      const title = stripSkillInvocation(body);
      if (title) titles.push(title);
      i += 1;
    }
    if (titles.length >= 2 && rawItems.some((item) => hasScriptInvocation(item))) {
      best = titles;
    }
  }
  return uniqueSuggestions(best);
}

/**
 * 从助手回复抽出已列出的下一步。先看「下一步」类标题，再看末尾带脚本调用的编号列表。
 */
export function extractListedNextSteps(text: string): string[] {
  const fromHeading = extractHeadingNextSteps(text);
  if (fromHeading.length > 0) return fromHeading;
  return extractTrailingScriptedList(text);
}

function skillTitle(content: string): string {
  const first = (content.split(/\r?\n/, 1)[0] ?? '').trim();
  const matched = first.match(/^#{1,6}\s+(.*)$/);
  return matched ? matched[1].trim() : '';
}

/**
 * 各技能「下一步」节的合并结果，本轮点到名的技能排在前面。用户可能装了很多
 * 技能，与本轮无关的后续不该把相关的那几条挤出 {@link SUGGESTED_REPLY_MAX}。
 */
function extractFromSkillContents(
  skillContents: string[] | undefined,
  turnText = '',
): string[] {
  if (!skillContents || skillContents.length === 0) return [];
  const ranked = skillContents
    .map((content, index) => {
      const title = skillTitle(content);
      return { content, index, mentioned: title.length >= 2 && turnText.includes(title) };
    })
    .sort((a, b) => Number(b.mentioned) - Number(a.mentioned) || a.index - b.index);
  const collected: string[] = [];
  for (const entry of ranked) {
    collected.push(...extractSkillNextSteps(entry.content));
  }
  return uniqueSuggestions(collected);
}

/** 本轮可用的下一步：助手回复里已列出的，加上用户技能写明的。 */
function collectNextSteps(
  userText: string,
  assistantText: string,
  skillContents: string[] | undefined,
): string[] {
  return uniqueSuggestions([
    ...extractListedNextSteps(assistantText),
    ...extractFromSkillContents(skillContents, `${userText}\n${assistantText}`),
  ]);
}

/**
 * 技能或助手回复里已经写明的下一步。有内容时调用方应直接采用，不必再跑 LLM。
 */
export function listedSuggestedReplies(
  userText: string,
  assistantText: string,
  opts: { skillContents?: string[] } = {},
): string[] {
  return collectNextSteps(userText, assistantText, opts.skillContents);
}

/**
 * 从模型原文抽出建议。接受 JSON 数组、markdown 代码块、或编号/项目列表。
 * 最多 {@link SUGGESTED_REPLY_MAX} 条。
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
        return uniqueSuggestions(parsed.filter((item): item is string => typeof item === 'string'));
      }
    } catch {
      // 落到分行解析。
    }
  }

  const lines = candidate
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !/^```/.test(line));
  const listLines = lines.filter((line) => isListItem(line));
  return uniqueSuggestions(listLines.length > 0 ? listLines : lines);
}

const PPT_FALLBACK = ['调整封面标题和配色', '把某一页内容写得更具体', '再加一页项目案例'];
const PLAN_FALLBACK = ['按这个计划开始执行', '先改第 2 步再执行', '把计划写得更细一点'];
const CODE_FALLBACK = ['解释这段实现的思路', '帮我补上测试', '再优化一下可读性'];
const GENERIC_FALLBACK = ['继续完善这份结果', '换一种呈现方式', '告诉我下一步怎么做'];
const PPT_OFFER_FALLBACK = ['调整幻灯片的内容和文案', '调整配色和版式', '再加一页补充材料'];

export interface FallbackSuggestedRepliesOptions {
  /** 用户/工作区技能正文（可多份）。会从中抽出「下一步」节。 */
  skillContents?: string[];
}

/** 没有下一步可用时的句子：PPT / 计划 / 代码走专用三条，其它走通用三条。 */
function heuristicSuggestions(userText: string, assistantText: string): string[] {
  const blob = `${userText}\n${assistantText}`;
  if (/\.pptx\b|幻灯片|演示文稿|\bppt\b/i.test(blob)) { // shell-neutral:allow — Office 扩展名 'ppt'/'.pptx'，不是产品品牌
    if (/修改内容|调整样式/.test(assistantText)) {
      return uniqueSuggestions(PPT_OFFER_FALLBACK);
    }
    return uniqueSuggestions(PPT_FALLBACK);
  }
  if (/标准工作流程|待办清单|\bTODO\b|先制定计划/.test(blob) || /^\s*计划已/.test(assistantText)) {
    return uniqueSuggestions(PLAN_FALLBACK);
  }
  if (/```|单元测试|函数实现|补测试/.test(blob)) {
    return uniqueSuggestions(CODE_FALLBACK);
  }
  return uniqueSuggestions(GENERIC_FALLBACK);
}

/**
 * 不调模型的建议。技能或回复里已有下一步时原样采用（条数不固定），
 * 否则回落到启发式句子。
 */
export function fallbackSuggestedReplies(
  userText: string,
  assistantText: string,
  opts: FallbackSuggestedRepliesOptions = {},
): string[] {
  const extracted = collectNextSteps(userText, assistantText, opts.skillContents);
  if (extracted.length > 0) return extracted;
  return heuristicSuggestions(userText, assistantText);
}

export interface GenerateSuggestedRepliesResult {
  suggestions: string[];
  /** true = LLM 失败或输出不合格，且没有从技能/回复抽出下一步，suggestions 来自启发式兜底。 */
  usedFallback: boolean;
}

export interface GenerateSuggestedRepliesOptions {
  perAttemptTimeoutMs?: number;
  /** 用户/工作区技能正文。生成器只抽「下一步」节，不依赖内置技能。 */
  skillContents?: string[];
}

/**
 * 提示词里「用户技能中的下一步」一节。只收真的写了下一步的技能——
 * 把其余技能的正文片段塞进来只会稀释上下文，且与小节标题不符。
 */
function buildSkillExcerpt(skillContents: string[] | undefined): string {
  if (!skillContents || skillContents.length === 0) return '';
  const parts: string[] = [];
  let used = 0;
  for (const content of skillContents) {
    const steps = extractSkillNextSteps(content);
    if (steps.length === 0) continue;
    const title = skillTitle(content);
    const body = steps.map((step, index) => `${index + 1}. ${step}`).join('\n');
    const chunk = title ? `【${title}】\n${body}` : body;
    const next = used + chunk.length;
    if (next > SKILL_EXCERPT_LIMIT && parts.length > 0) break;
    parts.push(chunk);
    used = next;
  }
  return parts.join('\n\n');
}

/**
 * 生成本轮追问建议。永不抛错；有技能/回复下一步时按实际条数返回。
 */
export async function generateSuggestedReplies(
  userText: string,
  assistantText: string,
  opts: GenerateSuggestedRepliesOptions = {},
): Promise<GenerateSuggestedRepliesResult> {
  const extracted = collectNextSteps(userText, assistantText, opts.skillContents);
  const user = clip(userText, USER_TEXT_LIMIT);
  const assistant = clipHeadTail(assistantText, ASSISTANT_HEAD_LIMIT, ASSISTANT_TAIL_LIMIT);
  const skillExcerpt = buildSkillExcerpt(opts.skillContents);
  if (!user && !assistant && extracted.length === 0) {
    return { suggestions: heuristicSuggestions(userText, assistantText), usedFallback: true };
  }

  const perAttemptTimeoutMs = opts.perAttemptTimeoutMs ?? 12_000;
  try {
    const skillBlock = skillExcerpt
      ? `\n\n用户技能中的下一步：\n${skillExcerpt}`
      : '';
    const extractedBlock = extracted.length > 0
      ? `\n\n已从回复或技能抽出的候选（请改写成用户口吻，保留这些动作，不要压成 3 条）：\n${extracted.map((item, index) => `${index + 1}. ${item}`).join('\n')}`
      : '';
    const result = await withTimeout(
      llmService.generate({
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          {
            role: 'user',
            content: `用户请求：\n${user || '（空）'}\n\n助手回复：\n${assistant || '（空）'}${skillBlock}${extractedBlock}`,
          },
        ],
        temperature: 0.6,
      }),
      perAttemptTimeoutMs,
      'ai-suggestions',
    );
    const parsed = parseSuggestedReplies(result.content ?? '');
    if (parsed.length > 0) {
      const suggestions =
        extracted.length > parsed.length
          ? uniqueSuggestions(parsed, extracted)
          : uniqueSuggestions(parsed);
      if (suggestions.length > 0) {
        return { suggestions, usedFallback: false };
      }
    }
  } catch (err) {
    console.warn('[ai-suggestions] LLM 追问建议失败，用启发式兜底', err);
  }
  if (extracted.length > 0) {
    return { suggestions: extracted, usedFallback: false };
  }
  return { suggestions: heuristicSuggestions(userText, assistantText), usedFallback: true };
}

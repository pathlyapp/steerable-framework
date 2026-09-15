/**
 * 单轮上下文压缩辅助（context compaction helpers）。
 *
 * 跨轮历史压缩已下沉框架 CoreLoop（token 压力触发 `CompactionHooks`，压缩
 * 边界持久化到 durable record，桌面发全量原始历史种子、由框架 reconcile）。
 * 桌面不再维护滚动摘要。本模块只剩两类纯函数：
 *
 * 1. **单轮工具结果截断**：`compactToolResultJson` 在把 tool 结果写回
 *    messages 前逐字段截断 + 总量封顶（reverse-tools 用），`truncateMiddle`
 *    是通用中间截断原语。
 * 2. **@引用对话摘录**：`formatHistoryForSummary` 把最近消息格式化成
 *    "用户：… / 助手：…" 的 transcript 摘录（referenced-chat 上下文用）。
 *
 * 全部是纯函数，不依赖 electron / 数据库，便于单测。
 */

import type { LlmMessage } from '../llm/types.js';

// ─── token 估算 ──────────────────────────────────────────────────────────────

/**
 * 粗略 token 估算（无 tokenizer 依赖）。
 * 经验值：CJK 字符 ≈ 0.6 token/字（DeepSeek 官方口径），其它字符 ≈ 0.25 token/字。
 * 用于"要不要压缩"的阈值判断，不追求精确。
 */
export function estimateTokens(text: string): number {
  if (!text) return 0;
  let cjk = 0;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    // CJK 统一表意文字 + 常用中文标点区间
    if (
      (code >= 0x4e00 && code <= 0x9fff) ||
      (code >= 0x3000 && code <= 0x303f) ||
      (code >= 0xff00 && code <= 0xffef)
    ) {
      cjk++;
    }
  }
  const other = text.length - cjk;
  return Math.ceil(cjk * 0.6 + other * 0.25);
}

/** 估算整个 messages 数组的 token（content + reasoningContent + toolCalls 参数）。 */
export function estimateMessagesTokens(messages: LlmMessage[]): number {
  let total = 0;
  for (const msg of messages) {
    total += estimateTokens(msg.content || '');
    if (msg.reasoningContent) total += estimateTokens(msg.reasoningContent);
    if (msg.toolCalls) {
      for (const call of msg.toolCalls) {
        total += estimateTokens(call.name) + estimateTokens(JSON.stringify(call.arguments || {}));
      }
    }
    total += 8; // 每条消息的角色 / 分隔符开销
  }
  return total;
}

// ─── 字符串截断原语 ──────────────────────────────────────────────────────────

const TRUNCATION_MARKER = (omitted: number): string => `\n…[已截断 ${omitted} 字符]…\n`;

/**
 * 中间截断：保留头 60% + 尾 40%（头部通常是结构化字段，尾部通常是 error / 退出码）。
 * `maxChars` 含截断标记本身。
 */
export function truncateMiddle(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  const marker = TRUNCATION_MARKER(text.length - maxChars);
  const budget = Math.max(40, maxChars - marker.length);
  const head = Math.ceil(budget * 0.6);
  const tail = budget - head;
  return text.slice(0, head) + marker + text.slice(text.length - tail);
}

// ─── 单条工具结果压缩（写入 messages 前） ────────────────────────────────────

export interface ToolResultCompactionOptions {
  /** 单条 tool 消息 content 的总字符上限，默认 8000（≈ 2-4K token）。 */
  maxTotalChars?: number;
  /** 单个字符串字段的上限，默认 2000。 */
  maxFieldChars?: number;
  /** 数组元素数量上限，默认 50。 */
  maxArrayItems?: number;
}

const DEFAULT_TOOL_RESULT_MAX_TOTAL = 8000;
const DEFAULT_TOOL_RESULT_MAX_FIELD = 2000;
const DEFAULT_TOOL_RESULT_MAX_ARRAY = 50;

/** 递归截断对象里的长字符串字段 / 超长数组，返回新对象（不改原值）。 */
export function deepTruncateStrings(
  value: unknown,
  maxFieldChars: number,
  maxArrayItems: number = DEFAULT_TOOL_RESULT_MAX_ARRAY,
): unknown {
  if (typeof value === 'string') {
    return value.length > maxFieldChars ? truncateMiddle(value, maxFieldChars) : value;
  }
  if (Array.isArray(value)) {
    const capped = value.slice(0, maxArrayItems).map((item) =>
      deepTruncateStrings(item, maxFieldChars, maxArrayItems)
    );
    if (value.length > maxArrayItems) {
      capped.push(`…[已省略 ${value.length - maxArrayItems} 项]`);
    }
    return capped;
  }
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = deepTruncateStrings(v, maxFieldChars, maxArrayItems);
    }
    return out;
  }
  return value;
}

/**
 * 压缩一条要塞回 LLM 上下文的 tool 结果 JSON。
 * 短的原样返回；超限先逐字段截断，仍超限则整体中间截断并包成合法 JSON 信封，
 * 保证输出永远是合法 JSON（部分 OpenAI 兼容服务器会解析 tool content）。
 *
 * 注意：只影响喂给 LLM 的 messages，**不影响**落库 / 前端展示用的
 * executedActions（那份保留完整结果）。
 */
export function compactToolResultJson(
  json: string,
  options: ToolResultCompactionOptions = {},
): string {
  const maxTotal = options.maxTotalChars ?? DEFAULT_TOOL_RESULT_MAX_TOTAL;
  const maxField = options.maxFieldChars ?? DEFAULT_TOOL_RESULT_MAX_FIELD;
  const maxArray = options.maxArrayItems ?? DEFAULT_TOOL_RESULT_MAX_ARRAY;
  if (json.length <= maxTotal) return json;

  try {
    const parsed = JSON.parse(json);
    let out = JSON.stringify(deepTruncateStrings(parsed, maxField, maxArray));
    if (out.length <= maxTotal) return out;
    // 逐字段截断后仍超限（大量小字段 / 深层结构）：按比例再收紧字段上限
    const tighterField = Math.max(200, Math.floor((maxField * maxTotal) / out.length));
    out = JSON.stringify(deepTruncateStrings(parsed, tighterField, Math.min(maxArray, 20)));
    if (out.length <= maxTotal) return out;
  } catch {
    // 非法 JSON：走下面的信封兜底
  }

  return JSON.stringify({
    truncated: true,
    originalChars: json.length,
    note: '工具结果过长，已截断。完整结果已展示给用户，无需复述原文。',
    preview: truncateMiddle(json, Math.max(500, maxTotal - 300)),
  });
}

// ─── 单轮 agent loop 的滚动压缩（每轮 LLM 调用前） ───────────────────────────

export interface MessagesCompactionOptions {
  /** 上下文 token 预算（对齐 HarnessBudget.maxContextTokens）。 */
  maxContextTokens: number;
  /** 最近 N 条 tool 消息保持原文不压，默认 4（约等于最近 1-2 轮的结果）。 */
  keepRecentToolResults?: number;
  /** 被压缩后的 tool 消息 content 目标字符数，默认 600。 */
  compactedMaxChars?: number;
}

export interface MessagesCompactionResult {
  /** 本次被收缩的 tool 消息数。 */
  compactedCount: number;
  /** 压缩后的估算 token 总量。 */
  estimatedTokens: number;
}

/** 从 tool 结果 JSON 里抽取关键语义字段，生成收缩版 content（合法 JSON）。 */
function briefToolContent(json: string, maxChars: number): string {
  const PICK_KEYS = [
    'success', 'status', 'error', 'message', 'hint',
    'exitCode', 'code', 'taskId', 'total', 'totalFound',
  ];
  try {
    const parsed = JSON.parse(json) as Record<string, unknown>;
    const result =
      parsed && typeof parsed.result === 'object' && parsed.result !== null
        ? (parsed.result as Record<string, unknown>)
        : parsed;
    const picked: Record<string, unknown> = {};
    for (const key of PICK_KEYS) {
      if (key in result && result[key] !== undefined && result[key] !== null) {
        const v = result[key];
        picked[key] = typeof v === 'string' && v.length > 300 ? truncateMiddle(v, 300) : v;
      }
    }
    return JSON.stringify({
      compacted: true,
      note: '完整结果已因上下文超限被压缩，仅保留关键字段。',
      result: picked,
    });
  } catch {
    return JSON.stringify({
      compacted: true,
      note: '完整结果已因上下文超限被压缩。',
      preview: truncateMiddle(json, maxChars),
    });
  }
}

/**
 * 每轮 LLM 调用前对 messages 做**原地**滚动压缩：
 * 估算 token 超出预算时，从最旧的 tool 消息开始把 content 收缩成关键字段摘要，
 * 直到回到预算内或没有可压对象。最近 `keepRecentToolResults` 条 tool 消息
 * 永远保持原文（当前任务大概率还依赖它们）。
 *
 * 只动 tool 消息的 content：
 * - 消息条数 / 顺序 / toolCallId 不变 → 不破坏 assistant(tool_calls)/tool 配对；
 * - 不碰 reasoningContent（DeepSeek thinking 模式要求原样回传）。
 */
export function compactMessagesForContext(
  messages: LlmMessage[],
  options: MessagesCompactionOptions,
): MessagesCompactionResult {
  const keepRecent = options.keepRecentToolResults ?? 4;
  const compactedMaxChars = options.compactedMaxChars ?? 600;

  let total = estimateMessagesTokens(messages);
  if (total <= options.maxContextTokens) {
    return { compactedCount: 0, estimatedTokens: total };
  }

  const toolIndexes: number[] = [];
  for (let i = 0; i < messages.length; i++) {
    if (messages[i].role === 'tool') toolIndexes.push(i);
  }
  const protectedIndexes = new Set(toolIndexes.slice(-keepRecent));

  let compactedCount = 0;
  for (const idx of toolIndexes) {
    if (total <= options.maxContextTokens) break;
    if (protectedIndexes.has(idx)) continue;
    const msg = messages[idx];
    if (!msg.content || msg.content.length <= compactedMaxChars) continue;
    const before = estimateTokens(msg.content);
    msg.content = briefToolContent(msg.content, compactedMaxChars);
    total -= before - estimateTokens(msg.content);
    compactedCount++;
  }
  return { compactedCount, estimatedTokens: total };
}

// ─── @引用对话摘录 ───────────────────────────────────────────────────────────

/**
 * 把消息列表格式化成 "用户：… / 助手：…" 的 transcript 摘录，单条消息中间
 * 截断防止单条爆掉。referenced-chat（@引用别的对话）上下文用。
 */
export function formatHistoryForSummary(
  items: Array<{ role: string; content: string }>,
  perMessageMaxChars = 1200,
): string {
  return items
    .map((item) => {
      const label = item.role === 'user' ? '用户' : '助手';
      const content = truncateMiddle((item.content || '').trim(), perMessageMaxChars);
      return `${label}：${content}`;
    })
    .filter((line) => line.length > 3)
    .join('\n\n');
}

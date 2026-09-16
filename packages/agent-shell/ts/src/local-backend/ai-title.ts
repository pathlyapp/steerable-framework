/**
 * AI 聊天标题生成。
 *
 * 移植自上游 Python 服务的 ai_title 模块，行为对齐：
 *   - 用一段固定的中文 system prompt 让模型把首条用户消息总结成 5-15 字的标题
 *   - max tokens 限制在 ~32，避免模型啰嗦
 *   - 失败时返回 '新对话'（与 storage createChat 默认值一致），不抛错
 *
 * 实现差异：
 *   - 本地用户模型不固定是 deepseek-v3，直接复用 llmService.generate 的默认 settings
 *     （Ollama / OpenAI-compat）。模型小、prompt 短、max tokens 低，对大多数本地模型
 *     都足够好用了。
 *   - 调用方应该 fire-and-forget：不要 await，也别把它挂在 SSE 主流程里阻塞 [DONE]。
 *
 * 公开函数返回 Promise<{ title; usedFallback }>，方便调用方判断要不要 emit
 * `chat_title_updated` SSE 事件（fallback 时跳过 emit、避免覆盖用户手动改过的标题）。
 */

import { llmService } from '../llm/index.js';

const DEFAULT_TITLE = '新对话';

const SYSTEM_PROMPT = `你是一个专业的标题生成助手。请将用户的消息总结为一个简短、清晰、准确的对话标题。
标题要求：
1. 长度控制在5-15个字之间
2. 提取消息的核心目标或主题
3. 使用简洁明了的语言
4. 不要使用引号或特殊符号
5. 直接返回标题文本，不要有任何解释或前缀`;

/**
 * 把 LLM 吐出来的"标题"做一遍人肉清洗：
 *  - 去除首尾空白、各类引号
 *  - 取第一行（防止模型给一段解释 + 标题）
 *  - 截断到 30 字，超过的兜底（本地模型偶尔会狂吐）
 *  - 完全为空 → 返回 DEFAULT_TITLE
 */
function cleanTitle(raw: string): string {
  if (!raw) return DEFAULT_TITLE;
  let title = raw.trim();
  // 取第一行，模型经常会先吐一段思考再给标题
  const firstLine = title.split(/\r?\n/)[0]?.trim();
  if (firstLine) title = firstLine;
  // 去除常见包裹：英文/中文引号、书名号、句号、冒号前缀
  title = title.replace(/^[\s"'“”‘’「」『』《》【】]+|[\s"'“”‘’「」『』《》【】。.！!?？，,；;:：]+$/g, '');
  // 去掉 "标题：xxx" / "Title: xxx" 这种前缀
  title = title.replace(/^(标题|题目|对话标题|chat\s*title|title)\s*[:：-]\s*/i, '');
  // 折叠多余空格
  title = title.replace(/\s+/g, ' ').trim();
  if (!title) return DEFAULT_TITLE;
  // 兜底长度——5-15 字是理想，但小模型偶尔会塞 50 字进来。30 是个软上限。
  if (title.length > 30) title = title.slice(0, 30).trim();
  return title || DEFAULT_TITLE;
}

export interface GenerateChatTitleResult {
  title: string;
  /** true 表示返回的是 DEFAULT_TITLE（生成失败/被清空），调用方应该不写库或不发事件。 */
  usedFallback: boolean;
}

/**
 * 给 promise 套一个超时。超时时 reject 一个明确的 Error，让上层 catch 走重试 /
 * 短消息兜底分支，而**不**是直接绕过这些分支返回 DEFAULT_TITLE。
 *
 * 注意：被 race 掉的 promise 仍在后台继续跑——本地 Ollama 调用没有 abort 接口，
 * 这是无害的浪费（结果会被丢弃），但比让用户等着强。
 */
function withTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  label: string,
): Promise<T> {
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

/** 短消息快速路径阈值：消息 ≤ 该字数时跳过 LLM，直接拿消息本身当标题。
 *
 * 选 8 字的理由：
 *  - 「你好」「在吗」「hi」「help me」这种问候 / 唤起词，LLM 跑半天也总结
 *    不出比"你好"更好的标题。早期实验中 12s 等待 → 兜底回原消息，纯白等
 *  - 5-8 字的短指令（"帮我查天气" "新建任务"）跟 LLM 给的"5-15字 标题"
 *    长度上没差别，跳过 LLM 不会丢信息
 *  - 超过 8 字才上 LLM——这时通常有足够语义让模型抽个有意义的主题
 */
const SHORT_MESSAGE_THRESHOLD = 8;

/**
 * 生成 chat 标题。永不抛错。
 *
 * @param message 首条用户消息（已去空白；空字符串会直接返回 fallback）
 * @param opts.maxRetries 最大重试次数（不含首次），默认 0。
 *   本地 Ollama 出错主要是"超时 / 推理慢"——retry 同一个慢模型几乎不会更快，
 *   反而把用户的等待时间 ×N。慢机器宁可一次失败走短消息兜底也别再等。
 * @param opts.perAttemptTimeoutMs 单次 LLM 调用的硬超时，默认 12000ms。
 *   早期版本是 6000ms，但 Ollama 在主回复刚结束后需要重做 KV cache，
 *   30 token 的小输出也常吃到 7-10s。12s 给中速本地模型留足余量；再慢
 *   就别折腾用户了。
 *   注意：这是**单次**的超时，不是整个函数的超时。这样超时后还能走"短消息直接
 *   当标题"的兜底——把超时放在外层（用 Promise.race 包整个函数）会绕过兜底。
 * @param opts.skipLlmForShortMessages 短消息（≤ SHORT_MESSAGE_THRESHOLD 字）
 *   直接跳过 LLM，默认 true。可设 false 强制走 LLM（基本只在测试时用）。
 */
export async function generateChatTitle(
  message: string,
  opts: {
    maxRetries?: number;
    perAttemptTimeoutMs?: number;
    skipLlmForShortMessages?: boolean;
  } = {},
): Promise<GenerateChatTitleResult> {
  const trimmed = (message ?? '').trim();
  if (!trimmed) {
    return { title: DEFAULT_TITLE, usedFallback: true };
  }
  const maxRetries = Math.max(0, opts.maxRetries ?? 0);
  const perAttemptTimeoutMs = opts.perAttemptTimeoutMs ?? 12000;
  const skipShort = opts.skipLlmForShortMessages ?? true;

  // ── 快速路径：极短消息直接当标题 ─────────────────────────────────────
  // LLM 对这种输入只会返回比它本身更糟的东西（"新对话" / "你好" / 重复一遍）。
  // 算成 Unicode 码点而不是 byte——一个汉字算 1 个字符。
  const charCount = Array.from(trimmed).length;
  if (skipShort && charCount <= SHORT_MESSAGE_THRESHOLD) {
    const cleanedShort = cleanTitle(trimmed);
    if (cleanedShort && cleanedShort !== DEFAULT_TITLE) {
      return { title: cleanedShort, usedFallback: false };
    }
  }

  // 截断用户消息——超长输入对小模型反而是噪音，标题只看核心意图就够了。
  const truncated = trimmed.length > 500 ? trimmed.slice(0, 500) : trimmed;

  let lastErr: unknown = null;
  let lastRawContent = '';
  for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
    try {
      const result = await withTimeout(
        llmService.generate({
          messages: [
            { role: 'system', content: SYSTEM_PROMPT },
            { role: 'user', content: truncated },
          ],
          temperature: 0.7,
        }),
        perAttemptTimeoutMs,
        `ai-title attempt ${attempt + 1}`,
      );
      lastRawContent = result.content ?? '';
      const cleaned = cleanTitle(lastRawContent);
      if (cleaned && cleaned !== DEFAULT_TITLE) {
        return { title: cleaned, usedFallback: false };
      }
      // 模型返回了空白 / 只剩兜底字符串，重试一次再说
      lastErr = new Error(`empty title (attempt ${attempt + 1}, raw=${JSON.stringify(lastRawContent.slice(0, 80))})`);
    } catch (err) {
      lastErr = err;
    }
    // 简单退避：本地模型 retry 不指数也行
    if (attempt < maxRetries) {
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }

  console.warn('[ai-title] LLM 标题生成失败，用消息开头兜底', lastErr);

  // 兜底：用消息本身做标题。
  //
  // 设计取舍：早期版本把这个分支限制在「≤ 20 字」——长消息直接退到 DEFAULT_TITLE，
  // 结果用户输入「看一下最近的记录里有哪些跟某指标对比相关的内容...」这种
  // 30+ 字指令时，标题就回到"新对话"，sidebar 啥信息都没。截断后的原文哪怕
  // 只有前 20 字也比"新对话"强得多——至少能回忆起这条对话在干嘛。
  //
  // 截断规则：按 Unicode 码点取前 20 字符，避免把 emoji / 中文截在半字节上。
  const codepoints = Array.from(trimmed);
  const head = codepoints.slice(0, 20).join('');
  const cleaned = cleanTitle(head);
  // 加省略号提示 sidebar 上看到的是截断版本，而不是用户输入的完整 query
  const finalTitle =
    cleaned && cleaned !== DEFAULT_TITLE
      ? codepoints.length > 20
        ? `${cleaned}…`
        : cleaned
      : DEFAULT_TITLE;
  if (finalTitle !== DEFAULT_TITLE) {
    return { title: finalTitle, usedFallback: false };
  }
  return { title: DEFAULT_TITLE, usedFallback: true };
}

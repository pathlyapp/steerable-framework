/**
 * 正文里提到的文件路径：识别 + 存在性解析缓存。
 *
 * 助手回答里的路径通常写在行内代码里（`` `./报告.docx` ``）。这里先用
 * 廉价的形状判断挑出候选，再交给后端 stat 证伪——路径是模型写出来的自然
 * 语言片段，形状判断必然有误报，只有后端确认存在的才会变成可点击。
 *
 * 缓存是模块级的：流式渲染期间同一个代码节点会重渲染很多次，没有缓存和
 * 请求合并会把 resolve-paths 打成风暴。同一 (chatId, 字面量) 只解析一次，
 * 同一批渲染里的多个候选合并成一次请求。
 */

import { resolveLocalPaths, type ResolvedLocalPath } from '@/lib/local-api';

/** 协议前缀（http:// 等）不是本地路径。 */
const URL_LIKE = /^[a-z][a-z0-9+.-]*:\/\//i;
/** `1.2.3` / `v0.6.20` 这类版本号会被「带扩展名的裸文件名」规则误收。 */
const VERSION_LIKE = /^v?\d+(\.\d+)+$/;
/** Windows 绝对路径：`C:\x` / `C:/x`。 */
const WINDOWS_ABSOLUTE = /^[A-Za-z]:[\\/]/;
/** 不含分隔符但带扩展名的裸文件名：`报告.docx`。 */
const BARE_FILE_NAME = /^[^\\/<>:"|?*]+\.[A-Za-z0-9]{1,10}$/;

/** 单条候选的长度上限（与后端一致）。 */
const MAX_CANDIDATE_LENGTH = 512;

/**
 * 形状上像不像本地文件/目录路径。只做廉价判断，存在性由后端把关。
 * 含空白的一律否决——行内代码里带空格的多是命令（`npm install`），
 * 带空格的真实路径在正文里极少，误伤远小于把命令渲染成可点击。
 */
export function looksLikeFilePath(text: string): boolean {
  const candidate = text.trim();
  if (!candidate || candidate.length > MAX_CANDIDATE_LENGTH) return false;
  if (/\s/.test(candidate)) return false;
  if (URL_LIKE.test(candidate)) return false;
  if (VERSION_LIKE.test(candidate)) return false;
  if (candidate.startsWith('~/') || WINDOWS_ABSOLUTE.test(candidate)) return true;
  if (candidate.includes('/')) return true;
  return BARE_FILE_NAME.test(candidate);
}

/** null = 后端确认过但不存在（不可点击）；undefined = 还没解析。 */
type CacheEntry = ResolvedLocalPath | null;

const cache = new Map<string, CacheEntry>();
const waiters = new Map<string, Set<(entry: CacheEntry) => void>>();
const queued = new Map<string, Set<string>>();
let flushHandle: ReturnType<typeof setTimeout> | null = null;

function cacheKey(candidate: string, chatId: string): string {
  return `${chatId}\u0000${candidate}`;
}

/** 已解析过就直接返回结果，否则返回 undefined（调用方需要订阅）。 */
export function peekResolvedPath(
  candidate: string,
  chatId?: string | null,
): CacheEntry | undefined {
  return cache.get(cacheKey(candidate.trim(), chatId ?? ''));
}

/**
 * 订阅一个候选的解析结果。已有缓存时同步回调；否则入队，同一批渲染的候选
 * 合并成一次请求。返回取消订阅函数（组件卸载后不再回调）。
 */
export function subscribeResolvedPath(
  candidate: string,
  chatId: string | null | undefined,
  onResolve: (entry: CacheEntry) => void,
): () => void {
  const trimmed = candidate.trim();
  const scope = chatId ?? '';
  const key = cacheKey(trimmed, scope);
  const cached = cache.get(key);
  if (cached !== undefined) {
    onResolve(cached);
    return () => {};
  }

  let listeners = waiters.get(key);
  if (!listeners) {
    listeners = new Set();
    waiters.set(key, listeners);
  }
  listeners.add(onResolve);

  let scoped = queued.get(scope);
  if (!scoped) {
    scoped = new Set();
    queued.set(scope, scoped);
  }
  scoped.add(trimmed);
  scheduleFlush();

  return () => {
    listeners?.delete(onResolve);
  };
}

function scheduleFlush(): void {
  if (flushHandle !== null) return;
  flushHandle = setTimeout(() => {
    flushHandle = null;
    void flushQueued();
  }, 0);
}

async function flushQueued(): Promise<void> {
  const batches = [...queued.entries()];
  queued.clear();
  for (const [scope, candidates] of batches) {
    const list = [...candidates];
    if (list.length === 0) continue;
    let resolved: ResolvedLocalPath[] = [];
    try {
      const response = await resolveLocalPaths(list, scope || null);
      resolved = response.resolved ?? [];
    } catch {
      // 解析失败（后端不可达等）当作「不存在」落缓存：正文照旧渲染成
      // 普通行内代码，不给用户一个点不开的假affordance。
      resolved = [];
    }
    const byCandidate = new Map(resolved.map((item) => [item.candidate, item]));
    for (const candidate of list) {
      settle(cacheKey(candidate, scope), byCandidate.get(candidate) ?? null);
    }
  }
}

function settle(key: string, entry: CacheEntry): void {
  cache.set(key, entry);
  const listeners = waiters.get(key);
  waiters.delete(key);
  if (!listeners) return;
  for (const listener of listeners) listener(entry);
}

/** 测试用：清空模块级缓存与待解析队列。 */
export function resetPathMentionCache(): void {
  cache.clear();
  waiters.clear();
  queued.clear();
  if (flushHandle !== null) {
    clearTimeout(flushHandle);
    flushHandle = null;
  }
}

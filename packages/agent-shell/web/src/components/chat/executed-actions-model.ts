/**
 * ExecutedActionsCard 的纯函数半侧（W5-2）：web_search / web_fetch 的
 * 中文摘要。从 action.arguments / action.result 提取关键信息，让工具卡
 * 一眼可读（查了什么、抓了什么、几条结果、HTTP 状态），而不是甩原始 JSON。
 *
 * result 的形态：sidecar ToolResult `{ success, data?, error? }`；桌面
 * CoreLoop 路径上 data 可能被 context-compactor 截断过大字段，但
 * result_count / status / bytes / truncated 这些小字段原样保留。
 */

interface WebToolResultData {
  result_count?: unknown;
  status?: unknown;
  bytes?: unknown;
  truncated?: unknown;
  url?: unknown;
}

function resultData(result: unknown): WebToolResultData | null {
  if (!result || typeof result !== 'object') return null;
  const data = (result as { data?: unknown }).data;
  return data && typeof data === 'object' ? (data as WebToolResultData) : null;
}

function shortUrl(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw.length === 0) return null;
  try {
    const u = new URL(raw);
    const path = u.pathname === '/' ? '' : u.pathname;
    const text = `${u.host}${path}`;
    return text.length > 48 ? `${text.slice(0, 45)}…` : text;
  } catch {
    return raw.length > 48 ? `${raw.slice(0, 45)}…` : raw;
  }
}

function formatBytes(n: unknown): string | null {
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return null;
  if (n < 1024) return `${n} B`;
  return `${(n / 1024).toFixed(1)} KB`;
}

/**
 * web_search / web_fetch 的卡片摘要；非这两个工具返回 null（调用方回落到
 * 通用参数摘要）。
 */
export function summarizeWebAction(
  tool: string,
  args: unknown,
  result: unknown,
): string | null {
  const obj =
    args && typeof args === 'object' ? (args as Record<string, unknown>) : {};
  const data = resultData(result);
  const failed =
    result &&
    typeof result === 'object' &&
    (result as { success?: unknown }).success === false;

  if (tool === 'web_search') {
    const query = typeof obj.query === 'string' ? obj.query : '';
    const head = `搜索“${query.length > 40 ? `${query.slice(0, 37)}…` : query}”`;
    if (failed || !data) return head;
    const count = typeof data.result_count === 'number' ? data.result_count : null;
    return count === null ? head : `${head} → ${count} 条结果`;
  }
  if (tool === 'web_fetch') {
    const head = `抓取 ${shortUrl(obj.url) ?? '未知地址'}`;
    if (failed || !data) return head;
    const parts: string[] = [];
    if (typeof data.status === 'number') parts.push(String(data.status));
    const size = formatBytes(data.bytes);
    if (size) parts.push(size);
    if (data.truncated === true) parts.push('已截断');
    return parts.length > 0 ? `${head} → ${parts.join(' · ')}` : head;
  }
  return null;
}

interface RunCodeCall {
  tool?: unknown;
  arguments?: unknown;
  result?: unknown;
}

/**
 * run_code 的卡片摘要：描述 + 内层工具次数，而不是整段程序 JSON。
 */
export function summarizeRunCodeAction(
  tool: string,
  args: unknown,
  result: unknown,
): string | null {
  if (tool !== 'run_code') return null;
  const obj =
    args && typeof args === 'object' ? (args as Record<string, unknown>) : {};
  const description =
    typeof obj.description === 'string' && obj.description.trim()
      ? obj.description.trim()
      : '程序';
  const head =
    description.length > 40 ? `${description.slice(0, 37)}…` : description;
  const data = resultData(result);
  const calls = Array.isArray((data as { calls?: unknown } | null)?.calls)
    ? ((data as { calls: RunCodeCall[] }).calls)
    : [];
  const failed =
    result &&
    typeof result === 'object' &&
    (result as { success?: unknown }).success === false;
  if (failed) return `程序「${head}」失败`;
  return calls.length > 0
    ? `程序「${head}」· ${calls.length} 个内层工具`
    : `程序「${head}」`;
}

export interface ExecutedActionLike {
  tool: string;
  arguments?: unknown;
  result?: unknown;
  sandbox?: { backend?: string; enforcement: string };
}

/** Flatten a run_code action into the outer program plus inner tool rows. */
export function expandRunCodeActions<T extends ExecutedActionLike>(actions: T[]): T[] {
  const out: T[] = [];
  for (const action of actions) {
    out.push(action);
    if (action.tool !== 'run_code') continue;
    const data = resultData(action.result) as { calls?: RunCodeCall[] } | null;
    const calls = Array.isArray(data?.calls) ? data.calls : [];
    for (const call of calls) {
      const name = typeof call.tool === 'string' ? call.tool : 'tool';
      out.push({
        ...action,
        tool: name,
        arguments: call.arguments,
        result: call.result,
      });
    }
  }
  return out;
}

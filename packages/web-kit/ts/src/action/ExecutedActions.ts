export interface ExecutedAction {
  tool: string;
  arguments?: Record<string, unknown>;
  result?: Record<string, unknown>;
  policyDecision?: Record<string, unknown>;
  idempotencyKey?: string;
  entityHints?: Record<string, string>;
}

/**
 * Derive a short, user-friendly summary of a read-shape tool's result for
 * inline display (e.g. "5 条" / "12 个事件" / "无结果"). Returns null when
 * the result shape isn't recognised so callers can hide the badge entirely.
 */
export function summarizeReadResult(result: unknown): string | null {
  if (result === null || result === undefined) return null;
  if (Array.isArray(result)) {
    return result.length === 0 ? '无结果' : `${result.length} 条`;
  }
  if (typeof result !== 'object') return null;
  const obj = result as Record<string, unknown>;

  if ('error' in obj && obj.error) return null;
  if ('success' in obj && obj.success === false) return null;

  for (const k of ['count', 'total', 'totalCount']) {
    const v = obj[k];
    if (typeof v === 'number' && Number.isFinite(v)) {
      return v === 0 ? '无结果' : `${v} 条`;
    }
  }

  const arrayKeys = [
    'items', 'results', 'matches', 'memories', 'tasks', 'events',
    'goals', 'notes', 'documents', 'templates', 'tools', 'skills',
    'chunks', 'records', 'entries',
  ];
  for (const k of arrayKeys) {
    const v = obj[k];
    if (Array.isArray(v)) {
      return v.length === 0 ? '无结果' : `${v.length} 条`;
    }
  }

  const nested = obj.data;
  if (nested && typeof nested === 'object') {
    if (Array.isArray(nested)) {
      return nested.length === 0 ? '无结果' : `${nested.length} 条`;
    }
    const nestedObj = nested as Record<string, unknown>;
    for (const k of arrayKeys) {
      const v = nestedObj[k];
      if (Array.isArray(v)) {
        return v.length === 0 ? '无结果' : `${v.length} 条`;
      }
    }
    for (const k of ['count', 'total', 'totalCount']) {
      const v = nestedObj[k];
      if (typeof v === 'number' && Number.isFinite(v)) {
        return v === 0 ? '无结果' : `${v} 条`;
      }
    }
  }

  return null;
}

export function getEntityIdFromExecutedAction(action: ExecutedAction): string | null {
  const result = action.result;
  if (result && typeof result === 'object') {
    if ('id' in result && typeof result.id === 'string') return result.id;
    if ('actionId' in result && typeof result.actionId === 'string') return result.actionId;

    const data = (result as Record<string, unknown>).data;
    if (
      data &&
      typeof data === 'object' &&
      'id' in (data as Record<string, unknown>) &&
      typeof (data as Record<string, unknown>).id === 'string'
    ) {
      return (data as Record<string, unknown>).id as string;
    }
  }
  return null;
}

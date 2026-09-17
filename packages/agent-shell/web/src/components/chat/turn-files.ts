/**
 * 回合产物文件列表（「本轮写了哪些文件」）的前端模型。
 *
 * 数据源：local-backend 在回合收尾时扫描可写根 + 写工具参数并集（见
 * `src/local-backend/turn-files.ts`），经 `turn_files` SSE 事件下发，并
 * 持久化进助手消息的 messageMetadata（键 `turnFiles`）供刷新后水合。
 */

export interface TurnFile {
  /** 绝对路径 —— 点击打开直接传给后端。 */
  path: string;
  /** created = 本轮新建；modified = 已有文件被改动。 */
  kind: 'created' | 'modified';
  /** 字节数（展示用，可缺省）。 */
  size?: number;
}

/** 解析 SSE 事件 / 持久化元数据里的文件列表；形状不符返回 null（与 parseTurnBlocks 同约定）。 */
export function parseTurnFiles(raw: unknown): TurnFile[] | null {
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const files: TurnFile[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') return null;
    const rec = item as Record<string, unknown>;
    if (typeof rec.path !== 'string' || !rec.path) return null;
    if (rec.kind !== 'created' && rec.kind !== 'modified') return null;
    files.push({
      path: rec.path,
      kind: rec.kind,
      ...(typeof rec.size === 'number' ? { size: rec.size } : {}),
    });
  }
  return files;
}

/** 文件名的展示分段：basename 加粗、目录部分弱化。 */
export function splitTurnFilePath(filePath: string): { dir: string; name: string } {
  const normalized = filePath.replace(/[/\\]+$/, '');
  const idx = Math.max(normalized.lastIndexOf('/'), normalized.lastIndexOf('\\'));
  if (idx < 0) return { dir: '', name: normalized };
  return { dir: normalized.slice(0, idx + 1), name: normalized.slice(idx + 1) };
}

/** 紧凑的字节数展示（不足 1 KB 显示 B）。 */
export function formatFileSize(size: number | undefined): string | null {
  if (size == null || !Number.isFinite(size) || size < 0) return null;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

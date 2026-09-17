import { useState } from 'react';
import {
  LuFilePen,
  LuFilePlus,
  LuFiles,
  LuLoaderCircle,
} from 'react-icons/lu';
import { openLocalPath } from '@/lib/local-api';
import {
  formatFileSize,
  splitTurnFilePath,
  type TurnFile,
} from './turn-files';

/**
 * TurnFilesCard — 回合产物文件列表（Codex 式「本轮写了哪些文件」）。
 *
 * 渲染在助手回合的末尾（回答气泡之下、时间戳行之上），回合内新建/修改的
 * 文件逐行列出；点击行用系统默认应用打开该文件（PPT → PowerPoint/WPS，
 * 报告 → 对应阅读器）。打开失败在行内给出错误提示，不打断会话。
 *
 * 数据来自 local-backend 的 `turn_files` SSE 事件 / messageMetadata 里的
 * `turnFiles`（见 `src/local-backend/turn-files.ts` 的收集逻辑）。
 */

interface TurnFilesCardProps {
  files: TurnFile[];
}

export function TurnFilesCard({ files }: TurnFilesCardProps) {
  const [openingPath, setOpeningPath] = useState<string | null>(null);
  const [openError, setOpenError] = useState<{ path: string; message: string } | null>(null);

  if (files.length === 0) return null;

  const handleOpen = async (file: TurnFile) => {
    if (openingPath) return;
    setOpeningPath(file.path);
    setOpenError(null);
    try {
      const result = await openLocalPath(file.path);
      if (!result.success) {
        setOpenError({ path: file.path, message: result.error || '打开失败' });
      }
    } catch (err) {
      setOpenError({
        path: file.path,
        message: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setOpeningPath(null);
    }
  };

  return (
    <div
      className="rounded-agent-lg border border-agent-border bg-agent-canvas shadow-sm"
      data-turn-files=""
    >
      <div className="flex items-center gap-1.5 border-b border-agent-border/60 px-3 py-1.5 text-[11px] text-agent-muted-foreground">
        <LuFiles className="h-3.5 w-3.5" />
        <span>本轮产生了 {files.length} 个文件，点击打开</span>
      </div>
      <ul className="max-h-64 overflow-y-auto py-1">
        {files.map((file) => {
          const { dir, name } = splitTurnFilePath(file.path);
          const size = formatFileSize(file.size);
          const busy = openingPath === file.path;
          const error = openError?.path === file.path ? openError.message : null;
          return (
            <li key={file.path}>
              <button
                type="button"
                onClick={() => void handleOpen(file)}
                disabled={openingPath !== null}
                title={file.path}
                className="flex w-full items-center gap-2 px-3 py-1 text-left text-xs transition-colors hover:bg-agent-foreground/5 disabled:cursor-wait"
                data-turn-file=""
                data-kind={file.kind}
              >
                {busy ? (
                  <LuLoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin text-agent-muted-foreground" />
                ) : file.kind === 'created' ? (
                  <LuFilePlus className="h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
                ) : (
                  <LuFilePen className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
                )}
                <span className="min-w-0 flex-1 truncate">
                  <span className="font-medium text-agent-foreground">{name}</span>
                  {dir && (
                    <span className="ml-1.5 text-agent-muted-foreground">{dir}</span>
                  )}
                </span>
                {size && (
                  <span className="shrink-0 tabular-nums text-[11px] text-agent-muted-foreground">
                    {size}
                  </span>
                )}
              </button>
              {error && (
                <div className="px-3 pb-1 text-[11px] text-agent-destructive" role="status">
                  打开失败：{error}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export default TurnFilesCard;

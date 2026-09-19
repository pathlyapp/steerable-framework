import { useEffect, useState, type ComponentPropsWithoutRef, type ReactNode } from 'react';
import { openLocalPath, type ResolvedLocalPath } from '@/lib/local-api';
import { peekResolvedPath, subscribeResolvedPath } from './path-mentions';
import { splitTurnFilePath } from './turn-files';

/**
 * 行内代码里的文件路径：后端确认存在后变成可点击，点击用系统默认应用打开。
 *
 * 未确认存在（或后端不可达）时渲染成与普通行内代码完全一致的外观——路径
 * 靠形状判断挑出，误报难免，不能给点不开的假 affordance。
 */

const INLINE_CODE_CLASS =
  'rounded bg-agent-muted px-1 py-0.5 font-mono text-[0.85em] text-agent-foreground';

interface FilePathCodeProps extends ComponentPropsWithoutRef<'code'> {
  /** 行内代码的纯文本，即待解析的路径字面量。 */
  candidate: string;
  /** 当前对话（相对路径按其绑定项目根解析），落地页可缺省。 */
  chatId?: string | null;
  children: ReactNode;
}

export function FilePathCode({ candidate, chatId, children, ...rest }: FilePathCodeProps) {
  const [resolved, setResolved] = useState<ResolvedLocalPath | null>(
    () => peekResolvedPath(candidate, chatId) ?? null,
  );
  const [openError, setOpenError] = useState<string | null>(null);

  useEffect(() => {
    setOpenError(null);
    return subscribeResolvedPath(candidate, chatId, (entry) => setResolved(entry));
  }, [candidate, chatId]);

  if (!resolved) {
    return (
      <code {...rest} className={INLINE_CODE_CLASS}>
        {children}
      </code>
    );
  }

  const handleClick = async () => {
    setOpenError(null);
    try {
      const result = await openLocalPath(resolved.path);
      if (!result.success) setOpenError(result.error || '打开失败');
    } catch (err) {
      setOpenError(err instanceof Error ? err.message : '打开失败');
    }
  };

  return (
    <button
      type="button"
      data-file-path-chip=""
      onClick={handleClick}
      title={openError ? `${resolved.path}（${openError}）` : `点击打开 ${resolved.path}`}
      className={`${INLINE_CODE_CLASS} cursor-pointer underline decoration-dotted underline-offset-2 transition-colors hover:bg-agent-accent/15 hover:text-agent-accent ${
        openError ? 'text-red-600 dark:text-red-400' : ''
      }`}
    >
      {splitTurnFilePath(resolved.path).name || children}
    </button>
  );
}

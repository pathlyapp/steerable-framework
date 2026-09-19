import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { LuCheck, LuGitBranch, LuLoaderCircle, LuX } from 'react-icons/lu';
import {
  activateChatBranch,
  getChatBranchTree,
  type ChatBranchTreeNode,
  type ChatBranchTreeResponse,
} from '@/lib/local-api';

/**
 * SessionTreeModal — pi 式全树分支视图（session tree）。
 *
 * 数据来自 getChatBranchTree（sidecar `agent.session.tree`）：从家族根
 * 展开的完整 fork 树，含堂兄弟分支——Header 下拉只有 lineage + 直接
 * 子节点，这里能看到并一键切换到任意家族成员。
 *
 * 渲染参考 pi 的 TreeSelector：DFS 铺平成缩进行，│/├─/└─ 连接线，
 * 当前 activeRecordId 高亮。交互：点击（或 ↑↓ + Enter）切换分支 →
 * activateChatBranch → onBranchSwitched 触发消息区重投影 → 关闭；
 * Escape / 点击遮罩关闭。
 */

interface SessionTreeModalProps {
  chatId: string;
  onClose: () => void;
  /** 切换成功后调用——页面据此重新水合消息列表（branchTick 机制）。 */
  onBranchSwitched?: () => void;
}

interface FlatNode {
  node: ChatBranchTreeNode;
  /** 已拼好的缩进前缀，如 "│   ├─ "（根节点为空）。 */
  prefix: string;
}

/** DFS 铺平树为渲染行，前缀沿用 pi 的 │/├─/└─ 连接线风格。 */
function flattenTree(root: ChatBranchTreeNode): FlatNode[] {
  const out: FlatNode[] = [];
  const walk = (node: ChatBranchTreeNode, ancestorPrefix: string, isLast: boolean, isRoot: boolean) => {
    out.push({
      node,
      prefix: isRoot ? '' : `${ancestorPrefix}${isLast ? '└─ ' : '├─ '}`,
    });
    const childPrefix = isRoot ? '' : `${ancestorPrefix}${isLast ? '   ' : '│  '}`;
    node.children.forEach((child, index) =>
      walk(child, childPrefix, index === node.children.length - 1, false),
    );
  };
  walk(root, '', true, true);
  return out;
}

export function SessionTreeModal({ chatId, onClose, onBranchSwitched }: SessionTreeModalProps) {
  const [data, setData] = useState<ChatBranchTreeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getChatBranchTree(chatId)
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch(() => {
        if (!cancelled) setData(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [chatId]);

  const rows = useMemo(() => (data?.tree ? flattenTree(data.tree) : []), [data]);
  // 无分支 = 树只有当前记录一个节点（还没有任何 regenerate fork）。
  const isEmpty = !loading && (!data?.tree || rows.length <= 1);

  useEffect(() => {
    if (!data) return;
    const activeIndex = rows.findIndex((r) => r.node.recordId === data.activeRecordId);
    setSelectedIndex(activeIndex >= 0 ? activeIndex : 0);
  }, [data, rows]);

  const switchTo = async (recordId: string) => {
    if (!data || switching || recordId === data.activeRecordId) return;
    setSwitching(true);
    try {
      await activateChatBranch(chatId, recordId);
      onBranchSwitched?.();
      onClose();
    } catch {
      // 切换失败（分支族外记录 / sidecar 离线）——模态保持打开，用户可重试。
    } finally {
      setSwitching(false);
    }
  };

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
        return;
      }
      if (rows.length === 0) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedIndex((i) => Math.min(i + 1, rows.length - 1));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const row = rows[selectedIndex];
        if (row) void switchTo(row.node.recordId);
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
    // switchTo 依赖 data/switching，每次渲染重建监听即可（模态生命周期短）。
  });

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="会话分支树"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      data-session-tree-modal
    >
      <div className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-agent-lg border border-agent-border bg-agent-canvas shadow-xl">
        <div className="flex items-center gap-2 border-b border-agent-border px-3 py-2">
          <LuGitBranch className="h-4 w-4 shrink-0 text-agent-muted-foreground" />
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-agent-foreground">
            会话分支树
            {data && data.nodeCount > 1 && (
              <span className="ml-1.5 text-xs font-normal text-agent-muted-foreground">
                {data.nodeCount} 个分支
              </span>
            )}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground"
            aria-label="关闭"
          >
            <LuX className="h-3.5 w-3.5" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-2">
          {loading ? (
            <div className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-agent-muted-foreground">
              <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
              正在加载分支…
            </div>
          ) : isEmpty ? (
            <div className="px-3 py-6 text-center text-xs text-agent-muted-foreground">
              暂无分支 — 重新生成回复后，旧版本会保留在这里。
            </div>
          ) : (
            rows.map((row, index) => {
              const active = row.node.recordId === data?.activeRecordId;
              const selected = index === selectedIndex;
              return (
                <button
                  key={row.node.recordId}
                  type="button"
                  onClick={() => void switchTo(row.node.recordId)}
                  onMouseEnter={() => setSelectedIndex(index)}
                  aria-current={active ? 'true' : undefined}
                  aria-disabled={switching || active || undefined}
                  className={`flex w-full items-center gap-1 rounded px-2 py-1.5 text-left text-xs transition-colors ${
                    active
                      ? 'cursor-default font-medium text-agent-foreground'
                      : 'text-agent-muted-foreground hover:text-agent-foreground'
                  } ${selected ? 'bg-agent-foreground/5' : ''} ${
                    switching ? 'pointer-events-none opacity-60' : ''
                  }`}
                  data-tree-row
                  data-active={active || undefined}
                >
                  {row.prefix && (
                    <span className="shrink-0 whitespace-pre font-mono text-agent-muted-foreground/60">
                      {row.prefix}
                    </span>
                  )}
                  <span className="min-w-0 flex-1 truncate">
                    {row.node.label || '(空分支)'}
                  </span>
                  {active && (
                    <LuCheck className="h-3 w-3 shrink-0 text-emerald-600 dark:text-emerald-400" />
                  )}
                </button>
              );
            })
          )}
        </div>

        {!loading && !isEmpty && (
          <div className="border-t border-agent-border px-3 py-1.5 text-[10px] text-agent-muted-foreground/80">
            {data?.truncated
              ? '分支过多，树已被截断（仅显示部分节点）。'
              : '点击切换分支；↑↓ 移动，Enter 切换，Esc 关闭。'}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

export default SessionTreeModal;

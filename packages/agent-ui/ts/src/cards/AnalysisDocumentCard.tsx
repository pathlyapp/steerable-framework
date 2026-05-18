/**
 * `<AnalysisDocumentCard />`
 *
 * Long-form markdown document card.
 *
 * The framework deliberately does not pull in `react-markdown` / `remark-gfm` /
 * `github-markdown-css` -- the consumer must pass a `renderMarkdown` slot (or
 * a `Markdown` child) to do the actual rendering. Default fallback is a `<pre>`
 * block.
 *
 * Header variants:
 *  - `inline` (default): title sits inline with the document icon.
 *  - `strip`: title becomes a `bg-muted/50` strip on top, with the icon hidden.
 *
 * Footer slot lets host apps attach an actions bar (deeppath uses this for
 * "转化为行动 / 保存到笔记 / 保存为 PDF / 深入研究 / 复制 / 模型 + 时间").
 */
import * as React from 'react';
import type { AnalysisDocumentPayload } from '@steerable/agent-protocol';
import { BookIcon } from './icons.js';
import { asReactNode, type RenderSlotResult } from './types.js';

export type AnalysisDocumentHeaderVariant = 'inline' | 'strip';

export interface AnalysisDocumentCardProps {
  payload: AnalysisDocumentPayload;
  isComplete?: boolean;
  renderMarkdown?: (body: string) => RenderSlotResult;
  renderFooter?: () => RenderSlotResult;
  className?: string;
  bodyClassName?: string;
  headerVariant?: AnalysisDocumentHeaderVariant;
}

export const AnalysisDocumentCard: React.FC<AnalysisDocumentCardProps> = ({
  payload,
  renderMarkdown,
  renderFooter,
  className,
  bodyClassName,
  headerVariant = 'inline',
}) => {
  return (
    <article
      className={[
        'steerable-analysis-document overflow-hidden rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] text-sm text-[var(--agent-foreground,#111827)]',
        className,
      ].filter(Boolean).join(' ')}
    >
      {payload.title && headerVariant === 'strip' ? (
        <div className="border-b border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-muted,#f3f4f6)]/50 px-4 py-2">
          <span className="block truncate text-xs font-medium text-[var(--agent-muted-foreground,#6b7280)]">
            {payload.title}
          </span>
        </div>
      ) : (
        <header className="mb-3 flex items-center gap-2 border-b border-[var(--agent-border,#e5e7eb)] px-4 pt-3 pb-2">
          <BookIcon size={14} className="text-[var(--agent-muted-foreground,#6b7280)]" />
          <h3 className="text-sm font-medium">{payload.title || '分析报告'}</h3>
          {payload.modelId && (
            <span className="ml-auto rounded-full bg-[var(--agent-muted,#f3f4f6)] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[var(--agent-muted-foreground,#6b7280)]">
              {payload.modelId}
            </span>
          )}
        </header>
      )}

      <div className={['p-4', bodyClassName].filter(Boolean).join(' ')}>
        {renderMarkdown ? (
          asReactNode(renderMarkdown(payload.body))
        ) : (
          <pre className="whitespace-pre-wrap break-words font-sans">{payload.body}</pre>
        )}
      </div>

      {renderFooter && asReactNode(renderFooter())}
    </article>
  );
};

export default AnalysisDocumentCard;

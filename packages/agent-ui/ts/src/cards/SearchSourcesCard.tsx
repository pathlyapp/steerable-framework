/**
 * `<SearchSourcesCard />`
 *
 * Renders a row of stacked favicons summarising the web sources an agent
 * consulted, plus an expandable detail panel listing each `(title, domain,
 * snippet)` triple.
 *
 * Appearance variants:
 *  - `card` (default): bordered card chrome with a divided expanded list.
 *  - `inline`: no chrome around the trigger; expanded items use a borderless
 *    `:hover` background. Matches the deeppath "above the input" aesthetic.
 *
 * Slots:
 *  - `renderSummary(count, more)` -- replace the default "{count} 个来源" label.
 *  - `faviconUrlFor(url)` -- replace the default favicon resolver.
 */
import * as React from 'react';
import type { SearchSourcesPayload } from '@steerable/agent-protocol';
import { GlobeIcon, ChevronDownIcon, ExternalLinkIcon } from './icons.js';

export type SearchSourcesCardAppearance = 'card' | 'inline';

export interface SearchSourcesCardProps {
  payload: SearchSourcesPayload;
  className?: string;
  appearance?: SearchSourcesCardAppearance;
  faviconUrlFor?: (url: string) => string | null;
  renderSummary?: (uniqueCount: number, overflowCount: number) => React.ReactNode;
}

const MAX_STACKED = 5;

function extractDomain(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

function defaultFaviconUrl(url: string): string | null {
  try {
    const host = new URL(url).hostname;
    return `https://www.google.com/s2/favicons?sz=64&domain=${host}`;
  } catch {
    return null;
  }
}

function FaviconCircle({ src, size = 18 }: { src: string | null; size?: number }) {
  const [failed, setFailed] = React.useState(false);
  const showFallback = !src || failed;

  if (showFallback) {
    return (
      <span
        className="inline-flex items-center justify-center rounded-full border-2 border-[var(--agent-background,#fff)] bg-[var(--agent-muted,#f3f4f6)]"
        style={{ width: size, height: size }}
      >
        <GlobeIcon
          size={Math.round(size * 0.55)}
          className="text-[var(--agent-muted-foreground,#6b7280)]"
        />
      </span>
    );
  }

  return (
    <img
      src={src}
      alt=""
      width={size}
      height={size}
      className="rounded-full border-2 border-[var(--agent-background,#fff)] bg-[var(--agent-background,#fff)] object-cover"
      style={{ width: size, height: size }}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

export const SearchSourcesCard: React.FC<SearchSourcesCardProps> = ({
  payload,
  className,
  appearance = 'card',
  faviconUrlFor = defaultFaviconUrl,
  renderSummary,
}) => {
  const [expanded, setExpanded] = React.useState(false);
  const sources = payload.sources ?? [];

  const unique = React.useMemo(() => {
    const seen = new Set<string>();
    return sources.filter((s) => {
      if (!s?.url || seen.has(s.url)) return false;
      seen.add(s.url);
      return true;
    });
  }, [sources]);

  if (unique.length === 0) return null;

  const stacked = unique.slice(0, MAX_STACKED);
  const more = unique.length - stacked.length;

  const isCard = appearance === 'card';

  const summaryLabel = renderSummary
    ? renderSummary(unique.length, more)
    : isCard
      ? <>
          {unique.length} 个来源{more > 0 ? `（+${more}）` : ''}
        </>
      : <>搜索了 {unique.length} 个网站</>;

  return (
    <div
      className={[
        'steerable-search-sources',
        isCard
          ? 'rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)]'
          : '',
        className,
      ].filter(Boolean).join(' ')}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className={
          isCard
            ? 'flex w-full items-center justify-between gap-2 px-3 py-2 text-left'
            : 'group flex items-center gap-2 rounded-md py-1 text-sm text-[var(--agent-muted-foreground,#6b7280)] transition-colors duration-150 hover:text-[var(--agent-foreground,#111827)]'
        }
      >
        <span className={isCard ? 'flex items-center gap-2' : 'flex items-center'}>
          {isCard ? (
            <>
              <span className="flex -space-x-2">
                {stacked.map((s, idx) => (
                  <FaviconCircle key={`${idx}-${s.url}`} src={s.favicon ?? faviconUrlFor(s.url)} />
                ))}
              </span>
              <span className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                {summaryLabel}
              </span>
            </>
          ) : (
            <>
              <span className="flex" style={{ paddingLeft: 0 }}>
                {stacked.map((s, i) => (
                  <span
                    key={`${i}-${s.url}`}
                    className="inline-block"
                    style={{ marginLeft: i === 0 ? 0 : -6, zIndex: MAX_STACKED - i }}
                  >
                    <FaviconCircle src={s.favicon ?? faviconUrlFor(s.url)} />
                  </span>
                ))}
                {more > 0 && (
                  <span
                    className="inline-flex items-center justify-center rounded-full border-2 border-[var(--agent-background,#fff)] bg-[var(--agent-muted,#f3f4f6)] text-[10px] font-medium text-[var(--agent-muted-foreground,#6b7280)]"
                    style={{ width: 18, height: 18, marginLeft: -6, zIndex: 0 }}
                  >
                    +{more}
                  </span>
                )}
              </span>
            </>
          )}
        </span>
        {!isCard && <span className="ml-2">{summaryLabel}</span>}
        <ChevronDownIcon
          size={isCard ? 14 : 14}
          className={[
            isCard ? 'text-[var(--agent-muted-foreground,#6b7280)]' : 'h-3.5 w-3.5',
            'transition-transform duration-200',
            expanded ? 'rotate-180' : '',
          ].filter(Boolean).join(' ')}
        />
      </button>
      {expanded && (
        isCard ? (
          <ul className="divide-y divide-[var(--agent-border,#e5e7eb)] border-t border-[var(--agent-border,#e5e7eb)]">
            {unique.map((s, idx) => (
              <li key={`${idx}-${s.url}`} className="px-3 py-2 text-sm">
                <a
                  href={s.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-start gap-2 text-[var(--agent-foreground,#111827)] hover:underline"
                >
                  <FaviconCircle src={s.favicon ?? faviconUrlFor(s.url)} size={16} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{s.title || extractDomain(s.url)}</span>
                    <span className="block truncate text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                      {extractDomain(s.url)}
                    </span>
                    {s.snippet && (
                      <span className="mt-1 block text-xs text-[var(--agent-muted-foreground,#6b7280)] line-clamp-2">
                        {s.snippet}
                      </span>
                    )}
                  </span>
                  <ExternalLinkIcon
                    size={12}
                    className="mt-1 flex-shrink-0 text-[var(--agent-muted-foreground,#6b7280)]"
                  />
                </a>
              </li>
            ))}
          </ul>
        ) : (
          <div className="mt-1.5 flex flex-col gap-0.5">
            {unique.map((s, idx) => (
              <a
                key={`${idx}-${s.url}`}
                href={s.url}
                target="_blank"
                rel="noopener noreferrer"
                className="group/item flex items-center gap-2.5 rounded-md px-2 py-1.5 transition-colors duration-150 hover:bg-[var(--agent-muted,#f3f4f6)]"
              >
                <FaviconCircle src={s.favicon ?? faviconUrlFor(s.url)} size={16} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs text-[var(--agent-foreground,#111827)]">
                    {s.title || extractDomain(s.url)}
                  </div>
                  <div className="truncate text-[11px] text-[var(--agent-muted-foreground,#6b7280)]">
                    {extractDomain(s.url)}
                  </div>
                </div>
                <ExternalLinkIcon
                  size={12}
                  className="shrink-0 text-[var(--agent-muted-foreground,#6b7280)] opacity-0 transition-opacity group-hover/item:opacity-100"
                />
              </a>
            ))}
          </div>
        )
      )}
    </div>
  );
};

export default SearchSourcesCard;

import { useEffect, useMemo, useRef, useState } from 'react';
import { cn } from './cn.js';

/**
 * `ModelSelector` — model + reasoning-effort picker backed by the gateway's
 * live catalog.
 *
 * The sidecar's `models.list` RPC answers the ids the gateway actually
 * accepts, each joined with catalog capabilities (window, modalities,
 * reasoning levels, pricing). The catalog is discovery, not a routing
 * whitelist: the current model stays selectable even when unlisted, and the
 * offline state degrades to a badge instead of blocking the composer.
 *
 * Stability contract (same as `useAgentSession`): `transport` is held by ref
 * and the listing refetches only when `baseUrl` changes — call the returned
 * refresh path by remounting or changing `baseUrl` if the transport swapped.
 */

export interface ModelCatalogEntry {
  id: string;
  name: string;
  /** Context window in tokens; null when neither gateway nor catalog knows. */
  window: number | null;
  modalities: string[];
  /** Supported reasoning-effort levels, canonical order; empty = no knob. */
  reasoningLevels: string[];
  pricing: { promptPerMtok: number | null; completionPerMtok: number | null } | null;
  /** Catalog key that supplied capability fields; null = capabilities unknown. */
  joinedFrom: string | null;
  capabilities: 'known' | 'unknown';
}

export interface ModelCatalogResponse {
  models: ModelCatalogEntry[];
  catalogStatus: 'live' | 'stale' | 'offline';
  /** Epoch seconds of the successful fetch the listing came from. */
  fetchedAt?: number;
  /** Present when catalogStatus is 'offline'. */
  error?: string;
  current?: { model: string | null; reasoningEffort: string | null };
}

export interface ModelCatalogTransport {
  listModels: (params?: { baseUrl?: string }) => Promise<ModelCatalogResponse>;
}

export interface ModelSelectorProps {
  transport: ModelCatalogTransport;
  /** Gateway base URL the sidecar queries; omit to use the sidecar's env. */
  baseUrl?: string;
  /** Currently selected model id (controlled). */
  model: string;
  /** Currently selected reasoning effort; null/undefined = no explicit request. */
  reasoningEffort?: string | null;
  onSelectModel: (id: string) => void;
  onSelectEffort?: (effort: string | null) => void;
  disabled?: boolean;
  className?: string;
}

const selectClass =
  'h-7 max-w-[180px] rounded-agent-md border border-agent-border bg-agent-canvas px-2 text-xs text-agent-foreground outline-none transition-colors hover:bg-agent-muted disabled:cursor-not-allowed disabled:opacity-50';

export function ModelSelector(props: ModelSelectorProps) {
  const {
    transport,
    baseUrl,
    model,
    reasoningEffort,
    onSelectModel,
    onSelectEffort,
    disabled = false,
    className,
  } = props;

  const transportRef = useRef(transport);
  transportRef.current = transport;
  const [catalog, setCatalog] = useState<ModelCatalogResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    transportRef.current
      .listModels(baseUrl ? { baseUrl } : undefined)
      .then((response) => {
        if (!cancelled) setCatalog(response);
      })
      .catch(() => {
        // A transport-level failure (sidecar gone, RPC error) degrades to the
        // same offline state the sidecar itself reports for an unreachable
        // gateway — the composer stays usable.
        if (!cancelled) {
          setCatalog({ models: [], catalogStatus: 'offline' });
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [baseUrl]);

  const entries = useMemo(() => {
    const list = catalog?.models ?? [];
    // Discovery, not a whitelist: the active model stays selectable even
    // when the gateway listing does not contain it (catalog offline, or an
    // id the gateway accepts but does not advertise).
    if (model && !list.some((entry) => entry.id === model)) {
      return [
        {
          id: model,
          name: model,
          window: null,
          modalities: [],
          reasoningLevels: [],
          pricing: null,
          joinedFrom: null,
          capabilities: 'unknown' as const,
        },
        ...list,
      ];
    }
    return list;
  }, [catalog, model]);

  const selected = entries.find((entry) => entry.id === model);

  if (loading) {
    return (
      <div
        className={cn(
          'inline-flex h-7 items-center rounded-agent-md border border-agent-border bg-agent-canvas px-2 text-xs text-agent-muted-foreground',
          className,
        )}
        aria-live="polite"
      >
        Loading models…
      </div>
    );
  }

  const offline = catalog?.catalogStatus === 'offline';

  return (
    <div
      className={cn('inline-flex items-center gap-1.5', className)}
      data-catalog-status={catalog?.catalogStatus ?? 'unknown'}
    >
      <select
        aria-label="Model"
        className={cn(selectClass, 'min-w-[8rem]')}
        value={model}
        disabled={disabled || offline}
        onChange={(event) => onSelectModel(event.target.value)}
        title={
          offline
            ? `Model catalog unavailable — using the configured model. ${catalog?.error ?? ''}`
            : selected
              ? `${selected.name}${selected.window ? ` — ${Math.round(selected.window / 1024)}k window` : ''}${selected.capabilities === 'unknown' ? ' — capabilities unknown' : ''}`
              : model
        }
      >
        {entries.map((entry) => (
          <option key={entry.id} value={entry.id}>
            {entry.id}
            {entry.capabilities === 'unknown' ? ' (?)' : ''}
          </option>
        ))}
        {entries.length === 0 && model ? <option value={model}>{model}</option> : null}
      </select>

      {selected && selected.reasoningLevels.length > 0 && onSelectEffort ? (
        <select
          aria-label="Reasoning effort"
          className={selectClass}
          value={reasoningEffort ?? ''}
          disabled={disabled}
          onChange={(event) => onSelectEffort(event.target.value || null)}
          title="Reasoning effort — an unsupported level is rejected by the sidecar, never silently dropped"
        >
          <option value="">default</option>
          {selected.reasoningLevels.map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
      ) : null}

      {offline ? (
        <span
          className="inline-flex items-center rounded-agent-md border border-agent-border bg-agent-muted px-1.5 py-0.5 text-[10px] text-agent-muted-foreground"
          title={catalog?.error ?? 'Model catalog unavailable'}
        >
          catalog offline
        </span>
      ) : catalog?.catalogStatus === 'stale' ? (
        <span
          className="inline-flex items-center rounded-agent-md border border-agent-border bg-agent-muted px-1.5 py-0.5 text-[10px] text-agent-muted-foreground"
          title="Refresh failed; showing the previous listing"
        >
          stale
        </span>
      ) : null}
    </div>
  );
}

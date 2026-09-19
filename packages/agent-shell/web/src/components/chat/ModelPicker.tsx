import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { LuChevronDown, LuRefreshCw } from 'react-icons/lu';
import { ModelCapabilityChips } from '@/components/settings/ModelCapabilityChips';
import {
  getLlmModels,
  getLlmSettings,
  type GatewayModelCatalog,
  type GatewayModelEntry,
} from '@/lib/local-api';

/**
 * ModelPicker — 聊天输入框的模型/档位选择器。
 *
 * 数据源是网关活目录（`GET /api/v2/llm/models` → sidecar `models.list`）：
 * 网关真正接受的 id，经 models.dev 能力表 join 出 reasoning 档位。目录
 * 只做发现、不做路由白名单——设置里的模型即使不在目录里也照常显示、
 * 照常可发（capabilities: 'unknown' 只是没有档位可选）。
 *
 * 目录状态按载荷如实披露：live 不标；stale 标「缓存」；offline 标
 * 「目录不可用」（此时选择器仍可用，只是没有目录行）。
 *
 * 选择语义：model 为 null = 跟随全局设置（LocalLlmSettingsModal）；
 * effort 为 null = 自动（厂商预制/服务端默认）。显式选择的档位随每轮
 * 请求下发，sidecar 严格校验——模型不支持的档位在流启动时就报错，
 * 不会静默丢弃。
 */

export interface ModelPickerProps {
  /** 当前每轮覆盖的模型；null = 跟随全局设置。 */
  model: string | null;
  /** 当前每轮 reasoning 档位；null = 自动。 */
  reasoningEffort: string | null;
  onSelectModel: (model: string | null) => void;
  onSelectEffort: (effort: string | null) => void;
  disabled?: boolean;
}

export function ModelPicker({
  model,
  reasoningEffort,
  onSelectModel,
  onSelectEffort,
  disabled = false,
}: ModelPickerProps) {
  const [catalog, setCatalog] = useState<GatewayModelCatalog | null>(null);
  const [settingsModel, setSettingsModel] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [effortMenuOpen, setEffortMenuOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [modelsResult, settingsResult] = await Promise.all([
        getLlmModels(),
        getLlmSettings(),
      ]);
      setCatalog(modelsResult);
      setSettingsModel(settingsResult.model);
    } catch {
      // 桥不可达（浏览器预览等）：视同目录不可用，选择器退化为纯展示。
      setCatalog({ models: [], catalogStatus: 'offline' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 点击组件外部时收起两个弹层。
  useEffect(() => {
    if (!modelMenuOpen && !effortMenuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setModelMenuOpen(false);
        setEffortMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [modelMenuOpen, effortMenuOpen]);

  const effectiveModel = model ?? settingsModel;
  const selectedEntry: GatewayModelEntry | undefined = useMemo(
    () => catalog?.models.find((entry) => entry.id === effectiveModel),
    [catalog, effectiveModel],
  );
  const reasoningLevels = selectedEntry?.reasoningLevels ?? [];

  const status = catalog?.catalogStatus ?? 'offline';
  const statusBadge =
    status === 'offline' ? (
      <span className="inline-flex items-center gap-1 rounded-full border border-amber-400/50 bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">
        目录不可用
      </span>
    ) : status === 'stale' ? (
      <span className="inline-flex items-center gap-1 rounded-full border border-agent-border bg-agent-muted px-1.5 py-0.5 text-[10px] text-agent-muted-foreground">
        缓存
      </span>
    ) : null;

  return (
    <div ref={rootRef} className="flex min-w-0 items-center gap-1">
      {/* flex + min-w-0 截断链：窄容器下模型 id 省略号收缩，而不是把
          旁边的圆形图标按钮压扁。 */}
      <div className="relative flex min-w-0">
        <button
          type="button"
          onClick={() => {
            const next = !modelMenuOpen;
            setModelMenuOpen(next);
            setEffortMenuOpen(false);
            // 打开时若目录不可用/还没拉到，重试一次（网关可能刚恢复）。
            if (next && status !== 'live' && !loading) void refresh();
          }}
          disabled={disabled}
          className="inline-flex h-6 min-w-0 max-w-[160px] items-center gap-1 rounded-full border border-agent-border bg-agent-canvas px-1.5 text-[12px] leading-[1.45] text-agent-foreground transition-colors hover:bg-agent-foreground/5 disabled:cursor-not-allowed disabled:opacity-70 @sm:max-w-[220px]"
          title={
            status === 'offline'
              ? `模型目录不可用${catalog?.error ? `：${catalog.error}` : ''}（仍可手动指定模型）`
              : `当前模型：${effectiveModel || '未配置'}`
          }
          aria-haspopup="menu"
          aria-expanded={modelMenuOpen}
        >
          <span className="min-w-0 truncate">{effectiveModel || '选择模型'}</span>
          {model != null && (
            <span className="inline-flex shrink-0 items-center rounded bg-agent-foreground/10 px-1 py-0.5 text-[9px] font-medium text-agent-foreground">
              本会话
            </span>
          )}
          <LuChevronDown className="h-3 w-3 shrink-0 text-agent-muted-foreground" />
        </button>

        {modelMenuOpen && (
          <div
            role="menu"
            className="absolute bottom-full left-0 z-50 mb-1 max-h-72 w-80 overflow-y-auto rounded-agent-md border border-agent-border bg-agent-canvas p-1 shadow-lg"
          >
            <div className="flex items-center justify-between gap-2 px-2 py-1">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-agent-muted-foreground">
                模型（网关目录）
              </span>
              <span className="flex items-center gap-1">
                {statusBadge}
                <button
                  type="button"
                  onClick={() => void refresh()}
                  className="flex h-4 w-4 items-center justify-center rounded text-agent-muted-foreground transition-colors hover:text-agent-foreground"
                  title="刷新目录"
                  aria-label="刷新目录"
                >
                  <LuRefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
                </button>
              </span>
            </div>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                onSelectModel(null);
                setModelMenuOpen(false);
              }}
              className={[
                'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] leading-[1.45] transition-colors',
                model == null
                  ? 'bg-agent-foreground/10 text-agent-foreground'
                  : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
              ].join(' ')}
            >
              <span className="min-w-0 flex-1 truncate">
                跟随设置{settingsModel ? `（${settingsModel}）` : ''}
              </span>
            </button>
            {catalog?.models.map((entry) => {
              const isActive = entry.id === effectiveModel && model != null;
              const displayName =
                entry.name && entry.name !== entry.id ? entry.name : null;
              return (
                <button
                  key={entry.id}
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    onSelectModel(entry.id);
                    // 换模型后旧档位未必支持：清掉让 sidecar 按新模型的
                    // 目录校验，而不是把旧档位硬发给新模型。
                    onSelectEffort(null);
                    setModelMenuOpen(false);
                  }}
                  className={[
                    'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] leading-[1.45] transition-colors',
                    isActive
                      ? 'bg-agent-foreground/10 text-agent-foreground'
                      : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
                  ].join(' ')}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{entry.id}</span>
                    {(displayName || entry.capabilities) && (
                      <span className="mt-0.5 flex min-w-0 flex-wrap items-center gap-1">
                        {displayName ? (
                          <span className="truncate text-[10px] text-agent-muted-foreground">
                            {displayName}
                          </span>
                        ) : null}
                        <ModelCapabilityChips entry={entry} />
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
            {catalog != null && catalog.models.length === 0 && (
              <div className="px-2 py-2 text-[11px] text-agent-muted-foreground">
                目录为空——{status === 'offline' ? '网关不可达，仍按设置里的模型发送' : '网关未报告任何模型'}
              </div>
            )}
          </div>
        )}
      </div>

      {reasoningLevels.length > 0 && (
        <div className="relative">
          <button
            type="button"
            onClick={() => {
              setEffortMenuOpen(!effortMenuOpen);
              setModelMenuOpen(false);
            }}
            disabled={disabled}
            className="inline-flex h-6 items-center gap-1 rounded-full border border-agent-border bg-agent-canvas px-1.5 text-[12px] leading-[1.45] text-agent-foreground transition-colors hover:bg-agent-foreground/5 disabled:cursor-not-allowed disabled:opacity-70"
            title="推理档位（随每轮请求下发；模型不支持会直接报错）"
            aria-haspopup="menu"
            aria-expanded={effortMenuOpen}
          >
            <span className="truncate">{reasoningEffort ?? '自动'}</span>
            <LuChevronDown className="h-3 w-3 shrink-0 text-agent-muted-foreground" />
          </button>
          {effortMenuOpen && (
            <div
              role="menu"
              className="absolute bottom-full left-0 z-50 mb-1 w-36 overflow-y-auto rounded-agent-md border border-agent-border bg-agent-canvas p-1 shadow-lg"
            >
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  onSelectEffort(null);
                  setEffortMenuOpen(false);
                }}
                className={[
                  'flex w-full rounded px-2 py-1.5 text-left text-[12px] leading-[1.45] transition-colors',
                  reasoningEffort == null
                    ? 'bg-agent-foreground/10 text-agent-foreground'
                    : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
                ].join(' ')}
              >
                自动
              </button>
              {reasoningLevels.map((level) => (
                <button
                  key={level}
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    onSelectEffort(level);
                    setEffortMenuOpen(false);
                  }}
                  className={[
                    'flex w-full rounded px-2 py-1.5 text-left text-[12px] leading-[1.45] transition-colors',
                    reasoningEffort === level
                      ? 'bg-agent-foreground/10 text-agent-foreground'
                      : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
                  ].join(' ')}
                >
                  {level}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default ModelPicker;

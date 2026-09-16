import type { GatewayModelEntry } from '@/lib/local-api';

/**
 * 设置页 / 聊天模型选择器共用的能力展示。
 *
 * 只把 `capabilities === 'known'`（models.dev 叶节点 join 上）当成核实过的
 * 事实。`unknown` 时 wire 仍可能带上前缀启发式的 window / text 模态，那些
 * 不能画成「思考」「图像」。文本是默认输入，不单独占一枚筹码。
 */

export interface ModelCapabilityChip {
  key: string;
  label: string;
  title: string;
  kind: 'reasoning' | 'modality' | 'window' | 'unknown';
}

const MODALITY_ORDER = ['image', 'pdf', 'audio', 'video'] as const;

const MODALITY_LABELS: Record<(typeof MODALITY_ORDER)[number], string> = {
  image: '图像',
  pdf: 'PDF',
  audio: '音频',
  video: '视频',
};

const MODALITY_TITLES: Record<(typeof MODALITY_ORDER)[number], string> = {
  image: '支持图像输入',
  pdf: '支持 PDF 输入',
  audio: '支持音频输入',
  video: '支持视频输入',
};

/** 上下文窗口：1_000_000 → 1M，131_072 → 131K。 */
export function formatContextWindow(window: number | null | undefined): string | null {
  if (window == null || window <= 0) return null;
  if (window >= 1_000_000) {
    const millions = Number((window / 1_000_000).toFixed(1));
    return `${Number.isInteger(millions) ? String(millions) : millions.toFixed(1)}M`;
  }
  if (window >= 1_000) return `${Math.round(window / 1_000)}K`;
  return String(window);
}

function unknownChip(): ModelCapabilityChip {
  return {
    key: 'unknown',
    label: '未识别',
    title: '网关目录未匹配到该 id，思考档位与多模态未核实。',
    kind: 'unknown',
  };
}

/**
 * @param detail 选中摘要用：思考筹码带上档位，如 `思考（high / max）`。
 */
export function modelCapabilityChips(
  entry: GatewayModelEntry | null | undefined,
  options?: { detail?: boolean },
): ModelCapabilityChip[] {
  if (!entry) return [];
  if (entry.capabilities === 'unknown') return [unknownChip()];
  if (entry.capabilities !== 'known') return [];

  const chips: ModelCapabilityChip[] = [];
  const levels = entry.reasoningLevels ?? [];
  if (levels.length > 0) {
    const joined = levels.join(' / ');
    chips.push({
      key: 'reasoning',
      label: options?.detail ? `思考（${joined}）` : '思考',
      title: `支持思考档位：${joined}`,
      kind: 'reasoning',
    });
  }

  const seen = new Set<string>();
  for (const modality of MODALITY_ORDER) {
    if (!(entry.modalities ?? []).includes(modality)) continue;
    seen.add(modality);
    chips.push({
      key: modality,
      label: MODALITY_LABELS[modality],
      title: MODALITY_TITLES[modality],
      kind: 'modality',
    });
  }
  for (const modality of entry.modalities ?? []) {
    const key = modality.toLowerCase();
    if (key === 'text' || seen.has(key) || key in MODALITY_LABELS) continue;
    seen.add(key);
    chips.push({
      key: key,
      label: modality,
      title: `输入模态：${modality}`,
      kind: 'modality',
    });
  }

  const windowLabel = formatContextWindow(entry.window);
  if (windowLabel) {
    chips.push({
      key: 'window',
      label: windowLabel,
      title: `上下文窗口 ${entry.window} tokens`,
      kind: 'window',
    });
  }
  return chips;
}

export function unknownGatewayEntry(id: string): GatewayModelEntry {
  return {
    id,
    name: null,
    window: null,
    modalities: [],
    reasoningLevels: [],
    pricing: null,
    joinedFrom: null,
    capabilities: 'unknown',
  };
}

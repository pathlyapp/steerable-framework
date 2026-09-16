import type { GatewayModelEntry } from '@/lib/local-api';
import {
  modelCapabilityChips,
  type ModelCapabilityChip,
} from '@/components/settings/model-capabilities';

const CHIP_CLASS: Record<ModelCapabilityChip['kind'], string> = {
  reasoning:
    'border-agent-border bg-agent-foreground/5 text-agent-foreground',
  modality: 'border-agent-border bg-agent-canvas text-agent-foreground',
  window: 'border-agent-border/80 text-agent-muted-foreground',
  unknown: 'border-agent-border text-agent-muted-foreground',
};

interface ModelCapabilityChipsProps {
  entry: GatewayModelEntry | null | undefined;
  /** 选中摘要：思考筹码带档位。 */
  detail?: boolean;
  className?: string;
}

export function ModelCapabilityChips({
  entry,
  detail = false,
  className,
}: ModelCapabilityChipsProps) {
  const chips = modelCapabilityChips(entry, { detail });
  if (chips.length === 0) return null;
  return (
    <span className={['inline-flex flex-wrap items-center gap-1', className].filter(Boolean).join(' ')}>
      {chips.map((chip) => (
        <span
          key={chip.key}
          data-capability={chip.key}
          title={chip.title}
          className={[
            'inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] leading-none',
            CHIP_CLASS[chip.kind],
          ].join(' ')}
        >
          {chip.label}
        </span>
      ))}
    </span>
  );
}

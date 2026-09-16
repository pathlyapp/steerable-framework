import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { LuChevronDown } from 'react-icons/lu';
import { ModelCapabilityChips } from '@/components/settings/ModelCapabilityChips';
import type { ModelPickerRow } from '@/components/settings/llm-vendors';

interface ModelIdComboboxProps {
  value: string;
  options: Array<string | ModelPickerRow>;
  placeholder?: string;
  onChange: (value: string) => void;
}

interface MenuBox {
  top: number;
  left: number;
  width: number;
}

function toRow(option: string | ModelPickerRow): ModelPickerRow {
  return typeof option === 'string' ? { id: option, entry: null } : option;
}

/**
 * 可手填的模型 id 选择器。不用原生 datalist：Electron / 滚动容器里
 * 系统建议层会相对输入框错位，也无法跟主题对齐。
 */
export function ModelIdCombobox({
  value,
  options,
  placeholder,
  onChange,
}: ModelIdComboboxProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const [menuBox, setMenuBox] = useState<MenuBox | null>(null);

  const rows = useMemo(() => options.map(toRow), [options]);

  const visible = useMemo(() => {
    const q = value.trim().toLowerCase();
    if (!q || rows.some((row) => row.id === value)) return rows;
    return rows.filter((row) => {
      if (row.id.toLowerCase().includes(q)) return true;
      const name = entryName(row);
      return name != null && name.toLowerCase().includes(q);
    });
  }, [rows, value]);

  const syncMenuBox = () => {
    const el = rootRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setMenuBox({ top: r.bottom + 4, left: r.left, width: r.width });
  };

  const show = () => {
    setOpen(true);
    syncMenuBox();
  };

  useEffect(() => {
    if (!open) return;
    syncMenuBox();
    const onLayout = () => syncMenuBox();
    window.addEventListener('resize', onLayout);
    document.addEventListener('scroll', onLayout, true);
    return () => {
      window.removeEventListener('resize', onLayout);
      document.removeEventListener('scroll', onLayout, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (rootRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopImmediatePropagation();
      setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    window.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      window.removeEventListener('keydown', onKey, true);
    };
  }, [open]);

  const menu =
    open && visible.length > 0 && menuBox && typeof document !== 'undefined'
      ? createPortal(
          <ul
            ref={menuRef}
            role="listbox"
            data-testid="llm-model-options"
            style={{ top: menuBox.top, left: menuBox.left, width: menuBox.width }}
            className="fixed z-[200] max-h-72 overflow-y-auto rounded-agent-md border border-agent-border bg-agent-canvas py-1 shadow-lg"
          >
            {visible.map((row) => {
              const selected = row.id === value;
              const name = entryName(row);
              return (
                <li key={row.id}>
                  <button
                    type="button"
                    role="option"
                    aria-label={row.id}
                    aria-selected={selected}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => {
                      onChange(row.id);
                      setOpen(false);
                    }}
                    className={[
                      'flex w-full flex-col gap-1 px-3 py-1.5 text-left text-sm',
                      selected
                        ? 'bg-agent-foreground/10 text-agent-foreground'
                        : 'text-agent-foreground hover:bg-agent-foreground/5',
                    ].join(' ')}
                  >
                    <span className="truncate">{row.id}</span>
                    {(name || row.entry) && (
                      <span className="flex min-w-0 flex-wrap items-center gap-1">
                        {name ? (
                          <span className="truncate text-[11px] text-agent-muted-foreground">
                            {name}
                          </span>
                        ) : null}
                        <ModelCapabilityChips entry={row.entry} />
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>,
          document.body,
        )
      : null;

  return (
    <div ref={rootRef} className="relative">
      <input
        type="text"
        data-testid="llm-model-input"
        value={value}
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => {
          onChange(event.target.value);
          show();
        }}
        onFocus={show}
        placeholder={placeholder}
        className="h-9 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 pr-8 text-sm text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
      />
      <button
        type="button"
        tabIndex={-1}
        aria-label="打开模型列表"
        onClick={() => {
          if (open) setOpen(false);
          else show();
        }}
        className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-agent-muted-foreground hover:text-agent-foreground"
      >
        <LuChevronDown className="h-4 w-4" />
      </button>
      {menu}
    </div>
  );
}

function entryName(row: ModelPickerRow): string | null {
  const name = row.entry?.name?.trim();
  if (!name || name === row.id) return null;
  return name;
}

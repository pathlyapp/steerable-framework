/**
 * 聊天里「显示思考内容」的本机偏好：隐藏 / 显示 5 行 / 完整显示。
 *
 * 默认「显示 5 行」：流式中露出固定行数，结束后折叠，只留状态行。
 * 存在 localStorage，设置页切换立即生效，不走后端。
 */

import { useEffect, useState } from 'react';

export const SHOW_THINKING_CONTENT_STORAGE_KEY = 'agent-show-thinking-content';
export const SHOW_THINKING_CONTENT_EVENT = 'agent-show-thinking-content-changed';

export const THINKING_DISPLAY_MODES = ['hidden', 'peek', 'full'] as const;
export type ThinkingDisplayMode = (typeof THINKING_DISPLAY_MODES)[number];
export const DEFAULT_THINKING_DISPLAY: ThinkingDisplayMode = 'peek';

export const THINKING_DISPLAY_OPTIONS: ReadonlyArray<{
  mode: ThinkingDisplayMode;
  label: string;
  hint: string;
}> = [
  { mode: 'hidden', label: '隐藏', hint: '不展示思考正文，只保留工作状态行。' },
  { mode: 'peek', label: '显示5行', hint: '点「思考」展开后露出 5 行，折叠后不显示。完整正文请用「完整显示」。' },
  { mode: 'full', label: '完整显示', hint: '思考正文完整展开；结束后工作行仍自动折叠。' },
];

export function isThinkingDisplayMode(value: unknown): value is ThinkingDisplayMode {
  return value === 'hidden' || value === 'peek' || value === 'full';
}

/** 兼容旧开关：'1' → 完整显示，'0' / 缺失 → 显示 5 行。 */
export function parseThinkingDisplay(raw: string | null | undefined): ThinkingDisplayMode {
  if (isThinkingDisplayMode(raw)) return raw;
  if (raw === '1') return 'full';
  return DEFAULT_THINKING_DISPLAY;
}

export function readThinkingDisplay(): ThinkingDisplayMode {
  if (typeof localStorage === 'undefined') return DEFAULT_THINKING_DISPLAY;
  return parseThinkingDisplay(localStorage.getItem(SHOW_THINKING_CONTENT_STORAGE_KEY));
}

export function persistThinkingDisplay(mode: ThinkingDisplayMode): void {
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(SHOW_THINKING_CONTENT_STORAGE_KEY, mode);
  }
  if (typeof window === 'undefined') return;
  window.dispatchEvent(
    new CustomEvent<ThinkingDisplayMode>(SHOW_THINKING_CONTENT_EVENT, { detail: mode }),
  );
}

/** 当前页与跨标签的偏好变更都会刷新。 */
export function useThinkingDisplay(): ThinkingDisplayMode {
  const [mode, setMode] = useState(readThinkingDisplay);
  useEffect(() => {
    const onCustom = (event: Event) => {
      const detail = (event as CustomEvent<ThinkingDisplayMode>).detail;
      setMode(isThinkingDisplayMode(detail) ? detail : readThinkingDisplay());
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === SHOW_THINKING_CONTENT_STORAGE_KEY) {
        setMode(parseThinkingDisplay(event.newValue));
      }
    };
    window.addEventListener(SHOW_THINKING_CONTENT_EVENT, onCustom);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener(SHOW_THINKING_CONTENT_EVENT, onCustom);
      window.removeEventListener('storage', onStorage);
    };
  }, []);
  return mode;
}

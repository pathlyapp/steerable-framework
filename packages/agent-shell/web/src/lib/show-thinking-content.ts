/**
 * 聊天里「显示思考内容」的本机偏好。
 *
 * 默认关闭：思考 / 工具过程折叠，只留状态行。打开后流式中自动展开。
 * 存在 localStorage，设置页开关立即生效，不走后端。
 */

import { useEffect, useState } from 'react';

export const SHOW_THINKING_CONTENT_STORAGE_KEY = 'agent-show-thinking-content';
export const SHOW_THINKING_CONTENT_EVENT = 'agent-show-thinking-content-changed';

export function readShowThinkingContent(): boolean {
  if (typeof localStorage === 'undefined') return false;
  return localStorage.getItem(SHOW_THINKING_CONTENT_STORAGE_KEY) === '1';
}

export function persistShowThinkingContent(show: boolean): void {
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(SHOW_THINKING_CONTENT_STORAGE_KEY, show ? '1' : '0');
  }
  if (typeof window === 'undefined') return;
  window.dispatchEvent(
    new CustomEvent<boolean>(SHOW_THINKING_CONTENT_EVENT, { detail: show }),
  );
}

/** 当前页与跨标签的偏好变更都会刷新。 */
export function useShowThinkingContent(): boolean {
  const [show, setShow] = useState(readShowThinkingContent);
  useEffect(() => {
    const onCustom = (event: Event) => {
      const detail = (event as CustomEvent<boolean>).detail;
      setShow(typeof detail === 'boolean' ? detail : readShowThinkingContent());
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === SHOW_THINKING_CONTENT_STORAGE_KEY) {
        setShow(event.newValue === '1');
      }
    };
    window.addEventListener(SHOW_THINKING_CONTENT_EVENT, onCustom);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener(SHOW_THINKING_CONTENT_EVENT, onCustom);
      window.removeEventListener('storage', onStorage);
    };
  }, []);
  return show;
}

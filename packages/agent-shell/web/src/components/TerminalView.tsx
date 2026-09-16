import { useEffect, useRef, useState } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import { LuListTodo } from 'react-icons/lu';
import '@xterm/xterm/css/xterm.css';
import { DockHeaderButton, DockPanelHeader } from '@/components/DockPanelHeader';
import { getElectronBridge } from '@/lib/electron-bridge';
import { BRAND_NAME } from '@/brand';

/**
 * Renders an xterm.js terminal wired up to the main-process PTY via the
 * `terminal.*` bridge. Ported from `deeppath/apps/web/src/app/agent/terminal/
 * TerminalView.tsx`, with these adaptations:
 *
 *   - `window.electron` direct access → `getElectronBridge()` helper so the
 *     "no bridge" path is type-safe.
 *   - Removed `'use client'` directive (not a Next.js project).
 *   - Container is `h-full` instead of `h-screen` so the component is
 *     embeddable in any parent — currently the embedded `TerminalPanel`
 *     inside AgentLayout's split view.
 *   - Optional `onClose` renders a header close button (panel chrome).
 *   - Header chrome comes from the shared `DockPanelHeader`, so switching
 *     between terminal and 后台推理 doesn't shift the title bar.
 *
 * No business logic changes: same xterm config, same Cmd+C/V handling,
 * same context-menu copy-paste, same resize observer.
 */
export function TerminalView({
  onClose,
  onShowTaskProcess,
  taskProcessTitle,
}: {
  onClose?: () => void;
  /** 缺省时不渲染「后台任务」按钮（本次会话还没看过任何任务）。 */
  onShowTaskProcess?: () => void;
  /** 记住的那个任务的标题，只用于按钮 tooltip。 */
  taskProcessTitle?: string;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const [status, setStatus] = useState<'starting' | 'ready' | 'error'>('starting');
  const [statusMsg, setStatusMsg] = useState<string>('');

  useEffect(() => {
    if (!containerRef.current) return;
    const bridge = getElectronBridge();
    const terminal = bridge?.terminal;
    if (!terminal) {
      setStatus('error');
      setStatusMsg('window.electron.terminal 不可用');
      return;
    }

    const term = new Terminal({
      fontFamily:
        'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
      fontSize: 13,
      lineHeight: 1.2,
      cursorBlink: true,
      cursorStyle: 'bar',
      convertEol: true,
      scrollback: 5000,
      // macOS 上让 Option 当 Meta（习惯用 alt+f/b 在词间跳转）
      macOptionIsMeta: true,
      // 鼠标右键弹系统菜单时会偷走 selection，关掉
      rightClickSelectsWord: true,
      theme: {
        background: '#0b0b0c',
        foreground: '#e6e6e6',
        cursor: '#e6e6e6',
        selectionBackground: '#3a3d41',
      },
      allowProposedApi: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon());
    term.open(containerRef.current);
    fit.fit();

    // xterm 渲染在 canvas 上，应用菜单的 copy/paste role 抓不到选区。自己拦截：
    //   Cmd/Ctrl+C  → 有选区就复制，否则放行（让 xterm 走默认 ^C 发 SIGINT）
    //   Cmd+V / Ctrl+Shift+V → 从剪贴板读出来 paste 到 PTY
    //   Cmd/Ctrl+A  → 全选
    const isMac = navigator.platform.toUpperCase().includes('MAC');
    term.attachCustomKeyEventHandler((event) => {
      if (event.type !== 'keydown') return true;
      const mod = isMac ? event.metaKey : event.ctrlKey;
      const key = event.key.toLowerCase();

      if (mod && key === 'c') {
        const sel = term.getSelection();
        if (sel) {
          void navigator.clipboard.writeText(sel).catch(() => {});
          term.clearSelection();
          return false;
        }
        return true;
      }
      if (
        (isMac && mod && key === 'v') ||
        (!isMac && event.ctrlKey && event.shiftKey && key === 'v')
      ) {
        void navigator.clipboard
          .readText()
          .then((text) => {
            if (text) term.paste(text);
          })
          .catch(() => {});
        return false;
      }
      if (mod && key === 'a' && !event.shiftKey) {
        term.selectAll();
        return false;
      }
      return true;
    });

    const onContextMenu = (e: MouseEvent) => {
      e.preventDefault();
      const sel = term.getSelection();
      if (sel) {
        void navigator.clipboard.writeText(sel).catch(() => {});
        term.clearSelection();
      } else {
        void navigator.clipboard
          .readText()
          .then((text) => {
            if (text) term.paste(text);
          })
          .catch(() => {});
      }
    };
    containerRef.current.addEventListener('contextmenu', onContextMenu);

    let cancelled = false;
    const offData = terminal.onData(({ sessionId, chunk }) => {
      if (sessionIdRef.current && sessionId === sessionIdRef.current) {
        term.write(chunk);
      }
    });
    const offExit = terminal.onExit(({ sessionId, code }) => {
      if (sessionIdRef.current === sessionId) {
        term.writeln(`\r\n\x1b[33m[process exited with code ${code}]\x1b[0m`);
      }
    });

    (async () => {
      try {
        const dims = fit.proposeDimensions();
        const session = await terminal.ensure({
          cols: dims?.cols,
          rows: dims?.rows,
        });
        if (cancelled) return;
        sessionIdRef.current = session.id;
        setStatus('ready');
        term.writeln(
          `\x1b[90m[agent-shell] ${session.shell} pid=${session.pid} cwd=${session.cwd}\x1b[0m`,
        );
      } catch (err) {
        setStatus('error');
        setStatusMsg(err instanceof Error ? err.message : String(err));
      }
    })();

    const inputDisposable = term.onData((data) => {
      if (sessionIdRef.current) {
        void terminal.write(sessionIdRef.current, data);
      }
    });

    const handleResize = () => {
      try {
        fit.fit();
        if (sessionIdRef.current) {
          void terminal.resize(sessionIdRef.current, term.cols, term.rows);
        }
      } catch {
        // ignore
      }
    };
    window.addEventListener('resize', handleResize);
    const ro = new ResizeObserver(handleResize);
    ro.observe(containerRef.current);

    const containerEl = containerRef.current;
    return () => {
      cancelled = true;
      window.removeEventListener('resize', handleResize);
      containerEl?.removeEventListener('contextmenu', onContextMenu);
      ro.disconnect();
      offData?.();
      offExit?.();
      inputDisposable.dispose();
      term.dispose();
    };
  }, []);

  const statusLabel =
    status === 'starting' ? '启动中…' : status === 'ready' ? '在线' : `错误：${statusMsg}`;

  return (
    <div className="flex h-full w-full flex-col bg-[#0b0b0c]">
      <DockPanelHeader
        title={`${BRAND_NAME} · 终端 · ${statusLabel}`}
        onClose={onClose}
        closeLabel="关闭终端面板"
        actions={
          onShowTaskProcess && (
            <DockHeaderButton
              icon={<LuListTodo className="h-3 w-3" />}
              label="后台任务"
              title={
                taskProcessTitle
                  ? `切换到后台推理：${taskProcessTitle}`
                  : '切换到后台推理'
              }
              onClick={onShowTaskProcess}
            />
          )
        }
      />
      <div ref={containerRef} className="flex-1 overflow-hidden p-2" />
    </div>
  );
}

import { Suspense, lazy, useEffect, useState } from 'react';
import { getElectronBridge, isElectron } from '@/lib/electron-bridge';

// 把 xterm 那一坨 (~300KB) 拆到独立 chunk —— 用户没打开终端就不下载。
// 跟原 Next.js 版本里 `dynamic(() => import('./TerminalView'), {ssr:false})`
// 等价。
const TerminalView = lazy(() =>
  import('@/components/TerminalView').then((m) => ({ default: m.TerminalView })),
);

/**
 * 内嵌终端面板 —— 挂在 AgentLayout 右侧内容区，与 chat 两栏并排
 * （Codex 式一体化终端），由 sidebar 终端按钮 / Cmd+T 切换显隐。
 *
 * PTY 会话由 main 进程的 `TerminalManager` 单例持有：关闭面板只是卸载
 * xterm 视图，不杀 shell；重新打开时通过 `terminal:ensure` 的 replay
 * buffer 补回面板关闭期间的输出。
 *
 * Electron 检测保留给浏览器预览 / dev 模式（没有 `window.electron` 就
 * 连不到本地 PTY）。延迟到首个 effect 再渲染，避免检测闪烁。
 */
export function TerminalPanel({
  onClose,
  onShowTaskProcess,
  taskProcessTitle,
}: {
  onClose?: () => void;
  /** 头部「后台任务」按钮：切回本次会话看过的后台推理过程。 */
  onShowTaskProcess?: () => void;
  taskProcessTitle?: string;
}) {
  const [hasElectron, setHasElectron] = useState<boolean | null>(null);

  useEffect(() => {
    setHasElectron(isElectron() && !!getElectronBridge()?.terminal);
  }, []);

  if (hasElectron === null) {
    return null;
  }
  if (!hasElectron) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-black p-6 text-center font-mono text-sm text-white">
        <div>
          <p className="mb-2">⚠️ 此面板需要桌面客户端或 BS server 连接。</p>
          <p className="text-white/60">
            当前页面没有可用的宿主 bridge，无法连接到本地 PTY。
          </p>
        </div>
      </div>
    );
  }
  return (
    <Suspense
      fallback={
        <div className="flex h-full w-full items-center justify-center bg-[#0b0b0c] font-mono text-xs text-white/60">
          Loading terminal…
        </div>
      }
    >
      <TerminalView
        onClose={onClose}
        onShowTaskProcess={onShowTaskProcess}
        taskProcessTitle={taskProcessTitle}
      />
    </Suspense>
  );
}

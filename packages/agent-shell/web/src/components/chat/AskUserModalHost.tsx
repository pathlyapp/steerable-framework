import { useEffect, useState } from 'react';
import { getElectronBridge, type AskUserPromptRequest } from '@/lib/electron-bridge';
import { AskUserQuestionMenu } from './AskUserQuestionMenu';

/**
 * AskUserModalHost — W8 结构化提问的桌面 UI 半侧。
 *
 * 挂载一次（AgentLayout），订阅主进程/BS 服务器广播的 `ask-user:request`，
 * 以模态渲染自研的 `<AskUserQuestionMenu />`（分步菜单选择框：模型建议
 * 作为选项、底部追加「其他 / 自定义」输入）。答案经 `ask-user:answer`
 * （Electron IPC / BS HTTP）回到主进程，再应答 sidecar 的反向调用。
 *
 * 队列为 FIFO：ask_user 标记了 concurrency_safe=False，sidecar 串行等待
 * 每个应答。「交给我决定」应答空映射——模型收到「用户未作答」后自行
 * 推进（对齐 HostAskUserHandler 的 fail-open 语义）。
 */

export function AskUserModalHost() {
  const [queue, setQueue] = useState<AskUserPromptRequest[]>([]);

  useEffect(() => {
    const bridge = getElectronBridge();
    if (!bridge?.askUser) return;
    return bridge.askUser.onRequest((request) => {
      setQueue((prev) => [...prev, request]);
    });
  }, []);

  const current = queue[0] ?? null;
  if (!current) return null;

  const answer = (answers: Record<string, string | string[]>) => {
    const bridge = getElectronBridge();
    setQueue((prev) => prev.slice(1));
    void bridge?.askUser?.answer({ requestId: current.requestId, answers });
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Agent 提问"
    >
      <div className="max-h-[85vh] w-full max-w-lg overflow-auto rounded-agent-lg">
        <AskUserQuestionMenu
          key={current.requestId}
          intro={current.intro}
          questions={current.questions}
          onSubmit={(answers) => answer(answers)}
          onAutoContinue={() => answer({})}
          bottomHint={
            queue.length > 1 ? `还有 ${queue.length - 1} 组问题待回答` : undefined
          }
        />
      </div>
    </div>
  );
}

export default AskUserModalHost;

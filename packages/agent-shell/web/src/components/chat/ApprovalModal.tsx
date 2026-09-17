import { LuShieldAlert, LuTriangleAlert } from 'react-icons/lu';
import {
  type ApprovalDecisionKind,
  type ApprovalPromptRequest,
} from '@/lib/electron-bridge';

/**
 * ApprovalPromptMenu — W4-1 审批代数的输入区 UI。
 *
 * 由 ApprovalPromptProvider 提供当前请求，在普通输入框的位置让用户对
 * 工具调用做 7 变体决策（允许/拒绝 × 一次性/会话/持久 + 中止）。
 *
 * 持久化语义（与框架 ApprovalExecutor 对齐）：
 *   - 一次性：只作用于本次调用，不缓存。
 *   - 会话：按 category（默认=工具名）缓存在本 chat 的会话级 cache。
 *   - 持久：写入 ~/.steerable/approvals.json，跨会话生效。
 *
 * 请求队列及刷新恢复由 Provider 管理。
 */

const MODE_LABEL: Record<string, string> = {
  read: '只读',
  safe_write: '写入',
  destructive: '危险',
  other: '其他',
};

function summarizeArguments(args: Record<string, unknown>): string {
  // 'url' 必须在前列：批准一次 web_fetch 就是批准一次出网请求，
  // 用户必须看清目标地址（W5-2）。
  for (const key of ['url', 'command', 'cmd', 'path', 'file', 'target', 'query', 'content']) {
    const v = args[key];
    if (typeof v === 'string' && v.length > 0) {
      return v.length > 300 ? `${v.slice(0, 300)}…` : v;
    }
  }
  const json = JSON.stringify(args);
  return json.length > 300 ? `${json.slice(0, 300)}…` : json;
}

interface DecisionButton {
  kind: ApprovalDecisionKind;
  label: string;
  tone: 'allow' | 'deny' | 'abort';
}

const ALLOW_BUTTONS: DecisionButton[] = [
  { kind: 'allow_once', label: '允许一次', tone: 'allow' },
  { kind: 'allow_for_session', label: '本次会话允许', tone: 'allow' },
  { kind: 'allow_always', label: '始终允许', tone: 'allow' },
];

const DENY_BUTTONS: DecisionButton[] = [
  { kind: 'deny_once', label: '拒绝一次', tone: 'deny' },
  { kind: 'deny_for_session', label: '本次会话拒绝', tone: 'deny' },
  { kind: 'deny_always', label: '始终拒绝', tone: 'deny' },
];

function DecisionRow({
  buttons,
  onPick,
}: {
  buttons: DecisionButton[];
  onPick: (kind: ApprovalDecisionKind) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {buttons.map((b) => (
        <button
          key={b.kind}
          type="button"
          data-testid={`approval-${b.kind}`}
          onClick={() => onPick(b.kind)}
          className={
            b.tone === 'allow'
              ? 'rounded-agent-md bg-agent-foreground px-3 py-1.5 text-xs font-medium text-agent-canvas transition hover:opacity-90'
              : 'rounded-agent-md border border-agent-border px-3 py-1.5 text-xs font-medium text-agent-foreground transition hover:bg-agent-muted'
          }
        >
          {b.label}
        </button>
      ))}
    </div>
  );
}

export function ApprovalPromptMenu({
  request: current,
  pendingCount,
  onDecide,
}: {
  request: ApprovalPromptRequest;
  pendingCount: number;
  onDecide: (kind: ApprovalDecisionKind) => void;
}) {
  const destructive = current.mode === 'destructive';
  // 网络出口拓宽（W-egress-ask）：sidecar 的 web 工具被 egress 代理 403
  // 后发起。白名单是代理进程级的（会话寿命），所以「始终」变体对网络
  // 出口没有意义——只给一次性/会话两档，且默认焦点在拒绝（安全默认：
  // 用户能认出 api.github.com，认不出 cdn.jsdelivr.net.evil.com）。
  const isEgress = current.category === 'network_egress';
  const egressHost =
    typeof current.arguments.host === 'string' ? current.arguments.host : '';
  const egressPort =
    typeof current.arguments.port === 'number' ? current.arguments.port : null;

  return (
    <div
      className="overflow-hidden rounded-agent-lg border border-agent-border bg-agent-canvas text-sm text-agent-foreground shadow-xl"
      data-testid="approval-composer"
    >
        <div className="flex items-center gap-2 border-b border-agent-border px-4 py-3">
          {destructive ? (
            <LuTriangleAlert className="h-4 w-4 shrink-0 text-agent-destructive" />
          ) : (
            <LuShieldAlert className="h-4 w-4 shrink-0 text-amber-500" />
          )}
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-agent-foreground">
              {isEgress ? (
                <>
                  Agent 请求访问外网
                  <span className="mx-1 font-mono">
                    {egressHost}
                    {egressPort !== null ? `:${egressPort}` : ''}
                  </span>
                </>
              ) : (
                <>
                  Agent 请求执行
                  <span className="mx-1 font-mono">{current.toolName}</span>
                  <span className="text-agent-muted-foreground">
                    （{MODE_LABEL[current.mode] ?? current.mode}操作）
                  </span>
                </>
              )}
            </div>
            {pendingCount > 0 && (
              <div className="mt-0.5 text-[11px] text-agent-muted-foreground">
                还有 {pendingCount} 个待审批
              </div>
            )}
          </div>
        </div>

        <div className="max-h-48 overflow-auto px-4 py-3">
          <pre className="whitespace-pre-wrap break-all rounded-agent-md bg-agent-muted/50 px-3 py-2 font-mono text-xs text-agent-foreground">
            {summarizeArguments(current.arguments)}
          </pre>
            {isEgress ? (
            <p className="mt-2 text-[11px] leading-relaxed text-agent-muted-foreground">
              该域名不在出网白名单内。放行前请核对完整域名拼写（仿冒域名常
              用相似拼写，如 cdn.jsdelivr.net.evil.com）。放行仅对本次会话
              生效；要持久放行请把域名加入设置的出网白名单。
            </p>
          ) : (
            <>
              {current.category !== current.toolName && (
                <p className="mt-2 text-[11px] text-agent-muted-foreground">
                  持久化类别：{current.category}（会话/始终决定对该类别所有调用生效）
                </p>
              )}
              <p className="mt-2 text-[11px] leading-relaxed text-agent-muted-foreground">
                默认在工作区沙箱中执行（只能写入当前项目）。写入 Downloads
                等项目外路径会被系统拒绝；需要时在输入框把沙箱切到「完整权限」后再让
                Agent 重试。
              </p>
            </>
          )}
        </div>

        <div className="space-y-2 border-t border-agent-border px-4 py-3">
          <DecisionRow
            buttons={
              isEgress
                ? ALLOW_BUTTONS.filter((b) => b.kind !== 'allow_always')
                : ALLOW_BUTTONS
            }
            onPick={onDecide}
          />
          <div className="flex items-center justify-between gap-2">
            <DecisionRow
              buttons={
                isEgress
                  ? DENY_BUTTONS.filter((b) => b.kind !== 'deny_always')
                  : DENY_BUTTONS
              }
              onPick={onDecide}
            />
            <button
              type="button"
              onClick={() => onDecide('abort')}
              className="shrink-0 rounded-agent-md bg-agent-destructive px-3 py-1.5 text-xs font-medium text-white transition hover:opacity-90"
              data-testid="approval-abort"
            >
              中止本轮
            </button>
          </div>
          <p className="text-[11px] leading-relaxed text-agent-muted-foreground">
            {isEgress
              ? '「会话」决定在本对话内记住（代理进程退出即失效）。不操作约 3 分钟后按拒绝处理。'
              : '「会话」决定在本对话内记住；「始终」决定写入本机 ~/.steerable/approvals.json，跨对话生效。不操作约 2 分钟后按拒绝处理。'}
          </p>
        </div>
    </div>
  );
}

export default ApprovalPromptMenu;

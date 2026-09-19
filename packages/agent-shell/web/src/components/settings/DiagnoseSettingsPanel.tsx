import { useState } from 'react';
import { LuCircleCheck, LuCircleX, LuLoader, LuPlay } from 'react-icons/lu';
import { diagnoseLlmConnection, type DiagnoseResult } from '@/lib/local-api';

/**
 * 设置页「链路诊断」面板：一键探测当前 LLM 配置的连通性
 * （DNS → TCP → TLS → HTTP /models → chat completion），并报告
 * 宿主机的 ambient 代理配置。
 */
export function DiagnoseSettingsPanel() {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<DiagnoseResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const res = await diagnoseLlmConnection({});
      setResult(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="space-y-2 rounded-agent-md border border-agent-border bg-agent-card p-2.5">
      <p className="text-xs text-agent-muted-foreground">
        诊断当前模型服务的网络连通性，包括 DNS 解析、TCP 连接、TLS 握手、HTTP 接口与对话补全。
        诊断在宿主进程内执行，会同时报告系统/环境代理配置。
      </p>

      <button
        type="button"
        onClick={() => void run()}
        disabled={running}
        className="inline-flex items-center gap-1.5 rounded-agent-md bg-agent-primary px-3 py-1.5 text-xs font-medium text-agent-primary-foreground transition hover:bg-agent-primary/90 disabled:opacity-50"
      >
        {running ? (
          <>
            <LuLoader className="h-3.5 w-3.5 animate-spin" />
            诊断中…
          </>
        ) : (
          <>
            <LuPlay className="h-3.5 w-3.5" />
            开始诊断
          </>
        )}
      </button>

      {error && (
        <p className="rounded-agent-md border border-agent-destructive/20 bg-agent-destructive/10 p-2.5 text-xs text-agent-destructive">
          诊断请求失败：{error}
        </p>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs">
            {result.ok ? (
              <span className="inline-flex items-center gap-1 text-agent-success">
                <LuCircleCheck className="h-4 w-4" />
                全部通过
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 text-agent-destructive">
                <LuCircleX className="h-4 w-4" />
                诊断失败
              </span>
            )}
          </div>

          <ol className="space-y-1.5">
            {result.steps.map((step) => (
              <li
                key={step.name}
                className="flex items-start gap-2 rounded-agent-sm bg-agent-muted/50 px-2.5 py-1.5 text-xs"
              >
                {step.ok ? (
                  <LuCircleCheck className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-agent-success" />
                ) : (
                  <LuCircleX className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-agent-destructive" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium text-agent-foreground">{step.name}</span>
                    <span className="text-agent-muted-foreground">{step.durationMs}ms</span>
                  </div>
                  <p className="mt-0.5 break-all text-agent-muted-foreground">{step.detail}</p>
                </div>
              </li>
            ))}
          </ol>

          {result.ambientProxies.length > 0 && (
            <div className="rounded-agent-sm border border-agent-border bg-agent-muted/30 p-2.5 text-xs">
              <p className="font-medium text-agent-foreground">检测到的代理配置</p>
              <ul className="mt-1 list-inside list-disc space-y-0.5 text-agent-muted-foreground">
                {result.ambientProxies.map((proxy) => (
                  <li key={proxy}>{proxy}</li>
                ))}
              </ul>
            </div>
          )}

          {result.hint && (
            <p className="rounded-agent-md border border-agent-warning/20 bg-agent-warning/10 p-2.5 text-xs text-agent-warning">
              {result.hint}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

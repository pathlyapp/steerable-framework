import { useCallback, useEffect, useState } from 'react';
import { LuLoaderCircle, LuShieldCheck } from 'react-icons/lu';
import { isElectron } from '@/lib/electron-bridge';
import {
  getSidecarSandboxPosture,
  type EgressPosture,
  type SidecarSandboxPosture,
} from '@/lib/local-api';

/**
 * SecuritySettingsPanel — W4-3 layer-1（sidecar 进程沙箱）态势的持久披露。
 *
 * sidecar 进程持有服务商 API 密钥并接收不可信工具输出。收容失败时主进程
 * 拒绝启动（不是裸跑），态势记在 supervisor / lastSpawnRefusal 上
 * （GET /api/v2/sidecar/sandbox-posture）。手动关闭（sandbox:false /
 * STEERABLE_SIDECAR_SANDBOX=0）中性展示；收容失败告知「无法收容、已拒绝启动」。
 * layer-3（每次命令的沙箱）披露在工具卡片，不在此重复。
 *
 * 数据自管理：挂载拉一次，走 local-backend REST，无专用 IPC channel。
 */

type PostureReason = SidecarSandboxPosture['reason'];

/** 收容失败、已拒绝启动（告警行首句）。 */
const REFUSED_REASON_TEXT: Partial<Record<PostureReason, string>> = {
  platform_unsupported: '当前平台没有可用的进程沙箱后端',
  seatbelt_missing: '未找到 /usr/bin/sandbox-exec',
  profile_failed: '沙箱配置（Seatbelt profile）生成失败',
  wrap_failed: 'Linux 进程沙箱（bwrap / Landlock）未能包住 sidecar',
  helper_missing: '未找到 win-spawn-helper.exe',
};

/** 手动关闭原因的中文说明（中性行，不告警）。 */
const OPT_OUT_REASON_TEXT: Partial<Record<PostureReason, string>> = {
  disabled_by_option: '已通过 sandbox: false 关闭',
  disabled_by_env: '已通过 STEERABLE_SIDECAR_SANDBOX=0 关闭',
};

function activeHeadline(backend: SidecarSandboxPosture['backend']): string {
  switch (backend) {
    case 'seatbelt':
      return 'Seatbelt · 部分强制';
    case 'bwrap':
      return 'bwrap · 部分强制';
    case 'landlock':
      return 'Landlock · 部分强制';
    case 'windows-restricted-token':
      return 'Windows 受限令牌 · 部分强制';
    case 'none':
      return '未沙箱';
  }
}

function activeBody(backend: SidecarSandboxPosture['backend']): string {
  switch (backend) {
    case 'seatbelt':
      return 'sidecar 进程在 macOS Seatbelt 下运行：写入限制在 ~/.steerable 与临时目录。远程主机的出站网络只能按端口限制（Seatbelt 不识别主机名），故记为「部分」而非「完全」。';
    case 'bwrap':
      return 'sidecar 进程在 bubblewrap 下运行：写入限制在 ~/.steerable 与临时目录。出站网络保持开启（sidecar 要访问 LLM），故记为「部分」。';
    case 'landlock':
      return 'sidecar 进程在 Linux Landlock 下运行：写入限制在 ~/.steerable 与临时目录。出站网络保持开启（sidecar 要访问 LLM），故记为「部分」。';
    case 'windows-restricted-token':
      return 'sidecar 进程在 Windows 受限令牌 + Job Object 下运行：写入限制在 ~/.steerable。网络策略未由 helper 强制（无 WFP），故记为「部分」。';
    case 'none':
      return '';
  }
}

/** 出网管控态势行（W-egress-posture）：退回分支此前只有主进程日志可见。 */
function EgressPostureRow({ egress }: { egress: EgressPosture }) {
  if (egress.mode === 'per-host-proxy') {
    return (
      <p className="text-[11px] text-agent-muted-foreground">
        出网管控：按主机白名单代理生效中；白名单外的请求会请你逐项放行（仅本次会话有效）。
      </p>
    );
  }
  if (egress.mode === 'disabled') {
    return (
      <p className="text-[11px] text-agent-muted-foreground">
        出网管控：已关闭{egress.reason ? ` — ${egress.reason}` : ''}。
      </p>
    );
  }
  return (
    <p className="text-[11px] text-amber-600 dark:text-amber-400">
      出网管控：已退回端口级（较弱的管控粒度）{egress.reason ? ` — ${egress.reason}` : ''}。
    </p>
  );
}

export function SecuritySettingsPanel() {
  const [posture, setPosture] = useState<SidecarSandboxPosture | null>(null);
  const [egress, setEgress] = useState<EgressPosture | null>(null);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState<string | null>(null);

  const fetchPosture = useCallback(async () => {
    if (!isElectron()) return;
    setLoading(true);
    setUnavailable(null);
    try {
      const res = await getSidecarSandboxPosture();
      setPosture(res.posture ?? null);
      setEgress(res.egress ?? null);
      if (!res.posture) setUnavailable('无法读取沙箱状态');
    } catch {
      // 503 = sidecar 未就绪且没有收容失败记录。
      setPosture(null);
      setEgress(null);
      setUnavailable('sidecar 未就绪 — 无法读取沙箱状态');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchPosture();
  }, [fetchPosture]);

  const refusedReason = posture ? REFUSED_REASON_TEXT[posture.reason] : undefined;
  const optOutReason = posture ? OPT_OUT_REASON_TEXT[posture.reason] : undefined;

  return (
    <div className="bg-agent-muted/30 border border-agent-border/60 rounded-agent-md p-3.5 space-y-3">
      <h4 className="text-xs font-semibold text-agent-foreground flex items-center gap-1.5">
        <LuShieldCheck className="h-3.5 w-3.5 text-agent-muted-foreground" />
        Sidecar 进程沙箱
      </h4>

      {!isElectron() ? (
        <p className="text-[11px] text-agent-muted-foreground">需要在桌面客户端中打开</p>
      ) : loading ? (
        <div className="flex items-center gap-2 py-2 text-xs text-agent-muted-foreground">
          <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
          读取沙箱状态...
        </div>
      ) : unavailable ? (
        <p className="text-[11px] text-agent-muted-foreground">{unavailable}</p>
      ) : posture && refusedReason ? (
        <div className="rounded-agent-md border border-agent-destructive/20 bg-agent-destructive/10 p-2.5 text-xs text-agent-destructive space-y-1.5">
          <p className="font-medium">无法收容、已拒绝启动 — {refusedReason}。</p>
          <p>
            持有服务商 API 密钥的 sidecar 进程没有启动，因此也没有在无隔离状态下运行。
            唯一裸跑入口是显式设置 STEERABLE_SIDECAR_SANDBOX=0。
          </p>
          <p>
            如需隔离，请安装对应平台的收容后端（macOS Seatbelt、Linux bwrap/Landlock、Windows
            win-spawn-helper），或将本应用运行在容器内。
          </p>
        </div>
      ) : posture && optOutReason ? (
        <div className="rounded-agent-md border border-agent-border/60 bg-agent-canvas p-2.5 text-xs text-agent-muted-foreground space-y-1.5">
          <p className="font-medium text-agent-foreground">已手动关闭 — {optOutReason}。</p>
          <p>
            sidecar 进程（持有服务商 API 密钥）无操作系统隔离运行。如需恢复，移除该设置并重启应用。
          </p>
        </div>
      ) : posture ? (
        <div className="rounded-agent-md border border-agent-border/60 bg-agent-canvas p-2.5 text-xs text-agent-muted-foreground space-y-1.5">
          <p className="font-medium text-agent-foreground">{activeHeadline(posture.backend)}</p>
          <p>{activeBody(posture.backend)}</p>
        </div>
      ) : null}

      {egress && <EgressPostureRow egress={egress} />}
    </div>
  );
}

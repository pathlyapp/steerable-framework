/**
 * W6-5 项目信任门控的 UI 入口。
 *
 * 项目目录里的 `AGENTS.md` / `CLAUDE.md` 等规则文件是「项目作者写给 agent
 * 的指令」，属于不可信输入——打开恶意仓库时一段构造的规则就能劫持 agent。
 * 因此规则文件只在用户显式信任该项目后才注入模型上下文。本横幅在「会话绑定
 * 了项目、项目里有规则文件、但尚未信任」时提示用户授权；已信任时提供一个
 * 低调的「撤销」入口。信任状态持久化在后端（agent-projects.json）。
 */
import { useCallback, useEffect, useState } from 'react';
import { LuShieldAlert, LuShieldCheck } from 'react-icons/lu';
import type { LocalProject } from '@/lib/local-api';
import { getChatProjectContext, setProjectTrusted } from '@/lib/local-api';

export interface ProjectTrustBannerProps {
  chatId: string;
  project: LocalProject | null;
  /** 信任状态变化后回调（重新拉项目列表，让徽章/设置同步）。 */
  onTrustChanged?: () => void;
}

export function ProjectTrustBanner({ chatId, project, onTrustChanged }: ProjectTrustBannerProps) {
  const [ruleFileCount, setRuleFileCount] = useState(0);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const res = await getChatProjectContext(chatId);
      setRuleFileCount(res.ruleFileCount ?? 0);
    } catch {
      setRuleFileCount(0);
    }
  }, [chatId]);

  useEffect(() => {
    setRuleFileCount(0);
    if (project) void refresh();
  }, [project, refresh]);

  if (!project || ruleFileCount === 0) return null;
  const trusted = project.trusted === true;

  const toggle = async () => {
    setBusy(true);
    try {
      await setProjectTrusted(project.id, !trusted);
      onTrustChanged?.();
    } finally {
      setBusy(false);
    }
  };

  if (!trusted) {
    return (
      <div className="mx-3 mb-1 flex items-center justify-between gap-3 rounded-agent-md border border-sky-400/40 bg-sky-400/10 px-3 py-2 text-xs">
        <div className="flex items-center gap-2 text-sky-700 dark:text-sky-300">
          <LuShieldAlert className="h-4 w-4 shrink-0" />
          <span>
            项目「{project.name}」包含 {ruleFileCount} 个规则文件（AGENTS.md / CLAUDE.md）。
            信任前这些项目方指令<strong>不会</strong>注入模型。
          </span>
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={() => void toggle()}
          className="inline-flex shrink-0 items-center gap-1 rounded-full bg-agent-foreground px-3 py-1 font-medium text-agent-canvas transition hover:opacity-90 disabled:opacity-50"
        >
          信任并加载
        </button>
      </div>
    );
  }

  return (
    <div className="mx-3 mb-1 flex items-center justify-between gap-3 rounded-agent-md border border-agent-border bg-agent-muted/30 px-3 py-1.5 text-[11px] text-agent-muted-foreground">
      <div className="flex items-center gap-1.5">
        <LuShieldCheck className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
        <span>已加载项目「{project.name}」的 {ruleFileCount} 个规则文件</span>
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={() => void toggle()}
        className="shrink-0 rounded px-1.5 py-0.5 transition-colors hover:text-agent-destructive disabled:opacity-50"
      >
        撤销信任
      </button>
    </div>
  );
}

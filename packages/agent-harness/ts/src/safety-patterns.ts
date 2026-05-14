export type PatternSeverity = "critical" | "warning";
export type PatternPlatform = "all" | "unix" | "windows";

export interface SafetyPatternDef {
  id: string;
  label: string;
  description: string;
  pattern: string;
  category: string;
  severity: PatternSeverity;
  platform: PatternPlatform;
}

export const BUILTIN_PATTERNS: SafetyPatternDef[] = [
  {
    id: "rm",
    label: "rm 删除",
    description: "匹配 rm 命令",
    pattern: "\\brm\\s",
    category: "file_ops",
    severity: "warning",
    platform: "unix",
  },
  {
    id: "rm_rf_root",
    label: "rm -rf /",
    description: "递归删除根目录",
    pattern: "rm\\s+-rf\\s+\\/(?:\\s|$)",
    category: "file_ops",
    severity: "critical",
    platform: "unix",
  },
  {
    id: "sudo",
    label: "sudo 提权",
    description: "以超级用户权限执行",
    pattern: "\\bsudo\\s",
    category: "system",
    severity: "critical",
    platform: "unix",
  },
  {
    id: "curl_pipe_sh",
    label: "curl | sh",
    description: "从网络下载并直接执行脚本",
    pattern: "\\bcurl\\s.*\\|\\s*(sh|bash|zsh)",
    category: "network",
    severity: "warning",
    platform: "unix",
  },
  {
    id: "git_push",
    label: "git push / reset --hard",
    description: "Git 远程推送或硬重置",
    pattern: "\\bgit\\s+(push|reset\\s+--hard|clean\\s+-fd)",
    category: "vcs",
    severity: "warning",
    platform: "all",
  },
  {
    id: "win_del_force",
    label: "del /f /s /q 强制删除",
    description: "强制递归删除整个驱动器",
    pattern: "\\bdel\\s+\\/f\\s+\\/s\\s+\\/q\\s+[a-z]:\\\\",
    category: "windows",
    severity: "critical",
    platform: "windows",
  },
];

export function classifyShellCommand(command: string): {
  severity: "safe" | PatternSeverity;
  matchedRules: string[];
} {
  const normalized = command.trim();
  if (!normalized) return { severity: "safe", matchedRules: [] };
  const matches = BUILTIN_PATTERNS.filter((rule) => {
    const flags = rule.platform === "windows" ? "i" : undefined;
    return new RegExp(rule.pattern, flags).test(normalized);
  });
  if (!matches.length) return { severity: "safe", matchedRules: [] };
  return {
    severity: matches.some((m) => m.severity === "critical") ? "critical" : "warning",
    matchedRules: matches.map((m) => m.id),
  };
}

/**
 * Shell command safety patterns — classify a command before it runs.
 *
 * Full 61-rule set reflowed from deeppath-agent's
 * `src/harness/safety-patterns.ts`; the framework is now the source of truth
 * (agent will import from here in A4). Kept in lockstep with the Python twin
 * `packages/agent-harness/py/src/steerable_agent_harness/safety.py` via the
 * conformance case `tests/conformance/cases/safety/classify_shell_command.yaml`.
 *
 * Rule order matters: `matchedRules` follows BUILTIN_PATTERNS order so both
 * languages return identical lists.
 */

export interface CommandSafetyConfig {
  disabledPatternIds?: string[];
  hiddenPatternIds?: string[];
  customPatterns?: Array<{
    id: string;
    label: string;
    pattern: string;
    category: string;
    enabled: boolean;
  }>;
}

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

// ─── Categories ──────────────────────────────────────────────────────

export const SAFETY_CATEGORIES: Record<string, string> = {
  file_ops: "文件操作",
  system: "系统管理",
  process: "进程管理",
  network: "网络安全",
  package: "包管理",
  vcs: "版本控制",
  container: "容器管理",
  file_write: "文件写入",
  windows: "Windows / PowerShell",
};

// ─── Built-in patterns ───────────────────────────────────────────────
// Merged from deeppath-agent local-actions.ts (web, severity=warning) and
// local-executor.ts (desktop, severity=critical). Keep aligned with the
// Python twin.

export const BUILTIN_PATTERNS: SafetyPatternDef[] = [
  // ── file_ops ──
  { id: "rm", label: "rm 删除", description: "匹配 rm 命令", pattern: "\\brm\\s", category: "file_ops", severity: "warning", platform: "unix" },
  { id: "rm_end", label: "rm（行尾）", description: "匹配行尾的 rm", pattern: "\\brm$", category: "file_ops", severity: "warning", platform: "unix" },
  { id: "rm_rf_root", label: "rm -rf /", description: "递归删除根目录", pattern: "rm\\s+-rf\\s+\\/(?:\\s|$)", category: "file_ops", severity: "critical", platform: "unix" },
  { id: "rmdir", label: "rmdir 删除目录", description: "匹配 rmdir 命令", pattern: "\\brmdir\\s", category: "file_ops", severity: "warning", platform: "unix" },
  { id: "mv", label: "mv 移动/重命名", description: "匹配 mv 命令", pattern: "\\bmv\\s.*\\/", category: "file_ops", severity: "warning", platform: "unix" },
  { id: "shred", label: "shred 安全擦除", description: "不可恢复地擦除文件", pattern: "\\bshred\\b", category: "file_ops", severity: "warning", platform: "unix" },
  { id: "truncate", label: "truncate 截断文件", description: "截断文件内容", pattern: "\\btruncate\\b", category: "file_ops", severity: "warning", platform: "unix" },

  // ── system ──
  { id: "sudo", label: "sudo 提权", description: "以超级用户权限执行", pattern: "\\bsudo\\s", category: "system", severity: "critical", platform: "unix" },
  { id: "mkfs", label: "mkfs 格式化磁盘", description: "创建文件系统（格式化）", pattern: "\\bmkfs\\b", category: "system", severity: "critical", platform: "unix" },
  { id: "dd", label: "dd 磁盘写入", description: "底层磁盘数据复制", pattern: "\\bdd\\s", category: "system", severity: "warning", platform: "unix" },
  { id: "dd_if", label: "dd if= 磁盘镜像", description: "使用 dd if= 读写磁盘", pattern: "\\bdd\\s+if=", category: "system", severity: "critical", platform: "unix" },
  { id: "format", label: "format 格式化", description: "格式化磁盘", pattern: "\\bformat\\b", category: "system", severity: "warning", platform: "all" },
  { id: "shutdown", label: "shutdown 关机", description: "关闭系统", pattern: "\\bshutdown\\b", category: "system", severity: "warning", platform: "all" },
  { id: "reboot", label: "reboot 重启", description: "重启系统", pattern: "\\breboot\\b", category: "system", severity: "warning", platform: "all" },
  { id: "chmod", label: "chmod 修改权限", description: "修改文件权限", pattern: "\\bchmod\\s", category: "system", severity: "warning", platform: "unix" },
  { id: "chmod_777_root", label: "chmod -R 777 /", description: "递归赋予根目录所有权限", pattern: "chmod\\s+-R\\s+777\\s+\\/(?:\\s|$)", category: "system", severity: "critical", platform: "unix" },
  { id: "chown", label: "chown 修改所有者", description: "修改文件所有者", pattern: "\\bchown\\s", category: "system", severity: "warning", platform: "unix" },
  { id: "fork_bomb", label: "Fork Bomb", description: ":(){ :|:& };: fork 炸弹", pattern: ":\\(\\)\\s*\\{\\s*:\\|:&\\s*\\};:", category: "system", severity: "critical", platform: "unix" },

  // ── process ──
  { id: "kill", label: "kill 终止进程", description: "向进程发送信号", pattern: "\\bkill\\s", category: "process", severity: "warning", platform: "unix" },
  { id: "killall", label: "killall 终止所有", description: "按名称终止进程", pattern: "\\bkillall\\s", category: "process", severity: "warning", platform: "unix" },

  // ── network ──
  { id: "curl_pipe_sh", label: "curl | sh", description: "从网络下载并直接执行脚本", pattern: "\\bcurl\\s.*\\|\\s*(sh|bash|zsh)", category: "network", severity: "warning", platform: "unix" },
  { id: "wget_pipe_sh", label: "wget | sh", description: "从网络下载并直接执行脚本", pattern: "\\bwget\\s.*\\|\\s*(sh|bash|zsh)", category: "network", severity: "warning", platform: "unix" },
  { id: "redirect_dev", label: "重定向到 /dev/", description: "向设备文件写入数据", pattern: ">\\s*\\/dev\\/", category: "network", severity: "warning", platform: "unix" },

  // ── package ──
  { id: "npm_publish", label: "npm publish", description: "发布/取消发布 npm 包", pattern: "\\bnpm\\s+(publish|unpublish)", category: "package", severity: "warning", platform: "all" },
  { id: "pip_install", label: "pip install", description: "安装 Python 包", pattern: "\\bpip\\s+install\\b", category: "package", severity: "warning", platform: "all" },
  { id: "npm_install", label: "npm install", description: "安装 npm 包", pattern: "\\bnpm\\s+install\\b", category: "package", severity: "warning", platform: "all" },
  { id: "yarn_add", label: "yarn add", description: "添加 yarn 依赖", pattern: "\\byarn\\s+add\\b", category: "package", severity: "warning", platform: "all" },
  { id: "pnpm_add", label: "pnpm add", description: "添加 pnpm 依赖", pattern: "\\bpnpm\\s+add\\b", category: "package", severity: "warning", platform: "all" },
  { id: "uv_add", label: "uv add", description: "添加 uv 依赖", pattern: "\\buv\\s+add\\b", category: "package", severity: "warning", platform: "all" },
  { id: "apt_install", label: "apt install/remove", description: "系统包管理器操作", pattern: "\\bapt(-get)?\\s+(install|remove|purge)", category: "package", severity: "warning", platform: "unix" },
  { id: "brew_install", label: "brew install/uninstall", description: "Homebrew 包管理器操作", pattern: "\\bbrew\\s+(install|uninstall|remove)", category: "package", severity: "warning", platform: "unix" },

  // ── vcs ──
  { id: "git_push", label: "git push / reset --hard", description: "Git 远程推送或硬重置", pattern: "\\bgit\\s+(push|reset\\s+--hard|clean\\s+-fd)", category: "vcs", severity: "warning", platform: "all" },

  // ── container ──
  { id: "docker_rm", label: "docker rm/rmi/prune", description: "删除容器/镜像或清理系统", pattern: "\\bdocker\\s+(rm|rmi|system\\s+prune)", category: "container", severity: "warning", platform: "all" },

  // ── file_write ──
  { id: "redirect_overwrite", label: "> / >> 重定向写入", description: "文件重定向覆盖或追加", pattern: "\\b(>\\s|>>)\\s*[^|]", category: "file_write", severity: "warning", platform: "all" },
  { id: "tee", label: "tee 写入文件", description: "将输出写入文件", pattern: "\\btee\\s", category: "file_write", severity: "warning", platform: "unix" },
  { id: "sed_inplace", label: "sed -i 原地修改", description: "直接修改文件内容", pattern: "\\bsed\\s+-i", category: "file_write", severity: "warning", platform: "unix" },

  // ── windows ──
  { id: "win_del", label: "del 删除", description: "Windows 删除命令", pattern: "\\bdel\\s", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_rd", label: "rd 删除目录", description: "Windows 删除目录", pattern: "\\brd\\s", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_rd_end", label: "rd（行尾）", description: "匹配行尾的 rd", pattern: "\\brd$", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_rdel", label: "rdel", description: "Windows rdel 命令", pattern: "\\brdel\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_del_force", label: "del /f /s /q 强制删除", description: "强制递归删除整个驱动器", pattern: "\\bdel\\s+\\/f\\s+\\/s\\s+\\/q\\s+[a-z]:\\\\", category: "windows", severity: "critical", platform: "windows" },
  { id: "win_rd_force", label: "rd /s /q 强制删除目录", description: "强制递归删除整个驱动器目录", pattern: "\\brd\\s+\\/s\\s+\\/q\\s+[a-z]:\\\\", category: "windows", severity: "critical", platform: "windows" },
  { id: "win_remove_item", label: "Remove-Item", description: "PowerShell 删除项", pattern: "\\bRemove-Item\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_stop_process", label: "Stop-Process", description: "PowerShell 终止进程", pattern: "\\bStop-Process\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_stop_computer", label: "Stop-Computer", description: "PowerShell 关机", pattern: "\\bStop-Computer\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_restart_computer", label: "Restart-Computer", description: "PowerShell 重启", pattern: "\\bRestart-Computer\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_set_execution_policy", label: "Set-ExecutionPolicy", description: "修改脚本执行策略", pattern: "\\bSet-ExecutionPolicy\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_format_volume", label: "Format-Volume", description: "PowerShell 格式化卷", pattern: "\\bFormat-Volume\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_clear_disk", label: "Clear-Disk", description: "PowerShell 清除磁盘", pattern: "\\bClear-Disk\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_wsl", label: "wsl 子系统", description: "调用 WSL 子系统", pattern: "\\bwsl\\s", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_powershell_cmd", label: "powershell -Command", description: "通过 PowerShell 执行命令", pattern: "\\bpowershell\\s.*-[Cc]ommand", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_pwsh", label: "pwsh", description: "PowerShell Core", pattern: "\\bpwsh\\s", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_cmd_c", label: "cmd /c", description: "CMD 执行命令", pattern: "\\bcmd\\s*\\/c\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_reg", label: "reg delete/add", description: "注册表操作", pattern: "\\breg\\s+(delete|add)\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_net", label: "net user/stop/start", description: "网络和用户管理", pattern: "\\bnet\\s+(user|stop|start)\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_sc", label: "sc delete/stop/config", description: "服务管理", pattern: "\\bsc\\s+(delete|stop|config)\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_diskpart", label: "diskpart", description: "磁盘分区工具", pattern: "\\bdiskpart\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_bcdedit", label: "bcdedit", description: "启动配置编辑", pattern: "\\bbcdedit\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_sfc", label: "sfc", description: "系统文件检查器", pattern: "\\bsfc\\b", category: "windows", severity: "warning", platform: "windows" },
  { id: "win_dism", label: "dism", description: "部署映像服务和管理", pattern: "\\bdism\\b", category: "windows", severity: "warning", platform: "windows" },
  // 只匹配 "format <盘符>:" 形式的磁盘格式化（含 format.com），避免误伤
  // PowerShell 的 Format-List / Format-Table 等格式化输出 cmdlet
  // （Format-Volume 由上面的 win_format_volume 单独覆盖）。
  { id: "win_format_cmd", label: "format（Windows）", description: "Windows 格式化磁盘命令", pattern: "\\bformat(\\.com)?\\s+[a-z]:", category: "windows", severity: "critical", platform: "windows" },
];

// ─── Helpers ─────────────────────────────────────────────────────────

/**
 * 根据用户配置计算当前生效的正则模式列表
 */
export function getActivePatterns(config?: CommandSafetyConfig | null): RegExp[] {
  const disabled = new Set(config?.disabledPatternIds ?? []);

  const patterns: RegExp[] = BUILTIN_PATTERNS.filter((p) => !disabled.has(p.id)).map((p) => {
    const flags = p.platform === "windows" ? "i" : undefined;
    return new RegExp(p.pattern, flags);
  });

  if (config?.customPatterns) {
    for (const cp of config.customPatterns) {
      if (!cp.enabled) continue;
      try {
        patterns.push(new RegExp(cp.pattern));
      } catch {
        // skip invalid regex
      }
    }
  }

  return patterns;
}

/**
 * 按分类分组返回内置模式
 */
export function getPatternsByCategory(): Record<string, SafetyPatternDef[]> {
  const grouped: Record<string, SafetyPatternDef[]> = {};
  for (const p of BUILTIN_PATTERNS) {
    if (!grouped[p.category]) grouped[p.category] = [];
    grouped[p.category].push(p);
  }
  return grouped;
}

/**
 * 默认空配置（所有内置模式均启用，无自定义模式）
 */
export const DEFAULT_COMMAND_SAFETY_CONFIG: CommandSafetyConfig = {
  disabledPatternIds: [],
  hiddenPatternIds: [],
  customPatterns: [],
};

export interface ShellCommandClassification {
  severity: "safe" | PatternSeverity;
  matchedRules: string[];
}

export function classifyShellCommand(
  command: string,
  config?: CommandSafetyConfig | null,
): ShellCommandClassification {
  const normalized = command.trim();
  if (!normalized) {
    return { severity: "safe", matchedRules: [] };
  }

  const disabled = new Set(config?.disabledPatternIds ?? []);
  const matches = BUILTIN_PATTERNS.filter((rule) => !disabled.has(rule.id)).filter((rule) => {
    const flags = rule.platform === "windows" ? "i" : undefined;
    return new RegExp(rule.pattern, flags).test(normalized);
  });

  if (!matches.length) {
    return { severity: "safe", matchedRules: [] };
  }

  const severity = matches.some((item) => item.severity === "critical") ? "critical" : "warning";
  return {
    severity,
    matchedRules: matches.map((item) => item.id),
  };
}

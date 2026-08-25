"""Shell command safety patterns — classify a command before it runs.

Ported from deeppath-agent's `src/harness/safety-patterns.ts` (61 built-in
rules) and kept in lockstep with the TS twin
``packages/agent-harness/ts/src/safety-patterns.ts`` via the conformance case
``tests/conformance/cases/safety/classify_shell_command.yaml``.

The classifier is a pure function: it never executes anything. Products decide
what to do with a ``critical`` / ``warning`` verdict (block, require consent,
or just annotate). Rule order matters: ``matched_rules`` follows
``BUILTIN_PATTERNS`` order so both languages return identical lists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

PatternSeverity = Literal["critical", "warning"]
PatternPlatform = Literal["all", "unix", "windows"]


@dataclass(frozen=True, slots=True)
class SafetyPatternDef:
    id: str
    label: str
    description: str
    pattern: str
    category: str
    severity: PatternSeverity
    platform: PatternPlatform


SAFETY_CATEGORIES: dict[str, str] = {
    "file_ops": "文件操作",
    "system": "系统管理",
    "process": "进程管理",
    "network": "网络安全",
    "package": "包管理",
    "vcs": "版本控制",
    "container": "容器管理",
    "file_write": "文件写入",
    "windows": "Windows / PowerShell",
}

# Merged from deeppath-agent local-actions.ts (web, severity=warning) and
# local-executor.ts (desktop, severity=critical). Keep aligned with the TS twin.
BUILTIN_PATTERNS: list[SafetyPatternDef] = [
    # ── file_ops ──
    SafetyPatternDef("rm", "rm 删除", "匹配 rm 命令", r"\brm\s", "file_ops", "warning", "unix"),
    SafetyPatternDef("rm_end", "rm（行尾）", "匹配行尾的 rm", r"\brm$", "file_ops", "warning", "unix"),
    SafetyPatternDef("rm_rf_root", "rm -rf /", "递归删除根目录", r"rm\s+-rf\s+\/(?:\s|$)", "file_ops", "critical", "unix"),
    SafetyPatternDef("rmdir", "rmdir 删除目录", "匹配 rmdir 命令", r"\brmdir\s", "file_ops", "warning", "unix"),
    SafetyPatternDef("mv", "mv 移动/重命名", "匹配 mv 命令", r"\bmv\s.*\/", "file_ops", "warning", "unix"),
    SafetyPatternDef("shred", "shred 安全擦除", "不可恢复地擦除文件", r"\bshred\b", "file_ops", "warning", "unix"),
    SafetyPatternDef("truncate", "truncate 截断文件", "截断文件内容", r"\btruncate\b", "file_ops", "warning", "unix"),
    # ── system ──
    SafetyPatternDef("sudo", "sudo 提权", "以超级用户权限执行", r"\bsudo\s", "system", "critical", "unix"),
    SafetyPatternDef("mkfs", "mkfs 格式化磁盘", "创建文件系统（格式化）", r"\bmkfs\b", "system", "critical", "unix"),
    SafetyPatternDef("dd", "dd 磁盘写入", "底层磁盘数据复制", r"\bdd\s", "system", "warning", "unix"),
    SafetyPatternDef("dd_if", "dd if= 磁盘镜像", "使用 dd if= 读写磁盘", r"\bdd\s+if=", "system", "critical", "unix"),
    SafetyPatternDef("format", "format 格式化", "格式化磁盘", r"\bformat\b", "system", "warning", "all"),
    SafetyPatternDef("shutdown", "shutdown 关机", "关闭系统", r"\bshutdown\b", "system", "warning", "all"),
    SafetyPatternDef("reboot", "reboot 重启", "重启系统", r"\breboot\b", "system", "warning", "all"),
    SafetyPatternDef("chmod", "chmod 修改权限", "修改文件权限", r"\bchmod\s", "system", "warning", "unix"),
    SafetyPatternDef("chmod_777_root", "chmod -R 777 /", "递归赋予根目录所有权限", r"chmod\s+-R\s+777\s+\/(?:\s|$)", "system", "critical", "unix"),
    SafetyPatternDef("chown", "chown 修改所有者", "修改文件所有者", r"\bchown\s", "system", "warning", "unix"),
    SafetyPatternDef("fork_bomb", "Fork Bomb", ":(){ :|:& };: fork 炸弹", r":\(\)\s*\{\s*:\|:&\s*\};:", "system", "critical", "unix"),
    # ── process ──
    SafetyPatternDef("kill", "kill 终止进程", "向进程发送信号", r"\bkill\s", "process", "warning", "unix"),
    SafetyPatternDef("killall", "killall 终止所有", "按名称终止进程", r"\bkillall\s", "process", "warning", "unix"),
    # ── network ──
    SafetyPatternDef("curl_pipe_sh", "curl | sh", "从网络下载并直接执行脚本", r"\bcurl\s.*\|\s*(sh|bash|zsh)", "network", "warning", "unix"),
    SafetyPatternDef("wget_pipe_sh", "wget | sh", "从网络下载并直接执行脚本", r"\bwget\s.*\|\s*(sh|bash|zsh)", "network", "warning", "unix"),
    SafetyPatternDef("redirect_dev", "重定向到 /dev/", "向设备文件写入数据", r">\s*\/dev\/", "network", "warning", "unix"),
    # ── package ──
    SafetyPatternDef("npm_publish", "npm publish", "发布/取消发布 npm 包", r"\bnpm\s+(publish|unpublish)", "package", "warning", "all"),
    SafetyPatternDef("pip_install", "pip install", "安装 Python 包", r"\bpip\s+install\b", "package", "warning", "all"),
    SafetyPatternDef("npm_install", "npm install", "安装 npm 包", r"\bnpm\s+install\b", "package", "warning", "all"),
    SafetyPatternDef("yarn_add", "yarn add", "添加 yarn 依赖", r"\byarn\s+add\b", "package", "warning", "all"),
    SafetyPatternDef("pnpm_add", "pnpm add", "添加 pnpm 依赖", r"\bpnpm\s+add\b", "package", "warning", "all"),
    SafetyPatternDef("uv_add", "uv add", "添加 uv 依赖", r"\buv\s+add\b", "package", "warning", "all"),
    SafetyPatternDef("apt_install", "apt install/remove", "系统包管理器操作", r"\bapt(-get)?\s+(install|remove|purge)", "package", "warning", "unix"),
    SafetyPatternDef("brew_install", "brew install/uninstall", "Homebrew 包管理器操作", r"\bbrew\s+(install|uninstall|remove)", "package", "warning", "unix"),
    # ── vcs ──
    SafetyPatternDef("git_push", "git push / reset --hard", "Git 远程推送或硬重置", r"\bgit\s+(push|reset\s+--hard|clean\s+-fd)", "vcs", "warning", "all"),
    # ── container ──
    SafetyPatternDef("docker_rm", "docker rm/rmi/prune", "删除容器/镜像或清理系统", r"\bdocker\s+(rm|rmi|system\s+prune)", "container", "warning", "all"),
    # ── file_write ──
    SafetyPatternDef("redirect_overwrite", "> / >> 重定向写入", "文件重定向覆盖或追加", r"\b(>\s|>>)\s*[^|]", "file_write", "warning", "all"),
    SafetyPatternDef("tee", "tee 写入文件", "将输出写入文件", r"\btee\s", "file_write", "warning", "unix"),
    SafetyPatternDef("sed_inplace", "sed -i 原地修改", "直接修改文件内容", r"\bsed\s+-i", "file_write", "warning", "unix"),
    # ── windows ──
    SafetyPatternDef("win_del", "del 删除", "Windows 删除命令", r"\bdel\s", "windows", "warning", "windows"),
    SafetyPatternDef("win_rd", "rd 删除目录", "Windows 删除目录", r"\brd\s", "windows", "warning", "windows"),
    SafetyPatternDef("win_rd_end", "rd（行尾）", "匹配行尾的 rd", r"\brd$", "windows", "warning", "windows"),
    SafetyPatternDef("win_rdel", "rdel", "Windows rdel 命令", r"\brdel\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_del_force", "del /f /s /q 强制删除", "强制递归删除整个驱动器", r"\bdel\s+\/f\s+\/s\s+\/q\s+[a-z]:\\", "windows", "critical", "windows"),
    SafetyPatternDef("win_rd_force", "rd /s /q 强制删除目录", "强制递归删除整个驱动器目录", r"\brd\s+\/s\s+\/q\s+[a-z]:\\", "windows", "critical", "windows"),
    SafetyPatternDef("win_remove_item", "Remove-Item", "PowerShell 删除项", r"\bRemove-Item\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_stop_process", "Stop-Process", "PowerShell 终止进程", r"\bStop-Process\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_stop_computer", "Stop-Computer", "PowerShell 关机", r"\bStop-Computer\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_restart_computer", "Restart-Computer", "PowerShell 重启", r"\bRestart-Computer\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_set_execution_policy", "Set-ExecutionPolicy", "修改脚本执行策略", r"\bSet-ExecutionPolicy\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_format_volume", "Format-Volume", "PowerShell 格式化卷", r"\bFormat-Volume\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_clear_disk", "Clear-Disk", "PowerShell 清除磁盘", r"\bClear-Disk\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_wsl", "wsl 子系统", "调用 WSL 子系统", r"\bwsl\s", "windows", "warning", "windows"),
    SafetyPatternDef("win_powershell_cmd", "powershell -Command", "通过 PowerShell 执行命令", r"\bpowershell\s.*-[Cc]ommand", "windows", "warning", "windows"),
    SafetyPatternDef("win_pwsh", "pwsh", "PowerShell Core", r"\bpwsh\s", "windows", "warning", "windows"),
    SafetyPatternDef("win_cmd_c", "cmd /c", "CMD 执行命令", r"\bcmd\s*\/c\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_reg", "reg delete/add", "注册表操作", r"\breg\s+(delete|add)\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_net", "net user/stop/start", "网络和用户管理", r"\bnet\s+(user|stop|start)\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_sc", "sc delete/stop/config", "服务管理", r"\bsc\s+(delete|stop|config)\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_diskpart", "diskpart", "磁盘分区工具", r"\bdiskpart\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_bcdedit", "bcdedit", "启动配置编辑", r"\bbcdedit\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_sfc", "sfc", "系统文件检查器", r"\bsfc\b", "windows", "warning", "windows"),
    SafetyPatternDef("win_dism", "dism", "部署映像服务和管理", r"\bdism\b", "windows", "warning", "windows"),
    # Only matches "format <drive>:" style disk formatting (incl. format.com),
    # not PowerShell's Format-List / Format-Table output cmdlets (Format-Volume
    # is covered separately above).
    SafetyPatternDef("win_format_cmd", "format（Windows）", "Windows 格式化磁盘命令", r"\bformat(\.com)?\s+[a-z]:", "windows", "critical", "windows"),
]


@dataclass(slots=True)
class CommandSafetyConfig:
    disabled_pattern_ids: list[str] = field(default_factory=list)
    custom_patterns: list[dict] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ShellCommandClassification:
    severity: Literal["safe", "critical", "warning"]
    matched_rules: list[str]


def _compile(rule: SafetyPatternDef) -> re.Pattern[str]:
    flags = re.IGNORECASE if rule.platform == "windows" else 0
    return re.compile(rule.pattern, flags)


def classify_shell_command(
    command: str, config: CommandSafetyConfig | None = None
) -> ShellCommandClassification:
    normalized = command.strip()
    if not normalized:
        return ShellCommandClassification(severity="safe", matched_rules=[])

    disabled = set(config.disabled_pattern_ids if config else [])
    matched = [
        rule
        for rule in BUILTIN_PATTERNS
        if rule.id not in disabled and _compile(rule).search(normalized)
    ]
    if not matched:
        return ShellCommandClassification(severity="safe", matched_rules=[])
    severity: Literal["critical", "warning"] = (
        "critical" if any(r.severity == "critical" for r in matched) else "warning"
    )
    return ShellCommandClassification(
        severity=severity, matched_rules=[r.id for r in matched]
    )


def get_patterns_by_category() -> dict[str, list[SafetyPatternDef]]:
    grouped: dict[str, list[SafetyPatternDef]] = {}
    for p in BUILTIN_PATTERNS:
        grouped.setdefault(p.category, []).append(p)
    return grouped

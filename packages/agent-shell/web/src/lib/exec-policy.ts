/**
 * 会话级命令执行沙箱（类 Codex 的 sandbox_mode）。
 *
 * 作用于下一轮 `chat.stream` 的 `execSandbox`，不是 sidecar 进程沙箱
 * （设置页那一层）。工作区 = 只能写入当前项目；完整权限 = 关闭逐条命令
 * 沙箱。工具审批（允许/拒绝）仍按原策略生效。
 */

export type ExecPolicy = 'workspace' | 'full';

export const EXEC_POLICY_STORAGE_KEY = 'agent-exec-policy';

export function parseExecPolicy(value: unknown): ExecPolicy {
  return value === 'full' ? 'full' : 'workspace';
}

export function readStoredExecPolicy(): ExecPolicy {
  if (typeof localStorage === 'undefined') return 'workspace';
  return parseExecPolicy(localStorage.getItem(EXEC_POLICY_STORAGE_KEY));
}

export function persistExecPolicy(policy: ExecPolicy): void {
  if (typeof localStorage === 'undefined') return;
  localStorage.setItem(EXEC_POLICY_STORAGE_KEY, policy);
}

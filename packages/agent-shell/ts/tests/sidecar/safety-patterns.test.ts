import { describe, expect, it } from 'vitest';
import { classifyShellCommand } from '../../src/sidecar/safety-patterns.js';

describe('safety-patterns', () => {
  it('classifies rm -rf / as critical', () => {
    const result = classifyShellCommand('rm -rf /');
    expect(result.severity).toBe('critical');
    expect(result.matchedRules.length).toBeGreaterThan(0);
  });

  it('classifies pwd as safe', () => {
    const result = classifyShellCommand('pwd');
    expect(result.severity).toBe('safe');
    expect(result.matchedRules).toEqual([]);
  });

  // 回归（2026-07-05 日志）：\bformat\b 把 PowerShell 的 Format-List /
  // Format-Table 误报成"格式化磁盘"critical。
  it('PowerShell Format-List / Format-Table 不误报为格式化磁盘', () => {
    const formatList = classifyShellCommand(
      'Get-ComputerInfo | Select-Object OsName,OsVersion | Format-List',
    );
    expect(formatList.severity).toBe('safe');
    expect(formatList.matchedRules).not.toContain('win_format_cmd');

    const formatTable = classifyShellCommand(
      'Get-ChildItem "C:\\Users\\x\\skills" -Recurse | Format-Table -AutoSize',
    );
    expect(formatTable.matchedRules).not.toContain('win_format_cmd');
  });

  it('format <盘符>: 仍是 critical', () => {
    const result = classifyShellCommand('format D: /q');
    expect(result.severity).toBe('critical');
    expect(result.matchedRules).toContain('win_format_cmd');

    const formatCom = classifyShellCommand('format.com c:');
    expect(formatCom.matchedRules).toContain('win_format_cmd');
  });

  it('Format-Volume 仍被独立规则覆盖', () => {
    const result = classifyShellCommand('Format-Volume -DriveLetter D');
    expect(result.matchedRules).toContain('win_format_volume');
  });
});

import { describe, it, expect } from 'vitest';
import { adaptCommandForPowerShell } from '../src/shell-adapt.js';

describe('adaptCommandForPowerShell', () => {
  describe('cmd 环境变量 %VAR%', () => {
    it('%PATH% → $env:PATH', () => {
      expect(adaptCommandForPowerShell('echo %PATH%')).toBe('echo $env:PATH');
    });

    it('引号内的 %VAR% 不改写', () => {
      expect(adaptCommandForPowerShell(`echo "%PATH%"`)).toBe(`echo "%PATH%"`);
      expect(adaptCommandForPowerShell(`echo '%PATH%'`)).toBe(`echo '%PATH%'`);
    });

    it('孤立的 % 不改写（取模等场景）', () => {
      expect(adaptCommandForPowerShell('Write-Host (5 % 2)')).toBe('Write-Host (5 % 2)');
    });
  });

  describe('bash 风格 && / ||', () => {
    it('A && B → A; if ($?) { B }', () => {
      expect(adaptCommandForPowerShell('cd C:\\proj && npm install')).toBe(
        'cd C:\\proj; if ($?) { npm install }'
      );
    });

    it('A || B → A; if (-not $?) { B }', () => {
      expect(adaptCommandForPowerShell('npm test || echo failed')).toBe(
        'npm test; if (-not $?) { echo failed }'
      );
    });

    it('三段链 A && B && C 从右往左嵌套', () => {
      expect(adaptCommandForPowerShell('a && b && c')).toBe(
        'a; if ($?) { b; if ($?) { c } }'
      );
    });

    it('混合 && / || 保持短路语义', () => {
      expect(adaptCommandForPowerShell('a && b || c')).toBe(
        'a; if ($?) { b; if (-not $?) { c } }'
      );
    });

    it('引号内的 && 不拆分', () => {
      expect(adaptCommandForPowerShell(`echo "a && b"`)).toBe(`echo "a && b"`);
    });

    it('畸形命令（结尾悬空 &&）不改写', () => {
      expect(adaptCommandForPowerShell('foo &&')).toBe('foo &&');
    });
  });

  describe('幂等性：合法 PowerShell 原样返回', () => {
    it('纯 PowerShell 命令不变', () => {
      const cmd = 'Get-ChildItem -Force; if ($?) { Write-Host ok }';
      expect(adaptCommandForPowerShell(cmd)).toBe(cmd);
    });

    it('$env: 写法不变', () => {
      expect(adaptCommandForPowerShell('$env:NODE_ENV="dev"; pnpm dev')).toBe(
        '$env:NODE_ENV="dev"; pnpm dev'
      );
    });

    it('空命令不变', () => {
      expect(adaptCommandForPowerShell('')).toBe('');
    });
  });

  describe('组合场景', () => {
    it('%VAR% + && 一起转换', () => {
      expect(adaptCommandForPowerShell('echo %USERPROFILE% && dir')).toBe(
        'echo $env:USERPROFILE; if ($?) { dir }'
      );
    });
  });
});

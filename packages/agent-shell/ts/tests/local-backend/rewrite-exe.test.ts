import { describe, expect, it } from 'vitest';
import { rewriteExeCommandIfNeeded } from '../../src/local-executor.js';

describe('rewriteExeCommandIfNeeded', () => {
  it('does not modify commands on Windows platform', () => {
    const originalPlatform = process.platform;
    Object.defineProperty(process, 'platform', { value: 'win32' });

    try {
      const input = '"/path/to/app_cli.exe" --help';
      const result = rewriteExeCommandIfNeeded(input);
      expect(result).toBe(input);
    } finally {
      Object.defineProperty(process, 'platform', { value: originalPlatform });
    }
  });

  it('rewrites .exe commands on non-Windows platforms', () => {
    // Run the test in non-win32 context
    const originalPlatform = process.platform;
    if (originalPlatform === 'win32') {
      Object.defineProperty(process, 'platform', { value: 'darwin' });
    }

    try {
      const testCases = [
        {
          input: '"/path/to/app_cli.exe" --help',
          expected: 'wine "/path/to/app_cli.exe" --help'
        },
        {
          input: './app_cli.exe --help',
          expected: 'wine ./app_cli.exe --help'
        },
        {
          input: 'cd "/path" && ./app_cli.exe --help',
          expected: 'cd "/path" && wine ./app_cli.exe --help'
        },
        {
          input: 'app_cli.exe --help',
          expected: 'wine app_cli.exe --help'
        },
        {
          input: 'git add app_cli.exe', // shouldn't match parameter
          expected: 'git add app_cli.exe'
        },
        {
          input: 'echo "running app_cli.exe"', // shouldn't match within quotes as plain text
          expected: 'echo "running app_cli.exe"'
        }
      ];

      for (const { input, expected } of testCases) {
        const result = rewriteExeCommandIfNeeded(input);
        const normalizedResult = result.replace(/wine64/g, 'wine');
        expect(normalizedResult).toBe(expected);
      }
    } finally {
      Object.defineProperty(process, 'platform', { value: originalPlatform });
    }
  });
});

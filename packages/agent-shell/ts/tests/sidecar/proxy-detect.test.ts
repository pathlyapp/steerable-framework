import { describe, expect, it } from 'vitest';
import {
  collectAmbientProxyEndpoints,
  detectSystemProxyEndpoints,
  endpointFromProxyUrl,
  envProxyEndpoints,
  parseScutilProxyOutput,
  parseWindowsRegProxyOutput,
} from '../../src/sidecar/proxy-detect.js';

// Captured verbatim from `scutil --proxy` on macOS 25.6 with a Clash-style
// local proxy enabled (the dogfooding case that motivated W4-8).
const SCUTIL_ENABLED = `
<dictionary> {
  ExceptionsList : <array> {
    0 : 192.168.0.0/16
    1 : 10.0.0.0/8
    2 : 172.16.0.0/12
    3 : 127.0.0.1
    4 : localhost
    5 : *.local
  }
  ExcludeSimpleHostnames : 0
  HTTPEnable : 1
  HTTPPort : 7890
  HTTPProxy : 127.0.0.1
  HTTPSEnable : 1
  HTTPSPort : 7890
  HTTPSProxy : 127.0.0.1
  ProxyAutoConfigEnable : 0
}
`;

const SCUTIL_DISABLED = `
<dictionary> {
  HTTPEnable : 0
  HTTPSEnable : 0
  ProxyAutoConfigEnable : 0
}
`;

describe('endpointFromProxyUrl', () => {
  it('parses a full URL with port', () => {
    expect(endpointFromProxyUrl('http://127.0.0.1:7890')).toBe('127.0.0.1:7890');
  });

  it('parses scheme-less host:port', () => {
    expect(endpointFromProxyUrl('127.0.0.1:7890')).toBe('127.0.0.1:7890');
  });

  it('applies the scheme default port when omitted', () => {
    expect(endpointFromProxyUrl('http://proxy.corp.com')).toBe('proxy.corp.com:80');
    expect(endpointFromProxyUrl('https://proxy.corp.com')).toBe('proxy.corp.com:443');
    expect(endpointFromProxyUrl('socks5://127.0.0.1')).toBe('127.0.0.1:1080');
  });

  it('strips auth and path', () => {
    expect(endpointFromProxyUrl('http://user:pass@proxy.corp.com:8080/p')).toBe(
      'proxy.corp.com:8080',
    );
  });

  it('returns null for empty or unparseable values', () => {
    expect(endpointFromProxyUrl('')).toBeNull();
    expect(endpointFromProxyUrl('   ')).toBeNull();
    expect(endpointFromProxyUrl('http://')).toBeNull();
  });
});

describe('envProxyEndpoints', () => {
  it('collects and dedupes the standard variables', () => {
    const endpoints = envProxyEndpoints({
      HTTP_PROXY: 'http://127.0.0.1:7890',
      https_proxy: 'http://127.0.0.1:7890',
      ALL_PROXY: 'socks5://10.0.0.2:1080',
    });
    expect(endpoints.sort()).toEqual(['10.0.0.2:1080', '127.0.0.1:7890']);
  });

  it('ignores unset and malformed values', () => {
    expect(envProxyEndpoints({ HTTP_PROXY: '', HTTPS_PROXY: 'http://' })).toEqual([]);
  });
});

describe('parseScutilProxyOutput', () => {
  it('extracts enabled HTTP/HTTPS endpoints from real output', () => {
    expect(parseScutilProxyOutput(SCUTIL_ENABLED)).toEqual(['127.0.0.1:7890']);
  });

  it('yields nothing when all protocols are disabled', () => {
    expect(parseScutilProxyOutput(SCUTIL_DISABLED)).toEqual([]);
  });

  it('applies default ports and includes SOCKS when enabled', () => {
    const output = `
<dictionary> {
  HTTPEnable : 1
  HTTPProxy : proxy.corp.com
  SOCKSEnable : 1
  SOCKSProxy : 10.0.0.2
  SOCKSPort : 1081
}
`;
    expect(parseScutilProxyOutput(output).sort()).toEqual([
      '10.0.0.2:1081',
      'proxy.corp.com:80',
    ]);
  });
});

describe('parseWindowsRegProxyOutput', () => {
  it('parses a single proxy entry', () => {
    const output = `
HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings
    ProxyServer    REG_SZ    127.0.0.1:7890
`;
    expect(parseWindowsRegProxyOutput(output)).toEqual(['127.0.0.1:7890']);
  });

  it('parses per-protocol entries', () => {
    const output = `
HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings
    ProxyServer    REG_SZ    http=proxy.corp.com:8080;https=proxy.corp.com:8443;socks=10.0.0.2:1080
`;
    expect(parseWindowsRegProxyOutput(output).sort()).toEqual([
      '10.0.0.2:1080',
      'proxy.corp.com:8080',
      'proxy.corp.com:8443',
    ]);
  });

  it('returns empty when ProxyServer is absent', () => {
    const output = `
HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings
    ProxyEnable    REG_DWORD    0x0
`;
    expect(parseWindowsRegProxyOutput(output)).toEqual([]);
  });

  it('returns empty for empty value', () => {
    const output = `
HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings
    ProxyServer    REG_SZ    
`;
    expect(parseWindowsRegProxyOutput(output)).toEqual([]);
  });
});

describe('detectSystemProxyEndpoints', () => {
  it('is empty off-darwin without invoking scutil', async () => {
    await expect(detectSystemProxyEndpoints('linux')).resolves.toEqual([]);
  });
});

describe('collectAmbientProxyEndpoints', () => {
  it('unions env and (off-darwin, empty) system endpoints', async () => {
    const endpoints = await collectAmbientProxyEndpoints({
      HTTPS_PROXY: 'http://127.0.0.1:7890',
    });
    expect(endpoints).toContain('127.0.0.1:7890');
  });
});

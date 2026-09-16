/**
 * WS4 工具契约一致性 + 防漂移门禁。
 *
 * 桌面的 4 个通用编码工具（local_read_file / local_write_file /
 * local_edit_file / local_exec_shell）与框架 steerable_sidecar.workspace_tools
 * 的 read_file / write_file / edit_file / bash 是同一能力的两个产品面。共享
 * 语义核心的唯一来源是框架的 tool_contract.json；本测试把桌面约束到它：
 *
 * 1. schema 一致性：local_* 的必填输入字段 / version 令牌字段符合契约
 *    （桌面扩展字段 cwd / timeout / createDirs 是允许的超集）。
 * 2. version 算法一致性：hashContent 与框架 content_version 同为
 *    sha256-utf8-hex，用契约里的硬编码向量各自断言，防止两侧独立实现漂移。
 * 3. version 令牌协议：read 返回 version；write/edit 带正确 expectedVersion
 *    成功、带过期 expectedVersion 拒绝且不写盘。
 * 4. 结果形状一致性：真实执行 read/write/exec（edit 注入 stub applyEditsFn）
 *    返回契约要求的字段。
 * 5. 同 workspace 同步校验：vendored 副本与框架 canonical 逐字节一致（非同
 *    workspace 时跳过——孤立 CI 看不到框架仓，由框架侧一致性测试兜底）。
 *
 * 契约变更流程：改框架 tool_contract.json（bump version）→ 重新 vendor 到
 * contracts/tool-contract.json → 两侧测试都必须通过。
 */
import { describe, expect, it } from 'vitest';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { ToolRouter } from '../src/tool-router.js';
import { LocalExecutor, hashContent } from '../src/local-executor.js';
import type { ApplyEditsResult } from '../src/local-edit.js';

interface ToolSpec {
  desktopName: string;
  requiredInput: string[];
  optionalInput?: string[];
  requiredResult: string[];
}
interface Contract {
  version: number;
  versionAlgorithm: string;
  versionToken: { resultField: string; inputField: string; onConflict: string };
  tools: Record<string, ToolSpec>;
  versionVectors: Array<{ input: string; sha256: string }>;
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VENDORED_PATH = path.join(HERE, '..', 'contracts', 'tool-contract.json');
// 3.2 起 shell 与 sidecar 同仓：canonical 在 packages/sidecar/py。
const FRAMEWORK_CANONICAL_PATH = path.join(
  HERE,
  '..', '..', '..',
  'sidecar', 'py', 'src', 'steerable_sidecar', 'tool_contract.json',
);

const CONTRACT = JSON.parse(readFileSync(VENDORED_PATH, 'utf-8')) as Contract;

function makeToolRouter(): ToolRouter {
  // schema 是静态的，执行体 / 注册表在 listSchemas 里不被触碰，传最小 stub。
  return new ToolRouter({} as never, { list: () => [] } as never);
}

function schemaFor(router: ToolRouter, name: string) {
  const schema = router.listSchemas().find((s) => s.name === name);
  if (!schema) throw new Error(`desktop tool not registered: ${name}`);
  return schema.inputSchema as { required?: string[]; properties?: Record<string, unknown> };
}

describe('tool-contract / version 算法', () => {
  it('hashContent 与框架 content_version 命中同一组硬编码向量', () => {
    expect(CONTRACT.versionAlgorithm).toBe('sha256-utf8-hex');
    for (const vector of CONTRACT.versionVectors) {
      expect(hashContent(vector.input)).toBe(vector.sha256);
    }
  });
});

describe('tool-contract / schema 一致性', () => {
  const router = makeToolRouter();
  const tokenField = CONTRACT.versionToken.inputField;

  for (const [canonicalName, spec] of Object.entries(CONTRACT.tools)) {
    it(`${canonicalName} → ${spec.desktopName} 必填字段与 version 字段齐全`, () => {
      const schema = schemaFor(router, spec.desktopName);
      const required = new Set(schema.required ?? []);
      const properties = new Set(Object.keys(schema.properties ?? {}));
      for (const field of spec.requiredInput) {
        expect(required.has(field), `${spec.desktopName} 必须 require ${field}`).toBe(true);
        expect(properties.has(field), `${spec.desktopName} 必须声明 ${field}`).toBe(true);
      }
      if (spec.optionalInput) {
        expect(spec.optionalInput).toContain(tokenField);
        expect(properties.has(tokenField), `${spec.desktopName} 必须接受 ${tokenField}`).toBe(true);
      }
    });
  }
});

describe('tool-contract / 执行结果形状与 version 协议', () => {
  // edit 算法单一来源在框架（RPC），这里注入 stub 只验证桌面结果包装形状。
  const stubApplyEdits = async (content: string): Promise<ApplyEditsResult> => ({
    content: `${content}\nedited`,
    diff: '--- a/f\n+++ b/f\n',
    matches: [{ level: 'exact', startLine: 0, oldLineCount: 1 }],
  });

  function setup(): { executor: LocalExecutor; dir: string } {
    const dir = mkdtempSync(path.join(tmpdir(), 'tool-contract-'));
    const executor = new LocalExecutor(undefined, stubApplyEdits);
    return { executor, dir };
  }

  it('read/write/exec/edit 返回契约要求的结果字段', async () => {
    const { executor, dir } = setup();
    try {
      const file = path.join(dir, 'a.txt');
      const written = await executor.writeLocalFile({ path: file, content: 'hello' });
      expect(written.success).toBe(true);
      for (const f of CONTRACT.tools.write_file.requiredResult) {
        expect(written, `write 缺 ${f}`).toHaveProperty(f);
      }

      const read = await executor.readLocalFile({ path: file });
      expect(read.success).toBe(true);
      for (const f of CONTRACT.tools.read_file.requiredResult) {
        expect(read, `read 缺 ${f}`).toHaveProperty(f);
      }

      const edited = await executor.editLocalFile({
        path: file,
        edits: [{ oldText: 'hello', newText: 'world' }],
      });
      expect(edited.success).toBe(true);
      for (const f of CONTRACT.tools.edit_file.requiredResult) {
        expect(edited, `edit 缺 ${f}`).toHaveProperty(f);
      }

      // printf 是 POSIX shell 内建；Windows 默认 PowerShell 没有该命令，
      // 用 echo 替代（本用例验证的是结果字段契约，命令本身无关紧要）。
      const run = await executor.executeShell({
        command: process.platform === 'win32' ? 'echo hi' : 'printf hi',
      });
      expect(run.success).toBe(true);
      for (const f of CONTRACT.tools.bash.requiredResult) {
        expect(run, `exec 缺 ${f}`).toHaveProperty(f);
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('version 令牌协议：过期 expectedVersion 拒绝且不写盘', async () => {
    const { executor, dir } = setup();
    try {
      const file = path.join(dir, 'a.txt');
      // 契约字段名必须与桌面 API 字段一致（漂移在此暴露），随后用字面量保证类型安全。
      expect(CONTRACT.versionToken.inputField).toBe('expectedVersion');
      expect(CONTRACT.versionToken.resultField).toBe('version');

      await executor.writeLocalFile({ path: file, content: 'v1' });
      const read = await executor.readLocalFile({ path: file });
      const token = read.version;
      expect(typeof token).toBe('string');
      expect(token && token.length).toBeGreaterThan(0);

      const ok = await executor.writeLocalFile({ path: file, content: 'v2', expectedVersion: token });
      expect(ok.success).toBe(true);

      const stale = await executor.writeLocalFile({ path: file, content: 'v3', expectedVersion: token });
      expect(stale.success).toBe(false);
      expect(readFileSync(file, 'utf-8')).toBe('v2');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('tool-contract / 同 workspace 同步校验', () => {
  const coLocated = existsSync(FRAMEWORK_CANONICAL_PATH);
  it.runIf(coLocated)(
    'vendored 副本与框架 canonical 逐字节一致',
    () => {
      const canonical = readFileSync(FRAMEWORK_CANONICAL_PATH, 'utf-8');
      const vendored = readFileSync(VENDORED_PATH, 'utf-8');
      expect(
        vendored === canonical,
        'vendored contracts/tool-contract.json 与框架 canonical 漂移——' +
          '请从框架 tool_contract.json 重新 vendor（bump version 后同步）。',
      ).toBe(true);
    },
  );
});

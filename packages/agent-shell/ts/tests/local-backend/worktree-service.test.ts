/**
 * WorktreeService（4.6b）测试——用**真实 git** 在临时仓库里跑。
 *
 * 这个服务的价值全在与 git 的真实交互上（porcelain 解析、info/exclude、
 * merge --no-ff、branch -D），假 GitRunner 只能验证我们自己编的输出格式，
 * 所以这里走真实 git；git 不可用的环境（极端 CI 镜像）整体跳过。
 *
 * 覆盖：create（+幂等 +exclude 写入）/ list（只认托管目录）/ remove
 * （+删分支）/ merge（提交 worktree 改动 + 合并回主仓 + 冲突时保留现场）。
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  WorktreeService,
  slugifyWorktreeName,
} from '../../src/local-backend/worktree-service.js';

function probeGit(): boolean {
  try {
    execFileSync('git', ['--version'], { stdio: 'pipe' });
    return true;
  } catch {
    return false;
  }
}
const HAS_GIT = probeGit();

function git(cwd: string, args: string[]): string {
  return execFileSync('git', args, { cwd, encoding: 'utf8' });
}

/** 建一个有一个初始提交的临时 git 仓库（返回 realpath——git 报的路径
 *  是解析过符号链接的，macOS 上 /var → /private/var）。 */
function makeRepo(): string {
  const dir = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'wt-test-')));
  git(dir, ['init', '-b', 'main']);
  git(dir, ['config', 'user.email', 'test@example.com']);
  git(dir, ['config', 'user.name', 'Test']);
  git(dir, ['config', 'commit.gpgsign', 'false']);
  writeFileSync(path.join(dir, 'README.md'), 'hello\n');
  git(dir, ['add', '.']);
  git(dir, ['commit', '-m', 'init']);
  return dir;
}

describe.skipIf(!HAS_GIT)('worktree-service', () => {
  let repoDir: string;
  let service: WorktreeService;
  const chatId = 'chat-1';

  beforeEach(() => {
    repoDir = makeRepo();
    service = new WorktreeService({
      resolveProject: (id) =>
        id === chatId ? { name: 'proj', folderPath: repoDir } : null,
    });
  });

  afterEach(() => {
    rmSync(repoDir, { recursive: true, force: true });
  });

  it('slugifyWorktreeName 收敛为安全 slug', () => {
    expect(slugifyWorktreeName('Fix Login Bug!')).toBe('fix-login-bug');
    expect(slugifyWorktreeName('  --..--  ')).toBe('wt');
    expect(slugifyWorktreeName('a'.repeat(80))).toHaveLength(40);
  });

  it('未绑定项目时响亮失败', async () => {
    await expect(service.createWorktree('no-project')).rejects.toThrow('绑定项目');
  });

  it('非 git 仓库的项目目录响亮失败', async () => {
    const plain = mkdtempSync(path.join(os.tmpdir(), 'wt-plain-'));
    try {
      const svc = new WorktreeService({
        resolveProject: () => ({ name: 'p', folderPath: plain }),
      });
      await expect(svc.createWorktree(chatId)).rejects.toThrow('不是 git 仓库');
    } finally {
      rmSync(plain, { recursive: true, force: true });
    }
  });

  it('create → list → remove 全生命周期', async () => {
    const wt = await service.createWorktree(chatId, 'demo');
    expect(wt.name).toBe('demo');
    expect(wt.branch).toBe('steerable/demo');
    expect(wt.path).toBe(path.join(repoDir, '.steerable', 'worktrees', 'demo'));
    expect(existsSync(wt.path)).toBe(true);

    // .steerable/ 进了 info/exclude——主检出 status 干净。
    const exclude = readFileSync(path.join(repoDir, '.git', 'info', 'exclude'), 'utf8');
    expect(exclude).toContain('.steerable/');
    expect(git(repoDir, ['status', '--porcelain']).trim()).toBe('');

    // list 只认托管目录 + steerable/ 分支。
    const listed = await service.listWorktrees(chatId);
    expect(listed).toEqual([wt]);

    // 幂等：同名复用，不重复建分支。
    const again = await service.createWorktree(chatId, 'demo');
    expect(again).toEqual(wt);

    const removed = await service.removeWorktree(chatId, 'demo');
    expect(removed.removed).toBe(wt.path);
    expect(removed.branchDeleted).toBe('steerable/demo');
    expect(existsSync(wt.path)).toBe(false);
    expect(await service.listWorktrees(chatId)).toEqual([]);
    // 分支也删了。
    const branches = git(repoDir, ['branch', '--list', 'steerable/*']);
    expect(branches.trim()).toBe('');
  });

  it('无名 worktree 自动加唯一后缀', async () => {
    const a = await service.createWorktree(chatId);
    const b = await service.createWorktree(chatId);
    expect(a.name).toMatch(/^wt-/);
    expect(b.name).toMatch(/^wt-/);
    expect(a.name).not.toBe(b.name);
  });

  it('merge：worktree 改动提交并合并回主仓当前分支，worktree 被清理', async () => {
    const wt = await service.createWorktree(chatId, 'feature-x');
    writeFileSync(path.join(wt.path, 'feature.txt'), 'from worktree\n');

    const result = await service.mergeWorktree(chatId, 'feature-x', 'task: add feature');
    expect(result.merged).toBe('steerable/feature-x');
    expect(result.commit).toBeTruthy();

    // 主仓拿到了文件；worktree 与分支都清掉了。
    expect(readFileSync(path.join(repoDir, 'feature.txt'), 'utf8')).toBe('from worktree\n');
    expect(existsSync(wt.path)).toBe(false);
    expect(await service.listWorktrees(chatId)).toEqual([]);
    const log = git(repoDir, ['log', '--oneline', '-3']);
    expect(log).toContain('Merge steerable/feature-x');
  });

  it('merge：worktree 无改动时直接合并（commit 为 null）', async () => {
    await service.createWorktree(chatId, 'no-change');
    const result = await service.mergeWorktree(chatId, 'no-change', 'task: noop');
    expect(result.commit).toBeNull();
    expect(await service.listWorktrees(chatId)).toEqual([]);
  });

  it('merge 冲突：错误上抛且 worktree 保留（用户可手工处理）', async () => {
    const wt = await service.createWorktree(chatId, 'conflict');
    // 主仓与 worktree 各自改同一行 → 必然冲突。
    writeFileSync(path.join(repoDir, 'README.md'), 'main version\n');
    git(repoDir, ['add', '.']);
    git(repoDir, ['commit', '-m', 'main change']);
    writeFileSync(path.join(wt.path, 'README.md'), 'worktree version\n');

    await expect(
      service.mergeWorktree(chatId, 'conflict', 'task: conflicting edit'),
    ).rejects.toThrow(/merge/i);

    // worktree 与分支都还在，主仓 merge 已中止（不留半合并状态）。
    expect(existsSync(wt.path)).toBe(true);
    const listed = await service.listWorktrees(chatId);
    expect(listed.map((w) => w.name)).toContain('conflict');
    expect(git(repoDir, ['status', '--porcelain'])).not.toContain('UU');
  });

  it('remove 不认识的 worktree 响亮失败', async () => {
    await expect(service.removeWorktree(chatId, 'ghost')).rejects.toThrow('未找到');
  });
});

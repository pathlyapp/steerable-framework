/**
 * 4.6b Worktree（git worktree 隔离）。
 *
 * 在绑定项目的仓库里管理隔离工作区：`<项目根>/.steerable/worktrees/<name>`
 * + 分支 `steerable/<name>`。任务（task_run worktree:true）在隔离区里跑，
 * 主检出不被触碰；完成后由用户选择「合并到主仓」（worktree 内提交 +
 * 主仓 merge）或「丢弃」（remove + 删分支）。
 *
 * 位置选择的取舍：worktree 放在项目根**之内**（而不是 sibling 目录），
 * 这样既有的项目围栏（writableRoots = 项目根、applyProjectCwdSandbox）
 * 天然覆盖它，无需为主检出之外的目录开洞；`.steerable/` 写进
 * `.git/info/exclude`（仓库本地、不动用户的 .gitignore），主检出的
 * `git status` 保持干净。
 *
 * 本模块只依赖注入的 git 执行器与项目根解析器——不 import electron，
 * 保持纯 Node 可测（与 project-registry.ts 同一模式）。
 */

import { execFile } from 'node:child_process';
import { existsSync, realpathSync } from 'node:fs';
import { appendFile, mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';

export interface WorktreeInfo {
  /** worktree 目录名（也是分支名的 slug 部分）。 */
  name: string;
  path: string;
  branch: string;
}

export interface WorktreeProject {
  name: string;
  folderPath: string;
}

/** git 执行缝：默认 execFile；测试可注入假实现。 */
export type GitRunner = (
  args: string[],
  cwd: string,
) => Promise<{ stdout: string; stderr: string }>;

const defaultGitRunner: GitRunner = (args, cwd) =>
  new Promise((resolve, reject) => {
    execFile(
      'git',
      args,
      { cwd, maxBuffer: 8 * 1024 * 1024 },
      (error, stdout, stderr) => {
        if (error) {
          reject(
            new Error(
              `git ${args.join(' ')} 失败: ${stderr.trim() || error.message}`,
            ),
          );
          return;
        }
        resolve({ stdout, stderr });
      },
    );
  });

/** worktree 名 → 目录/分支可用的 slug（小写字母数字中划线，防爆目录注入）。 */
export function slugifyWorktreeName(raw: string): string {
  const slug = raw
    .toLowerCase()
    .replace(/[^a-z0-9一-鿿]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
  return slug || 'wt';
}

export class WorktreeService {
  constructor(
    private readonly deps: {
      resolveProject: (chatId: string) => Promise<WorktreeProject | null>;
      runGit?: GitRunner;
    },
  ) {}

  private git(cwd: string, args: string[]) {
    return (this.deps.runGit ?? defaultGitRunner)(args, cwd);
  }

  /** 项目根下的托管 worktree 根目录。 */
  private managedRoot(projectRoot: string): string {
    return path.join(projectRoot, '.steerable', 'worktrees');
  }

  private async requireProject(chatId: string): Promise<WorktreeProject> {
    const project = await this.deps.resolveProject(chatId);
    if (!project) {
      throw new Error(
        'worktree 需要对话绑定项目（git 仓库）——请先把本对话关联到一个项目。',
      );
    }
    return project;
  }

  /** 校验目标确是 git 仓库（worktree 子命令只在仓库里才有意义）。 */
  private async requireGitRepo(projectRoot: string): Promise<void> {
    try {
      await this.git(projectRoot, ['rev-parse', '--git-dir']);
    } catch {
      throw new Error(`项目目录不是 git 仓库：${projectRoot}`);
    }
  }

  /**
   * 把 `.steerable/` 写进 `.git/info/exclude`（仓库本地排除，不动用户的
   * .gitignore），让托管 worktree 目录不出现在主检出的 git status 里。
   * 幂等。
   */
  private async ensureGitInfoExclude(projectRoot: string): Promise<void> {
    const { stdout } = await this.git(projectRoot, [
      'rev-parse',
      '--git-path',
      'info/exclude',
    ]);
    const excludePath = path.join(projectRoot, stdout.trim());
    const existing = existsSync(excludePath)
      ? await readFile(excludePath, 'utf8')
      : '';
    if (existing.split('\n').some((line) => line.trim() === '.steerable/')) {
      return;
    }
    await mkdir(path.dirname(excludePath), { recursive: true });
    await appendFile(excludePath, '.steerable/\n', 'utf8');
  }

  /**
   * 创建隔离 worktree：分支 `steerable/<name>` 从当前 HEAD 切出。
   * 同名已存在时直接复用（幂等——模型重试不该炸掉已建好的工作区）。
   */
  async createWorktree(
    chatId: string,
    rawName?: string,
  ): Promise<WorktreeInfo> {
    const project = await this.requireProject(chatId);
    await this.requireGitRepo(project.folderPath);
    const name = slugifyWorktreeName(rawName ?? '');
    const suffix = name === 'wt' ? `-${Date.now().toString(36)}` : '';
    const finalName = `${name}${suffix}`;
    const branch = `steerable/${finalName}`;
    const worktreePath = path.join(this.managedRoot(project.folderPath), finalName);

    const existing = await this.listWorktrees(chatId);
    const found = existing.find((w) => w.name === finalName);
    if (found) return found;

    await this.ensureGitInfoExclude(project.folderPath);
    await mkdir(this.managedRoot(project.folderPath), { recursive: true });
    await this.git(project.folderPath, [
      'worktree',
      'add',
      worktreePath,
      '-b',
      branch,
    ]);
    // 返回 realpath（与 listWorktrees 一致）——任务表存它、沙箱围栏用它，
    // 字符串前缀比较不能落在未解析的符号链接形式上（macOS /var 软链）。
    return { name: finalName, path: realpathSync(worktreePath), branch };
  }

  /** 列出本项目的托管 worktree（只认 .steerable/worktrees 下的）。 */
  async listWorktrees(chatId: string): Promise<WorktreeInfo[]> {
    const project = await this.requireProject(chatId);
    await this.requireGitRepo(project.folderPath);
    const { stdout } = await this.git(project.folderPath, [
      'worktree',
      'list',
      '--porcelain',
    ]);
    // git 报的是 realpath（macOS 上 /var → /private/var），而项目根可能
    // 是未解析的符号链接路径——项目根（必然存在）过 realpath 再拼托管后缀，
    // 否则前缀匹配在 macOS 上永远落空（managedRoot 本身可能还没建，
    // 不能直接 realpath）。
    const root = path.join(
      realpathSync(project.folderPath),
      '.steerable',
      'worktrees',
    );
    const out: WorktreeInfo[] = [];
    let currentPath: string | null = null;
    let currentBranch = '';
    const flush = () => {
      if (!currentPath) return;
      const normalized = realpathSync(currentPath);
      if (
        normalized.startsWith(root + path.sep) &&
        currentBranch.startsWith('refs/heads/steerable/')
      ) {
        out.push({
          name: path.basename(normalized),
          path: normalized,
          branch: currentBranch.slice('refs/heads/'.length),
        });
      }
      currentPath = null;
      currentBranch = '';
    };
    for (const line of stdout.split('\n')) {
      if (line.startsWith('worktree ')) {
        flush();
        currentPath = line.slice('worktree '.length);
      } else if (line.startsWith('branch ')) {
        currentBranch = line.slice('branch '.length);
      } else if (line === '') {
        flush();
      }
    }
    flush();
    return out;
  }

  /**
   * 移除 worktree。`deleteBranch` 同时删掉 `steerable/<name>` 分支
   * （丢弃语义）；保留分支用于合并后的清理（合并后分支引用留着无害，
   * 但默认删掉保持分支列表干净——已合并的分支删除是安全的）。
   */
  async removeWorktree(
    chatId: string,
    name: string,
    options: { deleteBranch?: boolean } = {},
  ): Promise<{ removed: string; branchDeleted: string | null }> {
    const project = await this.requireProject(chatId);
    const worktrees = await this.listWorktrees(chatId);
    const target = worktrees.find((w) => w.name === name || w.path === name);
    if (!target) {
      throw new Error(`未找到托管 worktree：${name}`);
    }
    // --force：worktree 里可能躺着任务留下的未提交改动，移除语义就是不要了。
    await this.git(project.folderPath, [
      'worktree',
      'remove',
      '--force',
      target.path,
    ]);
    let branchDeleted: string | null = null;
    if (options.deleteBranch ?? true) {
      try {
        await this.git(project.folderPath, ['branch', '-D', target.branch]);
        branchDeleted = target.branch;
      } catch {
        // 分支可能已被删/从未创建成功——移除 worktree 本身已成功，不因此翻车。
      }
    }
    return { removed: target.path, branchDeleted };
  }

  /**
   * 把 worktree 里的改动合并回主仓当前分支：
   *   1. worktree 内 `git add -A` + 提交（任务 agent 只改文件，不提交）；
   *   2. 主仓 `git merge --no-ff <branch>`；
   *   3. 移除 worktree 并删除已合并分支。
   * 冲突时 `merge --abort` 把主检出还原到干净状态（主检出是用户的工作区，
   * 不能留半合并的冲突现场），worktree 与分支保留，错误如实上抛——用户
   * 可稍后重试或手工处理。
   */
  async mergeWorktree(
    chatId: string,
    name: string,
    commitMessage: string,
  ): Promise<{ merged: string; commit: string | null }> {
    const project = await this.requireProject(chatId);
    const worktrees = await this.listWorktrees(chatId);
    const target = worktrees.find((w) => w.name === name || w.path === name);
    if (!target) {
      throw new Error(`未找到托管 worktree：${name}`);
    }
    let commit: string | null = null;
    const { stdout: status } = await this.git(target.path, [
      'status',
      '--porcelain',
    ]);
    if (status.trim()) {
      await this.git(target.path, ['add', '-A']);
      await this.git(target.path, ['commit', '-m', commitMessage]);
      const { stdout: sha } = await this.git(target.path, [
        'rev-parse',
        'HEAD',
      ]);
      commit = sha.trim();
    }
    try {
      await this.git(project.folderPath, [
        'merge',
        '--no-ff',
        '-m',
        `Merge ${target.branch}`,
        target.branch,
      ]);
    } catch (err) {
      // 主检出是用户的工作区——冲突时还原干净，绝不留 MERGE_HEAD/冲突标记。
      await this.git(project.folderPath, ['merge', '--abort']).catch(() => {});
      throw err;
    }
    await this.removeWorktree(chatId, target.name, { deleteBranch: true });
    return { merged: target.branch, commit };
  }
}

import { useCallback, useEffect, useState } from 'react';
import {
  LuBlocks,
  LuFolderOpen,
  LuLoaderCircle,
  LuTrash2,
} from 'react-icons/lu';
import { getElectronBridge, isElectron } from '@/lib/electron-bridge';
import { getPackHiddenSlashSkills } from '@/packs/registry';

/**
 * SkillsSettingsPanel — 本地技能管理面板（导入目录 / 列表 / 卸载）。
 *
 * 渲染在 `/settings?section=skills` 独立页（AgentLayout 右侧内容区）。
 *
 * 数据与状态完全自管理：挂载时拉一次列表，操作后刷新。后端走
 * local-backend REST（`GET/POST /api/v2/chat-agents/skills*`），无专用 IPC。
 */

const BUILTIN_SKILLS = [
  '00-identity',
  '10-goal',
  '11-loop',
  '12-create-skill',
  '70-plan-mode',
  '80-tool-usage',
  '81-anti-deferred',
  '82-data-grounding',
  '85-local-exec',
  '86-proactive-coding',
  'identity',
  'goal',
  'loop',
  'create-skill',
  'plan-mode',
  'tool-usage',
  'anti-deferred',
  'anti-deferred-execution',
  'data-grounding',
  'local-exec',
  'proactive-coding',
  // 场景包的内置技能不在此硬编码——
  // 由包渲染层的 hiddenSlashSkills 声明，经 isBuiltinSkillName 并入。
];

/** 内置技能判定：shell 内置表 + 包渲染层声明的隐藏技能（包技能随构建拷贝进技能根，属内置）。 */
function isBuiltinSkillName(name: string): boolean {
  return BUILTIN_SKILLS.includes(name) || getPackHiddenSlashSkills().has(name);
}

export function SkillsSettingsPanel() {
  const [skills, setSkills] = useState<any[]>([]);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [importPath, setImportPath] = useState('');
  const [importing, setImporting] = useState(false);
  const [importStatus, setImportStatus] = useState<string | null>(null);
  const [deletingName, setDeletingName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchSkills = useCallback(async () => {
    if (!isElectron()) return;
    setSkillsLoading(true);
    setError(null);
    try {
      const res = await getElectronBridge()!.localBackend.request<{ skills: any[] }>({
        method: 'GET',
        path: '/api/v2/chat-agents/skills',
      });
      setSkills(res.skills || []);
    } catch (err) {
      console.error('获取技能列表失败:', err);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSkillsLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchSkills();
  }, [fetchSkills]);

  const handleBrowseFolder = async () => {
    if (!isElectron()) return;
    try {
      const bridge = getElectronBridge();
      if (bridge?.local?.selectDirectory) {
        setImportStatus('正在打开文件夹选择器...');
        const result = await bridge.local.selectDirectory();
        if (result && !result.canceled && result.filePaths.length > 0) {
          const selectedPath = result.filePaths[0];
          setImportPath(selectedPath);
          setImportStatus(`已选择路径: ${selectedPath}`);
        } else {
          setImportStatus('已取消选择文件夹');
        }
      } else {
        setError('当前版本的客户端不支持文件夹选择器');
      }
    } catch (err) {
      console.error('选择文件夹失败:', err);
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleImportSkill = async () => {
    const trimmed = importPath.trim();
    if (!trimmed) return;
    setImporting(true);
    setError(null);
    setImportStatus('正在导入本地技能...');
    try {
      const res = await getElectronBridge()!.localBackend.request<{ success: boolean; name: string }>({
        method: 'POST',
        path: '/api/v2/chat-agents/skills/import',
        body: { path: trimmed },
      });
      if (res.success) {
        setImportPath('');
        setImportStatus(`导入成功! 已添加技能 "${res.name}"`);
        await fetchSkills();
      } else {
        setError('导入技能失败');
        setImportStatus('导入失败: 无法将技能拷贝到运行目录');
      }
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      setError(errMsg);
      setImportStatus(`导入出错: ${errMsg}`);
    } finally {
      setImporting(false);
    }
  };

  const handleDeleteSkill = async (name: string) => {
    if (isBuiltinSkillName(name)) return;
    setDeletingName(name);
    setError(null);
    try {
      const res = await getElectronBridge()!.localBackend.request<{ success: boolean }>({
        method: 'DELETE',
        path: `/api/v2/chat-agents/skills/delete/${encodeURIComponent(name)}`,
      });
      if (res.success) {
        await fetchSkills();
      } else {
        setError('删除技能失败');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeletingName(null);
    }
  };

  return (
    <div className="space-y-4">
      {/* Import Skill Bar */}
      <div className="bg-agent-muted/30 border border-agent-border/60 rounded-agent-md p-3.5 space-y-2">
        <h4 className="text-xs font-semibold text-agent-foreground flex items-center gap-1.5">
          <LuBlocks className="h-3.5 w-3.5 text-agent-muted-foreground" />
          导入本地技能目录 (Import Local Skill Directory)
        </h4>
        <p className="text-[11px] text-agent-muted-foreground">
          写到当前项目（或工作目录）下 <code>skills/技能名/</code> 的技能会自动出现在下方列表，无需导入。
          也可以在这里导入其它本地目录（包含 <code>SKILL.md</code> 的文件夹，例如：<code>my-team/sql-tools</code>）。
        </p>
        <div className="flex gap-2">
          <div className="relative flex-1 flex items-center">
            <input
              type="text"
              value={importPath}
              onChange={(e) => setImportPath(e.target.value)}
              placeholder="请输入技能路径，或点击右侧选择文件夹"
              className="w-full pl-3 pr-8 h-8 text-xs bg-agent-canvas text-agent-foreground border border-agent-border rounded-agent-md focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
              disabled={importing}
              data-testid="skills-import-path"
            />
            <button
              type="button"
              onClick={handleBrowseFolder}
              disabled={importing}
              className="absolute right-2.5 text-agent-muted-foreground hover:text-agent-foreground transition-colors"
              title="选择本地文件夹"
              data-testid="skills-import-browse"
            >
              <LuFolderOpen className="h-4 w-4" />
            </button>
          </div>
          <button
            type="button"
            onClick={handleImportSkill}
            disabled={importing || !importPath.trim()}
            data-testid="skills-import-submit"
            className={`h-8 px-4 rounded-full text-xs font-medium transition-all ${
              importing || !importPath.trim()
                ? 'bg-agent-muted text-agent-muted-foreground cursor-not-allowed'
                : 'bg-agent-foreground text-agent-canvas hover:opacity-90'
            }`}
          >
            {importing ? (
              <LuLoaderCircle className="h-3 w-3 animate-spin" />
            ) : (
              '导入'
            )}
          </button>
        </div>
        {importStatus && (
          <p
            className="text-[10px] text-agent-muted-foreground bg-agent-muted/10 px-2 py-1 rounded border border-agent-border/20 mt-1"
            data-testid="skills-import-status"
          >
            {importStatus}
          </p>
        )}
      </div>

      {/* Skills List */}
      <div className="space-y-2">
        <h4 className="text-xs font-semibold text-agent-muted-foreground uppercase tracking-wide">
          已加载技能
        </h4>
        {skillsLoading ? (
          <div className="flex items-center gap-2 py-4 text-xs text-agent-muted-foreground">
            <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
            获取已加载技能...
          </div>
        ) : skills.length === 0 ? (
          <div className="text-center py-6 text-xs text-agent-muted-foreground">
            暂无已加载技能
          </div>
        ) : (
          <div className="divide-y divide-agent-border/40 pr-1">
            {skills.map((skill) => {
              const origin = skill.origin as string | undefined;
              const isBuiltin =
                origin === 'builtin' ||
                (origin !== 'user' && origin !== 'workspace' && (skill.isBuiltin ?? isBuiltinSkillName(skill.name)));
              const isWorkspace = origin === 'workspace';
              const canUninstall = !isBuiltin && !isWorkspace && !isBuiltinSkillName(skill.name);
              return (
                <div
                  key={skill.name}
                  className="py-2.5 flex items-start justify-between gap-3 group"
                  data-testid={`skill-row-${skill.name}`}
                  data-skill-name={skill.name}
                >
                  <div className="space-y-1 min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <LuBlocks className="h-3.5 w-3.5 text-agent-muted-foreground flex-shrink-0" />
                      <span className="text-xs font-medium text-agent-foreground truncate">
                        {skill.displayName || skill.name}
                      </span>
                      {skill.displayName && (
                        <span className="text-[10px] text-agent-muted-foreground/70 truncate">
                          {skill.name}
                        </span>
                      )}
                      {isBuiltin && (
                        <span className="px-1 py-0.2 rounded bg-agent-muted text-[9px] text-agent-muted-foreground font-medium flex-shrink-0 scale-95 origin-left">
                          内置
                        </span>
                      )}
                      {isWorkspace && (
                        <span
                          className="px-1 py-0.2 rounded bg-agent-muted text-[9px] text-agent-muted-foreground font-medium flex-shrink-0 scale-95 origin-left"
                          title="来自项目或工作目录的 skills/ 文件夹，写进去即出现在此列表"
                        >
                          工作区
                        </span>
                      )}
                      {skill.layer === 'eager' ? (
                        <span
                          className="px-1 py-0.2 rounded bg-agent-muted text-[9px] text-agent-muted-foreground font-medium flex-shrink-0 scale-95 origin-left"
                          title="常驻层：正文始终注入系统提示词"
                        >
                          常驻
                        </span>
                      ) : (
                        <span
                          className="px-1 py-0.2 rounded bg-agent-muted text-[9px] text-agent-muted-foreground font-medium flex-shrink-0 scale-95 origin-left"
                          title="按需层：仅在目录中列出，模型经 skill 工具按需加载"
                        >
                          按需
                        </span>
                      )}
                      {skill.modelInvocable === false && (
                        <span
                          className="px-1 py-0.2 rounded bg-agent-muted text-[9px] text-agent-muted-foreground font-medium flex-shrink-0 scale-95 origin-left"
                          title="disable-model-invocation：模型不可调用，只能用 /名称 手动触发"
                        >
                          仅手动
                        </span>
                      )}
                    </div>
                    {skill.description && (
                      <p className="text-[11px] text-agent-muted-foreground leading-relaxed line-clamp-2">
                        {skill.description}
                      </p>
                    )}
                  </div>
                  {canUninstall && (
                    <button
                      type="button"
                      onClick={() => handleDeleteSkill(skill.name)}
                      disabled={deletingName === skill.name}
                      className="text-agent-muted-foreground hover:text-agent-destructive p-1 rounded-full hover:bg-agent-muted transition-colors"
                      title="卸载此技能"
                      data-testid={`skill-uninstall-${skill.name}`}
                    >
                      {deletingName === skill.name ? (
                        <LuLoaderCircle className="h-3 w-3 animate-spin" />
                      ) : (
                        <LuTrash2 className="h-3.5 w-3.5" />
                      )}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {error && (
        <div
          className="rounded-agent-md border border-agent-destructive/20 bg-agent-destructive/10 p-2.5 text-xs text-agent-destructive"
          data-testid="skills-error"
        >
          {error}
        </div>
      )}
    </div>
  );
}

import { useState, type ReactNode } from 'react';
import {
  LuCheck,
  LuChevronDown,
  LuFolder,
  LuFolderPen,
  LuFolderPlus,
  LuFolderX,
  LuLoaderCircle,
} from 'react-icons/lu';
import { getElectronBridge, isElectron } from '@/lib/electron-bridge';
import {
  updateChatProject,
  updateProject,
  type LocalProject,
} from '@/lib/local-api';

/**
 * 项目徽章 / 选择器（Codex 式 cwd 指示），渲染在输入框上方的 meta 行里，
 * 与专家选择器并排（项目在前）。
 *
 * 两个导出组件共用同一套按钮 + 上开菜单视觉：
 *   - ChatProjectBadge   — 会话内：显示当前项目（无项目时显示"选择项目"），
 *                          可关联/移动到其他项目、修改项目目录、移出项目。
 *   - ProjectPickerButton — 落地页（还没有 chat）：受控选择器，选中的
 *                          projectId 在首次发消息建会话时一并传入。
 *
 * 菜单用透明全屏 backdrop 关外击（一次性菜单，比 click-outside 管线简单）。
 */

/* ---------------- 共享内部件 ---------------- */

function BadgeButton({
  label,
  active,
  open,
  busy,
  onClick,
  title,
}: {
  label: string;
  /** true = 已关联/已选中项目（实心图标）；false = 未选择（虚线感 + Plus 图标）。 */
  active: boolean;
  open: boolean;
  busy: boolean;
  onClick: () => void;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      title={title}
      className={`flex max-w-full items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] transition-colors disabled:opacity-60 ${
        active
          ? 'text-agent-muted-foreground/80 hover:bg-agent-foreground/5 hover:text-agent-foreground'
          : 'border border-dashed border-agent-border text-agent-muted-foreground/70 hover:border-agent-muted-foreground/50 hover:text-agent-foreground'
      }`}
    >
      {busy ? (
        <LuLoaderCircle className="h-3 w-3 shrink-0 animate-spin" />
      ) : active ? (
        <LuFolder className="h-3 w-3 shrink-0" />
      ) : (
        <LuFolderPlus className="h-3 w-3 shrink-0" />
      )}
      <span className="max-w-[220px] truncate font-medium">{label}</span>
      <LuChevronDown
        className={`h-3 w-3 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
      />
    </button>
  );
}

function MenuShell({
  onClose,
  children,
}: {
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <>
      <div className="fixed inset-0 z-40 cursor-default" onClick={onClose} />
      <div
        role="menu"
        className="absolute bottom-full left-0 z-50 mb-1 w-64 overflow-hidden rounded-agent-md border border-agent-border bg-agent-canvas p-1 shadow-lg"
      >
        {children}
      </div>
    </>
  );
}

function MenuSectionLabel({ children }: { children: ReactNode }) {
  return (
    <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-agent-muted-foreground">
      {children}
    </div>
  );
}

function ProjectMenuRow({
  project,
  selected,
  onClick,
}: {
  project: LocalProject;
  selected?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-agent-foreground transition-colors hover:bg-agent-foreground/5"
      title={project.folderPath}
    >
      <LuFolder className="h-3.5 w-3.5 shrink-0 text-agent-muted-foreground" />
      <span className="min-w-0 flex-1 truncate">{project.name}</span>
      {selected && (
        <LuCheck className="h-3 w-3 shrink-0 text-agent-muted-foreground" />
      )}
    </button>
  );
}

/* ---------------- 会话内徽章（chat-bound） ---------------- */

export function ChatProjectBadge({
  chatId,
  project,
  projects,
  onProjectsChanged,
  onChatProjectChanged,
}: {
  chatId: string;
  /** 当前会话绑定的项目；null = 无项目会话（徽章变为"选择项目"）。 */
  project: LocalProject | null;
  /** 全部项目（用于关联列表）。 */
  projects: LocalProject[];
  /** 项目本身被修改（改目录）后回调，让父组件刷新项目列表。 */
  onProjectsChanged: () => void | Promise<void>;
  /** 会话归属变化后回调（通常是 refreshChats）。 */
  onChatProjectChanged: () => void | Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const otherProjects = project
    ? projects.filter((p) => p.id !== project.id)
    : projects;

  const run = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await action();
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const moveTo = (projectId: string | null) =>
    run(async () => {
      await updateChatProject(chatId, projectId);
      await onChatProjectChanged();
    });

  const changeFolder = () =>
    run(async () => {
      if (!isElectron() || !project) return;
      const result = await getElectronBridge()!.local?.selectDirectory({
        title: `重新选择「${project.name}」绑定的文件夹`,
      });
      if (!result || result.canceled || result.filePaths.length === 0) return;
      await updateProject(project.id, { folderPath: result.filePaths[0] });
      await onProjectsChanged();
    });

  return (
    <div className="relative">
      <BadgeButton
        label={project ? project.name : '选择项目'}
        active={Boolean(project)}
        open={open}
        busy={busy}
        onClick={() => {
          setError(null);
          setOpen((v) => !v);
        }}
        title={
          project
            ? `${project.folderPath}\n点击管理项目归属 / 修改绑定目录`
            : '把当前对话关联到一个项目（Agent 的文件操作将限制在项目目录内）'
        }
      />

      {open && (
        <MenuShell onClose={() => setOpen(false)}>
          {project ? (
            <>
              <MenuSectionLabel>当前项目</MenuSectionLabel>
              <div
                className="flex items-center gap-2 rounded px-2 py-1.5 text-xs text-agent-foreground"
                title={project.folderPath}
              >
                <LuFolder className="h-3.5 w-3.5 shrink-0 text-agent-muted-foreground" />
                <span className="min-w-0 flex-1 truncate font-medium">
                  {project.name}
                </span>
                <LuCheck className="h-3 w-3 shrink-0 text-agent-muted-foreground" />
              </div>
              <div
                className="truncate px-2 pb-1 text-[10px] font-mono text-agent-muted-foreground/70"
                title={project.folderPath}
              >
                {project.folderPath}
              </div>
            </>
          ) : (
            <MenuSectionLabel>关联到项目</MenuSectionLabel>
          )}

          {otherProjects.length > 0 && (
            <>
              {project && (
                <div className="mx-1 my-1 border-t border-agent-border/60" />
              )}
              {project && <MenuSectionLabel>移动到</MenuSectionLabel>}
              <div className="max-h-40 overflow-y-auto">
                {otherProjects.map((p) => (
                  <ProjectMenuRow
                    key={p.id}
                    project={p}
                    onClick={() => void moveTo(p.id)}
                  />
                ))}
              </div>
            </>
          )}
          {!project && projects.length === 0 && (
            <div className="px-2 py-1.5 text-[11px] text-agent-muted-foreground/70">
              还没有项目——先在侧边栏「会话」旁点 📁+ 新建。
            </div>
          )}

          {project && (
            <>
              <div className="mx-1 my-1 border-t border-agent-border/60" />
              <button
                type="button"
                role="menuitem"
                onClick={() => void changeFolder()}
                className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-agent-foreground transition-colors hover:bg-agent-foreground/5"
              >
                <LuFolderPen className="h-3.5 w-3.5 shrink-0 text-agent-muted-foreground" />
                修改项目目录…
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => void moveTo(null)}
                className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-agent-foreground transition-colors hover:bg-agent-foreground/5"
              >
                <LuFolderX className="h-3.5 w-3.5 shrink-0 text-agent-muted-foreground" />
                移出项目（变为无项目对话）
              </button>
            </>
          )}

          {error && (
            <div className="mx-1 mt-1 rounded bg-agent-destructive/10 px-2 py-1 text-[11px] text-agent-destructive">
              {error}
            </div>
          )}
        </MenuShell>
      )}
    </div>
  );
}

/* ---------------- 落地页选择器（受控，无 chat） ---------------- */

export function ProjectPickerButton({
  projects,
  value,
  onChange,
}: {
  projects: LocalProject[];
  /** 当前选中的 projectId；null = 无项目。 */
  value: string | null;
  onChange: (projectId: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const selected = value ? (projects.find((p) => p.id === value) ?? null) : null;

  return (
    <div className="relative">
      <BadgeButton
        label={selected ? selected.name : '选择项目'}
        active={Boolean(selected)}
        open={open}
        busy={false}
        onClick={() => setOpen((v) => !v)}
        title={
          selected
            ? `${selected.folderPath}\n新对话将绑定到此项目`
            : '为新对话选择项目（可不选）'
        }
      />

      {open && (
        <MenuShell onClose={() => setOpen(false)}>
          <MenuSectionLabel>新对话所属项目</MenuSectionLabel>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              onChange(null);
              setOpen(false);
            }}
            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-agent-foreground transition-colors hover:bg-agent-foreground/5"
          >
            <LuFolderX className="h-3.5 w-3.5 shrink-0 text-agent-muted-foreground" />
            <span className="min-w-0 flex-1 truncate">不关联项目</span>
            {!selected && (
              <LuCheck className="h-3 w-3 shrink-0 text-agent-muted-foreground" />
            )}
          </button>
          <div className="max-h-40 overflow-y-auto">
            {projects.map((p) => (
              <ProjectMenuRow
                key={p.id}
                project={p}
                selected={p.id === value}
                onClick={() => {
                  onChange(p.id);
                  setOpen(false);
                }}
              />
            ))}
          </div>
          {projects.length === 0 && (
            <div className="px-2 py-1.5 text-[11px] text-agent-muted-foreground/70">
              还没有项目——先在侧边栏「会话」旁点 📁+ 新建。
            </div>
          )}
        </MenuShell>
      )}
    </div>
  );
}

export default ChatProjectBadge;

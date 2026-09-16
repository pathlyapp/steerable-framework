import { useCallback, useEffect, useMemo, useState } from 'react';
import { LuLoaderCircle, LuSearch } from 'react-icons/lu';
import { isElectron } from '@/lib/electron-bridge';
import {
  listChatAgentSkills,
  listChatAgentTools,
  type ChatAgentSkillOption,
  type ChatAgentToolOption,
  type ChatAgentToolPolicy,
} from '@/lib/local-api';

/**
 * AgentCapabilityFields — 智能体表单里的「能力」部分：技能勾选、技能范围
 * 开关、工具权限。
 *
 * 这些字段是真实生效的，不是展示用的偏好：
 *   - 勾选的技能正文无条件进该智能体的系统提示词（绕过技能自身的触发条件）；
 *   - 关掉「允许其他技能」后，未勾选的技能在注入、按需加载目录、"/技能名"
 *     三处同时消失；
 *   - 工具权限决定每轮给模型的工具列表、`tool_search` 能发现什么，以及
 *     分发层是否接受调用。
 *
 * 技能与工具目录自己拉（挂载即取），父表单只持有草稿值。
 */

/** 表单里的能力面草稿。 */
export interface AgentCapabilityDraft {
  skillIds: string[];
  allowExternalSkills: boolean;
  loadAllSkills: boolean;
  toolPolicy: ChatAgentToolPolicy;
}

export const DEFAULT_CAPABILITY_DRAFT: AgentCapabilityDraft = {
  skillIds: [],
  allowExternalSkills: true,
  loadAllSkills: false,
  toolPolicy: { mode: 'all', tools: [] },
};

const TOOL_POLICY_MODES: Array<{
  mode: ChatAgentToolPolicy['mode'];
  label: string;
  hint: string;
}> = [
  { mode: 'all', label: '全部工具', hint: '不限制，可调用所有已接线的工具。' },
  {
    mode: 'allowlist',
    label: '仅允许勾选',
    hint: '只有勾选的工具进入工具列表，其余连名字都看不到。',
  },
  {
    mode: 'denylist',
    label: '禁用勾选',
    hint: '勾选的工具被拒绝，其余照常可用。',
  },
];

function toggle(list: readonly string[], item: string): string[] {
  return list.includes(item)
    ? list.filter((entry) => entry !== item)
    : [...list, item];
}

function CheckRow({
  checked,
  onChange,
  testId,
  title,
  badge,
  description,
}: {
  checked: boolean;
  onChange: () => void;
  testId: string;
  title: string;
  badge?: string;
  description?: string;
}) {
  return (
    <label
      className="flex cursor-pointer items-start gap-2 rounded px-1.5 py-1.5 transition-colors hover:bg-agent-foreground/5"
      data-testid={testId}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        className="mt-0.5 h-3 w-3 shrink-0 accent-agent-foreground"
      />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[11px] font-medium text-agent-foreground">
            {title}
          </span>
          {badge && (
            <span className="shrink-0 rounded bg-agent-muted px-1 py-0.5 text-[9px] text-agent-muted-foreground">
              {badge}
            </span>
          )}
        </span>
        {description && (
          <span className="mt-0.5 line-clamp-2 block text-[10px] text-agent-muted-foreground">
            {description}
          </span>
        )}
      </span>
    </label>
  );
}

function SwitchRow({
  checked,
  onChange,
  testId,
  label,
  hint,
}: {
  checked: boolean;
  onChange: () => void;
  testId: string;
  label: string;
  hint: string;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-2">
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        data-testid={testId}
        className="mt-0.5 h-3 w-3 shrink-0 accent-agent-foreground"
      />
      <span className="min-w-0 flex-1">
        <span className="block text-[11px] font-medium text-agent-foreground">{label}</span>
        <span className="block text-[10px] text-agent-muted-foreground">{hint}</span>
      </span>
    </label>
  );
}

export function AgentCapabilityFields({
  value,
  onChange,
}: {
  value: AgentCapabilityDraft;
  onChange: (next: AgentCapabilityDraft) => void;
}) {
  const [skills, setSkills] = useState<ChatAgentSkillOption[]>([]);
  const [tools, setTools] = useState<ChatAgentToolOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [skillFilter, setSkillFilter] = useState('');
  const [toolFilter, setToolFilter] = useState('');

  const fetchCatalogs = useCallback(async () => {
    if (!isElectron()) return;
    setLoading(true);
    try {
      const [skillRes, toolRes] = await Promise.all([
        listChatAgentSkills(),
        listChatAgentTools(),
      ]);
      setSkills(skillRes.skills || []);
      setTools(toolRes.tools || []);
    } catch {
      // 目录取不到时只降级为「没有可勾选项」——已保存的 skillIds /
      // toolPolicy 不受影响，表单的其余字段照常可用。
      setSkills([]);
      setTools([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchCatalogs();
  }, [fetchCatalogs]);

  const visibleSkills = useMemo(() => {
    const key = skillFilter.trim().toLowerCase();
    if (!key) return skills;
    return skills.filter((skill) =>
      [skill.id, skill.name, skill.displayName, skill.description]
        .filter(Boolean)
        .some((field) => field.toLowerCase().includes(key)),
    );
  }, [skills, skillFilter]);

  const visibleTools = useMemo(() => {
    const key = toolFilter.trim().toLowerCase();
    if (!key) return tools;
    return tools.filter((tool) =>
      [tool.name, tool.description]
        .filter(Boolean)
        .some((field) => field.toLowerCase().includes(key)),
    );
  }, [tools, toolFilter]);

  const policy = value.toolPolicy;
  const activeMode = TOOL_POLICY_MODES.find((item) => item.mode === policy.mode);

  return (
    <div className="space-y-3 rounded-agent-md border border-agent-border/50 bg-agent-canvas/60 p-3">
      <section className="space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <h5 className="text-[11px] font-semibold text-agent-foreground">技能</h5>
          {value.skillIds.length > 0 && (
            <span className="text-[10px] text-agent-muted-foreground">
              已选 {value.skillIds.length}
            </span>
          )}
        </div>
        <p className="text-[10px] text-agent-muted-foreground">
          勾选的技能正文会常驻该智能体的系统提示词，不必等触发条件命中。
        </p>
        {loading ? (
          <div className="flex items-center gap-2 py-3 text-[11px] text-agent-muted-foreground">
            <LuLoaderCircle className="h-3 w-3 animate-spin" />
            获取技能目录...
          </div>
        ) : skills.length === 0 ? (
          <p className="py-2 text-[11px] text-agent-muted-foreground">
            暂无可勾选的技能，可在「Skill 设置」里导入。
          </p>
        ) : (
          <>
            {skills.length > 8 && (
              <div className="relative">
                <LuSearch className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-agent-muted-foreground" />
                <input
                  type="text"
                  value={skillFilter}
                  onChange={(event) => setSkillFilter(event.target.value)}
                  placeholder="筛选技能"
                  className="h-7 w-full rounded-agent-md border border-agent-border bg-agent-canvas pl-7 pr-2 text-[11px] text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                  data-testid="agent-form-skill-filter"
                />
              </div>
            )}
            <div className="max-h-44 overflow-y-auto" data-testid="agent-form-skills">
              {visibleSkills.map((skill) => (
                <CheckRow
                  key={skill.id}
                  checked={value.skillIds.includes(skill.id)}
                  onChange={() =>
                    onChange({ ...value, skillIds: toggle(value.skillIds, skill.id) })
                  }
                  testId={`agent-form-skill-${skill.id}`}
                  title={skill.displayName || skill.name}
                  badge={skill.layer === 'eager' ? '常驻' : '按需'}
                  description={skill.description}
                />
              ))}
            </div>
          </>
        )}
        <div className="space-y-2 pt-1">
          <SwitchRow
            checked={value.allowExternalSkills}
            onChange={() =>
              onChange({ ...value, allowExternalSkills: !value.allowExternalSkills })
            }
            testId="agent-form-allow-external-skills"
            label="允许使用未勾选的技能"
            hint="关掉后这个智能体只能用上面勾选的技能，其余一律加载不到。"
          />
          <SwitchRow
            checked={value.loadAllSkills}
            onChange={() => onChange({ ...value, loadAllSkills: !value.loadAllSkills })}
            testId="agent-form-load-all-skills"
            label="无视触发条件，加载全部技能"
            hint="内置「智能助手」就是这个模式。会显著增加每轮提示词长度。"
          />
        </div>
      </section>

      <section className="space-y-1.5 border-t border-agent-border/40 pt-3">
        <h5 className="text-[11px] font-semibold text-agent-foreground">工具权限</h5>
        <div className="flex flex-wrap gap-1" data-testid="agent-form-tool-modes">
          {TOOL_POLICY_MODES.map((item) => (
            <button
              key={item.mode}
              type="button"
              onClick={() => onChange({ ...value, toolPolicy: { ...policy, mode: item.mode } })}
              data-testid={`agent-form-tool-mode-${item.mode}`}
              aria-pressed={policy.mode === item.mode}
              className={`h-6 rounded-full px-2.5 text-[10px] font-medium transition-colors ${
                policy.mode === item.mode
                  ? 'bg-agent-foreground text-agent-canvas'
                  : 'bg-agent-muted text-agent-muted-foreground hover:text-agent-foreground'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
        {activeMode && (
          <p className="text-[10px] text-agent-muted-foreground">{activeMode.hint}</p>
        )}
        {policy.mode !== 'all' && (
          <>
            {policy.tools.length === 0 && (
              <p
                className="text-[10px] text-agent-destructive"
                data-testid="agent-form-tool-policy-empty"
              >
                一个工具都没勾选——保存后按「全部工具」处理。
              </p>
            )}
            <div className="relative">
              <LuSearch className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-agent-muted-foreground" />
              <input
                type="text"
                value={toolFilter}
                onChange={(event) => setToolFilter(event.target.value)}
                placeholder="筛选工具"
                className="h-7 w-full rounded-agent-md border border-agent-border bg-agent-canvas pl-7 pr-2 text-[11px] text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                data-testid="agent-form-tool-filter"
              />
            </div>
            <div className="max-h-44 overflow-y-auto" data-testid="agent-form-tools">
              {visibleTools.map((tool) => (
                <CheckRow
                  key={tool.name}
                  checked={policy.tools.includes(tool.name)}
                  onChange={() =>
                    onChange({
                      ...value,
                      toolPolicy: { ...policy, tools: toggle(policy.tools, tool.name) },
                    })
                  }
                  testId={`agent-form-tool-${tool.name}`}
                  title={tool.name}
                  description={tool.description}
                />
              ))}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

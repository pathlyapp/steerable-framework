import { useRef, useState } from 'react';
import { LuActivity, LuBlocks, LuBot, LuChartBar, LuMonitor, LuNetwork, LuPlug, LuSearch, LuSettings, LuShieldCheck } from 'react-icons/lu';
import { useOutletContext, useSearchParams } from 'react-router-dom';
import { isElectron } from '@/lib/electron-bridge';
import { AgentsSettingsPanel } from '@/components/settings/AgentsSettingsPanel';
import { AppearanceSettingsPanel } from '@/components/settings/AppearanceSettingsPanel';
import { DiagnoseSettingsPanel } from '@/components/settings/DiagnoseSettingsPanel';
import { InsightsSettingsPanel } from '@/components/settings/InsightsSettingsPanel';
import {
  LlmSettingsPanel,
  type LlmSaveUi,
  type LlmSettingsPanelHandle,
} from '@/components/settings/LlmSettingsPanel';
import { McpSettingsPanel } from '@/components/settings/McpSettingsPanel';
import { SecuritySettingsPanel } from '@/components/settings/SecuritySettingsPanel';
import { SkillsSettingsPanel } from '@/components/settings/SkillsSettingsPanel';
import { SettingsSaveButton } from '@/components/settings/SettingsSaveButton';
import { TelemetrySettingsPanel } from '@/components/settings/TelemetrySettingsPanel';
import { UsagePanel } from '@/components/settings/UsagePanel';
import { WebSearchSettingsPanel } from '@/components/settings/WebSearchSettingsPanel';
import { getPackSettingsPanels } from '@/packs/registry';
import type { AgentOutletContext } from '@/layouts/AgentLayout';

type SettingsSection = 'skills' | 'mcp' | 'agents' | 'general';

function resolveSection(raw: string | null): SettingsSection {
  if (raw === 'skills' || raw === 'mcp' || raw === 'agents') return raw;
  return 'general';
}

/**
 * `/settings` — 右侧内容区的设置页，按 `?section=` 分成独立页面：
 *   - `skills`  侧栏「Skill 设置」
 *   - `mcp`     侧栏「MCP 设置」
 *   - `agents`  侧栏「智能体管理」
 *   - 缺省/`general`  侧栏底「设置」（界面 / 模型 / 搜索 / 用量 / 安全 / 洞察 / 遥测）
 *
 * Panel 数据自管理（挂载即拉取），页面本身不持有后端状态。
 */
export function SettingsPage() {
  const [searchParams] = useSearchParams();
  const section = resolveSection(searchParams.get('section'));
  const catalog = useOutletContext<AgentOutletContext | null>();
  const llmPanelRef = useRef<LlmSettingsPanelHandle>(null);
  const [llmSaveUi, setLlmSaveUi] = useState<LlmSaveUi>({
    saving: false,
    savedOk: false,
    loading: true,
  });

  const title =
    section === 'mcp'
      ? 'MCP 设置'
      : section === 'skills'
        ? 'Skill 设置'
        : section === 'agents'
          ? '智能体管理'
          : '设置';

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      <header className="flex h-9 flex-shrink-0 items-center justify-between gap-2 border-b border-agent-border px-2.5">
        <h1 className="text-xs font-semibold text-agent-foreground">{title}</h1>
        {section === 'general' && (
          <SettingsSaveButton
            testId="settings-header-save"
            saving={llmSaveUi.saving}
            savedOk={llmSaveUi.savedOk}
            disabled={llmSaveUi.saving || llmSaveUi.loading}
            onClick={() => void llmPanelRef.current?.save()}
          />
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl space-y-4 px-3 py-3">
          {!isElectron() && (
            <p className="rounded-agent-md border border-agent-destructive/20 bg-agent-destructive/10 p-2.5 text-xs text-agent-destructive">
              浏览器预览模式 — 没有 Electron IPC 桥接，部分设置不可用。
            </p>
          )}

          {section === 'skills' && (
            <section className="space-y-2" data-testid="settings-section-skills">
              <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                <LuBlocks className="h-3.5 w-3.5 text-agent-muted-foreground" />
                本地技能
              </h2>
              <SkillsSettingsPanel />
            </section>
          )}

          {section === 'mcp' && (
            <section className="space-y-2" data-testid="settings-section-mcp">
              <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                <LuPlug className="h-3.5 w-3.5 text-agent-muted-foreground" />
                MCP 服务
              </h2>
              <McpSettingsPanel />
            </section>
          )}

          {section === 'agents' && (
            <section className="space-y-2" data-testid="settings-section-agents">
              <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                <LuBot className="h-3.5 w-3.5 text-agent-muted-foreground" />
                智能体
              </h2>
              <AgentsSettingsPanel onCatalogChange={catalog?.refreshAgents} />
            </section>
          )}

          {section === 'general' && (
            <>
              <section className="space-y-2" data-testid="settings-section-appearance">
                <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                  <LuMonitor className="h-3.5 w-3.5 text-agent-muted-foreground" />
                  界面
                </h2>
                <AppearanceSettingsPanel />
              </section>

              <section className="space-y-2" data-testid="settings-section-llm">
                <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                  <LuSettings className="h-3.5 w-3.5 text-agent-muted-foreground" />
                  本地模型设置
                </h2>
                <LlmSettingsPanel
                  ref={llmPanelRef}
                  showFooterSave={false}
                  onSaveUiChange={setLlmSaveUi}
                />
              </section>

              <section className="space-y-2" data-testid="settings-section-web-search">
                <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                  <LuSearch className="h-3.5 w-3.5 text-agent-muted-foreground" />
                  网络搜索
                </h2>
                <WebSearchSettingsPanel />
              </section>

              <section className="space-y-2" data-testid="settings-section-usage">
                <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                  <LuChartBar className="h-3.5 w-3.5 text-agent-muted-foreground" />
                  用量与成本
                </h2>
                <UsagePanel />
              </section>

              <section className="space-y-2" data-testid="settings-section-diagnose">
                <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                  <LuNetwork className="h-3.5 w-3.5 text-agent-muted-foreground" />
                  链路诊断
                </h2>
                <DiagnoseSettingsPanel />
              </section>

              <section className="space-y-2" data-testid="settings-section-security">
                <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                  <LuShieldCheck className="h-3.5 w-3.5 text-agent-muted-foreground" />
                  安全
                </h2>
                <SecuritySettingsPanel />
              </section>

              <section className="space-y-2" data-testid="settings-section-insights">
                <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                  <LuChartBar className="h-3.5 w-3.5 text-agent-muted-foreground" />
                  帮助改进产品
                </h2>
                <InsightsSettingsPanel />
              </section>

              <section className="space-y-2" data-testid="settings-section-telemetry">
                <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                  <LuActivity className="h-3.5 w-3.5 text-agent-muted-foreground" />
                  遥测(OTLP)
                </h2>
                <TelemetrySettingsPanel />
              </section>

              {/* 场景包设置面板（1.2 起由包渲染层贡献）。
                  包经 packs/registry 注册，未激活 flavor 的产物里没有包组件。 */}
              {getPackSettingsPanels().map((panel) => (
                <section
                  key={panel.panelId}
                  className="space-y-2"
                  data-testid={`settings-section-${panel.panelId}`}
                >
                  <h2 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
                    {panel.Icon ? (
                      <panel.Icon className="h-3.5 w-3.5 text-agent-muted-foreground" />
                    ) : null}
                    {panel.title}
                  </h2>
                  <panel.Component />
                </section>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

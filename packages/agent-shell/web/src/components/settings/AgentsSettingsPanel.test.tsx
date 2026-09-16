import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LocalChatAgent } from '@/lib/local-api';

const listChatAgents = vi.fn();
const createChatAgent = vi.fn();
const updateChatAgent = vi.fn();
const archiveChatAgent = vi.fn();
const listChatAgentSkills = vi.fn();
const listChatAgentTools = vi.fn();

vi.mock('@/lib/electron-bridge', () => ({
  isElectron: () => true,
  getElectronBridge: () => ({
    localBackend: { request: vi.fn() },
    local: {},
  }),
}));

vi.mock('@/lib/local-api', () => ({
  listChatAgents: (...args: unknown[]) => listChatAgents(...args),
  createChatAgent: (...args: unknown[]) => createChatAgent(...args),
  updateChatAgent: (...args: unknown[]) => updateChatAgent(...args),
  archiveChatAgent: (...args: unknown[]) => archiveChatAgent(...args),
  listChatAgentSkills: (...args: unknown[]) => listChatAgentSkills(...args),
  listChatAgentTools: (...args: unknown[]) => listChatAgentTools(...args),
}));

const { AgentsSettingsPanel } = await import('./AgentsSettingsPanel');

const builtin: LocalChatAgent = {
  id: 'local-assistant',
  slug: 'local-assistant',
  name: '电脑操作员',
  icon: null,
  color: '#4f46e5',
  description: '默认本地助手',
  rolePrompt: '你是本地离线助手。',
  isBuiltin: true,
  sortOrder: 0,
};

const custom: LocalChatAgent = {
  id: 'geo-advisor',
  slug: null,
  name: '地质顾问',
  icon: null,
  color: '#16a34a',
  description: '答地质问题',
  rolePrompt: '你是地质顾问。',
  isBuiltin: false,
  sortOrder: 1,
};

beforeEach(() => {
  listChatAgents.mockReset();
  createChatAgent.mockReset();
  updateChatAgent.mockReset();
  archiveChatAgent.mockReset();
  listChatAgentSkills.mockReset();
  listChatAgentTools.mockReset();
  listChatAgents.mockResolvedValue({ agents: [builtin], total: 1 });
  createChatAgent.mockResolvedValue({ agent: custom });
  updateChatAgent.mockResolvedValue({ agent: custom });
  archiveChatAgent.mockResolvedValue({ id: custom.id, status: 'archived' });
  listChatAgentSkills.mockResolvedValue({
    skills: [
      {
        id: '90-cflog',
        name: 'cflog',
        displayName: '测井卡片',
        description: '驱动 CIFLog 回放卡片。',
        layer: 'catalog',
      },
      {
        id: '00-identity',
        name: 'identity',
        displayName: '',
        description: '基础身份。',
        layer: 'eager',
      },
    ],
  });
  listChatAgentTools.mockResolvedValue({
    tools: [
      { name: 'local_exec_shell', description: '执行本地命令。', category: 'local' },
      { name: 'local_read_file', description: '读取文件。', category: 'local' },
    ],
  });
});

afterEach(cleanup);

/** 技能/工具勾选项的复选框（CheckRow 把 input 包在 label 里）。 */
function checkbox(row: HTMLElement): HTMLInputElement {
  return row.querySelector('input') as HTMLInputElement;
}

describe('AgentsSettingsPanel', () => {
  it('lists builtin agents and hides archive on them', async () => {
    render(<AgentsSettingsPanel />);
    const row = await screen.findByTestId('agent-row-local-assistant');
    expect(row.textContent).toContain('电脑操作员');
    expect(row.textContent).toContain('内置');
    expect(screen.queryByTestId('agent-archive-local-assistant')).toBeNull();
  });

  it('creates a custom agent and refreshes the catalog', async () => {
    const onCatalogChange = vi.fn(async () => {});
    listChatAgents
      .mockResolvedValueOnce({ agents: [builtin], total: 1 })
      .mockResolvedValueOnce({ agents: [builtin, custom], total: 2 });
    render(<AgentsSettingsPanel onCatalogChange={onCatalogChange} />);
    await screen.findByTestId('agent-row-local-assistant');
    fireEvent.click(screen.getByTestId('agent-add'));
    fireEvent.change(screen.getByTestId('agent-form-name'), {
      target: { value: '地质顾问' },
    });
    fireEvent.change(screen.getByTestId('agent-form-role'), {
      target: { value: '你是地质顾问。' },
    });
    fireEvent.click(screen.getByTestId('agent-form-save'));
    await waitFor(() => {
      expect(createChatAgent).toHaveBeenCalledWith(
        expect.objectContaining({
          name: '地质顾问',
          rolePrompt: '你是地质顾问。',
          // 缺省能力面：不钉技能、不限制——新智能体不该一上手就少功能。
          skillIds: [],
          allowExternalSkills: true,
          loadAllSkills: false,
          toolPolicy: { mode: 'all', tools: [] },
        }),
      );
    });
    await screen.findByTestId('agent-row-geo-advisor');
    expect(onCatalogChange).toHaveBeenCalled();
  });

  it('保存勾选的技能与工具白名单', async () => {
    render(<AgentsSettingsPanel />);
    await screen.findByTestId('agent-row-local-assistant');
    fireEvent.click(screen.getByTestId('agent-add'));
    fireEvent.change(screen.getByTestId('agent-form-name'), {
      target: { value: '测井助手' },
    });

    const cflog = await screen.findByTestId('agent-form-skill-90-cflog');
    fireEvent.click(checkbox(cflog));
    fireEvent.click(screen.getByTestId('agent-form-allow-external-skills'));
    fireEvent.click(screen.getByTestId('agent-form-tool-mode-allowlist'));
    const readFile = await screen.findByTestId('agent-form-tool-local_read_file');
    fireEvent.click(checkbox(readFile));

    fireEvent.click(screen.getByTestId('agent-form-save'));
    await waitFor(() => {
      expect(createChatAgent).toHaveBeenCalledWith(
        expect.objectContaining({
          name: '测井助手',
          skillIds: ['90-cflog'],
          allowExternalSkills: false,
          toolPolicy: { mode: 'allowlist', tools: ['local_read_file'] },
        }),
      );
    });
  });

  it('编辑时回填已保存的能力面', async () => {
    const restricted: LocalChatAgent = {
      ...custom,
      skillIds: ['90-cflog'],
      allowExternalSkills: false,
      loadAllSkills: true,
      toolPolicy: { mode: 'denylist', tools: ['local_exec_shell'] },
    };
    listChatAgents.mockResolvedValue({ agents: [restricted], total: 1 });
    render(<AgentsSettingsPanel />);
    await screen.findByTestId('agent-row-geo-advisor');
    fireEvent.click(screen.getByTestId('agent-edit-geo-advisor'));

    const cflog = await screen.findByTestId('agent-form-skill-90-cflog');
    expect(checkbox(cflog).checked).toBe(true);
    expect(
      (screen.getByTestId('agent-form-allow-external-skills') as HTMLInputElement).checked,
    ).toBe(false);
    expect(
      (screen.getByTestId('agent-form-load-all-skills') as HTMLInputElement).checked,
    ).toBe(true);
    expect(
      screen.getByTestId('agent-form-tool-mode-denylist').getAttribute('aria-pressed'),
    ).toBe('true');
    const shell = await screen.findByTestId('agent-form-tool-local_exec_shell');
    expect(checkbox(shell).checked).toBe(true);
  });

  it('列表行摘要标出受限的智能体，缺省配置不占位', async () => {
    const restricted: LocalChatAgent = {
      ...custom,
      skillIds: ['90-cflog'],
      allowExternalSkills: false,
      toolPolicy: { mode: 'allowlist', tools: ['local_read_file'] },
    };
    listChatAgents.mockResolvedValue({ agents: [builtin, restricted], total: 2 });
    render(<AgentsSettingsPanel />);
    const summary = await screen.findByTestId('agent-capability-geo-advisor');
    expect(summary.textContent).toContain('技能 1');
    expect(summary.textContent).toContain('仅限所选技能');
    expect(summary.textContent).toContain('仅 1 个工具');
    expect(screen.queryByTestId('agent-capability-local-assistant')).toBeNull();
  });

  it('archives a custom agent after two-step confirm', async () => {
    listChatAgents
      .mockResolvedValueOnce({ agents: [builtin, custom], total: 2 })
      .mockResolvedValueOnce({ agents: [builtin], total: 1 });
    render(<AgentsSettingsPanel />);
    await screen.findByTestId('agent-row-geo-advisor');
    const archive = screen.getByTestId('agent-archive-geo-advisor');
    fireEvent.click(archive);
    expect(archiveChatAgent).not.toHaveBeenCalled();
    fireEvent.click(archive);
    await waitFor(() => {
      expect(archiveChatAgent).toHaveBeenCalledWith('geo-advisor');
    });
    await waitFor(() => {
      expect(screen.queryByTestId('agent-row-geo-advisor')).toBeNull();
    });
  });
});

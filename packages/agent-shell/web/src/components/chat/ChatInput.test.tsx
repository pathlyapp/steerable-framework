/**
 * ChatInput 的 streaming 期交互契约（W6-2）：
 *   - Enter 转向失败不再静默——hook 兜底后本组件按结果给反馈：'queued'
 *     提示「已改为排队」，三种结果都清草稿（消息必然已落地）；
 *   - 待发队列横幅展示数量、撤回入口与「停止将丢弃」预警；
 *   - ⌘/Ctrl+Enter 排队快捷键在 streaming 期间有可见提示。
 * 队列 drain / 兜底决策本身由框架 useChatStream 的测试覆盖，这里只验
 * 呈现层接线。
 */
import { useState, type ReactElement } from 'react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SteerOutcome } from '@steerable/agent-ui';
import { ChatInput, type ChatInputProps } from './ChatInput';

async function flushComposerSync() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 60));
  });
}

afterEach(() => cleanup());

function renderInput(overrides: Partial<ChatInputProps> = {}) {
  const props: ChatInputProps = {
    value: '',
    onChange: vi.fn(),
    onSubmit: vi.fn(),
    ...overrides,
  };
  render(<ChatInput {...props} />);
  return props;
}

function pressEnter(init: { metaKey?: boolean } = {}) {
  fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', ...init });
}

describe('ChatInput streaming 期 Enter 转向兜底（W6-2）', () => {
  it('转向被兜底为排队（queued）：清空草稿并提示「已改为排队」', async () => {
    const onSteer = vi.fn<(text: string) => Promise<SteerOutcome>>().mockResolvedValue('queued');
    const onChange = vi.fn();
    renderInput({ value: '补充一下', onChange, isStreaming: true, onSteer });

    pressEnter();
    // 消息已交给 hook（进入待发队列），不是丢进虚空。
    expect(onSteer).toHaveBeenCalledWith('补充一下');

    await act(async () => {});
    expect(onChange).toHaveBeenCalledWith('');
    const notice = screen.getByRole('status');
    expect(notice.textContent).toBe('当前回合无法追加，已加入排队，本轮结束后自动发出');
  });

  it('转向被接受（steered）：清空草稿且不显示排队提示', async () => {
    const onSteer = vi.fn<(text: string) => Promise<SteerOutcome>>().mockResolvedValue('steered');
    const onChange = vi.fn();
    renderInput({ value: '补一句', onChange, isStreaming: true, onSteer });

    pressEnter();
    await act(async () => {});
    expect(onChange).toHaveBeenCalledWith('');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('回合已结束兜底为直发（sent）：清空草稿且不显示排队提示', async () => {
    const onSteer = vi.fn<(text: string) => Promise<SteerOutcome>>().mockResolvedValue('sent');
    const onChange = vi.fn();
    renderInput({ value: '来迟了', onChange, isStreaming: true, onSteer });

    pressEnter();
    await act(async () => {});
    expect(onChange).toHaveBeenCalledWith('');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('⌘/Ctrl+Enter 直接排入 follow-up 队列并清空草稿', () => {
    const onFollowUp = vi.fn();
    const onSteer = vi.fn();
    const onChange = vi.fn();
    renderInput({ value: '排队这条', onChange, isStreaming: true, onFollowUp, onSteer });

    pressEnter({ metaKey: true });
    expect(onFollowUp).toHaveBeenCalledWith('排队这条');
    expect(onChange).toHaveBeenCalledWith('');
    expect(onSteer).not.toHaveBeenCalled();
  });
});

describe('ChatInput 待发队列可见性（W6-2）', () => {
  it('排队横幅展示数量与「停止将丢弃」预警，可撤回单条，快捷键提示可见', () => {
    const onRemoveFollowUp = vi.fn();
    renderInput({
      isStreaming: true,
      onFollowUp: vi.fn(),
      pendingFollowUps: ['第一条', '第二条'],
      onRemoveFollowUp,
    });

    expect(
      screen.getByText('排队中（2）· 本轮结束后自动发出 · 停止将丢弃'),
    ).toBeTruthy();
    expect(screen.getByText('第一条')).toBeTruthy();
    expect(screen.getByText('第二条')).toBeTruthy();
    // 发现性：streaming 期间快捷键区展示「⌘/Ctrl+Enter 排队」。
    expect(screen.getByText('排队')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '撤回排队消息 2' }));
    expect(onRemoveFollowUp).toHaveBeenCalledWith(1);
  });

  it('无排队消息时不渲染横幅', () => {
    renderInput({ isStreaming: true, pendingFollowUps: [] });
    expect(screen.queryByText(/排队中/)).toBeNull();
  });
});

describe('ChatInput 停止按钮的丢弃预警（W6-2）', () => {
  it('有排队消息时 title 提示将丢弃 N 条，无排队时不提示', () => {
    const base: Partial<ChatInputProps> = { isStreaming: true, onCancel: vi.fn() };
    const { rerender } = render(
      <ChatInput
        value=""
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        {...base}
        pendingFollowUps={['第一条', '第二条']}
      />,
    );
    expect(
      screen.getByRole('button', { name: '停止生成' }).getAttribute('title'),
    ).toContain('将丢弃 2 条排队消息');

    rerender(
      <ChatInput
        value=""
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        {...base}
        pendingFollowUps={[]}
      />,
    );
    const title = screen.getByRole('button', { name: '停止生成' }).getAttribute('title');
    expect(title).toContain('停止生成');
    expect(title).not.toContain('丢弃');
  });
});

describe('ChatInput IME composition (Pinyin)', () => {
  it('does not sync the first composing letter until composition ends', () => {
    const onChange = vi.fn();
    function Harness() {
      const [value, setValue] = useState('');
      return (
        <ChatInput
          value={value}
          onChange={(next) => {
            onChange(next);
            setValue(next);
          }}
          onSubmit={vi.fn()}
        />
      );
    }
    render(<Harness />);
    const editor = screen.getByRole('textbox');

    fireEvent.compositionStart(editor);
    editor.textContent = 'n';
    fireEvent.input(editor);

    expect(onChange).not.toHaveBeenCalled();
    expect(editor.textContent).toBe('n');
    expect(editor.getAttribute('data-composing')).toBe('true');

    fireEvent.compositionEnd(editor);
    expect(onChange).toHaveBeenCalledWith('n');
    expect(editor.getAttribute('data-composing')).toBeNull();
  });

  it('does not commit a letter that is followed by compositionstart before the deferred sync', async () => {
    const onChange = vi.fn();
    render(<ChatInput value="" onChange={onChange} onSubmit={vi.fn()} />);
    const editor = screen.getByRole('textbox');

    editor.textContent = 'n';
    fireEvent.input(editor);
    fireEvent.compositionStart(editor);
    await flushComposerSync();

    expect(onChange).not.toHaveBeenCalled();
    expect(editor.getAttribute('data-composing')).toBe('true');
  });

  it('marks composing on IME Process keydown (keyCode 229)', () => {
    render(<ChatInput value="" onChange={vi.fn()} onSubmit={vi.fn()} />);
    const editor = screen.getByRole('textbox');
    fireEvent.keyDown(editor, { key: 'Process', keyCode: 229 });
    expect(editor.getAttribute('data-composing')).toBe('true');
  });

  it('restores composing state on the first Pinyin letter keydown', () => {
    render(<ChatInput value="" onChange={vi.fn()} onSubmit={vi.fn()} />);
    const editor = screen.getByRole('textbox');
    fireEvent.keyDown(editor, { key: 'n' });
    expect(editor.getAttribute('data-composing')).toBe('true');
  });

  it('does not rewrite contenteditable DOM while composing', () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <ChatInput value="" onChange={onChange} onSubmit={vi.fn()} />,
    );
    const editor = screen.getByRole('textbox');

    fireEvent.compositionStart(editor);
    editor.textContent = 'ni';
    rerender(
      <ChatInput value="should-not-apply" onChange={onChange} onSubmit={vi.fn()} />,
    );

    expect(editor.textContent).toBe('ni');
    expect(onChange).not.toHaveBeenCalled();
  });

  it('keeps a text node in an empty editor so IME can attach marked text', () => {
    render(<ChatInput value="" onChange={vi.fn()} onSubmit={vi.fn()} />);
    const editor = screen.getByRole('textbox');
    expect(editor.querySelector('br')).not.toBeNull();
    expect(
      Array.from(editor.childNodes).some((node) => node.nodeType === Node.TEXT_NODE),
    ).toBe(true);
  });

  it('keeps the placeholder mounted so the first IME letter does not remount siblings', () => {
    render(<ChatInput value="" onChange={vi.fn()} onSubmit={vi.fn()} />);
    expect(document.querySelector('.chat-input-placeholder')).not.toBeNull();
  });

  it('still syncs ordinary non-IME typing', async () => {
    const onChange = vi.fn();
    function Harness() {
      const [value, setValue] = useState('');
      return (
        <ChatInput
          value={value}
          onChange={(next) => {
            onChange(next);
            setValue(next);
          }}
          onSubmit={vi.fn()}
        />
      );
    }
    render(<Harness />);
    const editor = screen.getByRole('textbox');
    editor.textContent = 'hello';
    fireEvent.input(editor);
    await flushComposerSync();
    expect(onChange).toHaveBeenCalledWith('hello');
  });

  it('does not mount the highlight mirror before the first letter is committed', () => {
    render(<ChatInput value="" onChange={vi.fn()} onSubmit={vi.fn()} />);
    const editor = screen.getByRole('textbox');
    fireEvent.keyDown(editor, { key: 'n' });
    editor.textContent = 'n';
    fireEvent.input(editor);
    expect(document.querySelector('.chat-input-mirror')).toBeNull();
  });
});

describe('ChatInput composer meta row', () => {
  const agent = {
    id: 'local-assistant',
    slug: 'local-assistant',
    name: '电脑操作员',
    icon: null,
    color: '#7c3aed',
    description: null,
    rolePrompt: null,
    isBuiltin: true,
  };

  function renderComposer(ui: ReactElement) {
    return render(<MemoryRouter>{ui}</MemoryRouter>);
  }

  it('renders the agent picker after leadingChrome, above the input box', () => {
    renderComposer(
      <ChatInput
        value=""
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        currentAgent={agent}
        agents={[agent]}
        onSelectAgent={vi.fn()}
        leadingChrome={<button type="button">选择项目</button>}
      />,
    );

    const row = screen.getByTestId('composer-meta-row');
    const project = screen.getByRole('button', { name: '选择项目' });
    const picker = screen.getByTestId('agent-select');
    const composer = screen.getByTestId('chat-composer');

    expect(row.contains(project)).toBe(true);
    expect(row.contains(picker)).toBe(true);
    expect(
      project.compareDocumentPosition(picker) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      picker.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('clicking another expert switches the current agent instead of inserting @mention', () => {
    const other = {
      ...agent,
      id: 'all-round-assistant',
      slug: 'all-round-assistant',
      name: '智能助手',
    };
    const onSelectAgent = vi.fn();
    const onChange = vi.fn();
    renderComposer(
      <ChatInput
        value=""
        onChange={onChange}
        onSubmit={vi.fn()}
        currentAgent={agent}
        selectedAgentId={agent.id}
        agents={[agent, other]}
        onSelectAgent={onSelectAgent}
      />,
    );

    fireEvent.click(screen.getByTestId('agent-select'));
    fireEvent.click(screen.getByTestId('agent-option-all-round-assistant'));

    expect(onSelectAgent).toHaveBeenCalledWith('all-round-assistant');
    expect(onChange).not.toHaveBeenCalled();
  });

  it('shows selectedAgentId as the current expert even if currentAgent is stale', () => {
    const other = {
      ...agent,
      id: 'all-round-assistant',
      slug: 'all-round-assistant',
      name: '智能助手',
    };
    renderComposer(
      <ChatInput
        value=""
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        currentAgent={agent}
        selectedAgentId={other.id}
        agents={[agent, other]}
        onSelectAgent={vi.fn()}
      />,
    );

    expect(screen.getByTestId('agent-select').textContent).toContain('智能助手');
    fireEvent.click(screen.getByTestId('agent-select'));
    expect(screen.getByTestId('agent-option-all-round-assistant').textContent).toContain('当前');
  });

  it('opens 智能体管理 from the picker footer', () => {
    function LocationProbe() {
      const loc = useLocation();
      return (
        <div data-testid="loc">
          {loc.pathname}
          {loc.search}
        </div>
      );
    }
    render(
      <MemoryRouter>
        <ChatInput
          value=""
          onChange={vi.fn()}
          onSubmit={vi.fn()}
          currentAgent={agent}
          agents={[agent]}
          onSelectAgent={vi.fn()}
        />
        <LocationProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByTestId('agent-select'));
    fireEvent.click(screen.getByTestId('agent-manage'));
    expect(screen.getByTestId('loc').textContent).toBe('/settings?section=agents');
  });

  it('turns an @mention into a chip that click-removes it', () => {
    const onChange = vi.fn();
    function Harness() {
      const [value, setValue] = useState('@电脑操作员 你好');
      return (
        <ChatInput
          value={value}
          onChange={(next) => {
            onChange(next);
            setValue(next);
          }}
          onSubmit={vi.fn()}
          currentAgent={agent}
          agents={[agent]}
          onSelectAgent={vi.fn()}
        />
      );
    }
    renderComposer(<Harness />);

    const chip = screen.getByRole('button', { name: '移除 @电脑操作员' });
    expect(chip.className).toContain('px-1.5');
    expect(chip.className).toContain('py-0.5');
    const remove = chip.querySelector('[data-mention-remove]');
    expect(remove?.className).toContain('opacity-0');
    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith('你好');
  });

  it('renders mention chips inside the editor without blocking trailing text', () => {
    renderComposer(
      <ChatInput
        value="@电脑操作员 111"
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        currentAgent={agent}
        agents={[agent]}
      />,
    );

    const editor = screen.getByRole('textbox');
    const chip = screen.getByRole('button', { name: '移除 @电脑操作员' });
    expect(editor.contains(chip)).toBe(true);
    // Trailing text ' 111' is a sibling node after chip in the editor layout flow
    expect(editor.textContent).toBe('@电脑操作员 111');
    expect(chip.nextSibling?.textContent).toBe(' 111');
  });

  it('turns a tool into a chip that click-removes it and does not block text', () => {
    const onChange = vi.fn();
    function Harness() {
      const [value, setValue] = useState('/mcp__sqlite__query 查询数据');
      return (
        <ChatInput
          value={value}
          onChange={(next) => {
            onChange(next);
            setValue(next);
          }}
          onSubmit={vi.fn()}
        />
      );
    }
    render(<Harness />);

    const chip = screen.getByRole('button', { name: '移除 /mcp__sqlite__query' });
    expect(chip.className).toContain('px-1.5');
    expect(chip.className).toContain('py-0.5');
    expect(chip.getAttribute('data-mention-type')).toBe('mcp');
    const remove = chip.querySelector('[data-mention-remove]');
    expect(remove?.className).toContain('opacity-0');
    expect(chip.nextSibling?.textContent).toBe(' 查询数据');

    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith('查询数据');
  });

  it('turns a skill tool into a chip with amber styling and click-removes it', () => {
    const onChange = vi.fn();
    function Harness() {
      const [value, setValue] = useState('/read-workspace 看看工区');
      return (
        <ChatInput
          value={value}
          onChange={(next) => {
            onChange(next);
            setValue(next);
          }}
          onSubmit={vi.fn()}
          skills={[{ name: 'read-workspace', description: 'Read workspace' }]}
        />
      );
    }
    render(<Harness />);

    const chip = screen.getByRole('button', { name: '移除 /read-workspace' });
    expect(chip.className).toContain('px-1.5');
    expect(chip.className).toContain('py-0.5');
    expect(chip.getAttribute('data-mention-type')).toBe('skill');
    const remove = chip.querySelector('[data-mention-remove]');
    expect(remove?.className).toContain('opacity-0');
    expect(chip.nextSibling?.textContent).toBe(' 看看工区');

    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith('看看工区');
  });

  it('selecting an option from slash menu inserts and renders a tool chip', async () => {
    const onChange = vi.fn();
    function Harness() {
      const [value, setValue] = useState('');
      return (
        <ChatInput
          value={value}
          onChange={(next) => {
            onChange(next);
            setValue(next);
          }}
          onSubmit={vi.fn()}
          skills={[{ name: 'web-search', displayName: '网络搜索', description: '网页检索' }]}
        />
      );
    }
    render(<Harness />);

    const editor = screen.getByRole('textbox');
    editor.textContent = '/';
    fireEvent.input(editor);
    await flushComposerSync();

    const slashOption = screen.getByTestId('slash-option-skill-web-search');
    fireEvent.click(slashOption);
    await flushComposerSync();

    const chip = screen.getByRole('button', { name: '移除 /web-search' });
    expect(chip.className).toContain('px-1.5');
    expect(chip.className).toContain('py-0.5');
    expect(chip.getAttribute('data-mention-type')).toBe('skill');
    expect(onChange).toHaveBeenCalledWith('/web-search ');
  });

  it('filters out internal skills like plan-mode, identity, and tool-usage from slash suggestions', async () => {
    function Harness() {
      const [value, setValue] = useState('');
      return (
        <ChatInput
          value={value}
          onChange={setValue}
          onSubmit={vi.fn()}
          skills={[
            { name: 'identity', id: '00-identity', description: 'Identity skill' },
            { name: 'plan-mode', id: '70-plan-mode', description: 'Plan mode skill' },
            { name: 'tool-usage', id: '80-tool-usage', description: 'Tool usage' },
            { name: 'anti-deferred-execution', id: '81-anti-deferred', description: 'Anti deferred' },
            { name: 'data-grounding', id: '82-data-grounding', description: 'Data grounding' },
            { name: 'local-exec', id: '85-local-exec', description: 'Local exec' },
            { name: 'proactive-coding', id: '86-proactive-coding', description: 'Proactive coding' },
            { name: 'goal', id: '10-goal', displayName: '目标跟踪', description: '目标与验收' },
            { name: 'loop', id: '11-loop', displayName: '循环执行', description: '重复执行' },
            {
              name: 'create-skill',
              id: '12-create-skill',
              displayName: '创建技能',
              description: '创建本地技能',
            },
            { name: 'web-search', displayName: '网络搜索', description: '网页检索' },
          ]}
        />
      );
    }
    render(<Harness />);

    const editor = screen.getByRole('textbox');
    editor.textContent = '/';
    fireEvent.input(editor);
    await flushComposerSync();

    expect(screen.queryByTestId('slash-option-skill-identity')).toBeNull();
    expect(screen.queryByTestId('slash-option-skill-plan-mode')).toBeNull();
    expect(screen.queryByTestId('slash-option-skill-tool-usage')).toBeNull();
    expect(screen.queryByTestId('slash-option-skill-anti-deferred-execution')).toBeNull();
    expect(screen.queryByTestId('slash-option-skill-data-grounding')).toBeNull();
    expect(screen.queryByTestId('slash-option-skill-local-exec')).toBeNull();
    expect(screen.queryByTestId('slash-option-skill-proactive-coding')).toBeNull();
    // 场景包技能的隐藏（如 cflog 的 90-cflog）由包渲染层声明，
    // 覆盖在 packages/pack-cflog/web/index.test.ts。
    expect(screen.getByTestId('slash-option-skill-web-search')).toBeTruthy();
    // 面向用户的内置工作流技能必须留在菜单里
    expect(screen.getByTestId('slash-option-skill-goal')).toBeTruthy();
    expect(screen.getByTestId('slash-option-skill-loop')).toBeTruthy();
    expect(screen.getByTestId('slash-option-skill-create-skill')).toBeTruthy();
  });
});

describe('ChatInput 命令沙箱选择器', () => {
  it('renders the picker and forwards a switch to full access', () => {
    const onExecPolicyChange = vi.fn();
    renderInput({
      execPolicy: 'workspace',
      onExecPolicyChange,
    });

    fireEvent.click(screen.getByTestId('exec-policy-picker'));
    fireEvent.click(screen.getByTestId('exec-policy-full'));
    expect(onExecPolicyChange).toHaveBeenCalledWith('full');
  });

  it('hides the picker when the change handler is omitted', () => {
    renderInput({ execPolicy: 'workspace' });
    expect(screen.queryByTestId('exec-policy-picker')).toBeNull();
  });
});

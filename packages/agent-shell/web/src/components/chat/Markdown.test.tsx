/**
 * 消息气泡里的 token 渲染契约：发出去的消息要和输入框看到的一致——
 * `@专家` / `@对话` 渲染成提及卡片，`/技能` / `/mcp__srv__tool` 渲染成工具
 * 卡片，而路径、URL 这类带斜杠的普通文本必须保持原样。
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { Markdown } from './Markdown';
import type { McpToolItem, SkillItem } from '@/lib/slash-sources';

afterEach(() => cleanup());

const SKILLS: SkillItem[] = [
  { name: 'loop', id: '11-loop', displayName: '循环执行', description: '重复执行' },
  { name: 'goal', id: '10-goal', displayName: '目标跟踪', description: '目标与验收' },
  { name: 'plan-mode', id: '70-plan-mode', description: '计划模式约束' },
];

const MCP_TOOLS: McpToolItem[] = [
  {
    token: 'mcp__sqlite__query',
    toolName: 'query',
    serverKey: 'sqlite',
    serverName: 'SQLite',
    description: '执行 SQL',
  },
];

function renderMessage(content: string) {
  render(
    <Markdown inlineParagraph skills={SKILLS} mcpTools={MCP_TOOLS}>
      {content}
    </Markdown>,
  );
}

describe('Markdown 消息内的工具 token', () => {
  it('把行首的技能 token 渲染成琥珀色工具卡片，后续文字保持原样', () => {
    const { container } = render(
      <Markdown inlineParagraph skills={SKILLS} mcpTools={MCP_TOOLS}>
        {'/loop 5分钟说一次你好'}
      </Markdown>,
    );

    const chip = container.querySelector('[data-message-tool-chip]');
    expect(chip).not.toBeNull();
    expect(chip!.getAttribute('data-tool-type')).toBe('skill');
    expect(chip!.textContent).toBe('/loop');
    expect(chip!.className).toContain('amber');
    expect(container.textContent).toBe('/loop 5分钟说一次你好');
  });

  it('把 MCP token 渲染成天蓝色工具卡片', () => {
    const { container } = render(
      <Markdown inlineParagraph skills={SKILLS} mcpTools={MCP_TOOLS}>
        {'/mcp__sqlite__query 查一下用户表'}
      </Markdown>,
    );

    const chip = container.querySelector('[data-message-tool-chip]');
    expect(chip!.getAttribute('data-tool-type')).toBe('mcp');
    expect(chip!.textContent).toBe('/mcp__sqlite__query');
    expect(chip!.className).toContain('sky');
  });

  it('句中的技能 token 也成卡片', () => {
    const { container } = render(
      <Markdown inlineParagraph skills={SKILLS} mcpTools={MCP_TOOLS}>
        {'帮我用 /goal 把这件事跟到底'}
      </Markdown>,
    );

    const chip = container.querySelector('[data-message-tool-chip]');
    expect(chip!.textContent).toBe('/goal');
    expect(container.textContent).toBe('帮我用 /goal 把这件事跟到底');
  });

  it('路径、URL 与未知 token 保持纯文本', () => {
    const { container } = render(
      <Markdown inlineParagraph skills={SKILLS} mcpTools={MCP_TOOLS}>
        {'看下 /mnt/c 和 /not-a-skill'}
      </Markdown>,
    );

    expect(container.querySelector('[data-message-tool-chip]')).toBeNull();
    expect(container.textContent).toBe('看下 /mnt/c 和 /not-a-skill');
  });

  it('引擎内部技能不渲染成卡片', () => {
    const { container } = render(
      <Markdown inlineParagraph skills={SKILLS} mcpTools={MCP_TOOLS}>
        {'/plan-mode 试试'}
      </Markdown>,
    );

    expect(container.querySelector('[data-message-tool-chip]')).toBeNull();
  });

  it('@提及与 /工具可以共存', () => {
    const { container } = render(
      <Markdown
        inlineParagraph
        skills={SKILLS}
        mcpTools={MCP_TOOLS}
        agents={[
          {
            id: 'a1',
            slug: 'ops',
            name: '电脑操作员',
            icon: null,
            color: '#2563eb',
            description: '',
            rolePrompt: '',
            isBuiltin: true,
            sortOrder: 1,
          },
        ]}
      >
        {'@电脑操作员 用 /loop 每小时跑一次'}
      </Markdown>,
    );

    const toolChip = container.querySelector('[data-message-tool-chip]');
    expect(toolChip!.textContent).toBe('/loop');
    const mentionChip = container.querySelector('[data-mention-chip]');
    expect(mentionChip!.getAttribute('data-mention-type')).toBe('agent');
    expect(screen.getByText('@电脑操作员')).toBeTruthy();
  });

  it('邮箱与认不出的 @token 保持纯文本', () => {
    const { container } = render(
      <Markdown inlineParagraph skills={SKILLS} mcpTools={MCP_TOOLS}>
        {'发到 a@b.com，参数写 @param'}
      </Markdown>,
    );

    expect(container.querySelector('[data-mention-chip]')).toBeNull();
    expect(container.textContent).toBe('发到 a@b.com，参数写 @param');
  });

  it('没有传目录时斜杠 token 保持纯文本', () => {
    const { container } = render(<Markdown inlineParagraph>{'/loop 你好'}</Markdown>);
    expect(container.querySelector('[data-message-tool-chip]')).toBeNull();
    expect(container.textContent).toBe('/loop 你好');
  });
});

describe('Markdown 消息内的代码块', () => {
  it('代码块里的斜杠 token 不做替换', () => {
    const { container } = renderFenced();
    expect(container.querySelector('[data-message-tool-chip]')).toBeNull();
    expect(container.textContent).toContain('/loop');
  });

  function renderFenced() {
    return render(
      <Markdown skills={SKILLS} mcpTools={MCP_TOOLS}>
        {'```\n/loop 5m\n```'}
      </Markdown>,
    );
  }
});

// 保证渲染入口本身没有在无 token 时改写文本节点
describe('Markdown 普通文本', () => {
  it('不含 token 的段落原样渲染', () => {
    renderMessage('普通一句话，没有任何 token。');
    expect(screen.getByText('普通一句话，没有任何 token。')).toBeTruthy();
  });
});

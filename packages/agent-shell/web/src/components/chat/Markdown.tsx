import {
  cloneElement,
  createElement,
  Fragment,
  isValidElement,
  type ComponentPropsWithoutRef,
  type ElementType,
  type ReactNode,
} from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeRaw from 'rehype-raw';
import { LuBlocks, LuMessageSquare, LuPlug } from 'react-icons/lu';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';
import {
  resolveSlashTool,
  type McpToolItem,
  type SkillItem,
} from '@/lib/slash-sources';
import { PlanTodoList } from './PlanTodoList';
import { FilePathCode } from './FilePathCode';
import { looksLikeFilePath } from './path-mentions';
import { stripNextStepsTags } from './next-steps-tags';

/**
 * Markdown renderer shared by `UserMessage` and `AssistantMessage`.
 *
 * Pinned closely to deeppath's renderer so visual parity holds:
 *   - GFM tables / strikethrough via `remark-gfm`
 *   - raw HTML escape via `rehype-raw` (agent backends occasionally emit
 *     `<sub>` / `<sup>` / `<details>` markup)
 *   - links open in new tab with `noopener`
 *   - tables wrap inside a scroll container so wide tables don't blow the
 *     bubble width
 *   - blockquote — the backend tool summary uses
 *       `🔧 \`tool\` ✅`
 *       `> 参数：…`
 *       `> 结果：…`
 *     so blockquote gets a subtle gray left border + indent to read as a
 *     "tool detail callout" instead of plain text.
 *   - lists, headings, hr — explicit margins / typography classes so the
 *     agent's multi-step responses don't collapse into a wall of text.
 *   - cells get `max-width` + word-break so long unbreakable text doesn't
 *     stretch the row
 *   - paragraphs preserve `whitespace: pre-line` so single newlines render
 *     (matches what users typed)
 */

const components: Components = {
  a: (props: ComponentPropsWithoutRef<'a'>) => (
    <a
      {...props}
      className="text-agent-accent hover:underline"
      target="_blank"
      rel="noopener noreferrer"
    />
  ),

  // ── Block-level typography ────────────────────────────────────────────
  h1: (props: ComponentPropsWithoutRef<'h1'>) => (
    <h1
      {...props}
      className="mb-0.5 mt-1.5 text-xs font-semibold text-agent-foreground first:mt-0"
    />
  ),
  h2: (props: ComponentPropsWithoutRef<'h2'>) => (
    <h2
      {...props}
      className="mb-0.5 mt-1.5 text-xs font-semibold text-agent-foreground first:mt-0"
    />
  ),
  h3: (props: ComponentPropsWithoutRef<'h3'>) => (
    <h3
      {...props}
      className="mb-0.5 mt-1.5 text-xs font-semibold text-agent-foreground first:mt-0"
    />
  ),
  h4: (props: ComponentPropsWithoutRef<'h4'>) => (
    <h4
      {...props}
      className="mb-0.5 mt-1.5 text-xs font-semibold text-agent-foreground first:mt-0"
    />
  ),

  p: (props: ComponentPropsWithoutRef<'p'>) => (
    <p
      style={{ whiteSpace: 'pre-line' }}
      {...props}
      className="my-0.5 leading-relaxed"
    />
  ),

  // ── Lists ─────────────────────────────────────────────────────────────
  ul: (props: ComponentPropsWithoutRef<'ul'>) => (
    <ul {...props} className="my-1 list-disc space-y-0.5 pl-4" />
  ),
  ol: (props: ComponentPropsWithoutRef<'ol'>) => (
    <ol {...props} className="my-1 list-decimal space-y-0.5 pl-4" />
  ),
  li: (props: ComponentPropsWithoutRef<'li'>) => (
    <li
      {...props}
      className="leading-relaxed marker:text-agent-muted-foreground/60"
    />
  ),

  // ── Inline emphasis ───────────────────────────────────────────────────
  strong: (props: ComponentPropsWithoutRef<'strong'>) => (
    <strong {...props} className="font-semibold text-agent-foreground" />
  ),
  em: (props: ComponentPropsWithoutRef<'em'>) => (
    <em {...props} className="italic" />
  ),

  // ── Blockquote (backend uses this for `> 参数：` / `> 结果：` lines) ─
  blockquote: (props: ComponentPropsWithoutRef<'blockquote'>) => (
    <blockquote
      {...props}
      className="my-1 border-l-2 border-agent-border bg-agent-muted/40 px-2 py-0.5 text-xs text-agent-muted-foreground [&>p]:my-0"
    />
  ),

  // ── Horizontal rule ──────────────────────────────────────────────────
  hr: (props: ComponentPropsWithoutRef<'hr'>) => (
    <hr {...props} className="my-2 border-agent-border" />
  ),

  // ── Tables ───────────────────────────────────────────────────────────
  table: (props: ComponentPropsWithoutRef<'table'>) => (
    <div className="my-1.5 w-full overflow-x-auto rounded-md border border-agent-border">
      <table {...props} className="w-full border-collapse text-[12px]" />
    </div>
  ),
  thead: (props: ComponentPropsWithoutRef<'thead'>) => (
    <thead {...props} className="bg-agent-muted/60" />
  ),
  th: (props: ComponentPropsWithoutRef<'th'>) => (
    <th
      {...props}
      className="border-b border-agent-border px-2 py-1 text-left font-medium text-agent-foreground"
    />
  ),
  td: (props: ComponentPropsWithoutRef<'td'>) => (
    <td
      {...props}
      className="border-b border-agent-border px-2 py-1 align-top break-words"
      style={{ maxWidth: '300px' }}
    />
  ),

  // ── Code (inline + block) ─────────────────────────────────────────────
  code: renderCode(null),
  pre: (props: ComponentPropsWithoutRef<'pre'>) => {
    // A ```plan fence arrives as <pre><code class="language-plan">…</code></pre>.
    // The code renderer above already swaps the inner node for PlanTodoList;
    // here we drop the <pre> shell so the card isn't boxed in a code block.
    if (containsPlanCode(props.children)) {
      return <>{props.children}</>;
    }
    return (
      <pre
        {...props}
        className="my-1.5 overflow-x-auto rounded-md border border-agent-border bg-agent-muted/60 p-2 text-[12px] leading-relaxed text-agent-foreground"
      />
    );
  },
};

/**
 * 行内代码 / 代码块渲染器。`chatId` 只影响行内代码：形状上像本地路径的
 * 字面量交给 FilePathCode，由它经后端确认存在后变成可点击（相对路径按
 * 该对话绑定的项目根解析）。代码块内的路径不参与——整块代码是给人读/
 * 复制的，逐 token 挑路径会把块结构打散。
 */
function renderCode(chatId: string | null): Components['code'] {
  return (props) => {
    const { children, className, ...rest } = props as ComponentPropsWithoutRef<'code'> & {
      // react-markdown v9 dropped the `inline` typed prop. Distinguish by
      // language fence (the only time class is set is for fenced blocks
      // like ```ts which receive `language-ts`).
      inline?: boolean;
    };
    // ```plan fences (emitted by the plan-mode skill) render as a visual
    // todolist card instead of a monospace block. The matching <pre>
    // wrapper is unwrapped below in the `pre` renderer.
    if (isPlanCodeClass(className)) {
      return <PlanTodoList content={childrenToText(children)} />;
    }
    const isBlock = typeof className === 'string' && className.startsWith('language-');
    if (isBlock) {
      return (
        <code {...rest} className={`${className} font-mono text-[12px]`}>
          {children}
        </code>
      );
    }
    const text = childrenToText(children);
    if (looksLikeFilePath(text)) {
      return (
        <FilePathCode {...rest} candidate={text.trim()} chatId={chatId}>
          {children}
        </FilePathCode>
      );
    }
    return (
      <code
        {...rest}
        className="rounded bg-agent-muted px-1 py-0.5 font-mono text-[0.85em] text-agent-foreground"
      >
        {children}
      </code>
    );
  };
}

function isPlanCodeClass(className: unknown): boolean {
  return typeof className === 'string' && className.split(/\s+/).includes('language-plan');
}

function containsPlanCode(children: ReactNode): boolean {
  const nodes = Array.isArray(children) ? children : [children];
  return nodes.some(
    (node) =>
      isValidElement<{ className?: string }>(node) &&
      isPlanCodeClass(node.props.className),
  );
}

function childrenToText(children: ReactNode): string {
  if (typeof children === 'string') return children;
  if (Array.isArray(children)) return children.map(childrenToText).join('');
  if (isValidElement<{ children?: ReactNode }>(children)) {
    return childrenToText(children.props.children);
  }
  return children == null ? '' : String(children);
}

/** Chip shell shared by @mention and /tool tokens inside a sent message. */
const CHIP_BASE =
  'inline-flex items-center gap-1 mx-0.5 px-1.5 py-0.5 rounded font-medium text-xs select-all align-middle';

/**
 * Split a text node on both token kinds so a message can carry `@专家` and
 * `/tool` side by side. Both only open at line start or after whitespace, via
 * a lookbehind so the leading space stays in the surrounding text: that keeps
 * paths (`/mnt/c`), URLs and email addresses (`a@b.com`) out of the split.
 */
const TOKEN_SPLIT_PATTERN = /(?<=^|\s)(?:(@[^\s@]+)|(\/[^\s/]+))/g;

/** Already-built chips are opaque: decorating them again would nest chips. */
function isChip(node: ReactNode): boolean {
  if (!isValidElement(node)) return false;
  const props = node.props as Record<string, unknown>;
  return 'data-message-tool-chip' in props || 'data-mention-chip' in props;
}

function renderTextWithMentions(
  node: ReactNode,
  agents: LocalChatAgent[] = [],
  chats: LocalChat[] = [],
  skills: SkillItem[] = [],
  mcpTools: McpToolItem[] = [],
): ReactNode {
  if (typeof node === 'string') {
    const parts = node.split(TOKEN_SPLIT_PATTERN).filter((p) => p !== undefined);
    if (parts.length === 1) return node;

    return parts.map((part, index) => {
      if (part.startsWith('/')) {
        const tool = resolveSlashTool(part.slice(1), skills, mcpTools);
        if (!tool) return part;
        return tool.type === 'skill' ? (
          <span
            key={index}
            data-message-tool-chip=""
            data-tool-type="skill"
            className={`${CHIP_BASE} bg-amber-500/10 text-amber-600 dark:bg-amber-500/20 dark:text-amber-400 border border-amber-500/20`}
          >
            <LuBlocks className="h-3 w-3 shrink-0 text-amber-500" />
            <span>{part}</span>
          </span>
        ) : (
          <span
            key={index}
            data-message-tool-chip=""
            data-tool-type="mcp"
            className={`${CHIP_BASE} bg-sky-500/10 text-sky-600 dark:bg-sky-500/20 dark:text-sky-400 border border-sky-500/20`}
          >
            <LuPlug className="h-3 w-3 shrink-0 text-sky-500" />
            <span>{part}</span>
          </span>
        );
      }

      if (part.startsWith('@')) {
        const name = part.slice(1);
        const agent = agents.find((a) => a.name === name || a.slug === name);
        if (agent) {
          return (
            <span
              key={index}
              data-mention-chip=""
              data-mention-type="agent"
              className="inline-flex items-center gap-1.5 mx-0.5 px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-600 dark:bg-indigo-500/20 dark:text-indigo-400 border border-indigo-500/20 font-medium text-xs select-all align-middle"
            >
              <span
                className="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full text-[8px] font-semibold text-white"
                style={{ backgroundColor: agent.color || '#7c3aed' }}
              >
                {agent.name.trim()[0]?.toUpperCase() ?? 'A'}
              </span>
              <span>{part}</span>
            </span>
          );
        }

        const chat = chats.find((c) => (c.title || '未命名对话') === name);
        if (chat) {
          return (
            <span
              key={index}
              data-mention-chip=""
              data-mention-type="chat"
              className="inline-flex items-center gap-1 mx-0.5 px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-600 dark:bg-emerald-500/20 dark:text-emerald-400 border border-emerald-500/20 font-medium text-xs select-all align-middle"
            >
              <LuMessageSquare className="h-3 w-3 text-emerald-500 shrink-0" />
              <span>{part}</span>
            </span>
          );
        }

        // 认不出的 @token 保持纯文本：助手回复里的 `@param`、`@media` 之类
        // 不是提及，套上卡片只会误导。
        return part;
      }
      return part;
    });
  }

  if (Array.isArray(node)) {
    return node.map((child, idx) => (
      <Fragment key={idx}>
        {renderTextWithMentions(child, agents, chats, skills, mcpTools)}
      </Fragment>
    ));
  }

  if (isValidElement(node)) {
    const type = node.type;
    // Custom components (`typeof type !== 'string'`) are react-markdown's own
    // renderers; they decorate their children themselves via `withTokens`.
    if (
      type === 'code' ||
      type === 'pre' ||
      type === 'a' ||
      typeof type !== 'string' ||
      isChip(node)
    ) {
      return node;
    }
    const children = (node.props as any).children;
    if (children) {
      return cloneElement(node, {
        ...node.props,
        children: renderTextWithMentions(children, agents, chats, skills, mcpTools),
      } as any);
    }
  }

  return node;
}

interface MarkdownProps {
  children: string;
  /** Override the `<p>` renderer (UserMessage uses `<span>` to inline). */
  inlineParagraph?: boolean;
  agents?: LocalChatAgent[];
  chats?: LocalChat[];
  /** Local skills behind `/name` chips. Omit to leave slash tokens as text. */
  skills?: SkillItem[];
  /** MCP tools behind `/mcp__srv__tool` chips. */
  mcpTools?: McpToolItem[];
  /**
   * 当前对话。行内代码里的路径要按它绑定的项目根解析相对路径；缺省时
   * 相对路径按 home 解析（同无项目对话里 exec 的缺省 cwd）。
   */
  chatId?: string | null;
}

/**
 * Tags whose children can hold raw text. Each gets its own decoration pass
 * because react-markdown hands every element to a custom renderer, so walking
 * the tree from the outside stops at the first override.
 */
const TOKEN_HOST_TAGS = [
  'p',
  'li',
  'td',
  'th',
  'h1',
  'h2',
  'h3',
  'h4',
  'strong',
  'em',
] as const;

function withTokens(
  base: Components,
  decorate: (children: ReactNode) => ReactNode,
): Components {
  // Indexing `Components` by a 10-tag union blows up TS's union budget
  // (TS2590), so build the overrides against a flat record.
  const wrapped = { ...base } as Record<string, ElementType>;
  for (const tag of TOKEN_HOST_TAGS) {
    const Renderer = (wrapped[tag] ?? tag) as ElementType;
    wrapped[tag] = ({ children, ...rest }: { children?: ReactNode }) =>
      createElement(Renderer, rest, decorate(children));
  }
  return wrapped as Components;
}

export function Markdown({
  children,
  inlineParagraph,
  agents = [],
  chats = [],
  skills = [],
  mcpTools = [],
  chatId = null,
}: MarkdownProps) {
  const baseComponents: Components = {
    ...components,
    code: renderCode(chatId),
    ...(inlineParagraph
      ? {
          p: (props: ComponentPropsWithoutRef<'p'>) => (
            <span style={{ whiteSpace: 'pre-line' }} {...props} />
          ),
        }
      : {}),
  };

  const finalComponents = withTokens(baseComponents, (node) =>
    renderTextWithMentions(node, agents, chats, skills, mcpTools),
  );

  return (
    <ReactMarkdown
      // `singleTilde: false` — GFM 删除线只认 `~~text~~`。remark-gfm 默认还把
      // 单个 `~`（"约~50%"、"5~10 个"、"~/path"）当删除线开闭符，导致普通
      // 文本被莫名划掉。GitHub 本站也只渲染双波浪线。
      remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
      rehypePlugins={[rehypeRaw]}
      components={finalComponents}
    >
      {stripNextStepsTags(children)}
    </ReactMarkdown>
  );
}

export default Markdown;

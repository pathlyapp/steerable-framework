import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type FormEvent,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
} from 'react';
import { useNavigate } from 'react-router-dom';
import {
  LuArrowUp,
  LuBot,
  LuChevronDown,
  LuSettings,
  LuSquare,
  LuBlocks,
  LuPlug,
  LuFile,
  LuPaperclip,
  LuX,
  LuInfinity,
  LuListChecks,
  LuMessageSquare,
} from 'react-icons/lu';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';
import type { ExecPolicy } from '@/lib/exec-policy';
import type { AttachmentFile } from '@/lib/attachments';
import type { SteerOutcome } from '@steerable/agent-ui';
import {
  isHiddenSlashSkill,
  resolveSlashTool,
  SLASH_TOKEN_PATTERN,
  useSlashSources,
  type McpToolItem,
  type SkillItem,
} from '@/lib/slash-sources';
import { useAskUserPrompt } from './AskUserPromptProvider';
import { AskUserQuestionMenu } from './AskUserQuestionMenu';
import { useApprovalPrompt } from './ApprovalPromptProvider';
import { ApprovalPromptMenu } from './ApprovalModal';
import { ExecPolicyPicker } from './ExecPolicyPicker';

export type ChatMode = 'agent' | 'plan';
export type { ExecPolicy, McpToolItem, SkillItem };

/**
 * ChatInput — Tier-1 port of `deeppath`'s ChatInput.
 *
 * Coverage (Phase 2a):
 *   - Bordered card container with shadow-sm + rounded-lg (matches the cloud
 *     product's `chat-input-box`).
 *   - Auto-growing rich text input (1 → 8 lines) that grows as the user types.
 *   - `Enter` to send, `Shift+Enter` for newline (`Cmd/Ctrl+Enter` also sends;
 *     IME composition is guarded so confirming a candidate never submits).
 *   - Send button on the right; turns into a Stop button when streaming.
 *   - Externally-controlled value (`LocalChatPanel` lifts it so EmptyChat can
 *     inject prompts via `setAndFocusInputMessage`).
 *
 * Phase 2b additions:
 *   - Agent picker in the meta row above the input box (next to the project
 *     badge). Clicking an expert switches who this composer talks to
 *     (`onSelectAgent`). Extra experts are still added by typing `@` in the
 *     input.
 *   - Settings gear — fires `onOpenSettings` so the parent (LocalChatPanel /
 *     AgentPage) can open the LocalLlmSettingsModal. Keeping modal ownership
 *     out of ChatInput avoids dragging state up just so this component can
 *     mount it.
 *
 * Deferred:
 *   - Multi-agent / runtime model picker (cloud product's ChatToolbar).
 *   - `FileUpload` + drag-and-drop overlay.
 *   - `AutomationAlert` (banner for upload progress / automation triggers).
 *   - Compact-mode responsive layout.
 */

const MIN_HEIGHT_PX = 36;
const MAX_HEIGHT_PX = 220;
const MENTION_PATTERN = /(@[^\s@]+)/g;
const MAX_MENTION_SUGGESTIONS = 8;

// macOS uses ⌘; Linux/Windows users see ⌃ instead so the tooltip matches the
// shortcut they can actually press. We detect via `navigator.platform` —
// `userAgent` works too but is more brittle as Apple drops Mac branding.
const IS_MAC =
  typeof navigator !== 'undefined' &&
  /Mac|iPod|iPhone|iPad/.test(navigator.platform || '');
const MOD_KEY_LABEL = IS_MAC ? '⌘' : 'Ctrl';

export interface ChatInputHandle {
  focus: () => void;
  focusAtEnd: () => void;
}

export interface ChatInputProps {
  value: string;
  onChange: (next: string) => void;
  onSubmit: () => void | Promise<void>;
  onCancel?: () => void;
  /**
   * 轮中转向（streaming 期间 Enter）：优先把文本注入运行中的回合；注入
   * 失败由 hook 兜底——回合仍在跑则排入 W6-2 follow-up 队列（'queued'，
   * 本组件提示"已改为排队"），回合恰好已结束则作为新消息直接发出
   * （'sent'）。三种结果消息都已落地，调用方据此清空草稿。
   */
  onSteer?: (text: string) => Promise<SteerOutcome>;
  /**
   * W6-2 follow-up 队列：streaming 期间按 ⌘/Ctrl+Enter 把当前文本排入
   * 待发队列，本轮结束后自动作为下一轮发出（与 Enter 的轮中转向相对）。
   */
  onFollowUp?: (text: string) => void;
  /** 当前排队待发的 follow-up 文本（用于在输入框上方展示待发队列）。 */
  pendingFollowUps?: string[];
  /** 撤回第 N 条排队中的 follow-up。 */
  onRemoveFollowUp?: (index: number) => void;
  isStreaming?: boolean;
  disabled?: boolean;
  placeholder?: string;
  /** Active agent for this chat. */
  currentAgent?: LocalChatAgent | null;
  /** Agent catalog for the meta-row selector above the input box. */
  agents?: LocalChatAgent[];
  /** Recent chats that can be referenced with @. */
  chats?: LocalChat[];
  /** Shared selection used for the next new chat. */
  selectedAgentId?: string | null;
  /** Called when the user chooses an agent from the meta-row selector. */
  onSelectAgent?: (agentId: string) => void | Promise<void>;
  /** Settings gear callback. When unset, the gear button is hidden. */
  onOpenSettings?: () => void;
  /** Extra controls on the left toolbar (e.g. the model/effort picker).
   * Rendered after the mode toggle and before the settings gear. */
  toolbarExtras?: ReactNode;
  /** Slot in the composer meta row above the input box, before the agent
   * picker (e.g. the project badge). */
  leadingChrome?: ReactNode;
  /** Current chat mode. When unset the toggle is hidden (defaults to 'agent'). */
  mode?: ChatMode;
  /** Called when the user switches between Agent / Plan mode. */
  onModeChange?: (mode: ChatMode) => void;
  /** 命令沙箱档（工作区 / 完整权限）。未传 onChange 时不显示选择器。 */
  execPolicy?: ExecPolicy;
  /** Called when the user switches the per-turn exec sandbox policy. */
  onExecPolicyChange?: (policy: ExecPolicy) => void;
  /** Attached files (drag-and-drop / file picker). */
  files?: AttachmentFile[];
  /** Callback when files change */
  onFilesChange?: (files: AttachmentFile[]) => void;
  /** Structured @ references selected from the input menu. */
  onMentionReferencesChange?: (references: MentionReference[]) => void;
  /** Local skills for slash command autocomplete and chip rendering. */
  skills?: SkillItem[];
  /** MCP tools for slash command autocomplete and chip rendering. */
  mcpTools?: McpToolItem[];
}

export type MentionReference = {
  type: 'agent' | 'chat' | 'skill' | 'mcp';
  id: string;
  label: string;
};

type MentionSuggestion = MentionReference & {
  description?: string | null;
  color?: string | null;
};

type MentionQuery = {
  start: number;
  end: number;
  query: string;
};

// ── "/" 触发器：本地技能 + MCP 工具 ─────────────────────────────────────
// 两类候选混排在同一个弹层里（技能在前、MCP 工具在后，分组标题隔开），
// 选中后统一把 insertToken 以 "/<token> " 形式写回输入框。MCP token 即
// 后端一等工具名 mcp__<serverKey>__<toolName>，cleanUserMessage 会识别。
// 候选清单与内置技能的隐藏规则由 `@/lib/slash-sources` 单点持有，消息气泡
// 里的工具卡片走同一份数据。

type SlashSuggestion =
  | { kind: 'skill'; insertToken: string; skill: SkillItem }
  | { kind: 'mcp'; insertToken: string; mcp: McpToolItem };

type MentionChipRange = {
  start: number;
  end: number;
  text: string;
  ref?: MentionReference;
};

function mentionChipClassName(ref?: MentionReference): string {
  const base = 'rounded-agent-sm px-1.5 py-0.5 font-medium';
  if (ref?.type === 'agent') {
    return `${base} bg-indigo-500/10 text-indigo-600 outline outline-1 outline-indigo-500/20 -outline-offset-1 dark:bg-indigo-500/20 dark:text-indigo-400`;
  }
  if (ref?.type === 'chat') {
    return `${base} bg-emerald-500/10 text-emerald-600 outline outline-1 outline-emerald-500/20 -outline-offset-1 dark:bg-emerald-500/20 dark:text-emerald-400`;
  }
  if (ref?.type === 'skill') {
    return `${base} bg-amber-500/10 text-amber-600 outline outline-1 outline-amber-500/20 -outline-offset-1 dark:bg-amber-500/20 dark:text-amber-400`;
  }
  if (ref?.type === 'mcp') {
    return `${base} bg-sky-500/10 text-sky-600 outline outline-1 outline-sky-500/20 -outline-offset-1 dark:bg-sky-500/20 dark:text-sky-400`;
  }
  return `${base} bg-agent-foreground/10 text-agent-foreground`;
}

function getEditableText(element: HTMLDivElement): string {
  return (element.textContent ?? '').replace(/\u00a0/g, ' ');
}

function isEditorDomEmpty(editor: HTMLDivElement): boolean {
  return getEditableText(editor).length === 0;
}

function hasEmptyImeScaffold(editor: HTMLDivElement): boolean {
  if (!isEditorDomEmpty(editor)) return false;
  const hasTextNode = Array.from(editor.childNodes).some(
    (node) => node.nodeType === Node.TEXT_NODE,
  );
  return hasTextNode && Boolean(editor.querySelector('br'));
}

function parseMentionSegments(
  value: string,
  mentionReferences: MentionReference[] = [],
  agents: LocalChatAgent[] = [],
  chats: LocalChat[] = [],
  skills: SkillItem[] = [],
  mcpTools: McpToolItem[] = [],
): Array<
  | { type: 'text'; text: string }
  | { type: 'br' }
  | { type: 'mention'; start: number; end: number; text: string; ref?: MentionReference }
> {
  const segments: Array<
    | { type: 'text'; text: string }
    | { type: 'br' }
    | { type: 'mention'; start: number; end: number; text: string; ref?: MentionReference }
  > = [];
  const lines = value.split('\n');
  let lineStartIdx = 0;

  lines.forEach((line, lineIndex) => {
    let lastIndex = 0;
    const ranges: MentionChipRange[] = [];

    mentionReferences.forEach((ref) => {
      const prefix = ref.type === 'skill' || ref.type === 'mcp' ? '/' : '@';
      const token = `${prefix}${ref.label}`;
      let from = 0;
      while (from < line.length) {
        const start = line.indexOf(token, from);
        if (start === -1) break;
        const end = start + token.length;
        if (!ranges.some((range) => start < range.end && end > range.start)) {
          ranges.push({ start, end, text: token, ref });
        }
        from = end;
      }
    });

    line.replace(MENTION_PATTERN, (match, _mention, offset) => {
      const end = offset + match.length;
      if (!ranges.some((range) => offset < range.end && end > range.start)) {
        const name = match.slice(1);
        const agent = agents.find((a) => a.name === name || a.slug === name);
        const chat = chats.find((c) => (c.title || '未命名对话') === name);
        const ref: MentionReference | undefined = agent
          ? { type: 'agent', id: agent.id, label: agent.name }
          : chat
            ? { type: 'chat', id: chat.id, label: chat.title || '未命名对话' }
            : undefined;

        ranges.push({ start: offset, end, text: match, ref });
      }
      return match;
    });

    const slashRegex = new RegExp(SLASH_TOKEN_PATTERN.source, 'g');
    let slashMatch: RegExpExecArray | null;
    while ((slashMatch = slashRegex.exec(line)) !== null) {
      const fullMatch = slashMatch[0];
      const token = slashMatch[1];
      const offset = slashMatch.index + (fullMatch.length - token.length);
      const end = offset + token.length;

      if (ranges.some((range) => offset < range.end && end > range.start)) continue;

      const ref = resolveSlashTool(token.slice(1), skills, mcpTools);
      if (ref) ranges.push({ start: offset, end, text: token, ref });
    }

    ranges.sort((a, b) => a.start - b.start);

    ranges.forEach((range) => {
      if (range.start > lastIndex) {
        segments.push({ type: 'text', text: line.slice(lastIndex, range.start) });
      }

      const globalStart = lineStartIdx + range.start;
      const globalEnd = lineStartIdx + range.end;

      segments.push({
        type: 'mention',
        start: globalStart,
        end: globalEnd,
        text: range.text,
        ref: range.ref,
      });
      lastIndex = range.end;
    });

    if (lastIndex < line.length) {
      segments.push({ type: 'text', text: line.slice(lastIndex) });
    }
    if (lineIndex < lines.length - 1) {
      segments.push({ type: 'br' });
    }
    lineStartIdx += line.length + 1;
  });

  return segments;
}

/**
 * Empty editors need a text node (IME marked-text attachment) plus a `<br>`
 * (caret height). Replacing this while focused kills the first Pinyin letter.
 */
function writeEditorValue(
  editor: HTMLDivElement,
  next: string,
  mentionReferences: MentionReference[] = [],
  agents: LocalChatAgent[] = [],
  chats: LocalChat[] = [],
  onRemoveMention?: (start: number, end: number, ref?: MentionReference) => void,
  skills: SkillItem[] = [],
  mcpTools: McpToolItem[] = [],
) {
  if (next.length === 0) {
    editor.replaceChildren(document.createTextNode(''), document.createElement('br'));
    return;
  }

  const segments = parseMentionSegments(next, mentionReferences, agents, chats, skills, mcpTools);
  const hasMentions = segments.some((s) => s.type === 'mention');
  if (!hasMentions) {
    editor.textContent = next;
    return;
  }

  const nodes: Node[] = [];
  segments.forEach((seg) => {
    if (seg.type === 'text') {
      nodes.push(document.createTextNode(seg.text));
    } else if (seg.type === 'br') {
      nodes.push(document.createElement('br'));
    } else {
      const isAgent = seg.ref?.type === 'agent';
      const isChat = seg.ref?.type === 'chat';
      const isSkill = seg.ref?.type === 'skill';
      const isMcp = seg.ref?.type === 'mcp';
      const removeLabel = `移除 ${seg.text}`;

      const chip = document.createElement('button');
      chip.type = 'button';
      chip.contentEditable = 'false';
      chip.setAttribute('data-mention-chip', '');
      chip.setAttribute('data-mention-type', seg.ref?.type ?? 'plain');
      chip.setAttribute('data-testid', `mention-chip-${seg.ref?.id ?? seg.text}`);
      chip.setAttribute('aria-label', removeLabel);
      chip.setAttribute('title', removeLabel);
      chip.tabIndex = -1;
      chip.className = `group relative inline-flex items-center cursor-pointer select-none align-baseline ${mentionChipClassName(seg.ref)}`;

      chip.onmousedown = (event) => {
        event.preventDefault();
        event.stopPropagation();
      };
      chip.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        onRemoveMention?.(seg.start, seg.end, seg.ref);
      };

      if (isSkill) {
        const iconSpan = document.createElement('span');
        iconSpan.setAttribute('aria-hidden', 'true');
        iconSpan.className = 'pointer-events-none mr-1 inline-flex items-center shrink-0 text-amber-600 dark:text-amber-400';
        iconSpan.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="h-3 w-3"><rect width="7" height="7" x="14" y="3" rx="1"/><path d="M10 21V8a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5a1 1 0 0 0-1-1H3"/></svg>';
        chip.appendChild(iconSpan);
      } else if (isMcp) {
        const iconSpan = document.createElement('span');
        iconSpan.setAttribute('aria-hidden', 'true');
        iconSpan.className = 'pointer-events-none mr-1 inline-flex items-center shrink-0 text-sky-600 dark:text-sky-400';
        iconSpan.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="h-3 w-3"><path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/></svg>';
        chip.appendChild(iconSpan);
      }

      chip.appendChild(document.createTextNode(seg.text));

      if (onRemoveMention) {
        const removeSpan = document.createElement('span');
        removeSpan.setAttribute('data-mention-remove', '');
        removeSpan.className = `pointer-events-none absolute -right-1 -top-1 flex h-3.5 w-3.5 items-center justify-center rounded-full text-white opacity-0 shadow-sm transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100 ${
          isAgent
            ? 'bg-indigo-500'
            : isChat
              ? 'bg-emerald-500'
              : isSkill
                ? 'bg-amber-500'
                : isMcp
                  ? 'bg-sky-500'
                  : 'bg-agent-muted-foreground'
        }`;
        removeSpan.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="h-2.5 w-2.5"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
        chip.appendChild(removeSpan);
      }

      nodes.push(chip);
    }
  });

  const last = nodes[nodes.length - 1];
  if (last instanceof HTMLButtonElement) {
    nodes.push(document.createTextNode(''));
  }

  editor.replaceChildren(...nodes);
}

function placeCaretInEmptyEditor(editor: HTMLDivElement) {
  const text = Array.from(editor.childNodes).find(
    (node): node is Text => node.nodeType === Node.TEXT_NODE,
  );
  if (!text) return;
  const range = document.createRange();
  range.setStart(text, 0);
  range.collapse(true);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}

function updateEditorHeight(editor: HTMLDivElement) {
  editor.style.height = 'auto';
  const next = Math.min(Math.max(editor.scrollHeight, MIN_HEIGHT_PX), MAX_HEIGHT_PX);
  editor.style.height = `${next}px`;
}

function offsetFromPoint(root: HTMLDivElement, x: number, y: number): number | null {
  const doc = document as Document & {
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
    caretRangeFromPoint?: (x: number, y: number) => Range | null;
  };
  if (typeof doc.caretPositionFromPoint === 'function') {
    const pos = doc.caretPositionFromPoint(x, y);
    if (!pos || !root.contains(pos.offsetNode)) return null;
    const measure = document.createRange();
    measure.selectNodeContents(root);
    measure.setEnd(pos.offsetNode, pos.offset);
    return measure.toString().length;
  }
  const range = doc.caretRangeFromPoint?.(x, y);
  if (!range || !root.contains(range.startContainer)) return null;
  const measure = document.createRange();
  measure.selectNodeContents(root);
  measure.setEnd(range.startContainer, range.startOffset);
  return measure.toString().length;
}

function getSelectionOffsets(root: HTMLDivElement): { start: number; end: number } {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) {
    const end = root.innerText.length;
    return { start: end, end };
  }

  const range = selection.getRangeAt(0);
  if (
    !root.contains(range.startContainer) ||
    !root.contains(range.endContainer)
  ) {
    const end = root.textContent?.length ?? 0;
    return { start: end, end };
  }

  const getOffset = (container: Node, offset: number) => {
    const measureRange = document.createRange();
    measureRange.selectNodeContents(root);
    measureRange.setEnd(container, offset);
    return measureRange.toString().length;
  };

  return {
    start: getOffset(range.startContainer, range.startOffset),
    end: getOffset(range.endContainer, range.endOffset),
  };
}

function setCaretOffset(root: HTMLDivElement, offset: number) {
  const range = document.createRange();
  const selection = window.getSelection();
  let remaining = offset;
  let placed = false;

  const walk = (node: Node): boolean => {
    if (
      node instanceof HTMLElement &&
      (node.getAttribute('contenteditable') === 'false' || node.tagName === 'BUTTON')
    ) {
      const len = node.textContent?.length ?? 0;
      if (remaining === 0) {
        range.setStartBefore(node);
        range.collapse(true);
        placed = true;
        return true;
      }
      if (remaining <= len) {
        range.setStartAfter(node);
        range.collapse(true);
        placed = true;
        return true;
      }
      remaining -= len;
      return false;
    }

    if (node.nodeType === Node.TEXT_NODE) {
      const textLength = node.textContent?.length ?? 0;
      if (remaining <= textLength) {
        range.setStart(node, remaining);
        range.collapse(true);
        placed = true;
        return true;
      }
      remaining -= textLength;
      return false;
    }

    if (node instanceof HTMLBRElement) {
      if (remaining <= 1) {
        range.setStartAfter(node);
        range.collapse(true);
        placed = true;
        return true;
      }
      remaining -= 1;
      return false;
    }

    for (const child of Array.from(node.childNodes)) {
      if (walk(child)) return true;
    }
    return false;
  };

  walk(root);

  if (!placed) {
    range.selectNodeContents(root);
    range.collapse(false);
  }

  selection?.removeAllRanges();
  selection?.addRange(range);
}

function getMentionQuery(value: string, caretOffset: number): MentionQuery | null {
  const beforeCaret = value.slice(0, caretOffset);
  const tokenStart = beforeCaret.search(/(?:^|\s)@\S*$/);
  if (tokenStart === -1) return null;

  const prefix = beforeCaret[tokenStart];
  const start = prefix === '@' ? tokenStart : tokenStart + 1;
  const token = value.slice(start, caretOffset);
  if (!token.startsWith('@')) return null;

  return {
    start,
    end: caretOffset,
    query: token.slice(1).toLowerCase(),
  };
}

/**
 * "/" 技能 token 检测 — 与 getMentionQuery 同构，基于光标位置而不是整个
 * 输入框内容，所以输入框已有文字时在任意位置输入 "/" 也能触发技能池。
 * token 必须出现在行首或空白之后（排除 "C:/path"、"https://" 这类路径 /
 * URL 中间的斜杠），且 token 内不允许再出现 "/"（排除 "/mnt/c" 这类路径）。
 */
function getSlashQuery(value: string, caretOffset: number): MentionQuery | null {
  const beforeCaret = value.slice(0, caretOffset);
  const tokenStart = beforeCaret.search(/(?:^|\s)\/[^\s/]*$/);
  if (tokenStart === -1) return null;

  const prefix = beforeCaret[tokenStart];
  const start = prefix === '/' ? tokenStart : tokenStart + 1;
  const token = value.slice(start, caretOffset);
  if (!token.startsWith('/')) return null;

  return {
    start,
    end: caretOffset,
    query: token.slice(1).toLowerCase(),
  };
}

function getMentionRanges(
  val: string,
  refs: MentionReference[],
): Array<{ start: number; end: number; ref: MentionReference }> {
  const ranges: Array<{ start: number; end: number; ref: MentionReference }> = [];
  if (refs.length === 0) return ranges;

  const lines = val.split('\n');
  let lineStartIdx = 0;

  const sortedRefs = [...refs].sort((a, b) => b.label.length - a.label.length);

  lines.forEach((line) => {
    const lineRanges: Array<{ start: number; end: number; ref: MentionReference }> = [];

    sortedRefs.forEach((ref) => {
      const prefix = ref.type === 'skill' || ref.type === 'mcp' ? '/' : '@';
      const tokenWithSpace = `${prefix}${ref.label} `;
      const tokenWithoutSpace = `${prefix}${ref.label}`;

      // Search for token with space first
      let from = 0;
      while (from < line.length) {
        const start = line.indexOf(tokenWithSpace, from);
        if (start === -1) break;
        const end = start + tokenWithSpace.length;
        if (!lineRanges.some((r) => start < r.end && end > r.start)) {
          lineRanges.push({
            start: lineStartIdx + start,
            end: lineStartIdx + end,
            ref,
          });
        }
        from = end;
      }

      // Search for token without space
      from = 0;
      while (from < line.length) {
        const start = line.indexOf(tokenWithoutSpace, from);
        if (start === -1) break;
        const end = start + tokenWithoutSpace.length;
        if (!lineRanges.some((r) => start < r.end && end > r.start)) {
          lineRanges.push({
            start: lineStartIdx + start,
            end: lineStartIdx + end,
            ref,
          });
        }
        from = end;
      }
    });

    ranges.push(...lineRanges);
    lineStartIdx += line.length + 1; // +1 for \n
  });

  return ranges;
}

export const ChatInput = forwardRef<ChatInputHandle, ChatInputProps>(
  function ChatInput(
    {
      value,
      onChange,
      onSubmit,
      onCancel,
      onSteer,
      onFollowUp,
      pendingFollowUps = [],
      onRemoveFollowUp,
      isStreaming = false,
      disabled = false,
      placeholder = '向 Agent 发送消息…',
      currentAgent,
      agents = [],
      chats = [],
      selectedAgentId,
      onSelectAgent,
      onOpenSettings,
      toolbarExtras,
      leadingChrome,
      mode,
      onModeChange,
      execPolicy,
      onExecPolicyChange,
      files,
      onFilesChange,
      onMentionReferencesChange,
      skills: propSkills,
      mcpTools: propMcpTools,
    },
    ref,
  ) {
    const askUserPrompt = useAskUserPrompt();
    const approvalPrompt = useApprovalPrompt();
    const editorRef = useRef<HTMLDivElement>(null);
    const agentMenuRef = useRef<HTMLDivElement>(null);
    const skillOptionRefs = useRef<Array<HTMLButtonElement | null>>([]);
    const mentionOptionRefs = useRef<Array<HTMLButtonElement | null>>([]);
    const pendingCaretOffsetRef = useRef<number | null>(null);
    // Overlay editor + IME: never rewrite textContent/selection while the
    // user is composing. macOS Pinyin otherwise commits the first letter as
    // Latin instead of starting a marked-text session.
    const isComposingRef = useRef(false);
    // Last value we pushed from the editor (or accepted from the parent).
    // While focused, a matching value must not rewrite contenteditable DOM —
    // that clobber is what turns the first Pinyin letter into ASCII.
    const lastPushedValueRef = useRef(value);
    // Chromium can fire `input` for the first Pinyin letter *before*
    // `compositionstart`. A pending sync is tagged with this generation so
    // compositionstart can cancel it. Use a macrotask (not a microtask):
    // compositionstart often lands in a later task than `input`.
    const inputSyncGenRef = useRef(0);
    const inputSyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    // Some IMEs (e.g. Microsoft Pinyin, Sogou) fire `compositionend` and then
    // a `keydown` for the same Enter keystroke used to confirm the candidate,
    // with `isComposing` already `false` by the time keydown runs. Without
    // this guard that Enter falls through to the "insert newline" branch below
    // and the user ends up with a stray blank line after committing text.
    const compositionEndAtRef = useRef<number>(0);

    const fileInputRef = useRef<HTMLInputElement>(null);
    // 工具栏"空间不足时优先隐藏快捷键提示"的测量 refs，见下方 useLayoutEffect。
    const toolbarRowRef = useRef<HTMLDivElement>(null);
    const toolbarLeftRef = useRef<HTMLDivElement>(null);
    const hintRef = useRef<HTMLDivElement>(null);
    const sendButtonRef = useRef<HTMLButtonElement>(null);
    const [hintsSuppressed, setHintsSuppressed] = useState(false);
    const [localFiles, setLocalFiles] = useState<AttachmentFile[]>([]);
    const actualFiles = files ?? localFiles;
    const actualOnFilesChange = onFilesChange ?? setLocalFiles;
    const [isDragging, setIsDragging] = useState(false);

    const slashSources = useSlashSources();
    const skills = useMemo(
      () => (propSkills ?? slashSources.skills).filter((s) => !isHiddenSlashSkill(s)),
      [propSkills, slashSources.skills],
    );
    const mcpTools = propMcpTools ?? slashSources.mcpTools;
    const [suggestions, setSuggestions] = useState<SlashSuggestion[]>([]);
    const [showSuggestions, setShowSuggestions] = useState(false);
    const [selectedIndex, setSelectedIndex] = useState(0);
    const [agentMenuOpen, setAgentMenuOpen] = useState(false);
    const [slashQuery, setSlashQuery] = useState<MentionQuery | null>(null);
    const [mentionQuery, setMentionQuery] = useState<MentionQuery | null>(null);
    const [selectedMentionIndex, setSelectedMentionIndex] = useState(0);
    const [mentionReferences, setMentionReferences] = useState<MentionReference[]>([]);
    // 转向降级提示：Enter 转向失败被兜底进 W6-2 队列时告知用户"不是注入"，
    // 几秒后自动消失（队列横幅本身常驻，提示只是动作反馈）。
    const [steerNotice, setSteerNotice] = useState<string | null>(null);
    const steerNoticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const showSteerNotice = useCallback((text: string) => {
      if (steerNoticeTimerRef.current) clearTimeout(steerNoticeTimerRef.current);
      setSteerNotice(text);
      steerNoticeTimerRef.current = setTimeout(() => setSteerNotice(null), 5000);
    }, []);

    useEffect(
      () => () => {
        if (steerNoticeTimerRef.current) clearTimeout(steerNoticeTimerRef.current);
        if (inputSyncTimerRef.current) clearTimeout(inputSyncTimerRef.current);
      },
      [],
    );

    const sortedAgents = useMemo(() => {
      return [...agents].sort((a, b) => {
        if (a.isBuiltin !== b.isBuiltin) return a.isBuiltin ? -1 : 1;
        const orderA = a.sortOrder ?? 0;
        const orderB = b.sortOrder ?? 0;
        if (orderA !== orderB) return orderA - orderB;
        return a.name.localeCompare(b.name, 'zh-CN');
      });
    }, [agents]);

    const selectedAgent =
      sortedAgents.find((agent) => agent.id === selectedAgentId) ??
      currentAgent ??
      sortedAgents[0] ??
      null;

    const mentionSuggestions = useMemo<MentionSuggestion[]>(() => {
      if (!mentionQuery) return [];
      const query = mentionQuery.query;
      const matchesQuery = (text: string | null | undefined) =>
        !query || (text ?? '').toLowerCase().includes(query);

      const agentSuggestions: MentionSuggestion[] = sortedAgents
        .filter((agent) => matchesQuery(agent.name) || matchesQuery(agent.slug))
        .map((agent) => ({
          type: 'agent',
          id: agent.id,
          label: agent.name,
          description: agent.description,
          color: agent.color,
        }));

      const chatSuggestions: MentionSuggestion[] = chats
        .filter((chat) => matchesQuery(chat.title))
        .slice(0, MAX_MENTION_SUGGESTIONS)
        .map((chat) => ({
          type: 'chat',
          id: chat.id,
          label: chat.title || '未命名对话',
          description: chat.updatedAt ? `历史对话 · ${new Date(chat.updatedAt).toLocaleString()}` : '历史对话',
        }));

      return [...agentSuggestions, ...chatSuggestions].slice(0, MAX_MENTION_SUGGESTIONS);
    }, [chats, mentionQuery, sortedAgents]);

    useEffect(() => {
      if (selectedMentionIndex >= mentionSuggestions.length) {
        setSelectedMentionIndex(0);
      }
    }, [mentionSuggestions.length, selectedMentionIndex]);

    useEffect(() => {
      onMentionReferencesChange?.(mentionReferences);
    }, [mentionReferences, onMentionReferencesChange]);

    useLayoutEffect(() => {
      if (!mentionQuery || mentionSuggestions.length === 0) return;
      mentionOptionRefs.current[selectedMentionIndex]?.scrollIntoView({
        block: 'nearest',
      });
    }, [mentionQuery, mentionSuggestions.length, selectedMentionIndex]);

    useLayoutEffect(() => {
      if (!showSuggestions || suggestions.length === 0) return;
      skillOptionRefs.current[selectedIndex]?.scrollIntoView({
        block: 'nearest',
      });
    }, [selectedIndex, showSuggestions, suggestions.length]);

    useEffect(() => {
      setMentionReferences((prev) =>
        prev.filter((ref) => value.includes(`@${ref.label}`)),
      );
      if (!value) {
        setMentionQuery(null);
        setSlashQuery(null);
      }
    }, [value]);

    // 外部直接改写 value（如 EmptyChat 注入提示词）不会走 handleInput，
    // slashQuery 里的偏移量可能失效——校验 token 还在原位，不在就清掉。
    useEffect(() => {
      if (!slashQuery) return;
      const token = value.slice(slashQuery.start, slashQuery.end);
      if (!token.startsWith('/') || token.slice(1).toLowerCase() !== slashQuery.query) {
        setSlashQuery(null);
      }
    }, [value, slashQuery]);

    // 弹层每次打开时刷新候选：MCP 服务/技能可能在设置里刚增删或重新连接，
    // 只在挂载时拉一次会拿到过期清单（2026-07-31 用户反馈 slash 里看不到
    // 已启动的 MCP）。首次拉取由 useSlashSources 自己完成。
    const refreshSlashSources = slashSources.refresh;
    useEffect(() => {
      if (slashQuery) void refreshSlashSources();
    }, [slashQuery, refreshSlashSources]);

    // 技能池由光标处的 "/" token 驱动（getSlashQuery）——输入框已有内容时
    // 在任意位置输入 "/" 同样触发，不再要求整段内容以 "/" 开头且不含空格。
    // 候选 = 本地技能 + MCP 工具，统一成 SlashSuggestion 扁平列表（键盘导航
    // 只认这个扁平顺序），渲染时再按 kind 分组加标题。
    useEffect(() => {
      if (slashQuery) {
        const query = slashQuery.query;
        const skillMatches: SlashSuggestion[] = skills
          .filter(
            (s) =>
              s.name.toLowerCase().includes(query) ||
              (s.displayName && s.displayName.toLowerCase().includes(query)) ||
              (s.description && s.description.toLowerCase().includes(query))
          )
          .map((s) => ({ kind: 'skill' as const, insertToken: s.name, skill: s }));
        const mcpMatches: SlashSuggestion[] = mcpTools
          .filter(
            (t) =>
              t.toolName.toLowerCase().includes(query) ||
              t.serverName.toLowerCase().includes(query) ||
              t.token.toLowerCase().includes(query) ||
              (t.description && t.description.toLowerCase().includes(query))
          )
          .map((t) => ({ kind: 'mcp' as const, insertToken: t.token, mcp: t }));
        const combined = [...skillMatches, ...mcpMatches];
        setSuggestions(combined);
        setShowSuggestions(combined.length > 0);
        setSelectedIndex(0);
      } else {
        setShowSuggestions(false);
      }
    }, [slashQuery, skills, mcpTools]);

    // 渲染行：分组标题 + 候选项（idx 对应扁平 suggestions 的下标，供高亮/键盘导航）
    const suggestionRows = useMemo(() => {
      const rows: Array<
        { type: 'header'; label: string } | { type: 'item'; item: SlashSuggestion; idx: number }
      > = [];
      const skillGroup = suggestions.filter((s) => s.kind === 'skill');
      const mcpGroup = suggestions.filter((s) => s.kind === 'mcp');
      let idx = 0;
      if (skillGroup.length > 0) {
        rows.push({ type: 'header', label: '指定运行的本地技能 (Skill)' });
        for (const s of skillGroup) rows.push({ type: 'item', item: s, idx: idx++ });
      }
      if (mcpGroup.length > 0) {
        rows.push({ type: 'header', label: '指定调用的 MCP 工具 (MCP Tool)' });
        for (const s of mcpGroup) rows.push({ type: 'item', item: s, idx: idx++ });
      }
      return rows;
    }, [suggestions]);

    useEffect(() => {
      if (!agentMenuOpen) return;
      const handlePointerDown = (event: PointerEvent) => {
        const target = event.target;
        if (
          target instanceof Node &&
          agentMenuRef.current?.contains(target)
        ) {
          return;
        }
        setAgentMenuOpen(false);
      };
      window.addEventListener('pointerdown', handlePointerDown);
      return () => window.removeEventListener('pointerdown', handlePointerDown);
    }, [agentMenuOpen]);

    const handleSelectSuggestion = (suggestion: SlashSuggestion) => {
      const token = `/${suggestion.insertToken} `;
      // 只替换光标处的 "/xxx" token，保留输入框里已有的其它内容。
      const range = slashQuery ?? { start: 0, end: value.length };
      const nextValue = `${value.slice(0, range.start)}${token}${value.slice(range.end)}`;
      const caret = range.start + token.length;
      pendingCaretOffsetRef.current = caret;
      const refType = suggestion.kind === 'skill' ? 'skill' : 'mcp';
      setMentionReferences((prev) => {
        const withoutDuplicate = prev.filter(
          (item) => !(item.type === refType && item.id === suggestion.insertToken),
        );
        return [
          ...withoutDuplicate,
          {
            type: refType,
            id: suggestion.insertToken,
            label: suggestion.insertToken,
          },
        ];
      });
      onChange(nextValue);
      setSlashQuery(null);
      setShowSuggestions(false);
      setTimeout(() => {
        const editor = editorRef.current;
        if (editor) {
          editor.focus();
          setCaretOffset(editor, Math.min(caret, getEditableText(editor).length));
        }
      }, 50);
    };

    const syncMentionQuery = (nextValue: string, caretOffset: number) => {
      const nextQuery = getMentionQuery(nextValue, caretOffset);
      setMentionQuery(nextQuery);
      if (nextQuery) {
        setSelectedMentionIndex(0);
      }
      setSlashQuery(getSlashQuery(nextValue, caretOffset));
    };

    useImperativeHandle(
      ref,
      () => ({
        focus: () => editorRef.current?.focus(),
        focusAtEnd: () => {
          const editor = editorRef.current;
          if (!editor) return;
          editor.focus();
          setCaretOffset(editor, value.length);
        },
      }),
      [value.length],
    );

    // Keep the browser-owned contentEditable DOM as plain text. React renders
    // mention chip backgrounds in the mirror layer to avoid reconciling
    // editable nodes. Never clobber glyphs from React while IME is attaching
    // or while the focused editor already owns this value.
    useLayoutEffect(() => {
      const editor = editorRef.current;
      if (!editor || isComposingRef.current || editor.hasAttribute('data-composing')) {
        return;
      }
      const editorText = getEditableText(editor);
      const focused = document.activeElement === editor;
      const pendingCaretOffset = pendingCaretOffsetRef.current;
      const parentChangedValue = value !== lastPushedValueRef.current;

      if (focused && !parentChangedValue && pendingCaretOffset === null) {
        updateEditorHeight(editor);
        return;
      }

      if (editorText !== value || pendingCaretOffset !== null) {
        writeEditorValue(
          editor,
          value,
          mentionReferences,
          agents,
          chats,
          disabled ? undefined : removeMentionToken,
          skills,
          mcpTools,
        );
      } else if (!value && !hasEmptyImeScaffold(editor)) {
        writeEditorValue(editor, '');
      }

      lastPushedValueRef.current = value;
      updateEditorHeight(editor);

      if (pendingCaretOffset !== null && focused) {
        setCaretOffset(editor, Math.min(pendingCaretOffset, value.length));
        pendingCaretOffsetRef.current = null;
      }
    }, [value, mentionReferences, agents, chats, disabled, skills, mcpTools]);

    // `Cmd+.` / `Ctrl+.` cancels the in-flight stream — matches macOS's
    // conventional "interrupt" chord (Xcode, Terminal). Bound at the window
    // level so users can hit it without the textarea being focused.
    // Confined to the `isStreaming` window so it never fights with other
    // app-level shortcuts when the chat is idle.
    useEffect(() => {
      if (!isStreaming || !onCancel) return;
      const onKey = (event: globalThis.KeyboardEvent) => {
        if ((event.metaKey || event.ctrlKey) && event.key === '.') {
          event.preventDefault();
          onCancel();
        }
      };
      window.addEventListener('keydown', onKey);
      return () => window.removeEventListener('keydown', onKey);
    }, [isStreaming, onCancel]);

    // 快捷键提示是工具栏里优先级最低的元素：空间不够时第一个隐藏（先于
    // 左侧选择器截断）。容器查询只看输入盒宽度、看不到左侧内容（模型 id
    // 长度可变）的实际占用，所以直接测量：左组自然宽度 + 提示 + 发送键
    // 放不下就收起提示。测量时临时取消左组 flex 收缩并强制显示提示，
    // 拿到的都是不依赖当前收起状态的自然宽度——结果稳定，不会来回闪烁。
    useLayoutEffect(() => {
      const row = toolbarRowRef.current;
      const left = toolbarLeftRef.current;
      if (!row || !left) return;

      const check = () => {
        if (row.clientWidth === 0) return; // 无布局环境（happy-dom）不干预
        const hint = hintRef.current;
        const send = sendButtonRef.current;
        const prevShrink = left.style.flexShrink;
        left.style.flexShrink = '0';
        const prevHintDisplay = hint?.style.display ?? '';
        if (hint) hint.style.display = 'block';
        const needed =
          left.getBoundingClientRect().width +
          (hint?.getBoundingClientRect().width ?? 0) +
          (send?.getBoundingClientRect().width ?? 0) +
          16; // gap-2 ×2：左右组之间 + 提示与发送键之间
        left.style.flexShrink = prevShrink;
        if (hint) hint.style.display = prevHintDisplay;
        const available = row.clientWidth - 16; // px-2 两侧
        const suppress = needed > available + 1;
        setHintsSuppressed((prev) => (prev === suppress ? prev : suppress));
      };

      check();
      const resizeObs = new ResizeObserver(check);
      resizeObs.observe(row);
      // 左组内容变化（切换模型/沙箱档、设置齿轮挂载）不改变行宽，
      // ResizeObserver 观察不到，用 MutationObserver 兜底重新测量。
      const mutationObs = new MutationObserver(check);
      mutationObs.observe(left, { childList: true, subtree: true, characterData: true });
      return () => {
        resizeObs.disconnect();
        mutationObs.disconnect();
      };
      // isStreaming 切换提示文案（kbd 标签不同、宽度不同），需要重测。
    }, [isStreaming]);

    const trimmed = value.trim();
    const canSend = (trimmed.length > 0 || actualFiles.length > 0) && !disabled && !isStreaming;

    const handleDragOver = (event: React.DragEvent) => {
      event.preventDefault();
      if (event.dataTransfer.types.includes('Files') && !disabled) {
        setIsDragging(true);
      }
    };

    const handleDragLeave = (event: React.DragEvent) => {
      event.preventDefault();
      setIsDragging(false);
    };

    // 把新文件并入列表：按 path（有路径时）或 name（浏览器模式无路径）去重。
    const addFiles = (incoming: AttachmentFile[]) => {
      const keyOf = (f: AttachmentFile) => f.path || f.name;
      const existingKeys = new Set(actualFiles.map(keyOf));
      const filteredNewFiles = incoming.filter((f) => !existingKeys.has(keyOf(f)));
      if (filteredNewFiles.length > 0) {
        actualOnFilesChange([...actualFiles, ...filteredNewFiles]);
      }
    };
    const toAttachment = (file: File): AttachmentFile => ({
      name: file.name,
      // Electron 的 File 对象带真实路径；浏览器模式没有路径（提交时读字节）。
      path: (file as { path?: string }).path || '',
      file,
    });

    // 按钮点击 → 触发隐藏的 <input type="file" multiple>，由浏览器/Electron
    // 直接弹出系统文件选择框（不依赖 IPC，避免「点击无反应」）。
    const handlePickFiles = () => {
      if (disabled) return;
      fileInputRef.current?.click();
    };
    const handleFileInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
      const selected = Array.from(event.target.files ?? []);
      // 重置 value，允许再次选择同名文件时仍触发 change。
      event.target.value = '';
      if (selected.length === 0) return;
      addFiles(selected.map(toAttachment));
    };

    const handleDrop = (event: React.DragEvent) => {
      event.preventDefault();
      setIsDragging(false);

      if (disabled) return;

      const droppedFiles = Array.from(event.dataTransfer.files);
      if (droppedFiles.length > 0) {
        addFiles(droppedFiles.map(toAttachment));
      }
    };

    const handleRemoveFile = (indexToRemove: number) => {
      actualOnFilesChange(actualFiles.filter((_, idx) => idx !== indexToRemove));
    };

    const replaceSelection = (insertText: string) => {
      const editor = editorRef.current;
      if (!editor) return;
      const { start, end } = getSelectionOffsets(editor);
      const nextValue = `${value.slice(0, start)}${insertText}${value.slice(end)}`;
      pendingCaretOffsetRef.current = start + insertText.length;
      onChange(nextValue);
      syncMentionQuery(nextValue, pendingCaretOffsetRef.current);
    };

    const replaceRange = (start: number, end: number, insertText: string) => {
      const nextValue = `${value.slice(0, start)}${insertText}${value.slice(end)}`;
      pendingCaretOffsetRef.current = start + insertText.length;
      onChange(nextValue);
      syncMentionQuery(nextValue, pendingCaretOffsetRef.current);
    };

    const removeMentionToken = (start: number, end: number, ref?: MentionReference) => {
      let removeEnd = end;
      if (value[removeEnd] === ' ') removeEnd += 1;
      replaceRange(start, removeEnd, '');
      if (ref) {
        setMentionReferences((prev) =>
          prev.filter((item) => !(item.type === ref.type && item.id === ref.id)),
        );
      }
    };

    const handleEditorPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      const editor = editorRef.current;
      if (!editor) return;
      const offset = offsetFromPoint(editor, event.clientX, event.clientY);
      if (offset == null) return;
      const ranges = getMentionRanges(value, mentionReferences);
      const matched = ranges.find((range) => range.start <= offset && offset < range.end);
      if (!matched) return;
      event.preventDefault();
      removeMentionToken(matched.start, matched.end, matched.ref);
    };

    const handleSelectMention = (suggestion: MentionSuggestion) => {
      if (!mentionQuery) return;
      const token = `@${suggestion.label} `;
      replaceRange(mentionQuery.start, mentionQuery.end, token);
      setMentionReferences((prev) => {
        const withoutDuplicate = prev.filter(
          (item) => !(item.type === suggestion.type && item.id === suggestion.id),
        );
        return [
          ...withoutDuplicate,
          {
            type: suggestion.type,
            id: suggestion.id,
            label: suggestion.label,
          },
        ];
      });
      setMentionQuery(null);
    };

    const cancelPendingInputSync = () => {
      inputSyncGenRef.current += 1;
      if (inputSyncTimerRef.current !== null) {
        clearTimeout(inputSyncTimerRef.current);
        inputSyncTimerRef.current = null;
      }
    };

    const markComposing = () => {
      isComposingRef.current = true;
      cancelPendingInputSync();
      editorRef.current?.setAttribute('data-composing', 'true');
    };

    const syncFromEditor = () => {
      const editor = editorRef.current;
      if (!editor) return;
      const { end } = getSelectionOffsets(editor);
      const nextValue = getEditableText(editor);
      lastPushedValueRef.current = nextValue;
      pendingCaretOffsetRef.current = end;
      onChange(nextValue);
      syncMentionQuery(nextValue, end);
    };

    const handleInput = (event: FormEvent<HTMLDivElement>) => {
      if (
        isComposingRef.current ||
        Boolean((event.nativeEvent as { isComposing?: boolean }).isComposing)
      ) {
        return;
      }
      // Defer past this task so a late `compositionstart` (first Pinyin letter
      // on Chromium) can cancel the sync. Immediate onChange mounts the
      // highlight mirror and kills the IME session.
      const gen = ++inputSyncGenRef.current;
      if (inputSyncTimerRef.current !== null) {
        clearTimeout(inputSyncTimerRef.current);
      }
      inputSyncTimerRef.current = setTimeout(() => {
        inputSyncTimerRef.current = null;
        if (gen !== inputSyncGenRef.current) return;
        if (isComposingRef.current) return;
        editorRef.current?.removeAttribute('data-composing');
        syncFromEditor();
      }, 0);
    };

    const handleBeforeInput = (event: FormEvent<HTMLDivElement>) => {
      const native = event.nativeEvent as unknown as InputEvent;
      if (
        native.inputType === 'insertCompositionText' ||
        native.inputType === 'insertFromComposition' ||
        native.isComposing
      ) {
        markComposing();
      }
    };

    const handleCompositionStart = () => {
      markComposing();
    };

    const handleCompositionEnd = () => {
      isComposingRef.current = false;
      compositionEndAtRef.current = Date.now();
      editorRef.current?.removeAttribute('data-composing');
      syncFromEditor();
      // Safari can flush the committed glyphs after compositionend.
      requestAnimationFrame(() => {
        if (!isComposingRef.current) syncFromEditor();
      });
    };

    const handleEditorFocus = () => {
      const editor = editorRef.current;
      if (!editor || !isEditorDomEmpty(editor)) return;
      const hasTextNode = Array.from(editor.childNodes).some(
        (node) => node.nodeType === Node.TEXT_NODE,
      );
      if (!hasTextNode) writeEditorValue(editor, '');
      placeCaretInEmptyEditor(editor);
    };

    useLayoutEffect(() => {
      const editor = editorRef.current;
      if (!editor) return;
      const onCompositionStart = () => {
        isComposingRef.current = true;
        inputSyncGenRef.current += 1;
        if (inputSyncTimerRef.current !== null) {
          clearTimeout(inputSyncTimerRef.current);
          inputSyncTimerRef.current = null;
        }
        editor.setAttribute('data-composing', 'true');
      };
      editor.addEventListener('compositionstart', onCompositionStart, true);
      return () => {
        editor.removeEventListener('compositionstart', onCompositionStart, true);
      };
    }, []);

    const handlePaste = (event: ClipboardEvent<HTMLDivElement>) => {
      event.preventDefault();
      replaceSelection(event.clipboardData.getData('text/plain'));
    };

    const handleEditorScroll = () => {
      // no-op without mirror overlay
    };

    const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
      // Honor IME composition (`isComposing` true while Chinese input is mid-
      // selection) — only intercept on plain key events.
      if (isComposingRef.current || event.nativeEvent.isComposing) return;
      // Windows / some Chromium IMEs report keyCode 229 on the first
      // Pinyin keydown, before compositionstart.
      if (event.keyCode === 229) {
        markComposing();
        return;
      }
      // Hide the placeholder on the first letter of an empty editor so it
      // does not cover IME marked text. Do this even when `isComposing` is
      // still false (macOS Pinyin first keydown).
      if (
        !event.metaKey &&
        !event.ctrlKey &&
        !event.altKey &&
        event.key.length === 1 &&
        !value
      ) {
        editorRef.current?.setAttribute('data-composing', 'true');
      }
      // See `compositionEndAtRef` above: treat an Enter that lands right after
      // `compositionend` as "confirm candidate", not "submit/newline".
      if (event.key === 'Enter' && Date.now() - compositionEndAtRef.current < 50) {
        return;
      }

      if (event.key === 'Backspace' || event.key === 'Delete') {
        const editor = editorRef.current;
        if (editor) {
          const { start, end } = getSelectionOffsets(editor);
          if (start === end) {
            const ranges = getMentionRanges(value, mentionReferences);
            const targetIdx = event.key === 'Backspace' ? end - 1 : end;
            const matchedRange = ranges.find(
              (r) => r.start <= targetIdx && targetIdx < r.end,
            );
            if (matchedRange) {
              event.preventDefault();
              removeMentionToken(matchedRange.start, matchedRange.end, matchedRange.ref);
              return;
            }
          }
        }
      }

      if (mentionQuery && mentionSuggestions.length > 0) {
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          setSelectedMentionIndex((prev) => (prev + 1) % mentionSuggestions.length);
          return;
        }
        if (event.key === 'ArrowUp') {
          event.preventDefault();
          setSelectedMentionIndex(
            (prev) => (prev - 1 + mentionSuggestions.length) % mentionSuggestions.length,
          );
          return;
        }
        if (event.key === 'Enter' || event.key === 'Tab') {
          event.preventDefault();
          handleSelectMention(mentionSuggestions[selectedMentionIndex]);
          return;
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          setMentionQuery(null);
          return;
        }
      }

      if (showSuggestions && suggestions.length > 0) {
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          setSelectedIndex((prev) => (prev + 1) % suggestions.length);
          return;
        }
        if (event.key === 'ArrowUp') {
          event.preventDefault();
          setSelectedIndex((prev) => (prev - 1 + suggestions.length) % suggestions.length);
          return;
        }
        if (event.key === 'Enter' || event.key === 'Tab') {
          event.preventDefault();
          handleSelectSuggestion(suggestions[selectedIndex]);
          return;
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          setSlashQuery(null);
          setShowSuggestions(false);
          return;
        }
      }

      // Enter 发送，Shift+Enter 换行（⌘/Ctrl+Enter 作为老习惯保留，同样发送）。
      // IME 候选确认的 Enter 已被上面的 composition 守卫拦截，不会误发送。
      const isSendCombo =
        event.key === 'Enter' && !event.shiftKey;
      if (isSendCombo) {
        event.preventDefault();
        // streaming 期间：Enter = 轮中转向（注入运行中的回合；注入不了时
        // hook 兜底为 W6-2 排队或新回合直发，消息不会丢）；⌘/Ctrl+Enter =
        // follow-up 排队（本轮结束后自动作为下一轮发出）。
        if (isStreaming) {
          if ((event.metaKey || event.ctrlKey) && onFollowUp && trimmed) {
            onFollowUp(trimmed);
            onChange('');
            return;
          }
          if (onSteer && trimmed) {
            void onSteer(trimmed).then((outcome) => {
              // steered / queued / sent 三种结果消息都已落地（注入当前回合 /
              // 进入待发队列 / 作为新回合发出），草稿都可以清；queued 额外
              // 提示用户"不是追加进当前回合"。
              if (outcome === 'queued') {
                showSteerNotice('当前回合无法追加，已加入排队，本轮结束后自动发出');
              }
              onChange('');
            });
          }
          return;
        }
        if (canSend) {
          void onSubmit();
        }
        return;
      }

      if (event.key === 'Enter') {
        // Shift+Enter — 换行。
        event.preventDefault();
        replaceSelection('\n');
      }
    };

    const handleSendClick = () => {
      if (isStreaming) {
        onCancel?.();
        return;
      }
      if (canSend) {
        void onSubmit();
      }
    };

    const handleSelectAgent = (agent: LocalChatAgent) => {
      setAgentMenuOpen(false);
      if (agent.id === selectedAgent?.id) return;
      void onSelectAgent?.(agent.id);

      // Switching this expert to primary: drop a leftover @mention token so
      // the same persona is not both the bound agent and a mention.
      const isMentioned = mentionReferences.some(
        (ref) => ref.type === 'agent' && ref.id === agent.id,
      );
      if (isMentioned) {
        const escapeRegExp = (str: string) => str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const mentionRegex = new RegExp(`@${escapeRegExp(agent.name)}\\s?`, 'g');
        const nextValue = value.replace(mentionRegex, '');
        pendingCaretOffsetRef.current = nextValue.length;
        onChange(nextValue);
        setMentionReferences((prev) =>
          prev.filter((ref) => !(ref.type === 'agent' && ref.id === agent.id)),
        );
      }

      editorRef.current?.focus();
    };

    if (approvalPrompt?.current) {
      return (
        <div className="chat-input-container @container relative px-2.5 pb-2 pt-0.5">
          <ApprovalPromptMenu
            key={approvalPrompt.current.requestId}
            request={approvalPrompt.current}
            pendingCount={approvalPrompt.pendingCount}
            onDecide={approvalPrompt.decide}
          />
        </div>
      );
    }

    if (askUserPrompt?.current) {
      const request = askUserPrompt.current;
      return (
        <div
          className="chat-input-container @container relative px-2.5 pb-2 pt-0.5"
          data-testid="ask-user-composer"
        >
          <AskUserQuestionMenu
            key={request.requestId}
            intro={request.intro}
            questions={request.questions}
            onSubmit={askUserPrompt.answer}
            onAutoContinue={() => askUserPrompt.answer({})}
            bottomHint={
              askUserPrompt.pendingCount > 0
                ? `还有 ${askUserPrompt.pendingCount} 组问题待回答`
                : undefined
            }
          />
        </div>
      );
    }

    return (
      // @container：底栏元素的显隐/宽度用容器查询（@sm = 384px）而不是
      // 视口断点——侧边栏占宽后，视口够宽但输入框本身可能已经很窄。
      <div className="chat-input-container @container relative px-2.5 pb-2 pt-0.5">
        {(leadingChrome || selectedAgent) && (
          <div
            className="relative mb-1 flex min-w-0 items-center gap-1.5"
            data-testid="composer-meta-row"
          >
            {leadingChrome}
            {selectedAgent && (
              <AgentSelect
                refEl={agentMenuRef}
                agent={selectedAgent}
                agents={sortedAgents}
                open={agentMenuOpen}
                disabled={disabled || isStreaming || !onSelectAgent}
                onOpenChange={setAgentMenuOpen}
                onSelect={handleSelectAgent}
                mentionReferences={mentionReferences}
              />
            )}
          </div>
        )}
        {mentionQuery && mentionSuggestions.length > 0 && (
          <div className="absolute bottom-full left-3 z-50 mb-1 max-h-72 w-80 max-w-[calc(100%-0.75rem)] overflow-y-auto rounded-agent-md border border-agent-border bg-agent-canvas p-1 shadow-lg">
            <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-agent-muted-foreground">
              @ 选择 Agent 或历史对话
            </div>
            {mentionSuggestions.map((item, idx) => (
              <button
                key={`${item.type}-${item.id}`}
                ref={(node) => {
                  mentionOptionRefs.current[idx] = node;
                }}
                type="button"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => handleSelectMention(item)}
                onMouseEnter={() => setSelectedMentionIndex(idx)}
                className={`flex w-full items-start gap-2 rounded px-2 py-2 text-left transition-colors ${
                  idx === selectedMentionIndex
                    ? 'bg-agent-foreground/10 text-agent-foreground'
                    : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground'
                }`}
              >
                {item.type === 'agent' ? (
                  <span
                    className="mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[9px] font-semibold text-white"
                    style={{ backgroundColor: item.color || '#7c3aed' }}
                  >
                    {item.label.trim()[0]?.toUpperCase() ?? 'A'}
                  </span>
                ) : (
                  <span className="mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-agent-muted text-agent-muted-foreground">
                    <LuMessageSquare className="h-3 w-3" />
                  </span>
                )}
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <span className="truncate text-xs font-medium">@{item.label}</span>
                    <span className="shrink-0 rounded-full bg-agent-muted px-1.5 py-0.5 text-[10px] text-agent-muted-foreground">
                      {item.type === 'agent' ? 'Agent' : '对话'}
                    </span>
                  </span>
                  {item.description && (
                    <span className="mt-0.5 line-clamp-1 block text-[10px] text-agent-muted-foreground/80">
                      {item.description}
                    </span>
                  )}
                </span>
              </button>
            ))}
          </div>
        )}
        {showSuggestions && suggestions.length > 0 && (
          <div
            className="absolute bottom-full left-3 z-50 mb-1 max-h-48 w-72 overflow-y-auto rounded-agent-md border border-agent-border bg-agent-canvas p-1 shadow-lg"
            data-testid="slash-menu"
          >
            {suggestionRows.map((row) =>
              row.type === 'header' ? (
                <div
                  key={`header-${row.label}`}
                  className="px-2 py-1 text-[10px] font-semibold text-agent-muted-foreground uppercase tracking-wider"
                >
                  {row.label}
                </div>
              ) : (
                <button
                  key={`${row.item.kind}-${row.item.insertToken}`}
                  ref={(node) => {
                    skillOptionRefs.current[row.idx] = node;
                  }}
                  type="button"
                  data-testid={`slash-option-${row.item.kind}-${row.item.insertToken}`}
                  onClick={() => handleSelectSuggestion(row.item)}
                  onMouseEnter={() => setSelectedIndex(row.idx)}
                  className={`flex w-full items-start gap-2 rounded px-2 py-1.5 text-left transition-colors ${
                    row.idx === selectedIndex
                      ? 'bg-agent-foreground/5 text-agent-foreground'
                      : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground'
                  }`}
                >
                  {row.item.kind === 'skill' ? (
                    <LuBlocks className="h-3.5 w-3.5 mt-0.5 shrink-0 text-agent-muted-foreground" />
                  ) : (
                    <LuPlug className="h-3.5 w-3.5 mt-0.5 shrink-0 text-agent-muted-foreground" />
                  )}
                  <div className="min-w-0 flex-1">
                    {row.item.kind === 'skill' ? (
                      <>
                        <div className="text-xs font-medium truncate">
                          {row.item.skill.displayName ? (
                            <>
                              {row.item.skill.displayName}
                              <span className="ml-1.5 text-[10px] font-normal text-agent-muted-foreground/70">
                                /{row.item.skill.name}
                              </span>
                            </>
                          ) : (
                            <>/{row.item.skill.name}</>
                          )}
                        </div>
                        {row.item.skill.description && (
                          <div className="text-[10px] text-agent-muted-foreground/80 line-clamp-1">
                            {row.item.skill.description}
                          </div>
                        )}
                      </>
                    ) : (
                      <>
                        <div className="text-xs font-medium truncate">
                          {row.item.mcp.toolName}
                          <span className="ml-1.5 text-[10px] font-normal text-agent-muted-foreground/70">
                            · {row.item.mcp.serverName}
                          </span>
                        </div>
                        <div className="text-[10px] text-agent-muted-foreground/60 truncate">
                          /{row.item.mcp.token}
                        </div>
                        {row.item.mcp.description && (
                          <div className="text-[10px] text-agent-muted-foreground/80 line-clamp-1">
                            {row.item.mcp.description}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </button>
              )
            )}
          </div>
        )}
        <div
          className={`chat-input-box flex flex-col rounded-agent-lg border bg-agent-canvas shadow-sm transition-all duration-200 ${
            isDragging
              ? 'border-blue-500 ring-2 ring-blue-500/20'
              : mode === 'plan'
                ? 'border-amber-400/70 ring-1 ring-amber-400/20'
                : 'border-agent-border'
          }`}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          {pendingFollowUps.length > 0 && (
            <div className="flex flex-col gap-1 border-b border-agent-border bg-agent-muted/30 px-2.5 py-1.5">
              <div className="text-[11px] font-medium text-agent-muted-foreground">
                排队中（{pendingFollowUps.length}）· 本轮结束后自动发出 · 停止将丢弃
              </div>
              {pendingFollowUps.map((text, i) => (
                <div
                  key={`${i}-${text.slice(0, 12)}`}
                  className="group flex items-center gap-2 rounded-agent-md bg-agent-canvas px-2 py-1 text-xs text-agent-foreground"
                >
                  <span className="min-w-0 flex-1 truncate">{text}</span>
                  {onRemoveFollowUp && (
                    <button
                      type="button"
                      aria-label={`撤回排队消息 ${i + 1}`}
                      onClick={() => onRemoveFollowUp(i)}
                      className="shrink-0 text-agent-muted-foreground opacity-0 transition-opacity hover:text-agent-destructive group-hover:opacity-100"
                    >
                      ✕
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
          {steerNotice && (
            <div
              role="status"
              className="border-b border-agent-border bg-agent-muted/20 px-2.5 py-1 text-[11px] text-agent-muted-foreground"
            >
              {steerNotice}
            </div>
          )}
          <div className="chat-input-surface relative overflow-hidden">
            <div
              className="chat-input-placeholder pointer-events-none absolute inset-x-0 top-0 px-2.5 pt-2 text-xs text-agent-muted-foreground"
              style={{ visibility: value ? 'hidden' : undefined }}
            >
              {placeholder}
            </div>
            <div
              ref={editorRef}
              contentEditable={!disabled}
              suppressContentEditableWarning
              role="textbox"
              aria-multiline="true"
              aria-disabled={disabled}
              data-testid="chat-composer"
              spellCheck={false}
              onFocus={handleEditorFocus}
              onPointerDown={handleEditorPointerDown}
              onInput={handleInput}
              onBeforeInput={handleBeforeInput}
              onPaste={handlePaste}
              onKeyDown={handleKeyDown}
              onCompositionStart={handleCompositionStart}
              onCompositionEnd={handleCompositionEnd}
              onScroll={handleEditorScroll}
              className="chat-input-editor relative z-10 block w-full overflow-y-auto whitespace-pre-wrap break-words border-0 bg-transparent px-2.5 pt-2 text-xs text-agent-foreground caret-agent-foreground outline-none selection:bg-agent-foreground/20"
              style={{
                minHeight: MIN_HEIGHT_PX,
                maxHeight: MAX_HEIGHT_PX,
              }}
            />
          </div>
          {actualFiles.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-2.5 pb-1.5 pt-0.5">
              {actualFiles.map((file, idx) => (
                <div
                  key={idx}
                  className="group flex items-center gap-1.5 rounded bg-agent-muted/60 hover:bg-agent-muted px-2.5 py-1 text-xs border border-agent-border text-agent-foreground select-none max-w-xs transition-all duration-150"
                  title={file.path}
                >
                  <LuFile className="h-3.5 w-3.5 shrink-0 text-agent-muted-foreground" />
                  <span className="truncate max-w-[160px] font-medium">
                    {file.name}
                  </span>
                  <button
                    type="button"
                    onClick={() => handleRemoveFile(idx)}
                    className="flex h-4 w-4 items-center justify-center rounded-full text-agent-muted-foreground hover:bg-agent-foreground/10 hover:text-agent-foreground transition-colors"
                    aria-label={`移除文件 ${file.name}`}
                  >
                    <LuX className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div
            ref={toolbarRowRef}
            className="flex items-center justify-between gap-1.5 px-2 py-1 text-[12px] leading-[1.45]"
          >
            {/* Left toolbar: mode + exec sandbox + host extras + settings.
                Agent picker lives in the meta row above the box, next to
                the project badge. min-w-0 允许窄屏时胶囊截断收缩。 */}
            <div ref={toolbarLeftRef} className="flex min-w-0 items-center gap-1">
              {mode && onModeChange && (
                <ModeToggle
                  mode={mode}
                  disabled={disabled || isStreaming}
                  onChange={onModeChange}
                />
              )}
              {execPolicy && onExecPolicyChange && (
                <ExecPolicyPicker
                  policy={execPolicy}
                  disabled={disabled}
                  onChange={onExecPolicyChange}
                />
              )}
              {toolbarExtras}
              <button
                type="button"
                onClick={handlePickFiles}
                disabled={disabled || isStreaming}
                className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground disabled:cursor-not-allowed disabled:opacity-50"
                title="上传文件（可多选）"
                aria-label="上传文件"
              >
                <LuPaperclip className="h-3 w-3" />
              </button>
              {/* 隐藏的原生多选文件输入：点击回形针按钮触发系统选择框。 */}
              <input
                ref={fileInputRef}
                type="file"
                multiple
                className="hidden"
                aria-hidden="true"
                tabIndex={-1}
                onChange={handleFileInputChange}
              />
              {onOpenSettings && (
                <button
                  type="button"
                  onClick={onOpenSettings}
                  className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground"
                  title="LLM 设置"
                  aria-label="LLM 设置"
                  data-testid="chat-llm-settings"
                >
                  <LuSettings className="h-3 w-3" />
                </button>
              )}
            </div>
            {/* shrink-0：发送键是固定尺寸圆钮，窄容器下不允许被压扁；
                收缩量由左侧可截断的选择器吸收。 */}
            <div className="flex shrink-0 items-center gap-2">
              {/* 快捷键提示：空间不足时优先隐藏（测量逻辑见上方 effect） */}
              <div
                ref={hintRef}
                className={`${hintsSuppressed ? 'hidden' : 'block'} text-[11px] text-agent-muted-foreground/80`}
              >
                {isStreaming ? (
                  <>
                    <kbd className="rounded border border-agent-border bg-agent-muted px-1 font-sans text-[10px]">
                      {MOD_KEY_LABEL}
                    </kbd>
                    <span className="mx-0.5">+</span>
                    <kbd className="rounded border border-agent-border bg-agent-muted px-1 font-sans text-[10px]">
                      .
                    </kbd>
                    <span className="ml-1">停止</span>
                    {onSteer && (
                      <span className="ml-2">
                        <kbd className="rounded border border-agent-border bg-agent-muted px-1 font-sans text-[10px]">
                          Enter
                        </kbd>
                        <span className="ml-1">追加</span>
                      </span>
                    )}
                    {onFollowUp && (
                      <span className="ml-2">
                        <kbd className="rounded border border-agent-border bg-agent-muted px-1 font-sans text-[10px]">
                          {MOD_KEY_LABEL}
                        </kbd>
                        <span className="mx-0.5">+</span>
                        <kbd className="rounded border border-agent-border bg-agent-muted px-1 font-sans text-[10px]">
                          Enter
                        </kbd>
                        <span className="ml-1">排队</span>
                      </span>
                    )}
                  </>
                ) : (
                  <>
                    <kbd className="rounded border border-agent-border bg-agent-muted px-1 font-sans text-[10px]">
                      Enter
                    </kbd>
                    <span className="ml-1">发送</span>
                    <span className="ml-2">
                      <kbd className="rounded border border-agent-border bg-agent-muted px-1 font-sans text-[10px]">
                        Shift
                      </kbd>
                      <span className="mx-0.5">+</span>
                      <kbd className="rounded border border-agent-border bg-agent-muted px-1 font-sans text-[10px]">
                        Enter
                      </kbd>
                      <span className="ml-1">换行</span>
                    </span>
                  </>
                )}
              </div>
              <button
                ref={sendButtonRef}
                type="button"
                onClick={handleSendClick}
                disabled={!isStreaming && !canSend}
                className={
                  isStreaming
                    ? 'flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-agent-destructive text-white transition hover:opacity-90'
                    : canSend
                      ? 'flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-agent-foreground text-agent-canvas transition hover:opacity-90'
                      : 'flex h-6 w-6 shrink-0 cursor-not-allowed items-center justify-center rounded-full bg-agent-muted text-agent-muted-foreground'
                }
                title={
                  isStreaming
                    ? pendingFollowUps.length > 0
                      ? `停止生成 (${MOD_KEY_LABEL}+.) · 将丢弃 ${pendingFollowUps.length} 条排队消息`
                      : `停止生成 (${MOD_KEY_LABEL}+.)`
                    : '发送 (Enter)'
                }
                aria-label={isStreaming ? '停止生成' : '发送消息'}
                data-testid="chat-send"
              >
                {isStreaming ? (
                  <LuSquare className="h-3 w-3 fill-current" />
                ) : (
                  <LuArrowUp className="h-3.5 w-3.5" />
                )}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  },
);

function agentInitial(agent: LocalChatAgent): string {
  return agent.name.trim()[0]?.toUpperCase() ?? 'A';
}

/**
 * ModeToggle — segmented Agent / Plan switch (类 Cursor 的 mode 切换).
 * Plan 模式下后端只暴露只读工具并引导模型先出结构化计划。
 */
function ModeToggle({
  mode,
  disabled,
  onChange,
}: {
  mode: ChatMode;
  disabled: boolean;
  onChange: (mode: ChatMode) => void;
}) {
  return (
    // shrink-0：分段控件被 flex 压缩会压扁文字；极窄容器（<@sm）退化为
    // 纯图标（title 兜底语义），把收缩量让给可截断的选择器。
    <div
      className="inline-flex h-6 shrink-0 items-center rounded-full border border-agent-border bg-agent-canvas p-0.5"
      role="radiogroup"
      aria-label="对话模式"
      data-testid="mode-toggle"
    >
      <button
        type="button"
        role="radio"
        aria-checked={mode === 'agent'}
        disabled={disabled}
        onClick={() => onChange('agent')}
        title="Agent 模式：直接执行任务"
        data-testid="mode-agent"
        className={[
          'inline-flex h-5 items-center gap-1 rounded-full px-1.5 text-[12px] leading-[1.45] transition-colors disabled:cursor-not-allowed disabled:opacity-70',
          mode === 'agent'
            ? 'bg-agent-foreground/10 text-agent-foreground'
            : 'text-agent-muted-foreground hover:text-agent-foreground',
        ].join(' ')}
      >
        <LuInfinity className="h-3 w-3" />
        <span className="@max-sm:hidden">Agent</span>
      </button>
      <button
        type="button"
        role="radio"
        aria-checked={mode === 'plan'}
        disabled={disabled}
        onClick={() => onChange('plan')}
        title="Plan 模式：先制定计划，只读不执行"
        data-testid="mode-plan"
        className={[
          'inline-flex h-5 items-center gap-1 rounded-full px-1.5 text-[12px] leading-[1.45] transition-colors disabled:cursor-not-allowed disabled:opacity-70',
          mode === 'plan'
            ? 'bg-amber-400/20 text-amber-600 dark:text-amber-400'
            : 'text-agent-muted-foreground hover:text-agent-foreground',
        ].join(' ')}
      >
        <LuListChecks className="h-3 w-3" />
        <span className="@max-sm:hidden">Plan</span>
      </button>
    </div>
  );
}

/**
 * AgentSelect — compact "你正在和谁说话" indicator in the composer meta row
 * (next to the project badge). Clicking an expert switches the bound / next
 * agent. Extra experts still come from `@` mentions in the input.
 */
function AgentSelect({
  refEl,
  agent,
  agents,
  open,
  disabled,
  onOpenChange,
  onSelect,
  mentionReferences = [],
}: {
  refEl: RefObject<HTMLDivElement>;
  agent: LocalChatAgent;
  agents: LocalChatAgent[];
  open: boolean;
  disabled: boolean;
  onOpenChange: (open: boolean) => void;
  onSelect: (agent: LocalChatAgent) => void;
  mentionReferences?: MentionReference[];
}) {
  const navigate = useNavigate();
  const mentionedAgentCount = useMemo(() => {
    return mentionReferences.filter((ref) => ref.type === 'agent' && ref.id !== agent.id).length;
  }, [mentionReferences, agent.id]);

  const buttonTitle = useMemo(() => {
    const activeNames = [
      agent.name,
      ...mentionReferences
        .filter((ref) => ref.type === 'agent' && ref.id !== agent.id)
        .map((ref) => ref.label),
    ].filter(Boolean) as string[];

    return `当前激活专家：${activeNames.join(', ')}`;
  }, [agent.name, agent.id, mentionReferences]);

  return (
    <div
      ref={refEl}
      className="relative"
    >
      <button
        type="button"
        onClick={() => onOpenChange(!open)}
        disabled={disabled}
        className="inline-flex max-w-[140px] items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] text-agent-muted-foreground/80 transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground disabled:cursor-not-allowed disabled:opacity-70 @sm:max-w-[220px]"
        title={buttonTitle}
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid="agent-select"
      >
        <AgentDot agent={agent} />
        <span className="truncate font-medium">
          {agent.name}
          {mentionedAgentCount > 0 ? ` +${mentionedAgentCount}` : ''}
        </span>
        <LuChevronDown className="h-3 w-3 shrink-0 text-agent-muted-foreground" />
      </button>

      {open && (
        <div
          role="menu"
          className="absolute bottom-full left-0 z-50 mb-1 max-h-64 w-72 overflow-y-auto rounded-agent-md border border-agent-border bg-agent-canvas p-1 shadow-lg"
        >
          <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-agent-muted-foreground">
            选择专家
          </div>
          {agents.map((item) => {
            const isPrimary = item.id === agent.id;
            const isMentioned = mentionReferences.some(
              (ref) => ref.type === 'agent' && ref.id === item.id,
            );
            return (
              <button
                key={item.id}
                type="button"
                role="menuitemradio"
                aria-checked={isPrimary}
                data-agent-id={item.id}
                data-testid={`agent-option-${item.id}`}
                onClick={() => onSelect(item)}
                className={[
                  'flex w-full items-start gap-2 rounded px-2 py-2 text-left transition-colors',
                  isPrimary
                    ? 'bg-agent-foreground/10 text-agent-foreground'
                    : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
                ].join(' ')}
              >
                <AgentDot agent={item} />
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <span className="block truncate text-xs font-medium">
                      {item.name}
                    </span>
                    {isPrimary && (
                      <span className="inline-flex items-center rounded bg-agent-foreground/10 px-1 py-0.5 text-[9px] font-medium text-agent-foreground">
                        当前
                      </span>
                    )}
                    {!isPrimary && isMentioned && (
                      <span className="inline-flex items-center rounded bg-agent-muted px-1 py-0.5 text-[9px] font-medium text-agent-muted-foreground border border-agent-border">
                        已引入
                      </span>
                    )}
                  </span>
                  {item.description && (
                    <span className="mt-0.5 line-clamp-2 block text-[10px] text-agent-muted-foreground/80">
                      {item.description}
                    </span>
                  )}
                </span>
              </button>
            );
          })}
          <button
            type="button"
            role="menuitem"
            data-testid="agent-manage"
            onClick={() => {
              onOpenChange(false);
              navigate('/settings?section=agents');
            }}
            className="mt-0.5 flex w-full items-center gap-2 rounded border-t border-agent-border/50 px-2 py-2 text-left text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground"
          >
            <LuBot className="h-3.5 w-3.5 shrink-0" />
            <span className="text-xs font-medium">管理智能体</span>
          </button>
        </div>
      )}
    </div>
  );
}

function AgentDot({ agent }: { agent: LocalChatAgent }) {
  return (
    <span
      className="inline-flex shrink-0 items-center justify-center rounded-full text-[9px] font-semibold text-white"
      style={{
        width: 14,
        height: 14,
        backgroundColor: agent.color || '#7c3aed',
      }}
    >
      {agentInitial(agent)}
    </span>
  );
}

export default ChatInput;

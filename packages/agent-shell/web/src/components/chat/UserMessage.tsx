import { useLayoutEffect, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import { LuChevronDown, LuChevronUp, LuCheck, LuCopy } from 'react-icons/lu';
import type { ChatMessage } from '@steerable/agent-protocol';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';
import { useSlashSources } from '@/lib/slash-sources';
import { Markdown } from './Markdown';
import { getFriendlyDate } from './timestamp';
import { useCopy } from './useCopy';

/**
 * UserMessage — port of `deeppath`'s user-bubble component, trimmed to the
 * agent-only surface.
 *
 * Differences vs the original:
 *   - **No RefCards.** Local-backend messages don't carry the `messageMetadata.refs`
 *     array used by the cloud product to attach goal/task/event/document refs;
 *     when we eventually want this for agent (e.g. uploaded files), it'll go
 *     here.
 *   - **No edit button.** The cloud product's `editChatMessage` rewrites the
 *     SSE turn in place — we don't have that machinery yet. Tracking under
 *     phase 2c.
 *   - **No mentioned-agent chip / inline `ref://` / `tool://` / `agent://`
 *     parsing.** Agent-only mode doesn't surface multi-agent mentions or
 *     pinned-ref shortcuts, so the markdown content is rendered directly.
 *
 * What we KEEP from the old version (and why it matters for parity):
 *   - Left-aligned bubble (not right-aligned). The product chat layout stacks
 *     both user + assistant bubbles flush-left in a vertical column — this is
 *     the most visible "feel" difference vs a typical chat UI.
 *   - Bubble background uses the secondary muted token so it sits a notch
 *     above the canvas without competing with assistant bubbles.
 *   - 4.5em fold + gradient mask + 「展开全部 / 收起」 toggle so a wall-of-text
 *     user prompt doesn't steal the whole viewport before the assistant
 *     replies.
 *   - Friendly timestamp at bottom-right.
 *   - `framer-motion` fade/slide-in on mount.
 */

interface UserMessageProps {
  message: ChatMessage;
  agents?: LocalChatAgent[];
  chats?: LocalChat[];
}

export function UserMessage({ message, agents = [], chats = [] }: UserMessageProps) {
  const content = message.content || '';
  // 发出去的消息里 `/技能` `/mcp__srv__tool` 要和输入框里同样渲染成工具卡片。
  // 目录来自共享缓存，整屏消息只拉一次。
  const { skills, mcpTools } = useSlashSources();

  const [isExpanded, setIsExpanded] = useState(false);
  const [isOverflowing, setIsOverflowing] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const { copied, copy } = useCopy(content);

  useLayoutEffect(() => {
    const el = contentRef.current;
    if (!el || isExpanded) return;
    setIsOverflowing(el.scrollHeight > el.clientHeight + 1);
  }, [content, isExpanded]);

  return (
    <motion.div
      className="group/message mb-2"
      data-message-role="user"
      data-message-id={message.id}
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
    >
      <div className="flex justify-start">
        <div className="mx-auto w-full max-w-[var(--chat-input-box-width)] rounded-lg border border-agent-border bg-agent-muted p-3 text-sm text-agent-foreground">
          <div className="relative">
            <div
              ref={contentRef}
              className={`markdown-content pl-1 ${
                !isExpanded ? 'overflow-hidden' : ''
              }`}
              style={!isExpanded ? { maxHeight: '4.5em' } : undefined}
            >
              {content ? (
                <Markdown
                  inlineParagraph
                  agents={agents}
                  chats={chats}
                  skills={skills}
                  mcpTools={mcpTools}
                >
                  {content}
                </Markdown>
              ) : (
                <div className="h-4" />
              )}
            </div>
            {!isExpanded && isOverflowing && (
              <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-5 bg-gradient-to-t from-agent-muted to-transparent" />
            )}
          </div>

          {isOverflowing && (
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                setIsExpanded((prev) => !prev);
              }}
              className="mt-1 inline-flex items-center gap-0.5 text-xs text-agent-muted-foreground transition-colors hover:text-agent-foreground"
            >
              {isExpanded ? (
                <>
                  收起
                  <LuChevronUp className="h-3 w-3" />
                </>
              ) : (
                <>
                  展开全部
                  <LuChevronDown className="h-3 w-3" />
                </>
              )}
            </button>
          )}

          <div className="mt-2 flex items-center justify-between">
            <button
              type="button"
              onClick={() => void copy()}
              className="inline-flex items-center gap-0.5 rounded text-xs text-agent-muted-foreground opacity-0 transition-all duration-200 hover:text-agent-foreground focus:opacity-100 group-hover/message:opacity-100"
              title={copied ? '已复制' : '复制消息'}
              aria-label={copied ? '已复制' : '复制消息'}
            >
              {copied ? (
                <>
                  <LuCheck className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
                  <span className="text-emerald-600 dark:text-emerald-400">
                    已复制
                  </span>
                </>
              ) : (
                <LuCopy className="h-3 w-3" />
              )}
            </button>
            <div className="text-[13px] text-agent-muted-foreground">
              {message.createdAt
                ? getFriendlyDate(new Date(message.createdAt))
                : getFriendlyDate(new Date())}
            </div>
          </div>
        </div>
      </div>
    </motion.div>
  );
}

export default UserMessage;

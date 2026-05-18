/**
 * `<CardMessageRenderer />`
 *
 * Custom `renderMessage` for `ChatPanel.Messages` that detects a card payload
 * carried in `message.messageMetadata` (set by the mock transport) and routes
 * to the matching rich card from `@steerable/agent-ui/cards`. Falls back to
 * the framework's default bubble for plain text messages.
 */
import * as React from 'react';
import type { MessageRendererProps } from '@steerable/agent-ui';
import {
  OrchestrationPlanCard,
  QuizCard,
  CoverageReportCard,
  AnalysisDocumentCard,
  ResearchPlanCard,
  SuggestedRepliesCard,
  AskUserQuestionsCard,
  ThinkingProcessCard,
  PlanStepsCard,
  PlanSelectorCard,
  SearchSourcesCard,
  SummaryMessageCard,
  ActionSegmentCard,
  ToolExecutionCard,
} from '@steerable/agent-ui/cards';
import { useChatSessionContext } from '@steerable/agent-ui';

type CardKind =
  | 'OrchestrationPlanPayload'
  | 'QuizPayload'
  | 'CoverageReportPayload'
  | 'AnalysisDocumentPayload'
  | 'ResearchPlanPayload'
  | 'SuggestedRepliesPayload'
  | 'AskUserQuestionsPayload'
  | 'ThinkingProcessPayload'
  | 'PlanStepsPayload'
  | 'PlanSelectorPayload'
  | 'SearchSourcesPayload'
  | 'SummaryMessagePayload'
  | 'ActionSegmentPayload'
  | 'ToolExecutionPayload';

interface CardMeta {
  card: CardKind;
  payload: unknown;
}

function extractCardMeta(message: { messageMetadata?: unknown }): CardMeta | null {
  const meta = message.messageMetadata;
  if (!meta) return null;
  if (typeof meta === 'string') {
    try {
      const parsed = JSON.parse(meta);
      if (parsed && typeof parsed === 'object' && 'card' in parsed) return parsed as CardMeta;
    } catch {
      return null;
    }
    return null;
  }
  if (typeof meta === 'object' && meta !== null && 'card' in meta) return meta as CardMeta;
  return null;
}

export function CardMessageRenderer(props: MessageRendererProps) {
  const { message, isLastAssistant, isStreaming, index } = props;
  const meta = extractCardMeta(message as { messageMetadata?: unknown });
  const session = useChatSessionContext();

  if (!meta) {
    return <DefaultBubble {...props} />;
  }

  const send = (text: string) => {
    void session.sendUserMessage({ content: text });
  };

  // Card chosen by the mock transport. We render the bubble's text content
  // above so the streaming intro the mock emitted is still visible.
  return (
    <div key={message.id ?? index} className="flex w-full flex-col items-start gap-2">
      {message.content ? (
        <div className="max-w-[80%] rounded-agent-md border border-agent-border bg-agent-muted px-3 py-2 text-sm text-agent-foreground">
          <span className="whitespace-pre-wrap">{message.content}</span>
          {isLastAssistant && isStreaming ? (
            <span className="ml-0.5 inline-block h-3 w-1.5 align-baseline bg-agent-foreground animate-agent-cursor-blink" />
          ) : null}
        </div>
      ) : null}
      <div className="w-full max-w-[80%]">
        {renderCard(meta, send)}
      </div>
    </div>
  );
}

function renderCard(meta: CardMeta, send: (text: string) => void): React.ReactNode {
  switch (meta.card) {
    case 'OrchestrationPlanPayload':
      return <OrchestrationPlanCard payload={meta.payload as never} />;
    case 'QuizPayload':
      return (
        <QuizCard
          payload={meta.payload as never}
          onSubmit={(answers) => send(`提交答卷：${JSON.stringify(answers)}`)}
        />
      );
    case 'CoverageReportPayload':
      return (
        <CoverageReportCard
          payload={meta.payload as never}
          onRemediate={() => send('生成针对性练习')}
        />
      );
    case 'AnalysisDocumentPayload':
      return <AnalysisDocumentCard payload={meta.payload as never} />;
    case 'ResearchPlanPayload':
      return <ResearchPlanCard payload={meta.payload as never} />;
    case 'SuggestedRepliesPayload':
      return <SuggestedRepliesCard payload={meta.payload as never} onSelect={send} />;
    case 'AskUserQuestionsPayload':
      return (
        <AskUserQuestionsCard
          payload={meta.payload as never}
          onSubmit={(answers) => send(`回答：${JSON.stringify(answers)}`)}
        />
      );
    case 'ThinkingProcessPayload':
      return <ThinkingProcessCard payload={meta.payload as never} />;
    case 'PlanStepsPayload':
      return <PlanStepsCard payload={meta.payload as never} isStreaming={false} />;
    case 'PlanSelectorPayload':
      return (
        <PlanSelectorCard
          payload={meta.payload as never}
          onSelect={(planId) => send(`选择方案 ${planId}`)}
        />
      );
    case 'SearchSourcesPayload':
      return <SearchSourcesCard payload={meta.payload as never} />;
    case 'SummaryMessagePayload':
      return <SummaryMessageCard payload={meta.payload as never} defaultExpanded />;
    case 'ActionSegmentPayload':
      return <ActionSegmentCard payload={meta.payload as never} />;
    case 'ToolExecutionPayload':
      return <ToolExecutionCard payload={meta.payload as never} defaultExpanded />;
    default:
      return null;
  }
}

function DefaultBubble({ message, isLastAssistant, isStreaming, index }: MessageRendererProps) {
  const isAssistant = message.role === 'assistant';
  return (
    <div
      key={message.id ?? index}
      data-role={message.role}
      className={`flex w-full ${isAssistant ? 'justify-start' : 'justify-end'}`}
    >
      <div
        className={`max-w-[80%] rounded-agent-md border px-3 py-2 text-sm leading-relaxed ${
          isAssistant
            ? 'border-agent-border bg-agent-muted text-agent-foreground'
            : 'border-transparent bg-agent-accent text-agent-accent-foreground'
        }`}
      >
        <span className="whitespace-pre-wrap">{message.content || (isLastAssistant && isStreaming ? '…' : '')}</span>
        {isLastAssistant && isStreaming ? (
          <span className="ml-0.5 inline-block h-3 w-1.5 align-baseline bg-agent-foreground animate-agent-cursor-blink" />
        ) : null}
      </div>
    </div>
  );
}

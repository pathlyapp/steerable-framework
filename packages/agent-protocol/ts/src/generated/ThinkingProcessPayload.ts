/**
 * Payload of a thinking-process card. Renders the agent's chain-of-thought (markdown) in a collapsible panel. UI clients are responsible for redacting / disabling this card if the deployment policy forbids exposing raw reasoning to end users.
 */
export interface ThinkingProcessPayload {
  /**
   * Markdown chain-of-thought text.
   */
  body: string;
  defaultExpanded?: boolean;
  [k: string]: any;
}

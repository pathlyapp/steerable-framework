/**
 * Payload of an analysis-document card. Renders as a long-form markdown document (research write-up, design memo, etc.). The body string can include GFM markdown, mermaid blocks, and inline references. The optional metadata block carries model / timestamp info shown in the document header.
 */
export interface AnalysisDocumentPayload {
  /**
   * Optional document title; defaults to a generic 'Analysis' label client-side.
   */
  title?: string | null;
  /**
   * Markdown body. May include mermaid code-fences.
   */
  body: string;
  createdAt?: string | null;
  /**
   * Display model badge id (resolved to model option client-side).
   */
  modelId?: string | null;
  [k: string]: any;
}

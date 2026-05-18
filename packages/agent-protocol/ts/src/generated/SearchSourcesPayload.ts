/**
 * Payload of a search-sources card. Lists the web pages that an agent consulted while answering. Rendered as a row of stacked favicons with an expandable detail list.
 */
export interface SearchSourcesPayload {
  sources: {
    url: string;
    title?: string | null;
    snippet?: string | null;
    favicon?: string | null;
    publishedAt?: string | null;
    [k: string]: any;
  }[];
  [k: string]: any;
}

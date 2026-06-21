export interface ParsedAction {
  id: string;
  type: string;
  params: Record<string, any>;
  rawTag: string;
}

export interface ProcessedContent {
  hasActions: boolean;
  content: string;
  actions: ParsedAction[];
}

/**
 * Regex-based helper to extract a single string field from JSON-like text
 * in case JSON.parse fails due to unescaped quotes or markdown characters.
 */
function extractStringField(raw: string, key: string): string | null {
  const re = new RegExp(`"${key}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`);
  const m = raw.match(re);
  return m ? m[1] : null;
}

/**
 * Parses XML-style action tags (e.g. <dp-action type="xxx" params="..." />)
 * from a message's text content.
 * Supports both double-quoted and single-quoted params, as well as unquoted or
 * brace-enclosed JSON formats.
 */
export function processActionTags(content: string, tagName = 'dp-action'): ProcessedContent {
  if (!content.includes(`<${tagName}`)) {
    return { hasActions: false, content, actions: [] };
  }

  // Regex to match the specified action tags and extract their type and params
  const regex = new RegExp(`<${tagName}\\s+type="([^"]+)"\\s+params=(?:'((?:[^'\\\\]|\\\\.)*)'|"((?:[^"\\\\]|\\\\.)*)"|({[\\s\\S]*?}))\\s*\\/?>`, 'g');

  const actions: ParsedAction[] = [];
  let processedContent = content;
  let match;
  let counter = 1;

  // We loop to match and parse each tag
  while ((match = regex.exec(content)) !== null) {
    const rawTag = match[0];
    const type = match[1];
    const paramsRaw = match[2] || match[3] || match[4];
    
    let params: Record<string, any> = {};
    if (paramsRaw) {
      try {
        // Try parsing JSON directly
        params = JSON.parse(paramsRaw.trim());
      } catch {
        // Fallback: extract common fields with regex if JSON is malformed
        try {
          const title = extractStringField(paramsRaw, 'title');
          if (title) params.title = title;
          const description = extractStringField(paramsRaw, 'description');
          if (description) params.description = description;
        } catch {
          // Noop: keep params empty
        }
      }
    }

    const id = `action_${counter++}`;
    actions.push({
      id,
      type,
      params,
      rawTag,
    });

    // Replace the raw tag with a clean placeholder slot
    processedContent = processedContent.replace(rawTag, `<!-- SLOT:${id} -->`);
  }

  return {
    hasActions: actions.length > 0,
    content: processedContent,
    actions,
  };
}

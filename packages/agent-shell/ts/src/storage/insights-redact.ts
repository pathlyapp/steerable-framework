const SECRET = /(sk-[A-Za-z0-9_\-]{8,}|api[_-]?key\s*[:=]\s*\S+|bearer\s+[A-Za-z0-9._\-]{8,})/gi;
const WIN_HOME = /[A-Za-z]:\\(?:Users|home)\\[^\\\s]+/gi;
const UNIX_HOME = /\/(?:Users|home)\/[^/\s]+/g;

export function redactInsightText(value: unknown, limit: number): string {
  if (typeof value !== 'string' || !value) return '';
  let text = value.replace(/\u0000/g, '');
  text = text.replace(SECRET, '[redacted]');
  text = text.replace(WIN_HOME, 'C:\\Users\\[redacted]');
  text = text.replace(UNIX_HOME, '/Users/[redacted]'); // shell-neutral:allow — 脱敏替换目标，非机器路径
  if (text.length > limit) return `${text.slice(0, limit)}…`;
  return text;
}

export function toolNamesOnly(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const names: string[] = [];
  for (const item of raw.slice(0, 40)) {
    if (typeof item === 'string' && item.trim()) names.push(item.trim().slice(0, 64));
    else if (item && typeof item === 'object') {
      const rec = item as { name?: unknown; tool?: unknown };
      const name = rec.name ?? rec.tool;
      if (typeof name === 'string' && name.trim()) names.push(name.trim().slice(0, 64));
    }
  }
  return names;
}
